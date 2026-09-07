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
from app.logging_config import setup_logging

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
        logger.info("Configuration OK: gateway=%s model=%s",
                     _settings.market_gateway_mode, _settings.openai_model)

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
from app.memory.storage import MarketMemory
memory = MarketMemory()


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


@app.post("/api/ask")
async def ask(request: dict):
    """Ask endpoint — accepts dict to match frontend payload."""
    question = request.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question cannot be empty")

    domain = request.get("domain") or None  # optional explicit domain override
    conversation_id = request.get("conversation_id") or None  # optional conversation session

    # 检查 domain 是否有效（支持从 tool_registry 动态获取）
    from app.gateway.tool_registry import get_enabled_domains, ALL_TOOLS

    # 构建支持的域列表（排除 unknown）
    supported_domains = get_enabled_domains()

    if domain and domain not in supported_domains:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported domain: {domain}. Supported: {supported_domains}"
        )

    try:
        logger.info("Received question: %s (len=%d) domain=%s conv_id=%s",
                     question[:50], len(question), domain, conversation_id)
        result = await _get_orchestrator().run(question, domain=domain, conversation_id=conversation_id)
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
    except ValueError as exc:
        logger.warning("Invalid input: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Internal error during research")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _stream_research(question: str, domain: str | None, conversation_id: str | None = None):
    """SSE event generator — wraps MarketDetective.investigate() with progress events.

    Instead of duplicating the investigation logic (which caused import errors
    for internal functions like _cross_tool_defaults), this delegates to the
    full investigate pipeline and emits progress events along the way.
    """
    import asyncio

    def json_event(event: str, data: dict):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # Phase 1: Planning
    yield json_event("progress", {
        "step": "planning",
        "message": "理解问题并生成研究计划",
    })

    # Phase 2-4: Delegate to full investigate, but emit progress periodically
    # We run investigate() in a background task and poll its progress
    detective = _get_orchestrator().market_detective

    async def run_investigation():
        """Run the full investigation and return the result."""
        return await detective.investigate(question, domain=domain, conversation_id=conversation_id)

    task = asyncio.create_task(run_investigation())

    # Emit periodic progress events while investigation runs
    phase_names = [
        "tool_call", "evaluating", "reasoning"
    ]
    phase_idx = 0
    last_phase = "planning"

    while not task.done():
        await asyncio.sleep(2)  # Check every 2 seconds
        current_phase = phase_names[phase_idx % len(phase_names)]
        phase_idx += 1
        yield json_event("progress", {
            "step": current_phase,
            "message": _get_step_message(current_phase),
        })
        last_phase = current_phase

    # Wait for completion (with timeout safety)
    try:
        result = await asyncio.wait_for(task, timeout=110)
    except asyncio.TimeoutError:
        yield json_event("error", {"message": "Research timed out"})
        return

    # Emit final progress
    yield json_event("progress", {
        "step": "done",
        "message": "调查完成",
    })

    # Save to memory (research record + daily state + conversation turn)
    # Using background tasks so they don't block the final result event
    save_result = result.model_dump()
    if conversation_id:
        asyncio.create_task(_save_turn(conversation_id, question, save_result))
    asyncio.create_task(_save_research_and_state(question, save_result))

    # Send result
    yield json_event("result", result.model_dump())


def _get_step_message(step: str) -> str:
    """Get display message for each research phase."""
    messages = {
        "planning": "理解问题并生成研究计划",
        "tool_call": "正在调取市场数据…",
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
    question = request.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question cannot be empty")

    domain = request.get("domain") or None
    conversation_id = request.get("conversation_id") or None
    supported_domains = get_enabled_domains()
    if domain and domain not in supported_domains:
        raise HTTPException(status_code=400, detail=f"Unsupported domain: {domain}. Supported: {supported_domains}")

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
