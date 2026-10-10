"""Mosaic FastAPI — API + Web UI 入口。"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.agent.orchestrator import Orchestrator
from app.agent.persistence import persist_research
from app.config import get_settings
from app.errors import LLMOutputError, UpstreamTimeoutError
from app.graph import run_log
from app.logging_config import setup_logging
from app.models.response import build_response_from_state

setup_logging(level="INFO")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: F841 — FastAPI passes app instance
    """Application startup health check + brief scheduler."""
    global _settings, _orchestrator

    # 初始化 settings
    _settings = get_settings()
    missing = []
    if not _settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if _settings.market_gateway_mode == "http" and not _settings.market_gateway_api_key:
        missing.append("MARKET_GATEWAY_API_KEY (HTTP mode)")
    if missing:
        logger.warning("Missing configuration: %s", ", ".join(missing))
    else:
        logger.info("Configuration OK: gateway=%s model=%s", _settings.market_gateway_mode, _settings.openai_model)

    # 懒初始化 orchestrator
    _orchestrator = Orchestrator(_settings)

    # B3：mcp 模式启动自检（默认 warn——失败不阻塞启动，但 ERROR 日志 + /health 标记）
    from app.gateway.selfcheck import startup_gateway_selfcheck

    await startup_gateway_selfcheck(_settings)

    # Start brief scheduler
    from app.scheduler.briefs import start_brief_scheduler

    start_brief_scheduler()
    logger.info("Brief scheduler started")

    yield

    # Shutdown: stop scheduler
    from app.scheduler.briefs import stop_brief_scheduler

    stop_brief_scheduler()
    logger.info("Brief scheduler stopped")


app = FastAPI(
    title="Mosaic",
    version="0.1.1",
    description="Market Intelligence Agent — See the market, not just the data.",
    lifespan=lifespan,
)

# 懒加载：避免模块加载时初始化崩溃
_settings: dict | None = None
_orchestrator: object | None = None
_settings_lock = __import__("threading").Lock()

from app.memory.storage import get_memory

def _get_settings():
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = get_settings()
    return _settings


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        with _settings_lock:
            if _orchestrator is None:
                _orchestrator = Orchestrator(_get_settings())
    return _orchestrator


INDEX = Path(__file__).parent / "web" / "index.html"


@app.get("/health")
async def health():
    from app.net_env import proxy_env_warnings
    from app.scheduler.briefs import is_running as scheduler_running

    _s = _get_settings()
    from app.gateway.selfcheck import channel_state

    return {
        "status": "ok",
        "service": "mosaic",
        "gateway_mode": _s.market_gateway_mode,
        #: B3：mcp 模式通道自检结果（ok / mcp_unavailable / not_checked / not_applicable）
        "gateway_channel": channel_state(),
        "model": _s.openai_model,
        "max_tool_calls": _s.max_tool_calls,
        "research_budget_seconds": _s.research_budget_seconds,
        "graph_recursion_limit": _s.graph_recursion_limit,
        "sqlite_busy_timeout_ms": _s.sqlite_busy_timeout_ms,
        "persistence_max_retries": _s.persistence_max_retries,
        "persistence_retry_backoff_ms": _s.persistence_retry_backoff_ms,
        "brief_scheduler": "running" if scheduler_running() else "stopped",
        #: 非空 = 本进程启动时归一化过 NO_PROXY（否则 httpx 连 client 都构造不出来）
        "proxy_env_warnings": proxy_env_warnings(),
    }


@app.get("/api/brief/morning")
async def morning_brief_endpoint():
    """触发晨间简报生成。"""
    try:
        from app.scheduler.briefs import generate_morning_brief

        memory = get_memory()
        brief = await generate_morning_brief(settings=_get_settings(), memory=memory)
        memory.save_daily_state(data={"type": "morning_brief", **brief})
        return JSONResponse(content=brief)
    except Exception as exc:
        logger.exception("Morning brief generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/brief/evening")
async def evening_brief_endpoint():
    """触发晚间简报生成。"""
    try:
        from app.scheduler.briefs import generate_evening_brief

        memory = get_memory()
        brief = await generate_evening_brief(settings=_get_settings(), memory=memory)
        memory.save_daily_state(data={"type": "evening_brief", **brief})
        return JSONResponse(content=brief)
    except Exception as exc:
        logger.exception("Evening brief generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _parse_ask_payload(request: dict, endpoint: str) -> tuple[str, str | None, str | None]:
    """校验 ``/api/ask`` 与 ``/api/ask/stream`` 的请求体。

    返回 ``(question, domain, conversation_id)``，不合法就抛 400。

    两个端点以前各自复制了一份校验逻辑，而且**拒绝时一行日志都不打** ——
    于是访问日志里只剩裸的 ``400 Bad Request``，既看不出是空 question 还是
    非法 domain，也无从判断请求是谁发的。所有拒绝路径现在都留 WARNING。
    """
    raw_question = request.get("question")
    # 非字符串（比如前端误传了 DOM 节点，JSON 化后变成 {}）以前会走到
    # ``.strip()`` 抛 AttributeError → 500；这里统一归为 400。
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    if not question:
        logger.warning(
            "%s rejected: empty or non-string question (type=%s, keys=%s)",
            endpoint,
            type(raw_question).__name__,
            sorted(request.keys()),
        )
        raise HTTPException(status_code=400, detail="question cannot be empty")

    domain = request.get("domain") or None  # optional explicit domain override
    conversation_id = request.get("conversation_id") or None  # optional conversation session

    if domain is not None:
        from app.gateway.tool_registry import get_enabled_domains

        supported_domains = get_enabled_domains()
        if domain not in supported_domains:
            logger.warning(
                "%s rejected: unsupported domain=%s (supported=%s)",
                endpoint,
                run_log.redact_text(domain),
                supported_domains,
            )
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported domain: {domain}. Supported: {supported_domains}",
            )

    return question, domain, conversation_id


@app.post("/api/ask")
async def ask(request: dict):
    """Ask endpoint — accepts dict to match frontend payload."""
    question, domain, conversation_id = _parse_ask_payload(request, "POST /api/ask")
    settings = _get_settings()
    request_run_id = run_log.new_run_id()
    orchestrator_ready = False

    try:
        logger.info(
            "Received question=%s (len=%d) domain=%s conv_id=%s",
            run_log.redact_text(question),
            len(question),
            domain,
            run_log.redact_text(conversation_id),
        )
        # 同步端点以前没有任何总超时：调查可以一直跑到把每个工具的
        # 30s 超时逐个耗尽（max_tool_calls=12 → 最坏几分钟），浏览器只能干等。
        orchestrator = _get_orchestrator()
        orchestrator_ready = True
        result = await asyncio.wait_for(
            orchestrator.run(question, domain=domain, conversation_id=conversation_id),
            timeout=settings.research_budget_seconds,
        )

        # 阶段 5：先看审计终态，再看有没有报告。blocked 不是运行失败（errors 可以是
        # 空的），但同样不能当作可信结论交付，因此不落库、不返回 200 + 正文报告。
        delivery = result.delivery_status or "failed"

        if result.report is None and delivery == "blocked":
            logger.warning(
                "Research blocked for %s (final_audit_status=%s)",
                run_log.redact_text(question),
                result.final_audit_status,
            )
            run_log.log_delivery(
                {"run_id": result.run_id, "final_audit_status": result.final_audit_status,
                 "delivery_reason": result.delivery_reason, "critique": result.critique,
                 "unresolved_issues": result.unresolved_issues},
                delivery_status="blocked", persisted=False, sink="sync"
            )
            return JSONResponse(
                status_code=200,
                content={
                    "question": question,
                    "report": None,
                    "conversation_id": conversation_id,
                    "critique": result.critique,
                    "unresolved_issues": result.unresolved_issues,
                    "errors": result.errors,
                    "delivery_status": "blocked",
                    "final_audit_status": result.final_audit_status,
                    "delivery_reason": result.delivery_reason
                    or "研究未通过证据审计，未返回可信结论，请重试或缩小问题范围",
                    "persisted": False,
                    "retryable": True,
                },
            )

        if result.report is None:
            # 推理环节失败：给出明确的失败原因，而不是 200 + 一份空报告。
            # detail 必须是字符串 —— 前端 askSync 直接把它塞进 Error.message。
            logger.error("Research produced no report for %s (error_count=%d)", run_log.redact_text(question), len(result.errors))
            run_log.log_delivery(
                {"run_id": result.run_id, "final_audit_status": result.final_audit_status,
                 "delivery_reason": result.delivery_reason, "critique": result.critique,
                 "unresolved_issues": result.unresolved_issues},
                delivery_status=str(delivery), persisted=False, sink="sync"
            )
            code = "no_report" if "reasoning produced no report" in result.errors else "failed"
            result.persisted = False
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "研究未能产出报告（推理环节失败），请重试",
                    "code": code,
                    "errors": result.errors,
                    "delivery_status": delivery,
                    "final_audit_status": result.final_audit_status,
                    "delivery_reason": result.delivery_reason,
                    "critique": result.critique,
                    "unresolved_issues": result.unresolved_issues,
                    "persisted": False,
                },
            )

        # T3：同步与 SSE 共用同一持久化逻辑，避免双路径漂移（SSE 曾读错字段层级，
        # 把空摘要/空状态写入库）。阶段 5 起内部按 delivery_status 门控。
        persisted = await persist_research(result, question, conversation_id)
        result.persisted = persisted
        #: run_id 只在内部串联日志，不出现在对外 JSON 响应里
        data = {k: v for k, v in result.model_dump().items() if k != "run_id"}
        run_log.log_delivery(
            {"run_id": result.run_id, "final_audit_status": result.final_audit_status,
             "delivery_reason": result.delivery_reason, "critique": result.critique,
             "unresolved_issues": result.unresolved_issues},
            delivery_status=str(delivery), persisted=persisted, sink="sync"
        )

        return JSONResponse(content=data)
    except TimeoutError as exc:
        logger.warning("Research exceeded %ds budget: %s", settings.research_budget_seconds, run_log.redact_text(question))
        return JSONResponse(
            status_code=504,
            content={
                "detail": f"Research timed out after {settings.research_budget_seconds}s",
                "code": "timeout",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "研究超出总预算",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
    except LLMOutputError as exc:
        # 必须排在 ValueError 之前 —— LLMOutputError 是 ValueError 的子类
        logger.error("Upstream LLM output unusable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=502,
            content={
                "detail": "上游模型返回了无法解析的内容，请重试或更换模型",
                "code": "upstream",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "上游模型输出不可解析",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
    except ValueError as exc:
        logger.warning("Invalid input: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if not orchestrator_ready:
            fallback = {
                "question": question,
                "run_id": request_run_id,
                "final_audit_status": "error",
                "delivery_reason": "研究初始化失败",
            }
            run_log.log_run_start(fallback)
            run_log.log_delivery(fallback, delivery_status="failed", persisted=False, sink="sync")
        logger.error("Internal error during research: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc),
                "code": "internal",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "研究流程发生内部异常",
                "unresolved_issues": [],
                "persisted": False,
            },
        )


#: 节点名 → (progress step, 默认文案)
_NODE_PROGRESS = {
    "supervisor": ("planning", "理解问题并生成研究计划"),
    "kernel": ("tool_call", "整包调查执行中…"),
    "technical": ("tool_call", "技术面分析员采集中"),
    "fundamental": ("tool_call", "基本面分析员采集中"),
    "moneyflow": ("tool_call", "资金面分析员采集中"),
    "news": ("tool_call", "新闻事件分析员采集中"),
    "sentiment": ("tool_call", "舆情分析员采集中"),
    "gate": ("evaluating", "证据质量检查"),
    "reasoning": ("reasoning", "正在生成结构化市场情报…"),
    "critic": ("critic", "结论-证据审计"),
}


async def _stream_research(question: str, domain: str | None, conversation_id: str | None = None):
    """SSE event generator — drives LangGraph astream with per-node progress events.

    Uses graph.astream(stream_mode=["updates","values"]) in a background pump task,
    yielding per-node progress from "updates" and capturing the final state from "values".
    Heartbeat events keep the connection alive during long-running nodes.
    """
    settings = _get_settings()
    budget = settings.research_budget_seconds
    heartbeat = max(1, settings.stream_heartbeat_seconds)

    def json_event(event: str, data: dict):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    initial_state = {
        "question": question,
        "domain": domain,
        "conversation_id": conversation_id,
        # 阶段 0：一次调查一个 run_id，跨节点落到每条结构化运行摘要日志
        "run_id": run_log.new_run_id(),
        # T23b：SSE 路径同样把整次调查的预算截止时刻写进 state（与 Orchestrator.run 对齐）
        "budget_deadline": time.monotonic() + budget,
    }
    run_log.log_run_start(initial_state)

    def _terminal_state(*, reason: str, audit: str = "error") -> dict:
        return {
            **initial_state,
            "final_audit_status": audit,
            "delivery_reason": reason,
        }

    # The endpoint preflights graph construction, but retain a run-correlated
    # SSE error if lazy construction fails or changes between requests.
    try:
        orchestrator = _get_orchestrator()
        graph = orchestrator._ensure_graph()
    except Exception as exc:
        logger.error("Stream graph initialization failed for: %s (%s)", run_log.redact_text(question), type(exc).__name__)
        run_log.log_delivery(
            _terminal_state(reason="研究流程发生内部异常"),
            delivery_status="failed", persisted=False, sink="sse",
        )
        yield json_event("progress", {"step": "error", "message": "研究初始化失败，请重试"})
        yield json_event(
            "result",
            {
                "error": str(exc),
                "code": "internal",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "研究流程发生内部异常",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
        return

    queue: asyncio.Queue = asyncio.Queue()

    async def _pump():
        try:
            async for mode, payload in graph.astream(
                initial_state,
                stream_mode=["updates", "values"],
                config={"recursion_limit": settings.graph_recursion_limit},
            ):
                await queue.put((mode, payload))
            await queue.put(("__done__", None))
        except Exception as exc:
            await queue.put(("__error__", exc))

    task = asyncio.create_task(_pump())
    final_state: dict = {}
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + budget

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                task.cancel()
                raise UpstreamTimeoutError(f"Research exceeded the {budget}s budget")
            try:
                mode, payload = await asyncio.wait_for(queue.get(), timeout=min(heartbeat, remaining))
            except TimeoutError:
                elapsed = int(loop.time() - started_at)
                yield json_event(
                    "progress",
                    {"step": "working", "node": None, "message": f"仍在调查中…（{elapsed}s / 预算 {budget}s）"},
                )
                continue

            if mode == "__done__":
                break
            if mode == "__error__":
                raise payload
            if mode == "values":
                final_state = payload
            elif mode == "updates":
                for node_name in payload.keys():
                    step, message = _NODE_PROGRESS.get(node_name, (node_name, node_name))
                    yield json_event("progress", {"step": step, "node": node_name, "message": message})

        # ── 组装结果（与 orchestrator.run 共用同一构造器）──
        result = build_response_from_state(final_state, question=question, conversation_id=conversation_id)

        # ── 阶段 5：交付门控 ────────────────────────────────
        # 只有 verified / degraded 才允许出现"调查完成"；blocked / failed 必须发
        # 对应状态，绝不伪装成一次正常完成的研究。
        delivery = result.delivery_status or "failed"
        # delivery 日志必须带上**本次图运行真实**的 run_id，与 run_start / plan /
        # executions / critic / route / finalize 对得上；绝不另生成一个 id。
        # The response builder is the source of truth for terminal fields.  In
        # particular, the pass fast path derives ``final_audit_status`` from
        # Critic and therefore the raw graph state may still contain ``None``.
        # Copy the resolved fields back before emitting delivery telemetry so
        # SSE and sync logs describe the same terminal result.
        final_state = {
            **final_state,
            "run_id": result.run_id or final_state.get("run_id"),
            "final_audit_status": result.final_audit_status,
            "delivery_status": result.delivery_status,
            "delivery_reason": result.delivery_reason,
            "unresolved_issues": result.unresolved_issues,
        }
        if result.report is None:
            if delivery == "blocked":
                # 审计没通过：不是运行失败（errors 可以是空的），但也不能当作结论交付。
                logger.warning(
                    "Stream research blocked for %s (final_audit_status=%s)",
                    run_log.redact_text(question),
                    result.final_audit_status,
                )
                yield json_event(
                    "progress",
                    {
                        "step": "blocked",
                        "node": None,
                        "message": "研究未通过证据审计，本次结果不可作为可信结论，请重试或缩小问题范围",
                    },
                )
                yield json_event(
                    "result",
                    {
                        "error": "研究未通过证据审计，未返回可信结论",
                        "code": "blocked",
                        "question": question,
                        "delivery_status": "blocked",
                        "final_audit_status": result.final_audit_status,
                        "delivery_reason": result.delivery_reason,
                        "critique": result.critique,
                        "unresolved_issues": result.unresolved_issues,
                        "errors": result.errors,
                        "persisted": False,
                    },
                )
                run_log.log_delivery(final_state, delivery_status="blocked", persisted=False, sink="sse")
                return
            # 推理环节失败：与超时/上游错误保持一致，发一个带 error 的 result 事件，
            # 既不落库，也不让前端拿空报告去渲染。
            logger.error("Stream research produced no report for %s (error_count=%d)", run_log.redact_text(question), len(result.errors))
            yield json_event("progress", {"step": "error", "message": "研究未能产出报告（推理环节失败），请重试"})
            code = "no_report" if "reasoning produced no report" in result.errors else "failed"
            result.persisted = False
            yield json_event(
                "result",
                {
                    "error": "研究未能产出报告（推理环节失败），请重试",
                    "code": code,
                    "question": question,
                    "errors": result.errors,
                    "delivery_status": delivery,
                    "final_audit_status": result.final_audit_status,
                    "delivery_reason": result.delivery_reason,
                    "critique": result.critique,
                    "unresolved_issues": result.unresolved_issues,
                    "persisted": False,
                },
            )
            run_log.log_delivery(final_state, delivery_status=str(delivery), persisted=False, sink="sse")
            return

        yield json_event("progress", {"step": "done", "node": None, "message": "调查完成"})

        # T3：SSE 与同步共用同一持久化函数，内部从 result.report 读字段；
        # 旧实现读 model_dump() 顶层（字段嵌在 report 下），导致摘要恒空、当日状态
        # 被空值覆盖且漏写 market_state。阶段 5 起该函数内部还会按
        # delivery_status 门控——未验证结果不写对话轮次 / 当日状态 / 记忆。
        # 阶段 5：只有 verified 才可能真正落库（门控在 persist_research 内部）；
        # 这里记录的是"是否放行到持久化"，与门控条件保持同一表达式，避免日志说谎。
        # Await the shared persistence gate so delivery telemetry records the
        # actual write decision rather than a predicted value.  The gate is
        # synchronous SQLite work wrapped in an async function and must finish
        # before the stream reports its terminal event.
        persisted = await persist_research(result, question, conversation_id)
        result.persisted = persisted
        final_state["persisted"] = persisted
        #: run_id 是内部字段，不出现在对外 SSE 载荷里
        public_result = {k: v for k, v in result.model_dump().items() if k != "run_id"}
        save_result = public_result
        run_log.log_delivery(final_state, delivery_status=str(delivery), persisted=persisted, sink="sse")

        yield json_event("result", save_result)

    except asyncio.CancelledError:
        # Starlette closes the generator when an SSE client disconnects.  Keep
        # the original run_id and record the cancellation as a terminal event.
        run_log.log_delivery(
            _terminal_state(reason="SSE 客户端断开连接"),
            delivery_status="failed", persisted=False, sink="sse",
        )
        raise
    except (UpstreamTimeoutError, TimeoutError):
        logger.warning("Stream research exceeded %ds budget for: %s", budget, run_log.redact_text(question))
        run_log.log_delivery(
            _terminal_state(reason="研究超出总预算"),
            delivery_status="failed", persisted=False, sink="sse",
        )
        yield json_event("progress", {"step": "error", "message": "研究超时，请重试"})
        yield json_event(
            "result",
            {
                "error": f"Research timed out after {budget}s",
                "code": "timeout",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "研究超出总预算",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
    except LLMOutputError as exc:
        logger.error("Stream research got unusable LLM output for: %s — %s", run_log.redact_text(question), type(exc).__name__)
        run_log.log_delivery(
            _terminal_state(reason="上游模型输出不可解析"),
            delivery_status="failed", persisted=False, sink="sse",
        )
        yield json_event("progress", {"step": "error", "message": "上游模型返回了无法解析的内容"})
        yield json_event(
            "result",
            {
                "error": "上游模型返回了无法解析的内容，请重试或更换模型",
                "code": "upstream",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "上游模型输出不可解析",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
    except Exception as exc:
        logger.error("Stream research failed for: %s (%s)", run_log.redact_text(question), type(exc).__name__)
        run_log.log_delivery(
            _terminal_state(reason="研究流程发生内部异常"),
            delivery_status="failed", persisted=False, sink="sse",
        )
        yield json_event(
            "progress",
            {
                "step": "error",
                "message": f"研究失败: {str(exc)[:100]}",
            },
        )
        yield json_event(
            "result",
            {
                "error": str(exc),
                "code": "internal",
                "question": question,
                "final_audit_status": "error",
                "delivery_status": "failed",
                "delivery_reason": "研究流程发生内部异常",
                "unresolved_issues": [],
                "persisted": False,
            },
        )
    finally:
        if not task.done():
            task.cancel()
        # T26：cancel 之后必须 await 收尸——task 若以异常告终而无人检索，
        # 事件循环会在 GC 时打 "Task exception was never retrieved"；
        # 顺带保证收尾是确定性的（取消真正落地，而不是留给循环异步处理）。
        await asyncio.gather(task, return_exceptions=True)


def _get_step_message(step: str) -> str:
    """Get display message for each research phase."""
    messages = {
        "planning": "理解问题并生成研究计划",
        "tool_call": "正在调取市场数据…",
        "working": "仍在调查中…",
        "evaluating": "正在整理证据链并交叉验证…",
        "reasoning": "正在生成结构化市场情报…",
    }
    return messages.get(step, "调查中…")


@app.post("/api/ask/stream")
async def ask_stream(request: dict):
    """SSE streaming Ask endpoint — returns real-time research progress."""
    # T26：_parse_ask_payload 留在 try 外——它抛的是 HTTPException(400)，
    # 放进 try 会被下面的 except Exception 改写成 500。
    question, domain, conversation_id = _parse_ask_payload(request, "POST /api/ask/stream")
    request_run_id = run_log.new_run_id()

    try:
        logger.info(
            "Streaming question=%s domain=%s conv_id=%s",
            run_log.redact_text(question),
            domain,
            run_log.redact_text(conversation_id),
        )
        # T26：把会失败的工作**提前**到返回 StreamingResponse 之前。以前 try 里
        # 只有 return StreamingResponse(...)，async generator 体要到响应开始后
        # 才执行，except 分支永远不可达——图构建失败表现为 200 + SSE error 事件，
        # 而前端只在非 2xx 时回退 /api/ask，于是永远拿不到失败信号。
        orchestrator = _get_orchestrator()
        orchestrator._ensure_graph()
        return StreamingResponse(
            _stream_research(question, domain, conversation_id=conversation_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    except Exception as exc:
        fallback = {
            "question": question,
            "run_id": request_run_id,
            "final_audit_status": "error",
            "delivery_reason": "研究初始化失败",
        }
        run_log.log_run_start(fallback)
        run_log.log_delivery(fallback, delivery_status="failed", persisted=False, sink="sse")
        logger.error("Internal error during streaming research: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
async def index():
    return FileResponse(INDEX)


if __name__ == "__main__":
    # 本地默认只绑回环（历史上如此）；容器部署用 HOST=0.0.0.0 覆盖——
    # 容器里绑 127.0.0.1 端口映射不到宿主机。
    import os

    _host = os.getenv("HOST", "127.0.0.1")
    _port = int(os.getenv("PORT", "8000"))
    logger.info("Starting uvicorn on %s:%s", _host, _port)
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
