"""Critic 节点 -- 对 Report + Evidence 进行审计评估。

输出 Critique（pass / revise / research_more）：
- pass: 证据充分，报告可信 -> END
- revise: 只需删改就能修好的问题 -> 回 Reasoning 重写（<= max_revisions 轮）
- research_more: 缺证据（缺数据 / 实体存疑 / 结论需要新数据）-> 带缺口回 Supervisor
- error: **审计自身失败**（内部裁决，模型不允许返回）-> 安全终止

阶段 1（docs/audit-reliability-remediation-plan.md §4）：
`issues[]` 是唯一的结构化问题清单，`verdict` 由**代码**从 issue 的 action 派生：
- 任意 ``action=research_more`` → research_more（补证据优先于改写）
- 否则存在 remove_or_qualify / repair_format → revise
- 没有 issue → pass

旧的 ``missing_points`` / ``missing_tool_keys`` / ``unsupported_claims`` 保留一版
兼容响应：既接受模型直接给（旧格式），也在 issues 存在时由 issues 派生补齐。

阶段 2：``missing_tool_keys`` 落库前按**覆盖度**（不是"工具名跑过"）过滤，
被丢弃的 key 带原因写入 ``gap_key_decisions``，并进入结构化运行日志。
"""

import logging
import re
import time
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError, model_validator

from app.agent.evidence_gate import match_claims_to_evidence
from app.config import Settings
from app.errors import LLMOutputError
from app.graph import run_log
from app.graph.gap_loop import (
    allowed_keys,
    classify_missing_tool_keys,
    coverage_requirements,
    render_registry_for_critic,
    satisfied_keys,
)
from app.llm_json import parse_json_object
from app.research.entity_check import nonexistence_assertions
from app.research.entity_check import report_text as entity_report_text

logger = logging.getLogger(__name__)

#: Critic 合法裁决。额外的 "error" 不允许模型返回，仅由本节点在「审计自身失败 /
#: verdict 无法识别」时内部产生，路由层据此安全终止，而不是当成 pass 或 research_more。
Verdict = Literal["pass", "revise", "research_more", "error"]

#: 阶段 1：问题的**性质**（决定它是什么问题，不是怎么处理）
IssueKind = Literal["unsupported_claim", "missing_evidence", "invalid_entity", "format"]
#: 阶段 1：问题的**修复动作**（决定路由）。research_more 是最"贵"也最保守的一条路：
#: 删改能修的一律 remove_or_qualify，必须拿新数据才能修的才 research_more。
IssueAction = Literal["remove_or_qualify", "research_more", "repair_format"]

#: 模型可以自行声明的裁决（不含内部 error）
_MODEL_VERDICTS = ("pass", "revise", "research_more")


class AuditIssue(BaseModel):
    """一条结构化审计问题。

    ``required_tool_keys`` / ``required_coverage`` 只在 ``action=research_more``
    时有意义：前者指明要补哪个 registry key，后者说明需要什么覆盖条件
    （例如 ``{"min_datum_count": 20}``、``{"allow_partial": false}``）。
    """

    kind: IssueKind
    claim: str = ""
    severity: Literal["low", "medium", "high"] = "medium"
    action: IssueAction = "remove_or_qualify"
    rationale: str = ""
    required_tool_keys: list[str] = []
    required_coverage: dict[str, Any] = {}


def derive_verdict_from_issues(issues: list[AuditIssue]) -> Verdict | None:
    """阶段 1 第 3 条：由 issue 的 action 派生 verdict；无结构化 issue 时返回 None。

    派生规则刻意是**单向保守**的：只要有一条需要补证据，就整体走 research_more，
    不允许"顺手把能改写的也改了"就判 revise——那样又回到"靠 LLM 改写伪装解决"。
    """
    actions = {i.action for i in issues}
    if "research_more" in actions:
        return "research_more"
    if actions & {"remove_or_qualify", "repair_format"}:
        return "revise"
    return None


class Critique(BaseModel):
    """Critic 评估结论。"""

    verdict: Verdict
    reason: str = ""
    #: 阶段 1：结构化问题清单（新的唯一事实来源）
    issues: list[AuditIssue] = []
    # ── legacy 兼容字段（迁移期保留，可由 issues 派生）──
    missing_points: list[str] = []  # 证据缺口（research_more 时喂回 Supervisor）
    missing_tool_keys: list[str] = []  # 建议补充的工具 registry key（代码级补齐）
    unsupported_claims: list[str] = []  # 无证据支撑的表述（revise 时喂回 Reasoning）
    #: 阶段 2：被丢弃的缺口 key 及原因（already_satisfied / not_visible /
    #: budget_truncated / duplicated）。必须能进最终响应——否则"为什么没补工具"不可解释。
    gap_key_decisions: list[dict[str, str]] = []

    @model_validator(mode="after")
    def _derive_legacy_fields(self) -> "Critique":
        """从 issues 派生 legacy 字段并做合并（不覆盖模型显式给出的值）。

        刻意用"并集"而不是"覆盖"：模型给的缺口与 issue 里声明的缺口只要有一条
        需要补数据，路由就必须看到它。
        """
        if not self.issues:
            return self

        def _merge(current: list[str], extra: list[str]) -> list[str]:
            out = list(current)
            for item in extra:
                if item and item not in out:
                    out.append(item)
            return out

        unsupported = [i.claim for i in self.issues if i.kind == "unsupported_claim" and i.action != "research_more"]
        missing = [(i.claim or i.rationale) for i in self.issues if i.kind in ("missing_evidence", "invalid_entity")]
        tool_keys: list[str] = []
        for issue in self.issues:
            if issue.action == "research_more":
                tool_keys.extend(issue.required_tool_keys)

        self.unsupported_claims = _merge(self.unsupported_claims, [c for c in unsupported if c])
        self.missing_points = _merge(self.missing_points, [m for m in missing if m])
        self.missing_tool_keys = _merge(self.missing_tool_keys, tool_keys)
        return self


