"""finalize_audit 节点 — 决定"这次运行到底交付什么"（阶段 5 保守版）。

方案 §4 阶段 5 的第 3 条明确允许先做一个更保守的第一版：**只要耗尽后 verdict 非
`pass`，一律 `blocked`，不返回正文报告**。自动"安全删句"降级（`degraded`）需要
确定性摘要生成，留作后续迭代——把 `degraded` 的判定位留在表里，但不自动启用。

四种终态（``delivery_status``）：

=================  ====================================  ===============
值                 条件                                   持久化
=================  ====================================  ===============
``verified``       Critic=pass                             是
``degraded``       预留：耗尽但可产出仅含已证实事实的报告   否（本版不产生）
``blocked``        耗尽且 verdict 非 pass，无法安全作答   否
``failed``         节点 / 上游 / 审计器自身错误            否
=================  ====================================  ===============

**`errors == []` 不再等价于可信**：errors 只表达运行失败。一个 ``verdict=revise``
且 errors 为空的运行，最终状态是 ``blocked`` 而不是正常完成。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from app.graph import run_log

logger = logging.getLogger(__name__)

#: 最终审计状态（写进 ResearchState / ResearchResponse）
FinalAuditStatus = Literal["pass", "revise_exhausted", "research_exhausted", "error"]
#: 交付状态（调用方判断"能否当作可信结论展示"的唯一依据）
DeliveryStatus = Literal["verified", "degraded", "blocked", "failed"]

#: verdict → 耗尽后的审计状态
_VERDICT_TO_AUDIT_STATUS = {
    "pass": "pass",
    "revise": "revise_exhausted",
    "research_more": "research_exhausted",
    "error": "error",
}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_delivery(
    critique: Any,
    errors: list[str] | None = None,
    *,
    blocked_reason: str | None = None,
    gap_key_decisions: list | None = None,
) -> tuple[FinalAuditStatus, DeliveryStatus, str]:
    """（纯函数）由 Critique + errors 推导终态。

    顺序刻意是"先看运行是否失败，再看审计是否通过"：
    审计器自己挂了（``verdict=error``）是 ``failed``；审计器说"没通过"是 ``blocked``。
    """
    verdict = str(_field(critique, "verdict", "") or "").strip().lower()
    errors = list(errors or [])
    reason = str(_field(critique, "reason", "") or "")

    def explain(base: str) -> str:
        details: list[str] = []
        if blocked_reason:
            details.append(f"阻断原因={blocked_reason}")
        issues = unresolved_issue_summary(critique)
        if issues:
            details.append("未解决issue=" + "; ".join(issues[:5]))
        decisions = gap_key_decisions or _field(critique, "gap_key_decisions", []) or []
        if decisions:
            rendered = ", ".join(
                f"{_field(d, 'key', '?')}:{_field(d, 'reason', _field(d, 'decision', '?'))}"
                for d in decisions[:10]
            )
            details.append("gap=" + rendered)
        return base + ("；" + "；".join(details) if details else "")

    if verdict == "error":
        return "error", "failed", explain(reason or "Critic 审计失败，未产生可信结论")

    if not verdict:
        # 没有 critique（理论上 critic 节点总会写）——不猜、不当 pass。
        return "error", "failed", explain(reason or "本次运行没有产生审计结论")

    if verdict == "pass":
        return "pass", "verified", explain(reason or "审计通过")

    # 非 pass：保守阻断。方案 §4 阶段 5 第 3 条 —— 本版不自动生成降级报告。
    audit_status = _VERDICT_TO_AUDIT_STATUS.get(verdict, "error")
    return audit_status, "blocked", explain(reason or f"审计未通过（verdict={verdict}），不作为可信结论交付")


def unresolved_issue_summary(critique: Any) -> list[str]:
    """把未解决的 issue 压成可展示的短句（进日志与阻断原因）。"""
    issues = _field(critique, "issues", []) or []
    out: list[str] = []
    for issue in issues:
        parts = [str(_field(issue, "kind", "?"))]
        claim = str(_field(issue, "claim", "") or "")
        if claim:
            parts.append(claim[:80])
        parts.append(f"action={_field(issue, 'action', '?')}")
        out.append(" | ".join(parts))
    return out


class FinalizeAuditNode:
    """终态节点：写 ``final_audit_status`` / ``delivery_status``，不产生新数据。"""

    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, state):
        try:
            if hasattr(state, "model_dump"):
                s = state.model_dump(exclude_none=False)
            else:
                s = state

            critique = s.get("critique")
            errors = list(s.get("errors") or [])
            remaining_budget = None
            if s.get("budget_deadline") is not None:
                try:
                    remaining_budget = float(s["budget_deadline"]) - time.monotonic()
                except (TypeError, ValueError):
                    remaining_budget = None
            audit_status, delivery_status, reason = resolve_delivery(
                critique,
                errors,
                blocked_reason=s.get("blocked_reason"),
                gap_key_decisions=s.get("gap_key_decisions"),
            )

            unresolved = unresolved_issue_summary(critique)
            gap_decisions = s.get("gap_key_decisions") or _field(critique, "gap_key_decisions", []) or []
            run_log.log_finalize(
                s,
                final_audit_status=audit_status,
                delivery_status=delivery_status,
                reason=reason,
                unresolved_issues=unresolved,
                blocked_reason=s.get("blocked_reason"),
                research_round_count=int(s.get("research_round_count", 0) or 0),
                rewrite_count=int(s.get("rewrite_count", 0) or 0),
                remaining_budget=remaining_budget,
                gap_key_decisions=gap_decisions,
                executable_gap_steps=s.get("executable_gap_steps"),
            )
            if delivery_status != "verified":
                logger.warning(
                    "finalize_audit: delivery_status=%s final_audit_status=%s（不持久化，errors=%d）",
                    delivery_status,
                    audit_status,
                    len(errors),
                )

            out: dict[str, Any] = {
                "final_audit_status": audit_status,
                "delivery_status": delivery_status,
                "delivery_reason": reason,
                "blocked_reason": s.get("blocked_reason"),
                "remaining_budget": remaining_budget,
                "unresolved_issues": unresolved,
            }
            # blocked / failed 时**清空正文报告**：未经审计的报告不允许作为结论返回。
            # 失败原因留在 critique（reviewer 可读）与 final_audit_status 里。
            if delivery_status != "verified" and s.get("report") is not None:
                out["report"] = None
            return out
        except Exception as exc:  # noqa: BLE001 — 终态节点不能把图炸掉
            logger.exception("finalize_audit failed")
            return {
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": f"finalize audit failed: {exc}",
                "report": None,
                "errors": [f"finalize audit failed: {exc}"],
            }
