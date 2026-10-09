"""research_more 回环的缺口上下文与步骤修补（纯函数，无 IO / 无 LLM）。

背景：Critic 判 research_more 时只把控制权交回 Supervisor（回环边不携带 payload），
而 Supervisor._plan() 只读 conversation_id/domain/question —— 第二轮既不知道 Critic
指出的缺口，也不知道上一轮跑过哪些工具（route 被整体覆盖）。本模块渲染 planner 可见的
缺口上下文，并在代码层做两件确定性的事：

1. drop_repeat_steps：丢掉「同工具同参数已经调过」的步骤（**重复调用**判定，粗粒度是对的）；
2. append_gap_steps：把 Critic 点名、LLM 没覆盖、且在本次注册表文本里的缺口工具补进 plan 头部。

阶段 2 的核心区分（方案 §4 阶段 2）：

============================  ============================================
判定                            依据
============================  ============================================
"这个调用重复了吗"              operationId + 参数（``drop_repeat_steps``）
"这条缺口被满足了吗"            tool_key + status + partial + 数据量 + 参数覆盖
                               （``ExecutionCoverage`` / ``satisfied_keys``）
============================  ============================================

**工具执行过 ≠ 缺口已被满足。** error / partial / 空结果 / 参数不覆盖，都算未满足；
这正是"同一个缺口被反复判 research_more 却补不上"的根因。operationId 复用同一端点的
兄弟 registry key 互不替代——除非结果里带着 tool_key 且就是那一个 key。

``ToolResult.tool_key`` 由 analyst 从原计划写入（见 analysts/base.py）。老结果没有
该字段时一律按"不满足"处理（保守方向：宁可多补一次，不要假装已满足）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.models.research import ToolCallPlan
from app.gateway.arguments import canonicalize_tool_arguments, semantic_signature

#: 视为"拿到过调用结果"的状态 —— error 不算，允许回环轮重试失败的工具
_DONE_STATUSES = ("success", "partial")

#: 这些 tool_name 前缀的工具必须带 query，缺口补齐时用用户问题兜底
_QUERY_TOOL_PREFIXES = ("news_",)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ------------------------------------------------------------------ #
# 阶段 2：执行覆盖度                                                   #
# ------------------------------------------------------------------ #


@dataclass
class ExecutionCoverage:
    """一次工具执行相对某条缺口的覆盖情况（方案 §4 阶段 2 示例结构）。

    ``satisfies`` 是**代码判定**的结论，``reason`` 是可解释的理由——两者都会进入
    结构化运行日志，"为什么某个工具被视为满足 / 不满足某条缺口"必须可查。
    """

    tool_key: str | None
    tool_name: str
    arguments: dict[str, Any]
    status: str
    partial: bool
    datum_count: int
    note: str | None = None
    #: 判为满足时，本次执行满足掉的缺口能力标签；判为不满足时为空集合
    fulfills: set[str] = field(default_factory=set)
    #: 代码判定：这次执行是否构成"已拿到充分证据"
    satisfies: bool = False
    #: 不满足 / 满足的机器可读理由（already_covered_by_arguments / status_error / ...）
    reason: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "tool_key": self.tool_key,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "status": self.status,
            "partial": self.partial,
            "datum_count": self.datum_count,
            "satisfies": self.satisfies,
            "reason": self.reason,
        }


def _arguments_cover(executed: dict, required: dict) -> bool:
    """required 的每个键都被 executed 以相同值覆盖（``{}`` 视为总是被覆盖）。"""
    for key, value in (required or {}).items():
        if executed.get(key) != value:
            return False
    return True


def coverage_records(results: Any) -> list[ExecutionCoverage]:
    """state["results"] → 逐条执行覆盖度记录。

    **不做 operationId → registry key 的反查**：那是"粗粒度去重"，正是本阶段要
    消灭的判据。没有 tool_key 的执行照样进入记录，只是永远无法满足任何 key 缺口。
    """
    records: list[ExecutionCoverage] = []
    for r in results or []:
        status = str(_field(r, "status", "") or "")
        normalized = _field(r, "normalized", []) or []
        tool_name = str(_field(r, "tool", "") or "")
        args = dict(_field(r, "arguments", {}) or {})
        try:
            args = canonicalize_tool_arguments(tool_name, args)
        except Exception:
            pass
        records.append(
            ExecutionCoverage(
                tool_key=_field(r, "tool_key") or None,
                tool_name=tool_name,
                arguments=args,
                status=status,
                partial=bool(_field(r, "partial", False)),
                datum_count=len(normalized),
                note=_field(r, "note"),
            )
        )
    return records


def judge_coverage(
    record: ExecutionCoverage,
    required_tool_keys: list[str] | set[str],
    required_coverage: dict[str, Any] | None = None,
) -> ExecutionCoverage:
    """判定一次执行是否满足「某组 tool key + 覆盖条件」的缺口，并回填理由。

    规则（方案 §4 阶段 2 第 3 条，逐条对应）：

    - ``error``：从未满足，可重试 → ``status_error``
    - ``partial``：默认**不**满足高严重度缺口 → ``status_partial``
      （只有 issue 显式 ``allow_partial=true`` 才复用）
    - ``success`` 但 ``normalized=[]``：不满足 → ``no_data``
    - 同 tool key 但参数不覆盖 required coverage：不满足 → ``arguments_not_covering``
    - operationId 复用的兄弟 key：互不替代 → ``tool_key_mismatch``
    - 都过了 → ``satisfied``，并把 tool_key 记进 fulfills
    """
    required_coverage = required_coverage or {}
    keys = set(required_tool_keys or [])
    allow_partial = bool(required_coverage.get("allow_partial", False))
    min_datum_count = required_coverage.get("min_datum_count")

    # 同一条记录可能被反复拿去判定不同的 requirement 变体（见 satisfied_keys），
    # 先清空上一轮的结论，避免"上一变体满足过"留下过期的 fulfills 标签。
    record.satisfies = False
    record.fulfills = set()

    if record.status not in _DONE_STATUSES:
        record.satisfies = False
        record.reason = "status_error"
        return record

    if record.status == "partial" and not allow_partial:
        record.satisfies = False
        record.reason = "status_partial"
        return record

    if record.tool_key is None or record.tool_key not in keys:
        record.satisfies = False
        record.reason = "tool_key_mismatch"
        return record

    if record.datum_count == 0:
        record.satisfies = False
        record.reason = "no_data"
        return record

    if isinstance(min_datum_count, int) and record.datum_count < min_datum_count:
        record.satisfies = False
        record.reason = "insufficient_datum_count"
        return record

    required_arguments = required_coverage.get("arguments")
    if isinstance(required_arguments, dict) and not _arguments_cover(record.arguments, required_arguments):
        record.satisfies = False
        record.reason = "arguments_not_covering"
        return record

    record.satisfies = True
    record.reason = "satisfied"
    record.fulfills = {record.tool_key}
    return record


def _variant_satisfied(key: str, records: list[ExecutionCoverage], variant: dict[str, Any]) -> bool:
    """某一条参数要求（变体）是否已被**至少一次**执行满足。"""
    return any(judge_coverage(record, {key}, dict(variant)).satisfies for record in records)


def _with_key_policy(variant: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """把 **key 级** ``allow_partial`` 一票否决下压到单个参数变体上。

    ``allow_partial`` 的语义是 key 级的：**同 key 的任一 issue 声明
    ``allow_partial=false``，该 key 的任何变体都不得用 partial 结果满足**。
    变体只描述参数取值，若只按变体自己的 ``allow_partial`` 判定，就会出现

    - issue A：``date=07``、``allow_partial=false``
    - issue B：``date=08``、``allow_partial=true``

    时 ``date=08`` 的变体拿 partial 结果顶包，于是整条 key 被误判为已满足 ——
    A 的"禁止 partial"被 B 的参数变体绕过。这里统一收紧，禁就全禁。

    注意 ``min_datum_count`` **不**在此下压：它按"同参数变体取最大"的语义在
    ``coverage_requirements`` 里合并，跨变体取全局最大值会无端加严另一条 issue 的要求。
    """
    if not spec.get("allow_partial", True):
        return {**variant, "allow_partial": False}
    return dict(variant)


def _key_satisfied(key: str, records: list[ExecutionCoverage], spec: dict[str, Any]) -> bool:
    """该 key 的全部要求是否都被（可能是多次不同的）执行满足。

    - 有 ``argument_variants``：**每个变体都要有至少一条记录满足**。冲突参数因此
      不能被单次调用蒙混过关，但两天的数据分别到位时（多调用）可以判为满足。
      每个变体都先经 ``_with_key_policy`` 应用 key 级 ``allow_partial`` 禁令。
    - 没有变体（未声明参数要求）：按合并后的通用约束，任意一条记录满足即可。
    """
    variants = spec.get("argument_variants")
    if not variants:
        generic = {k: v for k, v in spec.items() if k != "argument_variants"}
        return any(judge_coverage(record, {key}, generic).satisfies for record in records)
    return all(
        _variant_satisfied(key, records, _with_key_policy(variant, spec)) for variant in variants
    )


def satisfied_keys(
    results: Any,
    requirements: dict[str, dict[str, Any]] | None = None,
) -> set[str]:
    """"已拿到充分证据"的 registry key 集合。

    与旧的 ``executed_keys`` 的区别就是本阶段的意义：
    - error → 不算（可重试）
    - partial → **不算**（除非该 key 的要求显式 allow_partial）
    - success 但 0 条 normalized → 不算
    - 没有 tool_key → 不算（不做 operationId 反查）

    Args:
        results: state["results"]
        requirements: ``tool_key → required_coverage`` 映射（来自 Critic 的
            ``AuditIssue.required_coverage``，见 ``coverage_requirements``）。
            **同一个 key 可以有不同 issue 提出不同要求**（例如"要 ≥20 条涨停明细"
            和"要覆盖 2026-10-08"），因此覆盖度必须逐条按 issue 判定；参数要求
            互相冲突时**每个变体都要被满足**，不能被最后一条覆盖。
            没声明要求的 key 走通用规则（status/partial/数据量 > 0）。
    """
    records = coverage_records(results)
    requirements = requirements or {}
    keys: set[str] = set()
    for key, spec in requirements.items():
        if _key_satisfied(key, records, spec):
            keys.add(key)
    for record in records:
        if not record.tool_key or record.tool_key in requirements:
            continue
        if judge_coverage(record, {record.tool_key}, None).satisfies:
            keys.add(record.tool_key)
    return keys


def _variant_signature(arguments: dict[str, Any]) -> str:
    """参数取值的规范化指纹（用于把"同一条参数要求"聚成同一个变体）。"""
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def coverage_requirements(critique: Any) -> dict[str, dict[str, Any]]:
    """Critique → ``{tool_key: required_coverage}``（**逐条 issue 保留要求**）。

    多个 issue 点名同一个 key 时：

    - ``min_datum_count``：取最大值（同一条参数要求内合并，语义不变）；
    - ``allow_partial``：只要有一条不允许，就不允许（一票否决，语义不变）；
    - ``argument_variants``：**每个不同的 ``arguments`` 取值各留一条**，形如
      ``{"arguments": {...}, "min_datum_count": int | None, "allow_partial": bool}``。

    为什么参数不能像旧实现那样 ``merged.update(args)`` 合并：那是"后者覆盖前者"。
    issue A 要 ``date=2026-10-07``、issue B 要 ``date=2026-10-08`` 时，合并结果只剩
    10-08，于是任意一条 10-08 的结果就把整条缺口判成已满足 —— A 的缺口凭空消失。
    改成保留变体后，**每个变体都必须被某次执行满足**；彼此冲突的参数（同一个键不同
    取值）无法由单次调用满足，因此缺口会保持未满足，直到两天的数据分别到位
    （Supervisor 可规划多个不同参数的调用，见 ``append_gap_steps``），或者耗尽预算后
    由最终审计安全阻断。

    没有声明 ``arguments`` 的 issue 记为 ``{}`` 变体（``{}`` 视为总被覆盖），
    这样它自己的 ``min_datum_count`` / ``allow_partial`` 要求不会被别的 issue 吞掉。
    """
    issues = _field(critique, "issues", []) or []
    requirements: dict[str, dict[str, Any]] = {}
    variants: dict[str, dict[str, dict[str, Any]]] = {}
    partial_flags: dict[str, bool] = {}

    for issue in issues:
        if str(_field(issue, "action", "")) != "research_more":
            continue
        coverage = _field(issue, "required_coverage", {}) or {}
        if not isinstance(coverage, dict):
            coverage = {}
        raw_args = coverage.get("arguments")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        min_count = coverage.get("min_datum_count")
        allow_partial = bool(coverage.get("allow_partial", False))

        for key in _field(issue, "required_tool_keys", []) or []:
            if not isinstance(key, str) or not key:
                continue
            target = requirements.setdefault(key, {})
            if isinstance(min_count, int):
                current = target.get("min_datum_count")
                target["min_datum_count"] = min_count if current is None else max(current, min_count)
            if "allow_partial" in coverage:
                flag = allow_partial
                partial_flags[key] = flag if key not in partial_flags else (partial_flags[key] and flag)

            by_signature = variants.setdefault(key, {})
            signature = _variant_signature(args)
            entry = by_signature.get(signature)
            if entry is None:
                by_signature[signature] = {
                    "arguments": args,
                    "min_datum_count": min_count if isinstance(min_count, int) else None,
                    "allow_partial": allow_partial,
                }
            else:
                if isinstance(min_count, int):
                    current = entry["min_datum_count"]
                    entry["min_datum_count"] = min_count if current is None else max(current, min_count)
                entry["allow_partial"] = entry["allow_partial"] and allow_partial

    for key, by_signature in variants.items():
        requirements[key]["argument_variants"] = list(by_signature.values())
    for key, flag in partial_flags.items():
        requirements[key]["allow_partial"] = flag
    return requirements


# ------------------------------------------------------------------ #
# 重复调用判定（粗粒度是对的）                                          #
# ------------------------------------------------------------------ #


def executed_index(results: Any) -> dict[str, list[dict]]:
    """state["results"] → {tool_name: [arguments, ...]}（只含 success/partial）。

    注意用途：**只用于"别再调一遍同样的东西"**（重复步骤剔除、给 planner 展示已执行
    清单）。它不参与"缺口是否被满足"的判定。
    """
    index: dict[str, list[dict]] = {}
    for r in results or []:
        if _field(r, "status") not in _DONE_STATUSES:
            continue
        name = _field(r, "tool", "")
        if not name:
            continue
        args = _field(r, "arguments", {}) or {}
        try:
            args = canonicalize_tool_arguments(name, dict(args))
        except Exception:
            args = dict(args)
        index.setdefault(name, []).append(dict(args))
    return index


def executed_keys(results: Any) -> set[str]:
    """**已删除**（阶段 6，方案 §4 灰度第 4 条）——保留符号只为让旧调用点立刻报错。

    旧实现按 operationId 反查规范条目，回答的是"跑过没有"；同一 operationId 被
    多个 registry key 复用时它会把兄弟 key 的缺口误判成"已满足"（阶段 2 修掉的
    真实缺陷）。证据充分性判据只有一个：``satisfied_keys(results, requirements)``。
    """
    raise NotImplementedError(
        "executed_keys() 已删除（阶段 6）：证据充分性请用 satisfied_keys(results, requirements)"
    )


def allowed_keys(registry: str) -> set[str]:
    """从 registry_text() 输出里抽出全部 key（行格式 `- {key}: {tool_name} [...]`）。"""
    keys: set[str] = set()
    for line in registry.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            keys.add(stripped[2:].split(":", 1)[0].strip())
    return keys


def render_registry_for_critic(registry: str, results: Any) -> str:
    """注册表文本（供 Critic 点名补充工具），**保留全部条目**并标注现有覆盖度。

    为什么不删掉"已执行"的行（阶段 2 的旧做法）：prompt 组装时 Critic 的
    ``required_coverage`` 还**不存在**（它就是这次要产出的东西），此时无法判断
    "success + 3 条数据"对"要 ≥20 条"的缺口是否够用。把这类工具从清单里删掉，
    Critic 就永远看不见它、也就永远点不到名——缺口永远补不上。
    因此这里改为**标注而非隐藏**：已拿到数据的工具带上数据量/状态，Critic 可自行
    判断覆盖是否足够；真的不满足时它仍然能点名，落库时再由
    ``classify_missing_tool_keys`` 按**逐条 issue 的要求**精确过滤。
    """
    satisfied = satisfied_keys(results)
    records_by_key: dict[str, ExecutionCoverage] = {}
    for record in coverage_records(results):
        if record.tool_key:
            records_by_key.setdefault(record.tool_key, record)

    lines: list[str] = []
    for line in registry.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("- ") and ":" in stripped):
            lines.append(line)
            continue
        key = stripped[2:].split(":", 1)[0].strip()
        record = records_by_key.get(key)
        if record is None:
            lines.append(line)
            continue
        note = (
            f"  [已调用：status={record.status}"
            f"{' / partial' if record.partial else ''} / 现有数据 {record.datum_count} 条"
        )
        if key in satisfied:
            note += "；若本条缺口的覆盖度要求更高，仍可点名重拉]"
        else:
            note += "；尚未满足缺口，可点名] "
        lines.append(f"{line}{note}")
    return "\n".join(lines) or "（无可补充的工具）"


def render_unexecuted_registry(registry: str, results: Any) -> str:
    """兼容别名：旧名字保留，内容语义见 ``render_registry_for_critic``。"""
    return render_registry_for_critic(registry, results)


def _args_subset(planned: dict, executed: dict) -> bool:
    """planned 的参数是否被 executed 覆盖（`{}` 视为总是被覆盖）。"""
    for k, v in planned.items():
        if k not in executed or executed[k] != v:
            return False
    return True


def is_repeat_step(step: ToolCallPlan, results: Any) -> bool:
    """该 step 是否为「同工具同参数已经调过、且拿到过结果」的重复。

    这里用 operationId 是**正确的**：它回答的是"要不要再调一次"，不是"证据够不够"。
    """
    from app.gateway.tool_registry import resolve_tool

    try:
        meta = resolve_tool(step.tool_key)
    except KeyError:
        return False
    try:
        planned = canonicalize_tool_arguments(meta.tool_name, dict(step.arguments or {}))
    except Exception:
        planned = dict(step.arguments or {})
    for args in executed_index(results).get(meta.tool_name, []):
        try:
            executed = canonicalize_tool_arguments(meta.tool_name, dict(args or {}))
        except Exception:
            executed = dict(args or {})
        if _args_subset(planned, executed):
            return True
    return False


def drop_repeat_steps(steps: list[ToolCallPlan], results: Any) -> tuple[list[ToolCallPlan], list[str]]:
    """丢掉重复步骤 → (保留的 steps, 被丢弃的 tool_key 列表)。"""
    kept: list[ToolCallPlan] = []
    dropped: list[str] = []
    for step in steps:
        try:
            meta = resolve_tool(step.tool_key)
            step.arguments = canonicalize_tool_arguments(meta.tool_name, dict(step.arguments or {}))
        except Exception:
            pass
        if is_repeat_step(step, results):
            dropped.append(step.tool_key)
        else:
            kept.append(step)
    return kept, dropped


# ------------------------------------------------------------------ #
# 缺口 key 分类（阶段 2 第 4 条）                                       #
# ------------------------------------------------------------------ #


@dataclass
class GapKeyDecision:
    """Critic 点名的缺口 key 去向——四类原因都必须可解释、可进最终响应。"""

    kept: list[str] = field(default_factory=list)
    #: 工具已经跑过且**确实满足**了这条缺口（再点一次是浪费）
    already_satisfied: list[str] = field(default_factory=list)
    #: key 不在本次注册表可见集合里（幻觉 / 串域）
    not_visible: list[str] = field(default_factory=list)
    #: 已保留但因 max_keys 预算被截断（还有缺口没补上，必须看得见）
    budget_truncated: list[str] = field(default_factory=list)
    #: 同一次响应里重复点名
    duplicated: list[str] = field(default_factory=list)
    #: 非法类型 / 空串（直接忽略，不进任何原因列表）
    invalid: list[str] = field(default_factory=list)
    #: 被丢弃的全部 key（保持输入顺序，便于回归测试）
    dropped: list[str] = field(default_factory=list)
    #: key → 丢弃原因（按首次判定记录），让 as_records 能保持输入顺序
    reasons: dict[str, str] = field(default_factory=dict)

    def reason_counts(self) -> dict[str, int]:
        return {
            "already_satisfied": len(self.already_satisfied),
            "not_visible": len(self.not_visible),
            "budget_truncated": len(self.budget_truncated),
            "duplicated": len(self.duplicated),
            "invalid": len(self.invalid),
        }

    def as_records(self) -> list[dict[str, str]]:
        """给 Critique.gap_key_decisions / 运行日志用的扁平记录（保持输入顺序）。"""
        return [{"key": key, "reason": self.reasons[key]} for key in self.dropped if key in self.reasons]


def classify_missing_tool_keys(
    raw: Any,
    allowed: set[str],
    satisfied: set[str],
    max_keys: int | None = None,
) -> GapKeyDecision:
    """把 Critic 给的缺口 key 分成 保留 / 已满足 / 不可见 / 预算截断 四类。

    ``satisfied`` 是"已拿到**充分**证据"的 key 集合（``satisfied_keys``），
    不是"跑过"的集合——跑过但 partial / 空数据的 key 仍会保留，等待重拉。
    """
    decision = GapKeyDecision()
    seen: set[str] = set()
    limit = max_keys if max_keys is not None and max_keys >= 0 else None

    for key in raw or []:
        if not isinstance(key, str) or not key:
            decision.invalid.append(str(key))
            continue
        if key in seen:
            decision.duplicated.append(key)
            decision.dropped.append(key)
            decision.reasons.setdefault(key, "duplicated")
            continue
        seen.add(key)

        if key not in allowed:
            decision.not_visible.append(key)
            decision.dropped.append(key)
            decision.reasons.setdefault(key, "not_visible")
            continue
        if key in satisfied:
            decision.already_satisfied.append(key)
            decision.dropped.append(key)
            decision.reasons.setdefault(key, "already_satisfied")
            continue
        if limit is not None and len(decision.kept) >= limit:
            decision.budget_truncated.append(key)
            decision.dropped.append(key)
            decision.reasons.setdefault(key, "budget_truncated")
            continue
        decision.kept.append(key)

    return decision


def sanitize_missing_tool_keys(raw: Any, allowed: set[str], executed: set[str]) -> tuple[list[str], list[str]]:
    """兼容包装：旧签名（allowed / executed）→ (合法 keys, 被丢弃的 keys)。

    ``executed`` 这个参数名现在语义是"已满足"，调用方（Supervisor 回环轮）
    应传 ``satisfied_keys(results)``；保留旧名只为不打断既有调用点与回归测试。
    """
    decision = classify_missing_tool_keys(raw, allowed=allowed, satisfied=executed)
    return decision.kept, decision.dropped


def gap_tool_keys(critique: Any, registry: str, results: Any) -> tuple[list[str], list[str]]:
    """Critic 的缺口 key ∩ 本次注册表可见 key − **按本条缺口要求**已满足的 key。

    覆盖度要求逐条来自 ``AuditIssue.required_coverage``：上一次"成功但只有 3 条"
    的 limit_up_pool，不会让"要 ≥20 条"的新缺口被判成已满足。
    """
    return sanitize_missing_tool_keys(
        _field(critique, "missing_tool_keys", []),
        allowed_keys(registry),
        satisfied_keys(results, coverage_requirements(critique)),
    )


def append_gap_steps(
    steps: list[ToolCallPlan],
    gap_keys: list[str],
    registry: str,
    max_steps: int,
    max_gap_steps: int,
    question: str = "",
    requirements: dict[str, dict[str, Any]] | None = None,
    results: Any = None,
) -> tuple[list[ToolCallPlan], list[str]]:
    """把缺口 key 以 high 优先级补进 steps **头部**（幂等）再截断 → (新 steps, 补入的 key)。

    参数冲突的执行策略（多调用语义）：同一个 key 若带有多个 ``argument_variants``
    （例如 issue A 要 ``date=2026-10-07``、issue B 要 ``date=2026-10-08``），
    单次调用无法同时满足，因此**为每个"尚未被满足的变体"各补一条独立步骤**，
    带上该变体自己的 ``arguments``。这样 Supervisor 的可执行计划里会真实出现
    "同一工具、不同参数"的多次调用，而不是靠合并参数假装覆盖。

    传入 ``results`` 时，已被历史执行满足的变体不再重复补入；若某个变体始终
    无法被满足，它会一直留在 ``gap_tool_keys`` 里，最终由 Critic 耗尽预算 →
    ``finalize_audit`` 安全阻断（不会静默当成正常报告交付）。

    不传 ``requirements`` 时保持旧行为：按 key 补一条空参数步骤。
    """
    from app.gateway.tool_registry import resolve_tool

    allowed = allowed_keys(registry)
    present = {s.tool_key for s in steps}
    records = coverage_records(results) if results is not None else []
    forced: list[ToolCallPlan] = []
    for key in gap_keys:
        if key not in allowed or len(forced) >= max_gap_steps:
            continue
        try:
            meta = resolve_tool(key)
        except KeyError:
            continue
        specs: list[dict[str, Any] | None]
        spec = (requirements or {}).get(key, {})
        variants = spec.get("argument_variants")
        if variants:
            # key 级 allow_partial 禁令同样下压到变体：被禁令挡下的变体必须继续补研究
            specs = [
                v for v in variants if not _variant_satisfied(key, records, _with_key_policy(v, spec))
            ]
        else:
            specs = [None]
        for variant in specs:
            if len(forced) >= max_gap_steps:
                break
            if variant is None:
                if key in present:
                    continue
                arguments: dict[str, Any] = {}
            else:
                arguments = dict(variant.get("arguments") or {})
                signature = _variant_signature(arguments)
                if any(
                    s.tool_key == key and _variant_signature(dict(s.arguments or {})) == signature
                    for s in forced
                ):
                    continue
            if meta.tool_name.startswith(_QUERY_TOOL_PREFIXES):
                arguments.setdefault("query", question)
            try:
                # Quote gap steps must use the canonical batch contract even
                # when Critic only names the logical key.
                if meta.tool_name == "get_market_quotes" and not arguments:
                    arguments = {"symbols": ["000300"]}
                arguments = canonicalize_tool_arguments(meta.tool_name, arguments)
            except Exception:
                continue
            signature = semantic_signature(key, arguments)
            if any(semantic_signature(s.tool_key, dict(s.arguments or {})) == signature for s in steps + forced):
                continue
            forced.append(
                ToolCallPlan(
                    tool_key=key,
                    arguments=arguments,
                    purpose="Critic 指出的证据缺口（代码级补齐）",
                    priority="high",
                )
            )
            present.add(key)
    merged = forced + list(steps)
    return merged[:max_steps], [s.tool_key for s in forced]


def render_gap_context(critique: Any, results: Any, max_steps: int) -> str:
    """渲染回环轮上下文块（塞进 PLANNER_PROMPT 的 {revision_context}）。"""
    if critique is None:
        return "（首轮，无缺口信息）"
    reason = _field(critique, "reason", "") or "（未给出）"
    missing_points = _field(critique, "missing_points", []) or []
    missing_keys = _field(critique, "missing_tool_keys", []) or []
    lines = [
        "Critic 判定: research_more",
        f"Critic 理由: {reason}",
        "Critic 指出的证据缺口（自然语言）:",
    ]
    lines.extend([f"  - {p}" for p in missing_points] or ["  - （无）"])
    lines.append("Critic 建议补充的工具 key（必须全部列入 steps）:")
    lines.extend([f"  - {k}" for k in missing_keys] or ["  - （无）"])
    lines.append("已执行且已拿到数据的工具（不要重复规划这些调用）:")
    index = executed_index(results)
    if index:
        for name, arg_list in sorted(index.items()):
            rendered = "; ".join(str(a) for a in arg_list[:3]) or "{}"
            lines.append(f"  - {name} 参数: {rendered}")
    else:
        lines.append("  - （无）")
    lines.append("Critic 对参数/数据量的逐条要求（**每一条都必须被满足**）:")
    records = coverage_records(results)
    unresolved: list[str] = []
    for key, spec in coverage_requirements(critique).items():
        for variant in spec.get("argument_variants") or []:
            effective = _with_key_policy(variant, spec)
            if _variant_satisfied(key, records, effective):
                continue
            detail = f"arguments={dict(variant.get('arguments') or {})}"
            count = variant.get("min_datum_count")
            if isinstance(count, int):
                detail += f", 至少 {count} 条"
            if effective.get("allow_partial"):
                detail += "（允许 partial）"
            unresolved.append(f"  - {key}: {detail}")
    lines.extend(unresolved or ["  - （无）"])
    if len(unresolved) > 1:
        lines.append("注意：同一 key 出现多条且参数互相冲突时（例如不同 date），单次调用无法同时满足；")
        lines.append("请为每个不同的参数各规划一次调用，不要只规划其中一条。")
    lines.append("注意：上面「已执行」只代表调用发生过。若该工具返回 partial / 空列表，")
    lines.append("它并没有真正满足 Critic 的缺口，仍然需要被重新规划并补齐。")
    lines.append(f"本轮最多规划 {max_steps} 个步骤。")
    return "\n".join(lines)
