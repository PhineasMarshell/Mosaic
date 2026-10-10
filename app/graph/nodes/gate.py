"""EvidenceGate 节点 -- 代码级证据质量硬检查。

包装 evidence_gate.run_evidence_gate() 为 LangGraph 节点。
不依赖 LLM，直接根据 ToolResult 的状态判断证据基本可用性。
如果失败写进 errors，不炸图。
"""

import re

from app.agent.evidence_gate import EvidenceGateResult, run_evidence_gate
from app.config import Settings
from app.models.market import ToolResult
from app.research.trading_calendar import normalize_date


def _observed_date_is_eligible(quality: dict) -> bool:
    if quality.get("valid"):
        return True
    reasons = set(quality.get("reason_codes") or [])
    return "stale_date" in reasons and reasons <= {
        "stale_date", "unknown_source", "unknown_tool_key", "request_scoped_instrument",
    }


class GateNode:
    """证据门控节点：ToolResult -> EvidenceGateResult。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def __call__(self, state):
        """运行证据门控，返回要写入 state 的字段字典。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)
            results = state.get("results", [])
            # Convert plain dicts back to ToolResult if needed
            tool_results: list[ToolResult] = [ToolResult(**r) if isinstance(r, dict) else r for r in results]
            requested_date = state.get("planned_as_of_date") or state.get("requested_date")
            if not requested_date:
                match = re.search(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})", str(state.get("question", "")))
                requested_date = match.group(1).replace("/", "-") if match else None
            gate: EvidenceGateResult = run_evidence_gate(
                tool_results, requested_date=requested_date, event_date=state.get("requested_date"),
            )
            observed_dates = {
                observed
                for quality in gate.evidence_quality
                if quality.get("date_scope") == "market"
                if _observed_date_is_eligible(quality)
                if (observed := normalize_date(quality.get("as_of_date")))
            }
            # Preserve a real stale observation date for disclosure; the gate
            # and Critic still compare it against the planned trading day.
            # Mixed observation dates remain unknown at the run level.
            as_of_date = next(iter(observed_dates)) if len(observed_dates) == 1 else None
            # The gate runs before Reasoning, but the analyst evidence ledger is
            # already available. Attach durable IDs so Critic can trace each
            # quality limitation back to the exact evidence item.
            ledger = state.get("evidence", []) or []
            for quality in gate.evidence_quality:
                tool_key = quality.get("tool_key")
                operation_id = quality.get("operation_id")
                def value(item, key):
                    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)
                candidates = [
                    item
                    for item in ledger
                    if (not operation_id or value(item, "operation_id") == operation_id or value(item, "source_tool") == operation_id)
                    and (value(item, "tool_key") == tool_key or not value(item, "tool_key") or tool_key == value(item, "source_tool"))
                ]
                metric = quality.get("metric")
                instrument = str(quality.get("instrument") or "").upper()
                instrument_codes = set(re.findall(r"\d{6}", instrument))
                matching = [
                    item
                    for item in candidates
                    if (not metric or value(item, "metric") == metric)
                    and (quality.get("datum_value") is None or value(item, "value") == quality.get("datum_value"))
                    and (
                        not instrument
                        or instrument == "UNKNOWN"
                        or not value(item, "instrument")
                        or bool(instrument_codes.intersection(re.findall(r"\d{6}", str(value(item, "instrument") or ""))))
                    )
                ]
                # Aggregate/error rows have no datum metric and must not be
                # linked to every real item from the same tool call.
                selected = matching if metric else []
                quality["evidence_ids"] = [str(value(item, "id")) for item in selected if value(item, "id")]
                if quality["evidence_ids"]:
                    quality["evidence_id"] = quality["evidence_ids"][0]
            return {
                "gate": gate,
                "as_of_date": as_of_date,
            }
        except Exception as exc:
            # 门控失败降级：继续流程（report 会标注数据不足）
            return {"errors": [f"Gate failed: {exc}"]}