#: 阶段 1 审计纪律：写进 Critic prompt 的硬规则。
_ISSUE_RULES = (
    "[审计契约]\n"
    "- 逐条列出问题到 issues[]，不要只给结论。每个 issue 必须写清 kind / action / rationale。\n"
    "- 未验证实体本身不构成 invalid_entity；具体行情缺标的证据时用 missing_evidence/research_more。\n"
    "- 只有可追溯的权威证券主数据与成功返回的名称/代码冲突，才用 invalid_entity。\n"
    "- 无据断言股票不存在/未上市时要求改成未验证，不得凭静态常见表推断。\n"
    "- kind=missing_evidence 且需要工具核查时，action 必须是 research_more；\n"
    "  只有'删除该句 / 降低措辞强度 / 补一句风险提示'就能修好的，才允许 action=remove_or_qualify。\n"
    "- format 类问题（结构没写全、字段错位）用 action=repair_format。\n"
    "- 你不需要自己决定 verdict：系统会从 issues 的 action 派生 verdict。\n"
    "  只要存在任意一条 action=research_more，整体就会走'补充研究'回环。\n"
    "- required_tool_keys 只能从上面「可补充的工具」小节里挑；\n"
    '  required_coverage 说明这条缺口需要什么覆盖条件（如 {"min_datum_count": 20}、\n'
    '  {"allow_partial": false}、{"arguments": {"date": "2026-10-08"}}），\n'
    "  空对象表示没有额外覆盖要求。\n"
    "- 同一个 tool_key 出现在多条 issue 时，每条 issue 的 required_coverage 都会被独立要求满足。\n"
    '  若两条 issue 要求同一个参数的不同取值（例如分别要 date=2026-10-07 与 date=2026-10-08），\n'
    "  单次调用无法同时满足：系统会为每个取值各规划一次调用，你**不要**为了迁就单次调用而\n"
    "  只保留其中一个取值——那样会让前一条缺口凭空消失。\n"
    "- 「工具执行过」不等于「证据已充分」：如果某个工具返回 partial、空列表，"
    "或参数不覆盖这条缺口，照样把它算作未满足的缺口。\n"
    "- 证据被上下文截断时，不要据此判定'无证据'——只标记你确实看到了的部分。\n"
    "- status=partial、partial=true、缺日期锚点或 note 指明截断的证据不得支撑全市场或明确资金流向等强结论。\n"
    "- 没有完整同日上涨/下跌家数时，报告只能写无法确认，不能声称全市场全部上涨/下跌。\n"
    "- 因果、资金从一板块流向另一板块、跨市场带动，须有同日同标的/板块直接证据；否则要求改为可能/无法确认或删除。\n"
    "- 报告中的当前事实、外部实体/政策/新闻必须能在当前 evidence 中逐条核实；没有对应 evidence 的事实必须标为 unsupported_claim 或 missing_evidence，并按需要补研究。\n"
    "- partial、截断、无日期锚点或仅单一来源的 evidence 只能支持有限、带限定语的观察；不得据此放过全市场比较、绝对化或因果结论。\n"
    '- 非法输出会被安全终止：issues 必须是数组，元素必须是对象；'
    '显式写 "issues": null 会被判为非法 payload（要表达"没有 issue"请给空数组 []）。'
)


_CRITIC_SYSTEM_PROMPT = (
    "你是 Mosaic 的 Critic。你的任务是逐项审查报告的每个关键论断是否都有对应的"
    "证据支撑。不要凭空增加要求 -- 只看已有证据够不够回答用户问题。"
)

# Domain-specific review rules
_DOMAIN_RULES = {
    "a_share": (
        "[A股审查规则]\n"
        "- 如果报告声称市场情绪悲观/乐观，必须有 sentiment / limit_up_count 等数据支撑\n"
        "- 如果报告提到某个题材板块强势，必须有 sectors / pool 的数据支撑\n"
        "- 不能在没有涨停生态数据的情况下声称 '涨停板萎缩'"
    ),
    "crypto": (
        "[Crypto审查规则]\n"
        "- 关于杠杆风险的判断需要 OI / Funding 数据支撑\n"
        "- 不能在没有清算数据的情况下声称 '无爆仓风险'"
    ),
    "us_stock": (
        "[美股审查规则]\n"
        "- 关于美股个股涨跌的结论必须有 us_klines / us_window 数据支撑\n"
        "- 财务类结论（营收/利润/EPS/估值）必须有 us_fundamentals 数据支撑，"
        "且不得混淆年报值与季报值\n"
        "- 不能在没有 K 线数据的情况下声称 '趋势突破/破位'"
    ),
}


# 新闻证据纪律（A7）：跨域规则，不分市场域 —— 三域以上共用，且 unknown 域
# 的 _get_domain_rules 返回空串，放 _DOMAIN_RULES 里会丢。在 prompt 组装处
# 无条件附加（新闻面论断只可能引用 news_*/telegraph_* 证据，与域无关）。
_NEWS_RULES = (
    "[新闻证据纪律]\n"
    "- 涉及新闻/事件/消息面的论断必须引用 news_* 或 telegraph_* 证据，并标注时间与来源\n"
    "- 单一来源的传闻性表述必须明示『单一来源，未交叉确认』\n"
    "- 标题相同且出现于 ≥2 个独立来源（news_meta.multi_source_titles）方可称『已证实』\n"
    "- 数条新闻只是样本，不得据此推断全市场情绪或长期趋势"
)


# 舆情情绪证据纪律（Step 5.2，可选规则）：与新闻纪律同理是跨域规则 ——
# sentiment_* 证据只可能来自雪球评论区（xq_discussions），与市场域无关，
# 在 prompt 组装处无条件附加。
_SENTIMENT_RULES = (
    "[舆情情绪纪律]\n"
    "- 舆情结论（如『散户情绪乐观』）如果仅依赖 xq_discussions 单源、没有与资金流 / "
    "行情数据做关联验证，必须在 critique 中标注『单源、需与其他数据交叉确认』\n"
    "- 单只股票的评论情绪不得外推为整体市场情绪\n"
    "- 评论样本存在幸存者偏差，极端观点占比偏高，引用 sentiment_score 时应降低表述强度"
)


def _field(obj, key, default=None):
    """从 dict 或对象读取字段 —— LangGraph 可能传入任一形式。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_domain_rules(domain: str) -> str:
    return _DOMAIN_RULES.get(domain, "")


#: 阶段 4①：单个报告字段在审计上下文里的展示上限。超限**必须显式标注**
#: （旧实现把 what_happened 静默截到 200 字，Critic 以为报告就这么长，
#: 于是"报告没提到"被读成"报告没证据"）。
_MAX_REVIEW_FIELD_CHARS = 2000
#: 未被报告引用的证据，最多展示多少条摘要（其余只给总数）。
_MAX_UNREFERENCED_EVIDENCE_LINES = 60


def _market_priority(item) -> int:
    metric = str(_field(item, "metric", "") or "").lower()
    tool = str(_field(item, "source_tool", _field(item, "tool", "")) or "").lower()
    if _field(item, "partial") or _field(item, "note") or _field(item, "timestamp"):
        return 0
    if any(token in metric for token in ("up_count", "down_count", "advancing", "declining", "上涨家数", "下跌家数")):
        return 0
    if any(token in tool for token in ("quote", "sentiment", "limit_up_count", "limit_up_sectors", "telegraph")):
        return 1
    if any(token in metric for token in ("timestamp", "date", "涨停")):
        return 2
    return 3


def _incomplete(item) -> bool:
    note = str(_field(item, "note", "") or "").lower()
    return (_field(item, "status") != "success" or bool(_field(item, "partial"))
            or any(token in note for token in ("截断", "仅返回", "truncat", "partial")))


def _render_field(name: str, value: Any, truncations: list[dict]) -> str | None:
    """渲染单个字段；超限时截断并**留下明确的截断标记 + 截断记录**。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if len(text) > _MAX_REVIEW_FIELD_CHARS:
        truncations.append({"field": name, "shown": _MAX_REVIEW_FIELD_CHARS, "total": len(text), "reason": "field_char_limit"})
        text = text[:_MAX_REVIEW_FIELD_CHARS] + (
            f"…[已截断：仅展示前 {_MAX_REVIEW_FIELD_CHARS} 字，共 {len(text)} 字]"
        )
    return text


