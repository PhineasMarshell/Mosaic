"""Orchestrator — LangGraph 图入口。

公开签名：question/domain/conversation_id → ResearchResponse。
"""

import asyncio
import logging
import time

from app.config import Settings
from app.graph import run_log
from app.graph.builder import build_graph
from app.graph.state import ResearchState
from app.models.research import MarketDomain
from app.models.response import ResearchResponse, build_response_from_state
from app.research.trading_calendar import requested_date_from_question, resolve_trading_date_semantics

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        # ── 懒构建 LangGraph 图 ────────────────────────
        self._graph = None  # CompiledGraph — 首次调用时构建

    def _ensure_graph(self):
        if self._graph is None:
            self._graph = build_graph(self.settings)
        return self._graph

    async def run(
        self,
        question: str,
        domain: MarketDomain | None = None,
        conversation_id: str | None = None,
    ) -> ResearchResponse:
        """运行研究调查。

        Args:
            question: 用户问题
            domain: 可选的强制市场域；None 表示让 Supervisor 自动判断
            conversation_id: 可选的对话会话 ID，用于注入历史上下文
        """
        question = question.strip()
        if not question:
            raise ValueError("question cannot be empty")

        # ── LangGraph 图调用 ────────────────────────────────
        run_id = run_log.new_run_id()
        requested_date, _ = requested_date_from_question(question)
        date_semantics = resolve_trading_date_semantics(question) if domain == "a_share" else None
        state = ResearchState(
            question=question,
            domain=str(domain) if domain else None,
            conversation_id=conversation_id,
            run_id=run_id,
            requested_date=requested_date,
            # This is a planning target only.  GateNode replaces as_of_date
            # with the date actually returned by evidence, or leaves it None.
            planned_as_of_date=date_semantics.as_of_date if date_semantics else None,
            market_closed=date_semantics.market_closed if date_semantics else None,
            date_reason=date_semantics.reason if date_semantics else None,
            # T23b：预算按"整次调查"起算——research_more 回环的第二轮 analyst
            # 读到的是同一个截止时刻，不会重新获得一整份 research_budget_seconds
            budget_deadline=time.monotonic() + self.settings.research_budget_seconds,
            started_at=time.monotonic(),
        )
        run_log.log_run_start(state)
        stage = "graph_build"

        def _terminal_snapshot(*, final_audit_status: str, delivery_reason: str, error_category: str,
                               exception_type: str, reason_codes: list[str]) -> dict:
            """Return a log-safe dict for both dict and ResearchState inputs.

            LangGraph may return the Pydantic state object directly.  Expanding
            it with ``{**state}`` raises a second TypeError and hides the
            original graph/Gateway failure, so this boundary always uses the
            model's public serialization API.
            """
            if hasattr(state, "model_dump"):
                snapshot = state.model_dump(mode="python", exclude_none=False)
            elif isinstance(state, dict):
                snapshot = dict(state)
            else:
                snapshot = {"run_id": run_id}
            snapshot.update(
                {
                    "run_id": snapshot.get("run_id") or run_id,
                    "final_audit_status": final_audit_status,
                    "delivery_reason": delivery_reason,
                    "error_category": error_category,
                    "exception_type": exception_type,
                    "stage": stage,
                    "reason_codes": list(dict.fromkeys(reason_codes)),
                    "errors": list(snapshot.get("errors") or []) + [error_category],
                }
            )
            started = snapshot.get("started_at")
            if started is not None:
                try:
                    snapshot["elapsed_ms"] = round(max(0.0, time.monotonic() - float(started)) * 1000, 1)
                except (TypeError, ValueError):
                    pass
            return snapshot

        def _record_failed_terminal(snapshot: dict, reason_codes: list[str]) -> None:
            # Logging must never replace the original exception.  The helper is
            # deliberately best-effort while preserving a stable finalize and
            # delivery event whenever the logger itself is healthy.
            try:
                run_log.log_finalize(
                    snapshot,
                    final_audit_status="error",
                    delivery_status="failed",
                    reason=str(snapshot.get("delivery_reason") or "research_failed"),
                    reason_codes=reason_codes,
                )
                run_log.log_delivery(snapshot, delivery_status="failed", persisted=False, sink="sync")
            except Exception:  # pragma: no cover - logging must not mask source errors
                logger.exception("Failed to record terminal research error")

        try:
            graph = self._ensure_graph()
            stage = "graph_invoke"
            result_state = await graph.ainvoke(
                state,
                config={"recursion_limit": self.settings.graph_recursion_limit},
            )

            # ── 从 state 组装 ResearchResponse（与 SSE 路径共用）────
            stage = "response_build"
            result = build_response_from_state(result_state, question=question, conversation_id=conversation_id)
        except asyncio.CancelledError:
            # 外层总预算通过 wait_for 取消这里；保留同一个 run_id，避免
            # timeout 的 delivery 日志脱离 run_start 链路。
            codes = ["timeout", "research_cancelled"]
            _record_failed_terminal(
                _terminal_snapshot(
                    final_audit_status="error",
                    delivery_reason="研究任务已取消",
                    error_category="timeout",
                    exception_type="CancelledError",
                    reason_codes=codes,
                ),
                codes,
            )
            raise
        except TimeoutError:
            # A stage timeout is a terminal research failure, not an exception
            # that callers should reinterpret as a successful/empty report.
            codes = ["timeout", "research_deadline_exhausted"]
            snapshot = _terminal_snapshot(
                final_audit_status="error",
                delivery_reason="研究超出总预算",
                error_category="timeout",
                exception_type="TimeoutError",
                reason_codes=codes,
            )
            snapshot["remaining_budget"] = 0.0
            _record_failed_terminal(snapshot, codes)
            return ResearchResponse(
                question=question,
                report=None,
                errors=["research deadline exhausted"],
                reason_codes=codes,
                final_audit_status="error",
                delivery_status="failed",
                delivery_reason="研究超出总预算",
                persisted=False,
                run_id=run_id,
                stage=stage,
                exception_type="TimeoutError",
                elapsed_ms=round((time.monotonic() - float(state.started_at or time.monotonic())) * 1000, 1),
                remaining_budget=0.0,
            )
        except Exception as exc:
            # 上游/图组装异常也必须留下终态失败事件，不能只返回 HTTP 错误。
            error_type = type(exc).__name__
            codes = ["graph_node_error" if stage == "graph_invoke" else "research_error"]
            if exc.__class__.__module__.startswith("app.gateway"):
                codes.append("gateway_error")
            _record_failed_terminal(
                _terminal_snapshot(
                    final_audit_status="error",
                    delivery_reason=error_type,
                    error_category=error_type,
                    exception_type=error_type,
                    reason_codes=codes,
                ),
                codes,
            )
            raise

        # 可信与否只看 delivery_status；errors 只是"运行有没有出错"。
        if result.delivery_status != "verified":
            logger.warning(
                "Research delivery_status=%s final_audit_status=%s errors=%d critique=%s",
                result.delivery_status,
                result.final_audit_status,
                len(result.errors),
                (result.critique or {}).get("reason", "") if isinstance(result.critique, dict) else "",
            )

        return result
