"""Reasoning Engine — 将工具结果 + Evidence → 结构化市场情报。

包含向后兼容的 LLM 输出转换逻辑：
- 旧的 evidence 格式：[{claim, evidence_ids}]
- 新的 evidence 格式：[{id, source_tool, metric, value, ...}] (EvidenceItem)
- what_changed 可能是段落字符串或数组
"""

import json
import logging
import re
import time
from typing import Any, get_args

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agent.prompts import REASONING_PROMPT
from app.config import Settings
from app.errors import LLMOutputError
from app.llm_json import parse_json_object
from app.models.evidence import Evidence
from app.models.market import ToolResult
from app.models.response import ClaimEvidence, ClaimType, EvidenceItem, MarketIntelligence
from app.research.entity_check import check_report_entities, report_text

logger = logging.getLogger(__name__)

#: claim_type 的合法取值（与 ``app/models/response.py`` 的 ``ClaimType`` 同源）。
_CLAIM_TYPES: frozenset[str] = frozenset(get_args(ClaimType))


def _get_evidence_key(item) -> str:
    """提取 evidence item 的 source_tool，用于按工具分组截断。"""
    if isinstance(item, dict):
        return item.get("source_tool", "unknown")
    return getattr(item, "source_tool", "unknown")


def _ensure_list(val, default=None):
    """确保值是列表（LLM 可能返回字符串）。"""
    if val is None:
        return default or []
    if isinstance(val, list):
        return val
    return [val]


def _parse_evidence(raw_evidence: list, original_evidence: list[Evidence]) -> list[EvidenceItem]:
    """将 LLM 返回的 evidence 转换为 EvidenceItem 列表。

    支持两种格式：
    1. 新格式: [{id, source_tool, metric, value, domain, status, partial, timestamp, source, note}]
    2. 旧格式: [{claim, evidence_ids}] → 映射到原始 Evidence 数据

    无论哪种格式，**value/status/partial/source_tool/domain/metric 等核心字段**都来自
    真实工具调用结果（original_evidence），不是 LLM 说的。LLM 只负责提供 note / interpretation
    等辅助文字。这样防止 LLM 编造一个 "evidence-999" 并赋予虚假的 high 置信度数值——
    这是审计发现的高危问题：UI 上那个证据面板和置信度条目前是模型的作文，不是数据的呈现。

    当原始证据列表不可用时，回退为用证据ID构建占位 EvidenceItem。
    """
    items: list[EvidenceItem] = []

    # 建立 evidence_id → Evidence 的查找表。T4：build_evidence 历史上每次都从
    # evidence-001 开始编号，多个 analyst 合并后 id 重复、后者静默覆盖前者。
    # 这里额外建 (id, source_tool) 复合表并检测重复，重复时记 warning 并退化匹配。
    id_to_evidence: dict[str, Evidence] = {}
    id_source_to_evidence: dict[tuple[str, str], Evidence] = {}
    duplicate_ids: set[str] = set()
    for original in original_evidence:
        if original.id in id_to_evidence:
            duplicate_ids.add(original.id)
            logger.warning(
                "检测到重复 evidence id %s（source_tool=%s 与 %s），将按 (id, source_tool) 退化匹配以避免证据错配",
                original.id,
                id_to_evidence[original.id].source_tool,
                original.source_tool,
            )
        id_to_evidence[original.id] = original
        id_source_to_evidence[(original.id, original.source_tool)] = original

    def _resolve_evidence(eid: str, source_tool: str | None) -> Evidence | None:
        if eid in duplicate_ids:
            return id_source_to_evidence.get((eid, source_tool)) if source_tool else None
        return id_to_evidence.get(eid)

    for raw_item in raw_evidence or []:
        if not isinstance(raw_item, dict):
            continue

        item_id = raw_item.get("id")
        claim = raw_item.get("claim")
        ev_ids = raw_item.get("evidence_ids", [])

        # ── 识别是否为旧格式（有 claim 但没有 id）──
        is_old_format = bool(claim) and not item_id

        if is_old_format:
            # 旧格式：{claim, evidence_ids}
            matched_count = 0
            for eid in ev_ids or []:
                ev = _resolve_evidence(eid, None)
                if ev:
                    matched_count += 1
                    items.append(
                        EvidenceItem(
                            id=eid,
                            source_tool=ev.source_tool,
                            tool_key=ev.tool_key,
                            operation_id=ev.operation_id,
                            domain=ev.domain,
                            metric=ev.metric,
                            value=ev.value,
                            timestamp=ev.timestamp,
                            source=ev.source,
                            status=ev.status,
                            partial=ev.partial,
                            note=f"{claim} | {ev.note}" if ev.note else claim,
                        )
                    )
            # 如果没有匹配的证据IDs，至少创建一条说明性证据
            # （注意：matched_count 按每个 claim 单独计数，不是全局累计 —— 避免漏掉后续 claim）
            if matched_count == 0 and claim:
                items.append(
                    EvidenceItem(
                        id="evidence-interp",
                        source_tool="reasoning_engine",
                        metric="interpretation",
                        value=claim,
                        status="error",  # 没有证据支撑，标记为错误而非成功
                        partial=True,
                        note=claim,
                    )
                )
        else:
            # 新格式：从真实 Evidence 取 value/status/partial/source_tool/domain/metric，
            # LLM 只负责提供 note / interpretation。未知 id → 丢弃（不凭空编造证据）。
            # 这堵死了审计发现的严重缺陷：LLM 可以给不存在的 evidence-id 赋予任意
            # 数值和 confident status，而 UI 会渲染成 "数据高度一致"。
            if not item_id:
                # 没 id 的无法匹配真实数据 → 丢弃而不是生成假 evidence
                continue
            original_ev = _resolve_evidence(item_id, raw_item.get("source_tool"))
            if original_ev is None:
                # LLM 提到了一个不存在于证据链中的 id → 可能是幻觉
                # （比如报告说"我的模型能直接看到所有市场数据，不需要调用工具"）
                continue

            llm_note = raw_item.get("note")
            interpretation = raw_item.get("interpretation") or ""
            combined_note = (
                f"{llm_note} — {interpretation}".strip()
                if llm_note and interpretation
                else (llm_note or interpretation)
            )

            items.append(
                EvidenceItem(
                    id=item_id,
                    source_tool=original_ev.source_tool,
                    tool_key=original_ev.tool_key,
                    operation_id=original_ev.operation_id,
                    domain=original_ev.domain,
                    metric=original_ev.metric,
                    value=original_ev.value,
                    timestamp=original_ev.timestamp,
                    source=original_ev.source,
                    status=original_ev.status,
                    partial=original_ev.partial,
                    note="；".join(part for part in (original_ev.note, combined_note) if part) or None,
                    instrument=original_ev.instrument,
                )
            )

    return items