def _render_list_field(name: str, values: Any, truncations: list[dict]) -> str | None:
    """渲染列表字段（逐条渲染，逐条标注截断）。"""
    lines = []
    for item in values or []:
        rendered = _render_field(name, item, truncations)
        if rendered:
            lines.append(f"  - {rendered}")
    return "\n".join(lines) if lines else None


def _add_field(parts: list[str], label: str, value: str | None) -> None:
    if not value:
        return
    parts.append(f"{label}:\n{value}" if "\n" in value else f"{label}: {value}")


def _format_report_for_review(report) -> tuple[str, list[dict]]:
    """将报告格式化为审查文本，返回 ``(文本, 截断记录)``。

    阶段 4①：必须覆盖**全部用户可见字段**。旧实现只看 what_happened（还被截到
    200 字）/ 置信度 / 状态标签 / 强势方向 / 风险 / 原因，漏掉了 what_changed、
    what_matters、data_caveats 与 claim—evidence 映射 —— Critic 看不到的字段，
    在审计里等于不存在（报告可以写满无法核实的变化与关注点而无人核对）。
    """
    truncations: list[dict] = []
    parts: list[str] = []

    _add_field(parts, "标题", _render_field("title", _field(report, "title", ""), truncations))
    _add_field(parts, "置信度", str(_field(report, "confidence", "N/A")))
    _add_field(parts, "市场状态", str(_field(report, "state_label", "N/A")))
    _add_field(parts, "综合状态", _render_field("market_state", _field(report, "market_state", ""), truncations))
    _add_field(parts, "发生了什么", _render_field("what_happened", _field(report, "what_happened", ""), truncations))

    for key, label in (
        ("why", "原因分析"),
        ("strong_areas", "强势方向"),
        ("what_changed", "与之前相比的变化"),
        ("what_matters", "后续关注点"),
        ("risks", "风险/反证"),
        ("data_caveats", "数据口径与时效说明"),
    ):
        _add_field(parts, label, _render_list_field(key, _field(report, key), truncations))

    # ── 阶段 4④：代码级校验结论（这些不是 LLM 的判断，是代码已经确定的事实）──
    violations = [str(v) for v in (_field(report, "evidence_violations", []) or [])]
    if violations:
        parts.append(
            "【代码级校验：无效证据引用 — 本报告不可能 pass】\n"
            + "\n".join(f"  ! {v}" for v in violations)
        )
    unverified = [str(x) for x in (_field(report, "unverified_entities", []) or [])]
    missing_market = [str(x) for x in (_field(report, "entities_without_market_evidence", []) or [])]
    if unverified:
        parts.append(
            "【无校验源的实体 — 不得断言其「不存在/未上市」，只能写「未验证」】\n"
            + "\n".join(f"  ? {name}" for name in unverified)
        )
    if missing_market:
        parts.append("【缺少本次行情 datum 的实体】\n" + "\n".join(f"  ? {name}" for name in missing_market))

    # ── 阶段 4③：claim—evidence 映射（逐条论断 → 支撑它的 evidence id）──
    claims = list(_field(report, "claims", []) or [])
    if claims:
        claim_lines = []
        for claim in claims:
            text = _render_field("claims", _field(claim, "claim", ""), truncations)
            ids = [str(x) for x in (_field(claim, "evidence_ids", []) or [])]
            ctype = str(_field(claim, "claim_type", "other"))
            claim_lines.append(f"  - [{ctype}] {text} ← {', '.join(ids) if ids else '（无 evidence 引用）'}")
        parts.append("关键论断 → 证据映射:\n" + "\n".join(claim_lines))
    else:
        parts.append("关键论断 → 证据映射:（报告未给出任何 claim 映射，无法逐条核对引用）")

    report_evidence = list(_field(report, "evidence", []) or [])
    if report_evidence:
        ids = [str(_field(item, "id", "?")) for item in report_evidence]
        parts.append(f"报告自带的证据条目（{len(ids)} 条）: {', '.join(ids)}")

    return "\n".join(parts), truncations


def _referenced_evidence_ids(report) -> list[str]:
    """报告引用到的 evidence id（claim 映射 + 报告自带证据条目），按出现顺序去重。"""
    ids: list[str] = []
    seen: set[str] = set()
    for claim in _field(report, "claims", []) or []:
        for eid in _field(claim, "evidence_ids", []) or []:
            text = str(eid)
            if text and text not in seen:
                seen.add(text)
                ids.append(text)
    for item in _field(report, "evidence", []) or []:
        text = str(_field(item, "id", "") or "")
        if text and text not in seen:
            seen.add(text)
            ids.append(text)
    return ids


def _render_evidence_item(item, *, value_limit: int = 500) -> str:
    """渲染一条证据（datum 原文 + 状态元信息）。"""
    value = str(_field(item, "value", ""))
    if len(value) > value_limit:
        value = value[:value_limit] + "…"
    head = f"{_field(item, 'id', '?')} | {_field(item, 'source_tool', '?')} | {_field(item, 'metric', '?')} = {value}"
    meta: list[str] = []
    for key in ("tool_key", "operation_id"):
        if _field(item, key):
            meta.append(f"{key}={_field(item, key)}")
    status = _field(item, "status")
    if status:
        meta.append(f"status={status}")
    if _field(item, "partial"):
        meta.append("partial=true")
    instrument = _field(item, "instrument")
    if instrument:
        meta.append(f"instrument={instrument}")
    timestamp = _field(item, "timestamp")
    if timestamp:
        meta.append(f"ts={timestamp}")
    note = _field(item, "note")
    if note:
        meta.append(f"note={str(note)[:200]}")
    return f"{head} [{' ; '.join(meta)}]" if meta else head


