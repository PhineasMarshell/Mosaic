"""P4 本地预算与 Gateway 契约测试。

本文件只验证本地 normalizer、预算守卫和结果模型；不 mock 或调用真实外部
Gateway/MCP 服务，因此不覆盖真实上游可用性和网络时延。
"""

from __future__ import annotations

import time

from app.config import Settings
from app.gateway.normalizer import normalize_tool_result
from app.graph.budget import StageBudgetExceeded, stage_timeout
from app.models.market import NormalizedDatum, ToolResult


def test_cloudflare_error_envelope_is_not_evidence():
    result = normalize_tool_result(
        "get_stock_abnormal_reasons",
        {"symbol": "SH688981"},
        {"code": "gateway_error", "message": "The origin web server returned an invalid or incomplete response to Cloudflare"},
    )
    assert result.status == "error"
    assert result.normalized == []
    assert result.partial is False
    assert {"gateway_upstream_incomplete", "optional_evidence_unavailable"} <= set(result.reason_codes)


def test_stage_budget_reserves_downstream_terminal_time():
    settings = Settings(_env_file=None, llm_timeout_seconds=90)
    state = {"budget_deadline": time.monotonic() + 1.0}
    try:
        stage_timeout(state, settings, stage="reasoning", reserve=2.0)
    except StageBudgetExceeded as exc:
        assert exc.stage == "reasoning"
    else:  # pragma: no cover - assertion documents the fail-closed contract
        raise AssertionError("stage must not start without downstream reserve")


def test_tool_result_cache_and_refresh_flags_are_explicit():
    cached = ToolResult(
        tool="get_ashare_sentiment",
        arguments={},
        status="partial",
        partial=True,
        normalized=[NormalizedDatum(tool="get_ashare_sentiment", metric="x", value=1)],
        cache_hit=True,
    )
    refreshed = cached.model_copy(update={"cache_hit": False, "refresh_requested": True})
    assert cached.cache_hit is True and cached.refresh_requested is False
    assert refreshed.cache_hit is False and refreshed.refresh_requested is True