def _parse_claims(raw_claims) -> list[ClaimEvidence]:
    """把 LLM 的 claim—evidence 映射解析成模型对象（容忍形状错误）。

    形状错的条目**丢弃**而不是抛 ``ValidationError``：claims 是可追溯性增强
    字段，不该像 T20 那样"一个字段毁掉整份报告"（那会把此前所有已付费的工具
    调用一起丢掉）。丢弃之后报告在 Critic 眼里就是"没有可追溯论断"，
    会被要求逐条核对，比整份报告 502 更符合安全方向。
    """
    claims: list[ClaimEvidence] = []
    for raw in _ensure_list(raw_claims):
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim") or "").strip()
        if not claim_text:
            continue
        evidence_ids = [
            str(eid)
            for eid in _ensure_list(raw.get("evidence_ids"))
            if isinstance(eid, (str, int)) and str(eid).strip()
        ]
        claim_type = raw.get("claim_type")
        if claim_type not in _CLAIM_TYPES:
            claim_type = "other"
        claims.append(ClaimEvidence(claim=claim_text, evidence_ids=evidence_ids, claim_type=claim_type))
    return claims


def _validate_claims(
    claims: list[ClaimEvidence],
    valid_ids: set[str],
) -> tuple[list[ClaimEvidence], list[str]]:
    """代码级校验 claim—evidence 映射，返回 ``(保留的 claims, violations)``。

    LLM 编一个 evidence id 就宣称"有证据"的路必须在这里堵死：

    - 不存在的 id 一律剥离（不能让引用看起来成立）；
    - 一条论断原本引用了 id、剥离后一个都不剩 → 该论断在报告里就是无据的，
      从 claims 中移除，并记入 violations；
    - 本来就没引用任何 id 的论断不算 violation（它可以是显式标注的 inference），
      但仍留在 claims 里供 Critic 审查。

    violations 非空时调用方会把报告降级（confidence=low + data_caveats），
    并且 Critic 节点会据此在代码层强制产出 ``unsupported_claim`` issue，
    因此这种报告**不可能**被判 pass。
    """
    kept: list[ClaimEvidence] = []
    violations: list[str] = []
    for claim in claims:
        if not claim.evidence_ids:
            kept.append(claim)
            continue
        valid = [eid for eid in claim.evidence_ids if eid in valid_ids]
        missing = [eid for eid in claim.evidence_ids if eid not in valid_ids]
        if missing:
            violations.append(
                f"论断「{claim.claim[:80]}」引用了不存在的 evidence id: {', '.join(missing)}"
            )
        if not valid:
            continue
        claim.evidence_ids = valid
        kept.append(claim)
    return kept, violations