def _format_evidence_for_review(results, evidence, gate, referenced_ids=()) -> tuple[str, dict]:
    """格式化证据用于审查，返回 ``(文本, 统计/截断记录)``。

    阶段 4②：**报告引用的 evidence id 优先**，逐条给完整原始 datum ——
    Critic 要判断某句论断有没有证据，就必须先看到那句话引用的那条数据
    （旧实现把 ``evidence`` 参数收下却完全没用，只列了 gate 的工具名和
    前 20 个工具前 10 条 datum，报告引用的那条很可能根本不在里面）。
    """
    stats: dict[str, Any] = {
        "referenced_shown": 0,
        "referenced_missing": [],
        "other_shown": 0,
        "other_total": 0,
        "datums_omitted": 0,
        "results_omitted": 0,
        "kept": [],
        "omitted": [],
    }
    lines = ["=== 成功工具 ==="]
    if gate:
        for key in ("successful_tool_count", "valid_evidence_count", "invalid_evidence_count"):
            value = _field(gate, key, None)
            if value is not None:
                lines.append(f"  {key}={value}")
        for key in ("reason_codes", "stale_evidence", "unmatched_instruments", "partial_only"):
            value = _field(gate, key, []) or []
            if value:
                lines.append(f"  {key}={value}")
        for quality in _field(gate, "evidence_quality", []) or []:
            lines.append(f"  QUALITY evidence_id={quality.get('evidence_id', '?')} tool_key={quality.get('tool_key', '?')} source={quality.get('source', 'unknown')} as_of_date={quality.get('as_of_date', 'unknown')} retrieved_at={quality.get('retrieved_at', 'unknown')} completeness={quality.get('completeness', 'unknown')} coverage={quality.get('coverage', 'unknown')} reasons={quality.get('reason_codes', [])}")
    if gate:
        for t in _field(gate, "successful_tools", []) or []:
            lines.append(f"  OK {t}")
        for t in _field(gate, "partial_tools", []) or []:
            lines.append(f"  ~ partial: {t}")
        for err in _field(gate, "error_tools", []) or []:
            if isinstance(err, dict):
                lines.append(f"  FAIL {err.get('tool')}: {err.get('error', '?')}")
            else:
                lines.append(f"  FAIL {getattr(err, 'tool', '?')}: {getattr(err, 'error', '?')}")

    by_id: dict[str, Any] = {}
    for item in evidence or []:
        eid = str(_field(item, "id", "") or "")
        if eid:
            by_id.setdefault(eid, item)

    referenced = [str(x) for x in (referenced_ids or [])]
    if referenced:
        lines.append("")
        lines.append("=== 报告引用的证据（完整原始 datum）===")
        for eid in referenced:
            item = by_id.get(eid)
            if item is None:
                stats["referenced_missing"].append(eid)
                lines.append(f"  !! {eid}: 不在证据账本中（该引用无法核实）")
                continue
            stats["referenced_shown"] += 1
            stats["kept"].append({"id": eid, "reason": "report_reference"})
            lines.append(f"  - {_render_evidence_item(item)}")

    referenced_set = set(referenced)
    others = [item for eid, item in by_id.items() if eid not in referenced_set]
    stats["other_total"] = len(others)
    lines.append("")
    lines.append("=== 其余证据（摘要；未展示的内容不能据此判定为无证据）===")
    prioritized = sorted(enumerate(others), key=lambda pair: (_market_priority(pair[1]), pair[0]))
    selected = [item for _, item in prioritized[:_MAX_UNREFERENCED_EVIDENCE_LINES]]
    for item in selected:
        stats["other_shown"] += 1
        stats["kept"].append({"id": _field(item, "id"), "reason": "market_minimum" if _market_priority(item) < 3 else "tool_quota"})
        lines.append(f"  - {_render_evidence_item(item, value_limit=120)}")
    omitted = len(others) - stats["other_shown"]
    if omitted > 0:
        stats["omitted"].append({"kind": "evidence", "count": omitted, "reason": "unreferenced_evidence_limit"})
        lines.append(f"  …已省略 {omitted} 条（共 {len(others)} 条）；**未展示的内容不能据此判定为无证据**")

    # 原始 normalized 数据摘要（按工具）——保留旧视图，但把省略量显式记下来。
    lines.append("")
    lines.append("=== 原始 normalized 数据摘要（最多 20 个工具 × 10 条）===")
    shown_tools = 0
    sorted_results = sorted(enumerate(results or []), key=lambda pair: (
        _market_priority({"source_tool": _field(pair[1], "tool")}), pair[0]
    ))
    for _, result in sorted_results[:20]:
        normalized = _field(result, "normalized", []) or []
        shown_tools += 1
        result_meta = " ".join(
            f"{key}={_field(result, key)}" for key in ("status", "partial", "note")
            if _field(result, key)
        )
        lines.append(f"  TOOL {_field(result, 'tool_key', '?')} | {_field(result, 'operation_id') or _field(result, 'tool')} {result_meta}")
        selected_datums = sorted(enumerate(normalized), key=lambda pair: (_market_priority(pair[1]), pair[0]))[:10]
        for _, datum in selected_datums:
            metric = _field(datum, "metric", "?")
            value = str(_field(datum, "value", ""))[:80]
            meta = " ".join(
                f"{key}={_field(datum, key)}" for key in ("timestamp", "status", "partial", "note")
                if _field(datum, key)
            )
            lines.append(f"  - {_field(result, 'tool_key', '?')} | {_field(result, 'operation_id') or _field(result, 'tool')} | {metric}: {value} {meta}")
            stats["kept"].append({"metric": metric, "reason": "market_minimum" if _market_priority(datum) < 3 else "tool_quota"})
        if len(normalized) > 10:
            stats["datums_omitted"] += len(normalized) - 10
            stats["omitted"].append({"kind": "datum", "tool_key": _field(result, "tool_key"), "count": len(normalized) - 10, "reason": "per_tool_datum_limit"})
            lines.append(f"    …（该工具另有 {len(normalized) - 10} 条 datum 未展示）")
    total_tools = len(results or [])
    if total_tools > shown_tools:
        stats["results_omitted"] = total_tools - shown_tools
        stats["omitted"].append({"kind": "result", "count": total_tools - shown_tools, "reason": "tool_limit"})
        lines.append(f"  …还有 {total_tools - shown_tools} 个工具的输出未展示；**未展示的内容不能据此判定为无证据**")

    return "\n".join(lines), stats


def _truncation_summary(report_truncations: list[dict], evidence_stats: dict) -> str:
    """把截断情况写成 Critic 可读的说明（同时进入 telemetry）。"""
    lines: list[str] = []
    for item in report_truncations:
        lines.append(f"  - 报告字段 {item['field']}：展示 {item['shown']} 字 / 共 {item['total']} 字")
    if evidence_stats.get("referenced_missing"):
        lines.append(f"  - 报告引用了但账本中没有的 evidence id: {', '.join(evidence_stats['referenced_missing'])}")
    if evidence_stats.get("other_total", 0) > evidence_stats.get("other_shown", 0):
        lines.append(
            f"  - 未引用证据只展示 {evidence_stats['other_shown']} / {evidence_stats['other_total']} 条"
        )
    if evidence_stats.get("datums_omitted"):
        lines.append(f"  - 单个工具的 datum 摘要另有 {evidence_stats['datums_omitted']} 条未展示")
    if evidence_stats.get("results_omitted"):
        lines.append(f"  - 另有 {evidence_stats['results_omitted']} 个工具的输出未展示")
    return "\n".join(lines)


