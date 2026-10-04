"""Mosaic FastAPI — API + Web UI 入口。"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.errors import LLMOutputError, UpstreamTimeoutError
from app.logging_config import setup_logging
from app.models.response import build_response_from_state

setup_logging(level="INFO")
logger = logging.getLogger("mosaic.app")


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

# 初始化 Market Memory（轻量，文件缓存，模块级安全）
from app.memory.storage import get_memory

memory = get_memory()


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
    from app.scheduler.briefs import is_running as scheduler_running

    _s = _get_settings()
    return {
        "status": "ok",
        "service": "mosaic",
        "gateway_mode": _s.market_gateway_mode,
        "model": _s.openai_model,
        "max_tool_calls": _s.max_tool_calls,
        "research_budget_seconds": _s.research_budget_seconds,
        "brief_scheduler": "running" if scheduler_running() else "stopped",
    }


@app.get("/api/brief/morning")
async def morning_brief_endpoint():
    """触发晨间简报生成。"""
    try:
        from app.scheduler.briefs import generate_morning_brief

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
            "%s rejected: empty or non-string question (got %r, keys=%s)",
            endpoint,
            raw_question,
            sorted(request.keys()),
        )
        raise HTTPException(status_code=400, detail="question cannot be empty")

    domain = request.get("domain") or None  # optional explicit domain override
    conversation_id = request.get("conversation_id") or None  # optional conversation session

    if domain is not None:
        from app.gateway.tool_registry import get_enabled_domains

        supported_domains = get_enabled_domains()
        if domain not in supported_domains:
            logger.warning("%s rejected: unsupported domain=%r (supported=%s)", endpoint, domain, supported_domains)
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

    try:
        logger.info(
            "Received question: %s (len=%d) domain=%s conv_id=%s", question[:50], len(question), domain, conversation_id
        )
        # 同步端点以前没有任何总超时：调查可以一直跑到把每个工具的
        # 30s 超时逐个耗尽（max_tool_calls=12 → 最坏几分钟），浏览器只能干等。
        result = await asyncio.wait_for(
            _get_orchestrator().run(question, domain=domain, conversation_id=conversation_id),
            timeout=settings.research_budget_seconds,
        )
        data = result.model_dump()

        # 保存研究记录到 Memory
        try:
            memory.save_research(question, data)
        except Exception as exc:
            logger.debug("Memory save failed (non-fatal): %s", exc)

        # 保存对话轮次到 Memory
        if conversation_id:
            try:
                summary = f"{result.report.state_label}：{result.report.what_happened[:80]}…"
                memory.save_turn(conversation_id, question, summary)
            except Exception as exc:
                logger.debug("Turn save failed (non-fatal): %s", exc)

        # 更新 Daily State
        try:
            state_data = {
                "market_state": result.report.market_state,
                "state_label": result.report.state_label,
                "strong_areas": result.report.strong_areas,
                "confidence": result.report.confidence,
                "anomalies": result.report.anomalies[:5] if result.report.anomalies else [],
            }
            from app.gateway.tool_registry import get_enabled_domains

            for d in get_enabled_domains():
                key = d.replace("_", "-")
                domain_report = {"state_label": result.report.state_label}
                state_data[key] = domain_report
            memory.save_daily_state(data=state_data)
        except Exception as exc:
            logger.debug("Daily state save failed (non-fatal): %s", exc)

        return JSONResponse(content=data)
    except TimeoutError as exc:
        logger.warning("Research exceeded %ds budget: %s", settings.research_budget_seconds, question[:50])
        raise HTTPException(
            status_code=504,
            detail=f"Research timed out after {settings.research_budget_seconds}s",
        ) from exc
    except LLMOutputError as exc:
        # 必须排在 ValueError 之前 —— LLMOutputError 是 ValueError 的子类
        logger.error("Upstream LLM output unusable: %s", str(exc)[:800])
        raise HTTPException(
            status_code=502,
            detail="上游模型返回了无法解析的内容，请重试或更换模型",
        ) from exc
    except ValueError as exc:
        logger.warning("Invalid input: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Internal error during research")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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

    orchestrator = _get_orchestrator()
    graph = orchestrator._ensure_graph()
    initial_state = {"question": question, "domain": domain, "conversation_id": conversation_id}

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

        verdict = result.critique.get("verdict") if isinstance(result.critique, dict) else None
        if result.errors or verdict not in (None, "pass"):
            logger.warning("Stream research completed with audit verdict=%s errors=%d", verdict, len(result.errors))

        yield json_event("progress", {"step": "done", "node": None, "message": "调查完成"})

        save_result = result.model_dump()
        if conversation_id:
            asyncio.create_task(_save_turn(conversation_id, question, save_result))
        asyncio.create_task(_save_research_and_state(question, save_result))

        yield json_event("result", save_result)

    except (UpstreamTimeoutError, TimeoutError):
        logger.warning("Stream research exceeded %ds budget for: %s", budget, question[:50])
        yield json_event("progress", {"step": "error", "message": "研究超时，请重试"})
        yield json_event(
            "result",
            {
                "error": f"Research timed out after {budget}s",
                "code": "timeout",
                "question": question,
            },
        )
    except LLMOutputError as exc:
        logger.error("Stream research got unusable LLM output for: %s — %s", question[:50], str(exc)[:800])
        yield json_event("progress", {"step": "error", "message": "上游模型返回了无法解析的内容"})
        yield json_event(
            "result",
            {
                "error": "上游模型返回了无法解析的内容，请重试或更换模型",
                "code": "upstream",
                "question": question,
            },
        )
    except Exception as exc:
        logger.exception("Stream research failed for: %s", question[:50])
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
            },
        )
    finally:
        if not task.done():
            task.cancel()


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


async def _save_turn(conversation_id: str | None, question: str, report_dict: dict) -> None:
    """后台保存对话轮次（非致命错误）。"""
    if not conversation_id:
        return
    try:
        state_label = report_dict.get("state_label", "")
        what_happened = report_dict.get("what_happened", "")
        summary = f"{state_label}：{what_happened[:80]}…" if what_happened else state_label
        memory.save_turn(conversation_id, question, summary)
    except Exception as exc:
        logger.debug("Turn save failed (non-fatal): %s", exc)


async def _save_research_and_state(question: str, report_dict: dict) -> None:
    """后台保存研究记录和 Daily State（非致命错误）。"""
    try:
        memory.save_research(question, report_dict)
    except Exception as exc:
        logger.debug("Research save failed (non-fatal): %s", exc)

    # Build daily state snapshot — mirrors logic in POST /api/ask
    try:
        state_data = {
            "state_label": report_dict.get("state_label", ""),
            "strong_areas": report_dict.get("strong_areas", []),
            "confidence": report_dict.get("confidence", ""),
            "anomalies": report_dict.get("anomalies", [])[:5] if report_dict.get("anomalies") else [],
        }
        from app.gateway.tool_registry import get_enabled_domains

        for d in get_enabled_domains():
            key = d.replace("_", "-")
            domain_report = {"state_label": report_dict.get("state_label", "")}
            state_data[key] = domain_report
        memory.save_daily_state(data=state_data)
    except Exception as exc:
        logger.debug("Daily state save failed (non-fatal): %s", exc)


@app.post("/api/ask/stream")
async def ask_stream(request: dict):
    """SSE streaming Ask endpoint — returns real-time research progress."""
    question, domain, conversation_id = _parse_ask_payload(request, "POST /api/ask/stream")

    try:
        logger.info("Streaming question: %s domain=%s conv_id=%s", question[:50], domain, conversation_id)
        return StreamingResponse(
            _stream_research(question, domain, conversation_id=conversation_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Internal error during streaming research")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
async def index():
    return FileResponse(INDEX)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