_EVIDENCE_ID_RE = re.compile(r"\b(?:technical|fundamental|moneyflow|news|sentiment|evidence)-[A-Za-z0-9]+\b|\b[A-Za-z][A-Za-z0-9_-]*-\d{3,}\b")
_REPORT_TEXT_FIELDS = ("title", "market_state", "what_happened")
_REPORT_LIST_FIELDS = ("why", "strong_areas", "what_changed", "what_matters", "risks")


def _validate_report_references(payload: dict, valid_ids: set[str]) -> list[str]:
    """Remove invented evidence citations from user-visible prose as well as claims."""
    violations: list[str] = []

    def clean(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            eid = match.group()
            if eid in valid_ids:
                return eid
            violation = f"报告正文引用了不存在的 evidence id: {eid}"
            if violation not in violations:
                violations.append(violation)
            return "[无效证据引用已删除]"

        return _EVIDENCE_ID_RE.sub(replace, text)

    for field in _REPORT_TEXT_FIELDS:
        if isinstance(payload.get(field), str):
            payload[field] = clean(payload[field])
    for field in _REPORT_LIST_FIELDS:
        payload[field] = [clean(item) if isinstance(item, str) else item for item in payload[field]]
    for claim in _ensure_list(payload.get("claims")):
        if isinstance(claim, dict) and isinstance(claim.get("claim"), str):
            claim["claim"] = clean(claim["claim"])
    return violations


class ReasoningEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )
        #: 阶段 6：最近一次 LLM 调用的用量与耗时（调用方读它 → run_log `llm` 事件）。
        #: ``None`` = 没量到（打桩客户端 / 端点不回 usage），与"用了 0 token"不同。
        self.last_llm_usage: Any = None
        self.last_llm_duration_ms: float | None = None

    async def reason(
        self,
        question: str,
        results: list[ToolResult],
        evidence: list[Evidence],
        history_context: str = "",
        findings: list | None = None,
    ) -> MarketIntelligence:
        normalized_data = [datum.model_dump() for result in results for datum in result.normalized]

        # 拼装 findings 段（放在 history_context 之前）
        parts: list[str] = []
        if findings:
            lines = ["ANALYST SUMMARIES（各分析员中间结论，仅供交叉参考）:"]
            for f in findings:
                analyst = f.get("analyst") if isinstance(f, dict) else getattr(f, "analyst", "?")
                digest = f.get("digest") if isinstance(f, dict) else getattr(f, "digest", "")
                failed = f.get("failed") if isinstance(f, dict) else getattr(f, "failed", False)
                if failed:
                    lines.append(f"  - {analyst}: [失败] {digest}")
                elif digest:
                    lines.append(f"  - {analyst}: {digest}")
            parts.append("\n".join(lines))
        if history_context:
            parts.append(history_context)
        combined_context = "\n\n".join(parts)

        prompt = REASONING_PROMPT.format(
            question=question,
            data=json.dumps(normalized_data, ensure_ascii=False, default=str),
            evidence=json.dumps(
                [x.model_dump() for x in evidence],
                ensure_ascii=False,
                default=str,
            ),
            history_context=combined_context,
        )

        _llm_started = time.perf_counter()
        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": "你必须只返回合法 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        # 阶段 6：用量/耗时留给调用方打 run_log `llm` 事件（本层拿不到 run_id）。
        self.last_llm_usage = getattr(response, "usage", None)
        self.last_llm_duration_ms = (time.perf_counter() - _llm_started) * 1000

        content = response.choices[0].message.content
        # 空 completion / ```json 围栏 / 顶层数组，三种都在 parse_json_object 里
        # 统一变成 LLMOutputError（→ 502）。以前这里只有裸 json.loads：
        # 围栏直接 502（而且是在所有工具调用都已付费之后），数组会在下面的
        # payload.get(…) 上抛 AttributeError → 500。
        payload = parse_json_object(content, source="Reasoning")

        # ── 后处理：转换为模型可接受的格式 ──
        #
        # 整段必须包在 try 里。_parse_evidence 会 EvidenceItem(**…) 构造 pydantic
        # 模型，而以下两种**极常见**的模型输出会抛裸 ValidationError：
        #   - evidence 元素缺 id
        #   - timestamp 写成 epoch 毫秒（pydantic v2 不做 int→str 强制转换）
        # 裸 ValidationError 是 ValueError 的子类，会被 HTTP 层的 except ValueError
        # 兜成 400「你的请求有问题」—— 把上游模型的毛病甩锅给客户端。
        try:
            # T20：market_state / what_happened 缺失或为 null → 置空 + 写 data_caveats，
            # 不允许静默变成一份看似正常的报告（模型层已给默认值，这里是双保险）。
            data_caveats = _ensure_list(payload.get("data_caveats"))
            for key in ("market_state", "what_happened"):
                if not str(payload.get(key) or "").strip():
                    payload[key] = ""
                    data_caveats.append(f"模型未给出 {key}，已置空")
            payload["data_caveats"] = data_caveats

            # Ensure arrays are actually lists
            for key in ("why", "strong_areas", "what_changed", "what_matters", "risks"):
                payload[key] = _ensure_list(payload.get(key))

            # D3/D4：anomalies 最终由 build_response_from_state 用 detect_anomalies 填，
            # 模型给的条目不会进入最终报告；这里只把形状规范成 list[dict]——
            # 模型写 anomalies: "文本" / 123 之类的标量会把整份报告炸成 LLMOutputError
            # （list[dict[str, Any]] 校验不过），重演 T20 的"一个字段毁掉整份报告"。
            payload["anomalies"] = [a for a in _ensure_list(payload.get("anomalies")) if isinstance(a, dict)]

            # Process evidence with backward compatibility
            raw_evidence = payload.get("evidence", [])
            parsed_evidence = _parse_evidence(raw_evidence, evidence)
            payload["evidence"] = [ei.model_dump() for ei in parsed_evidence]

            # 阶段 4③/④：claim—evidence 映射 + **代码级**引用校验。
            # 合法 id 集合只来自真实证据账本。
            valid_ids = {item.id for item in evidence}
            body_violations = _validate_report_references(payload, valid_ids)
            claims, violations = _validate_claims(_parse_claims(payload.get("claims")), valid_ids)
            violations.extend(body_violations)
            payload["claims"] = [claim.model_dump() for claim in claims]
            payload["evidence_violations"] = violations
            if violations:
                # 引用了不存在的证据 id：报告必须降级，且证据可见（不能静默）。
                payload["confidence"] = "low"
                data_caveats.append(
                    f"有 {len(violations)} 条论断引用了不存在的证据 id，已按「无据」处理："
                    + "；".join(violations[:3])
                )
                payload["data_caveats"] = data_caveats
                logger.warning("Reasoning 报告存在 %d 条无效 evidence 引用: %s", len(violations), violations[:3])

            # 阶段 4（实体校验）：报告提到的实体必须有校验源。没有的记进
            # unverified_entities —— Critic 据此禁止"某公司不存在/未上市"一类断言
            # （无校验源时只能表达为"未验证"）。
            payload["unverified_entities"] = check_report_entities(report_text(payload), evidence)[0]

            # Ensure confidence is valid
            conf = payload.get("confidence")
            if conf not in ("high", "medium", "low"):
                # 默认 low 而不是 medium：缺失置信度时应表示"没有足够把握"，
                # 不是"中等把握"。medium 会让用户看到"数据高度一致"的错觉。
                payload["confidence"] = "low"

            # Ensure used_tools is a list
            payload["used_tools"] = _ensure_list(payload.get("used_tools"))

            return MarketIntelligence.model_validate(payload)
        except ValidationError as exc:
            # 后处理已尽力兜底，仍不符合 schema → 上游模型的问题，不是客户端入参问题
            raise LLMOutputError(f"Reasoning returned JSON that violates the schema: {exc}") from exc
        except (TypeError, AttributeError, KeyError) as exc:
            raise LLMOutputError(
                f"Reasoning returned JSON that could not be post-processed: {type(exc).__name__}: {exc}"
            ) from exc