def _has_truncation(report_truncations: list[dict], evidence_stats: dict) -> bool:
    return bool(
        report_truncations
        or evidence_stats.get("referenced_missing")
        or evidence_stats.get("other_total", 0) > evidence_stats.get("other_shown", 0)
        or evidence_stats.get("datums_omitted")
        or evidence_stats.get("results_omitted")
    )


def _code_level_issues(report) -> list["AuditIssue"]:
    """把**代码级**校验结论转成 Critic 的 issue（阶段 4④）。

    这些不是"LLM 的审计意见"，而是代码已经确定的事实：

    - 报告引用了证据账本中不存在的 evidence id（伪造引用）；
    - 报告对无校验源的实体断言"不存在 / 未上市"。

    两者都取 ``remove_or_qualify`` → 派生 ``revise``，所以这类报告**不可能**
    被判 pass。不写 ``errors``：审计没通过不是运行失败。
    """
    issues: list[AuditIssue] = []
    for violation in _field(report, "evidence_violations", []) or []:
        issues.append(
            AuditIssue(
                kind="unsupported_claim",
                claim=str(violation),
                severity="high",
                action="remove_or_qualify",
                rationale="代码级校验：报告引用了证据账本中不存在的 evidence id，该引用无法核实",
            )
        )
    unverified = [str(x) for x in (_field(report, "unverified_entities", []) or [])]
    missing_market = [str(x) for x in (_field(report, "entities_without_market_evidence", []) or [])]
    if unverified or missing_market:
        text = entity_report_text(report)
        nonexistence = set(nonexistence_assertions(text, unverified))
        for name in nonexistence:
            issues.append(
                AuditIssue(
                    kind="unsupported_claim",
                    claim=f"报告断言实体「{name}」不存在/未上市",
                    severity="high",
                    action="remove_or_qualify",
                    rationale=(
                        f"实体「{name}」当前未验证；没有权威来源证明其不存在或未上市，"
                        "应改为「未验证」"
                    ),
                )
            )
        for name in dict.fromkeys([*unverified, *missing_market]):
            if name in nonexistence:
                continue
            for sentence in re.split(r"[。！？；，,\n]|(?:但|然而)", text):
                if name not in sentence or re.search(r"未验证|无法确认|尚不能确认|未能确认|有待确认", sentence):
                    continue
                market_claim = re.search(r"领涨|上涨|下跌|涨停|跌停|收涨|收跌", sentence)
                event_claim = name in unverified and re.search(r"盈利|亏损|发布|宣布", sentence)
                if market_claim or event_claim:
                    issues.append(
                        AuditIssue(
                            kind="missing_evidence",
                            claim=sentence.strip()[:200],
                            severity="high",
                            action="research_more",
                            rationale=f"实体「{name}」及该句行情/事件缺少对应的实际返回数据，需要补充标的证据",
                            required_tool_keys=["quote"] if market_claim else [],
                        )
                    )
                    break
    for conflict in _field(report, "entity_conflicts", []) or []:
        source = str(_field(conflict, "source", "") or "")
        as_of = str(_field(conflict, "as_of", "") or "")
        detail = str(_field(conflict, "conflict", "") or "")
        name = str(_field(conflict, "name", "") or "")
        authority = str(_field(conflict, "authority", "") or "")
        if not (source and as_of and detail and name and authority == "authoritative"):
            continue
        issues.append(
            AuditIssue(
                kind="invalid_entity",
                claim=f"{name}: {detail}",
                severity="high",
                action="remove_or_qualify",
                rationale=f"权威证券主数据与成功返回的名称/代码冲突：{detail}; source={source}; as_of={as_of}",
            )
        )
    return issues


def _claim_evidence_issues(
    report,
    evidence,
    expected_date: str | None = None,
    *,
    requested_date: str | None = None,
    market_closed: bool | None = None,
) -> list["AuditIssue"]:
    """Apply the post-Reasoning claim→datum gate without weakening the LLM audit."""
    issues: list[AuditIssue] = []
    for row in match_claims_to_evidence(
        report, evidence, expected_date=expected_date,
        requested_date=requested_date, market_closed=market_closed,
    ):
        reasons = row["reason_codes"]
        if not reasons:
            continue
        action = "research_more" if any(x in reasons for x in ("missing_reference", "unknown_date", "unknown_source", "stale_date", "invalid_evidence", "instrument_name_unknown", "conflicting_evidence")) else "remove_or_qualify"
        if "invalid_reference" in reasons:
            action = "remove_or_qualify"
        issues.append(
            AuditIssue(
                kind="missing_evidence" if action == "research_more" else "unsupported_claim",
                claim=row["claim"][:200],
                severity="high" if any(x in reasons for x in ("invalid_reference", "invalid_evidence", "news_mention_not_price")) else "medium",
                action=action,
                rationale=f"逐条 claim to evidence 校验失败；evidence_ids={','.join(row['evidence_ids']) or 'none'}；reason_codes={','.join(reasons)}",
            )
        )
    return issues


_DATE_RE = re.compile(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}")
_BREADTH_RE = re.compile(r"(?:全市场|所有(?:股票|个股)|全部(?:股票|个股)).{0,12}(?:全部)?(?:上涨|下跌)|(?:全部|全线)(?:上涨|下跌)")
_CAUSAL_RE = re.compile(r"(?:导致|带动|引发|造成)")
_FLOW_RE = re.compile(r"资金.{0,24}(?:流向|转向|从.{1,20}流入)")
_QUALIFIED_RE = re.compile(r"可能|或许|疑似|无法确认|尚不能确认|未能确认")


def _coverage_issues(report, evidence) -> list["AuditIssue"]:
    """Reject market-wide and causal facts beyond the observable coverage."""
    items = list(evidence or [])
    text = entity_report_text(report)
    segments = [part.strip() for part in re.split(r"[。！？；\n]", text) if part.strip()]
    complete = [item for item in items if not _incomplete(item)]
    up = [item for item in complete if re.search(r"up_count|advancing|上涨家数", str(_field(item, "metric", "")), re.I)]
    down = [item for item in complete if re.search(r"down_count|declining|下跌家数", str(_field(item, "metric", "")), re.I)]
    issues: list[AuditIssue] = []
    for claim in _field(report, "claims", []) or []:
        claim_text = str(_field(claim, "claim", "") or "").strip()
        claim_type = str(_field(claim, "claim_type", "other") or "other")
        ids = list(_field(claim, "evidence_ids", []) or [])
        if claim_text and claim_type in {"fact", "comparison", "causation", "structure"} and not ids:
            issues.append(
                AuditIssue(
                    kind="unsupported_claim",
                    claim=claim_text[:200],
                    severity="high",
                    action="remove_or_qualify",
                    rationale="当前事实/比较/因果论断没有绑定真实 evidence id",
                )
            )
    for segment in segments:
        if _QUALIFIED_RE.search(segment):
            continue
        if _BREADTH_RE.search(segment):
            opposing = down if "上涨" in segment else up
            up_dates = {
                match.group()
                for i in up
                if (match := _DATE_RE.search(str(_field(i, "as_of_date", "") or _field(i, "timestamp", "") or "")))
            }
            down_dates = {
                match.group()
                for i in down
                if (match := _DATE_RE.search(str(_field(i, "as_of_date", "") or _field(i, "timestamp", "") or "")))
            }
            complete_breadth = bool(up_dates & down_dates)
            zero_opposing = all(str(_field(i, "value", "")).strip() in ("0", "0.0") for i in opposing)
            if not complete_breadth or not zero_opposing:
                issues.append(AuditIssue(kind="unsupported_claim", claim=segment[:200], severity="high",
                    action="remove_or_qualify", rationale="缺少完整、同日的上涨/下跌家数，不能断言全市场全部上涨或下跌；只能写无法确认"))
        if _CAUSAL_RE.search(segment) or _FLOW_RE.search(segment):
            # Separate price and news observations cannot establish the relationship.
            relation = re.sub(r"\s+", "", segment)
            claim_dates = set(_DATE_RE.findall(segment))
            direct = any(
                (stamp := _DATE_RE.search(str(_field(item, "as_of_date", "") or _field(item, "timestamp", "") or "")))
                and (not claim_dates or stamp.group() in claim_dates)
                and relation in re.sub(r"\s+", "", str(_field(item, "value", "")))
                for item in complete
            )
            if not direct:
                issues.append(AuditIssue(kind="unsupported_claim", claim=segment[:200], severity="high",
                    action="remove_or_qualify", rationale="缺少同日、同标的或板块的直接证据支撑因果或资金流向断言"))
    return issues


def _issues_error(detail: str) -> ValidationError:
    """构造一个可读的 ValidationError（用于 issues 容器的类型错误）。"""
    return ValidationError.from_exception_data(
        "AuditIssue[]",
        [{"type": "list_type", "loc": ("issues",), "input": detail}],
    )


_MISSING_FIELD: Any = object()
"""哨兵：区分「payload 里**没有** ``issues`` 字段」与「显式写了 ``issues: null``」。

前者是合法的"未使用该字段"，后者是非法 payload（模型说了有东西，却没给出合法数组）。
用 ``data.get("issues")`` 会把两者都变成 ``None``，正是缺陷 A 的成因。
"""


def parse_issues(raw: Any) -> list[AuditIssue]:
    """把 payload 的 ``issues`` 严格解析成 ``list[AuditIssue]``。

    **绝不静默过滤任何一项**（方案 §4 阶段 1 验收第 4 条）：
    - ``issues`` 字段缺失（``_MISSING_FIELD``）→ 合法，返回空数组；
    - ``issues`` 显式为 ``null`` → **报错**（不是"空数组"，是非法 payload）；
    - ``issues`` 不是 list → 报错；
    - 数组里出现非对象元素（字符串 / null / 数字 / 嵌套数组）→ 报错；
    - 对象的字段不合法（kind / action / severity 越界）→ 报错。

    旧实现用 ``if isinstance(item, dict)`` 把非法项悄悄丢掉，于是
    ``{"issues": ["缺数据"]}`` 会退化成"issues 键存在 → 派生 pass"——
    模型明明说了有缺口，系统却判它通过。任何一种情况都必须是 ``verdict="error"``。

    显式 ``null`` 同样致命：``{"issues": null}`` 没有任何 verdict 时若被当成空数组，
    同样会退化成"issues 键存在 → pass"。缺字段与显式 null 是两件事，必须分开。
    """
    if raw is _MISSING_FIELD:
        return []
    if raw is None:
        # 报错文本把类型名放在最前面：pydantic 渲染 ValidationError 时会截断
        # input_value（约 25 字符），"NoneType" 必须落在截断点之前才可被断言/检索到。
        raise _issues_error("issues=NoneType（显式 null）不是合法的 issues 数组；请省略该字段，或给出数组 []")
    if not isinstance(raw, list):
        raise _issues_error(f"issues={type(raw).__name__} 不是合法的 issues 数组（必须为 list）")

    issues: list[AuditIssue] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _issues_error(
                f"issues[{index}] must be an object, got {type(item).__name__}: {item!r:.120}"
            )
        issues.append(AuditIssue.model_validate(item))
    return issues


def _verdict_from_payload(data: dict, issues: list[AuditIssue]) -> str:
    """模型没给 verdict 时按 issues（再退 legacy 数组）派生；派生不出来返回 "error"。

    优先级刻意是"缺口 > 显式无问题 > 错误"：

    1. issues 里有 research_more / revise → 照此派生；
    2. legacy 缺口数组非空 → research_more（模型认为缺数据）；
    3. **模型显式给了合法 ``issues`` 数组（哪怕是空数组）** → pass
       —— 这是"逐条审查后没发现问题"的正式表态，与"什么都没说"不同；
    4. 都没有 → error。**绝不返回 pass**：没说话 ≠ 证据充分。

    第 3 步只认**合法数组**：``issues: null`` 由 ``parse_issues`` 提前拦下并置
    ``error``；这里再挡一层，避免"键存在就算通过"的退化路径被重新引入。

    与 ``resolve_conflicts`` 的口径一致（后者只看 Critique 上真实存在的信号），
    因此这里派生出的 pass 不会被后者"改回"任何别的值。
    """
    derived = derive_verdict_from_issues(issues)
    if derived:
        return derived
    if data.get("missing_points") or data.get("missing_tool_keys"):
        return "research_more"
    if data.get("unsupported_claims"):
        return "revise"
    if "issues" in data and data.get("issues") is not None:
        return "pass"
    return "error"


#: 裁决的「保守程度」排序（方案 §4 阶段 1 第 4 条）。
#: 越靠右越保守：补研究最贵但结论最弱，pass 最便宜但最容易放过未经验证的内容。
#: ``error`` 比三者都保守 —— 审计器自己失能时不可能有可信结论。
_VERDICT_CONSERVATISM = {"pass": 0, "revise": 1, "research_more": 2, "error": 3}


def more_conservative(a: str, b: str) -> str:
    """返回两个裁决中**更保守**的一个（research_more > revise > pass > error 之外）。

    冲突处理绝不能"用结构化派生覆盖模型自报值"——那会在模型更保守时反而放宽
    （例如模型判 research_more、action 只要求改写时，被降级成 revise）。
    """
    if a not in _VERDICT_CONSERVATISM:
        return b
    if b not in _VERDICT_CONSERVATISM:
        return a
    return a if _VERDICT_CONSERVATISM[a] >= _VERDICT_CONSERVATISM[b] else b


def resolve_conflicts(critique: Critique) -> list[dict[str, str]]:
    """阶段 1 第 4 条：actions 与模型自报 verdict 冲突时按**更保守**的一侧路由。

    返回冲突记录（写进结构化日志）。冲突本身不是运行失败，因此**不写 state.errors**
    —— errors 只表达运行失败；"模型裁决与 action 不一致"属于审计质量问题，
    已由保守路由吸收。

    保守方向 = 成本更高、结论更弱的一侧：
    ``research_more > revise > pass``（见 ``more_conservative``）。注意是**取两者的
    较大值**，而不是"以 action 派生为准"——后者会在模型更保守时把它放宽。
    """
    conflicts: list[dict[str, str]] = []

    structured = derive_verdict_from_issues(critique.issues)
    if structured is None:
        # 旧格式响应（只有 legacy 数组，没有结构化 issues）：按数组语义派生。
        # legacy 数组是"更弱"的信号，但只要它非空就说明模型认为有缺口，
        # 因此同样只朝保守方向修正。
        if critique.missing_points or critique.missing_tool_keys:
            structured = "research_more"
        elif critique.unsupported_claims:
            structured = "revise"

    if structured and structured != critique.verdict:
        resolved = more_conservative(critique.verdict, structured)
        conflicts.append(
            {
                "kind": "verdict_vs_actions",
                "model_verdict": critique.verdict,
                "action_verdict": structured,
                "resolved": resolved,
            }
        )
        critique.verdict = resolved

    if conflicts:
        logger.error(
            "Critic 裁决与 issue action 冲突，按更保守路径路由: %s",
            conflicts,
        )
    return conflicts


class CriticNode:
    """Critique 节点：对 Report 进行证据一致性审计。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def __call__(self, state):
        """执行 Critic 审查，返回 Critique。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)

            report = state.get("report")
            if not report:
                critique = Critique(
                    verdict="research_more",
                    reason="推理引擎未产出报告",
                    missing_points=["完整的市场情报报告"],
                )
                run_log.log_critic(state, critique)
                return {"critique": critique}

            results = state.get("results", [])
            evidence = state.get("evidence", [])
            gate = state.get("gate")

            # Build domain-aware review prompt
            intent = state.get("intent")
            domain = _field(intent, "domain") or state.get("domain") or "a_share"

            rules = _get_domain_rules(domain)

            # 阶段 4①②：报告全字段（含 claim—evidence 映射）+ 报告引用优先的证据。
            report_review, report_truncations = _format_report_for_review(report)
            referenced_ids = _referenced_evidence_ids(report)
            evidence_review, evidence_stats = _format_evidence_for_review(
                results, evidence, gate, referenced_ids
            )

            # Assemble prompt pieces
            prompt_pieces = [
                f"用户问题: {state.get('question', '')}\n",
                f"目标域: {domain}\n\n",
                "=== 日期语义（代码事实）===\n",
                f"requested_date={state.get('requested_date') or 'unknown'}；"
                f"planned_as_of_date={state.get('planned_as_of_date') or 'unknown'}；"
                f"as_of_date={state.get('as_of_date') or 'unknown'}；"
                f"market_closed={state.get('market_closed') if state.get('market_closed') is not None else 'unknown'}\n"
                "同日覆盖只能按 evidence 的 as_of_date 判断；retrieved_at 不能证明行情发生在请求日。\n\n",
                "=== 待审查的报告 ===\n",
                report_review,
                "\n\n=== 实际证据 ===\n",
                evidence_review,
            ]

            # 阶段 4③：上下文被截断时必须**明说**，否则 Critic 会把"没展示"
            # 读成"没有证据"，直接判 fail 或放过无法核实的论断。
            context_truncation: dict[str, Any] = {
                "report_fields": report_truncations,
                "evidence": evidence_stats,
            }
            if _has_truncation(report_truncations, evidence_stats):
                prompt_pieces.extend(
                    [
                        "\n\n=== 审查上下文截断说明 ===\n",
                        "以上报告与证据**有截断**。截断的内容你看不到，因此：\n"
                        "  - **未展示的内容不能据此判定为「无证据」**；\n"
                        "  - 只能就展示出来的内容判定；对看不到的部分如有疑问，"
                        "用 required_tool_keys 点名要求补充，而不是直接判定它没有依据。\n",
                        _truncation_summary(report_truncations, evidence_stats),
                    ]
                )

            if rules:
                prompt_pieces.extend(["\n=== 审查规则 ===\n", rules])
            # A7：新闻证据纪律跨域通用，无条件附加（不受 _DOMAIN_RULES 域组织限制）
            prompt_pieces.extend(["\n=== 新闻证据纪律 ===\n", _NEWS_RULES])
            # Step 5.2：舆情情绪纪律（sentiment 分析师启用时才有 sentiment_* 证据；
            # 规则本身无害，但仅在开关打开时注入，避免默认路径 prompt 膨胀与回归风险）
            if getattr(self.settings, "sentiment_enabled", False):
                prompt_pieces.extend(["\n=== 舆情情绪纪律 ===\n", _SENTIMENT_RULES])
            # 阶段 1：审计契约（issues + action 语义）
            prompt_pieces.extend(["\n=== 审计契约 ===\n", _ISSUE_RULES])

            # 结构化缺口：把「本域注册表 + 每个工具现有覆盖度」列给 Critic，它才能点名补充。
            # 阶段 2：判定从"工具名跑过"换成"这次执行是否真的满足了缺口"；
            # 阶段 2 补修：**已执行的工具也不隐藏**，只标注现有数据量——
            # prompt 组装时本轮 Critic 的 required_coverage 还不存在，
            # 无法预判"success + 3 条"对"要 ≥20 条"的缺口是否够用，
            # 隐藏等于让 Critic 永远点不到名。
            from app.gateway.tool_registry import registry_text

            gap_registry = registry_text(domains=[domain, "cross"])
            prompt_pieces.extend(
                [
                    "\n=== 可补充的工具（本域注册表；已执行的工具标注现有数据量，覆盖不足仍可点名重拉）===\n",
                    render_registry_for_critic(gap_registry, results),
                ]
            )

            prompt_pieces.extend(
                [
                    "\n\n请逐条审查报告中的核心论断是否有对应证据支撑。"
                    "注意：不要因为缺少理想数据就判 fail -- 看已有证据够不够回答用户问题。",
                    "\n\n必须只返回合法 JSON object。",
                    '{"issues": [{"kind": "unsupported_claim|missing_evidence|invalid_entity|format",'
                    ' "claim": "...", "severity": "low|medium|high",'
                    ' "action": "remove_or_qualify|research_more|repair_format",'
                    ' "rationale": "...", "required_tool_keys": [...],'
                    ' "required_coverage": {...}}],'
                    ' "reason": "...", "missing_points": [...], '
                    '"missing_tool_keys": [...], "unsupported_claims": [...]}',
                ]
            )

            prompt = "\n".join(prompt_pieces)

            _llm_started = time.perf_counter()
            response = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": "你是 Mosaic 的 Critic，只返回合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            # 阶段 6：审计这次 LLM 调用的用量/耗时（指标数据源）。
            run_log.log_llm_call(
                state,
                node="critic",
                model=self.settings.openai_model,
                usage=getattr(response, "usage", None),
                duration_ms=(time.perf_counter() - _llm_started) * 1000,
            )

            content = response.choices[0].message.content or "{}"
            data = parse_json_object(content, source="Critic")

            # T11：verdict 先归一化（去空白 + 小写），再做校验。
            raw_verdict = str(data.get("verdict", "")).strip().lower()
            # 阶段 1：模型不再"自由选择唯一动作"——新版 prompt 不再要求它给 verdict。
            # 这里先把 issues 单独校验出来（issues 是新的唯一事实来源），
            # 再据此派生 verdict；派生不出来且模型也没给 → 下面判 error，绝不伪造 pass。
            try:
                # 传哨兵而不是 data.get("issues")：缺字段合法，显式 null 非法（见 parse_issues）。
                issues = parse_issues(data.get("issues", _MISSING_FIELD))
            except ValidationError as exc:
                logger.error("Critic issues 校验失败，安全终止: %s", type(exc).__name__)
                critique = Critique(verdict="error", reason=f"Invalid critic payload: {exc}")
                run_log.log_critic(state, critique)
                return {
                    "critique": critique,
                    "errors": [f"Critic 输出无法解析为合法结论: {str(exc)[:200]}"],
                }

            # 阶段 4④：**代码级**校验结论并入 issues。这不是让 LLM 再判一次：
            # 报告引用了账本里不存在的 evidence id、或对无校验源实体断言"不存在"，
            # 都是代码已经确定的事实，必须变成 issue → 派生 revise → 不可能 pass。
            # Same-day coverage is judged against the evidence date.  A
            # weekend requested_date is user language, not a trading date.
            expected_date = state.get("planned_as_of_date") or state.get("as_of_date")
            if not expected_date:
                requested_match = _DATE_RE.search(str(state.get("question", "")))
                expected_date = requested_match.group().replace("/", "-") if requested_match else None
            code_issues = _code_level_issues(report) + _coverage_issues(report, evidence) + _claim_evidence_issues(
                report, evidence, expected_date=expected_date,
                requested_date=state.get("requested_date"), market_closed=state.get("market_closed"),
            )
            if code_issues:
                issues = issues + code_issues
                logger.error(
                    "Critic: 代码级校验产出 %d 条 issue（%s），本轮不可能判 pass",
                    len(code_issues),
                    [i.kind for i in code_issues],
                )

            if raw_verdict:
                if raw_verdict not in _MODEL_VERDICTS:
                    # 非法 / 无法识别的 verdict：安全终止并记 errors，
                    # 绝不能像旧逻辑那样落到路由默认 end（= 静默当 pass），
                    # 也不伪造 research_more 再烧一到两轮完整工具 + LLM。
                    logger.error("Critic verdict 无法识别 %r，安全终止", raw_verdict)
                    critique = Critique(verdict="error", reason=f"Unrecognized critic verdict: {raw_verdict!r}")
                    run_log.log_critic(state, critique)
                    return {
                        "critique": critique,
                        "errors": [f"Critic 返回了无法识别的 verdict: {raw_verdict!r}，已安全终止"],
                    }
                # 保留模型自报值：resolve_conflicts 会拿它和 issue action 对账，
                # 冲突时改写成更保守的一侧并记结构化日志。
                data["verdict"] = raw_verdict
            else:
                data["verdict"] = _verdict_from_payload(data, issues)
                if data["verdict"] == "error":
                    # 没有任何 issue、缺口线索与 verdict → 无法判定。安全终止并写 errors，
                    # 绝不能落到路由默认（= 静默当 pass）。
                    logger.error("Critic 未给出任何可判定的审计结论，安全终止")
                    critique = Critique(
                        verdict="error",
                        reason="Critic returned no issues, no gaps and no verdict",
                    )
                    run_log.log_critic(state, critique)
                    return {
                        "critique": critique,
                        "errors": ["Critic 输出无法解析为合法结论: 未给出 verdict 且没有任何 issue"],
                    }
            data["issues"] = [issue.model_dump() for issue in issues]

            try:
                critique = Critique.model_validate(data)
            except ValidationError as exc:
                logger.error("Critic 输出校验失败，安全终止: %s", type(exc).__name__)
                critique = Critique(verdict="error", reason=f"Invalid critic payload: {exc}")
                run_log.log_critic(state, critique)
                return {
                    "critique": critique,
                    "errors": [f"Critic 输出无法解析为合法结论: {str(exc)[:200]}"],
                }

            # 阶段 1：verdict 由 issue action 派生（保守方向优先）
            conflicts = resolve_conflicts(critique)

            logger.info("Critic verdict: %s (reason: %s)", critique.verdict, run_log.redact_text(critique.reason))

            # 落库前过滤：与 prompt 里「可补充的工具」小节用**同一份**可见集合，
            # 保证喂回 Supervisor 的 key 一定可被 resolve。判定依据是**逐条 issue 的
            # required_coverage**（tool_key → 覆盖条件），而不是"工具跑过"或一个
            # 全局默认值——同 key 的少量 success 数据不能冒充"已满足 ≥20 条"的缺口。
            # 过滤不是错误 —— 只写 warning + 结构化决策记录，不写 errors
            # （errors 只表达运行失败）。
            requirements = coverage_requirements(critique)
            decision = classify_missing_tool_keys(
                critique.missing_tool_keys,
                allowed=allowed_keys(gap_registry),
                satisfied=satisfied_keys(results, requirements),
            )
            critique.missing_tool_keys = decision.kept
            # 保留原始、保留和被过滤 key 的完整原因，不能让下一轮只看到 kept 列表。
            critique.gap_key_decisions = decision.as_state()
            if decision.dropped:
                logger.warning(
                    "Critic: 丢弃 %d 个缺口 key（%s）",
                    len(decision.dropped),
                    decision.reason_counts(),
                )

            run_log.log_critic(
                state,
                critique,
                conflicts=conflicts,
                gap_key_decisions=decision.as_state(),
                context_truncation=context_truncation,
            )
            return {
                "critique": critique,
                "gap_key_decisions": decision.as_state(),
                "executable_gap_steps": [],
            }

        except LLMOutputError as exc:
            # T11：审计自身失败（LLM 超时 / JSON 坏）不再返回 research_more——
            # 那会触发一到两轮完整工具 + LLM（烧钱）且伪造 missing_points。
            # 改为内部 error verdict 安全终止，只保留 errors。
            logger.error("Critic LLM 输出不可用，安全终止: %s", type(exc).__name__)
            return {
                "critique": Critique(verdict="error", reason=f"Critic audit failed: {exc}"),
                "errors": [f"Critic audit failed: {exc}"],
            }
        except Exception as exc:
            logger.exception("Critic node failed，安全终止")
            return {
                "critique": Critique(verdict="error", reason=f"Critic audit failed: {exc}"),
                "errors": [f"Critic audit failed: {exc}"],
            }
