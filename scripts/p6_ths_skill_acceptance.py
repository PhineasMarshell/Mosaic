"""P6 同花顺 Skill / HTTP / MCP 对照验收。

默认 ``--mode probe`` 会尝试真实 HTTPS、HTTP Gateway 和 MCP 调用；所有
JSONL 只写摘要，不写 query、raw 响应或密钥。网络或 MCP 不可用时场景
保留为 ``observed=false``，replay 会明确列出 ``not_observed``，不会把
成功的 Skill 调用解释为 MCP 修复。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.agent.evidence_gate import run_evidence_gate
from app.config import Settings
from app.gateway.controlled_fallback import CapabilityMetrics, RouteObservation
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.ths_skill_adapter import skill_adapter_for
from app.gateway.ths_skill_registry import P6_BASELINE_SKILLS, registry_snapshot
from app.graph.run_log import new_run_id
from app.graph.tool_runtime import ToolRuntime
from app.models.market import ToolResult
from app.research.evidence import build_evidence
from app.research.trading_calendar import resolve_trading_date_semantics

ARTIFACT = Path(__file__).parents[1] / "memory" / "p6-ths-skill-acceptance.jsonl"

REQUIRED_SCENARIOS = {
    "skill_probe", "quote_http", "quote_mcp", "quote_ths_skill", "sentiment_http", "sentiment_mcp",
    "sentiment_ths_skill", "abnormal_reasons_http", "abnormal_reasons_mcp", "abnormal_reasons_ths_skill",
    "abnormal_reasons_empty", "abnormal_reasons_upstream_error", "trading_day", "weekend_closed",
    "partial_refresh",
}


def write(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def summary(result: ToolResult, *, run_id: str, skill: str, transport: str, capability: str,
            requested_date: str | None = None, market_closed: bool | None = None,
            observed: bool = True, route: str = "primary", persisted: bool = False) -> dict[str, Any]:
    evidence = build_evidence([result]) if result.status != "error" else []
    gate = run_evidence_gate([result], requested_date=requested_date)
    datums = result.normalized
    return {
        "run_id": run_id,
        "skill": skill,
        "source": sorted({d.source for d in datums if d.source}),
        "source_tool": result.source_tool or result.tool,
        "transport": transport,
        "endpoint": result.endpoint,
        "skill_id": result.skill_id,
        "skill_version": result.skill_version,
        "mcp_initialize": result.mcp_initialize,
        "mcp_list_tools": result.mcp_list_tools,
        "mcp_real_call": result.mcp_real_call,
        "capability": capability,
        "status": result.status,
        "datum_count": len(datums),
        "evidence_count": len(evidence),
        "evidence_created": bool(evidence),
        "instrument": sorted({d.instrument for d in datums if d.instrument}),
        "instrument_name": sorted({d.instrument_name for d in datums if d.instrument_name}),
        "as_of_date": result.as_of_date,
        "datum_dates": sorted({d.as_of_date for d in datums if d.as_of_date}),
        "retrieved_at": result.retrieved_at,
        "authority": result.authority,
        "completeness": result.completeness,
        "coverage": result.coverage,
        "primary_source": result.primary_source,
        "fallback_source": result.fallback_source,
        "source_switch": result.source_switch,
        "partial": result.partial,
        "refresh_requested": result.refresh_requested,
        "cache_hit": result.cache_hit,
        "reason_codes": result.reason_codes,
        "latency_ms": result.duration_ms,
        "attempts": result.attempts,
        "upstream_status": result.upstream_status,
        "evidence_gate": {"has_evidence": gate.has_evidence, "reason_codes": gate.reason_codes,
                           "invalid_evidence_count": gate.invalid_evidence_count,
                           "valid_evidence_count": gate.valid_evidence_count},
        "critic_verdict": "not_invoked_tool_probe",
        "requested_date": requested_date,
        "market_closed": market_closed,
        "route": route,
        "finalize": "not_invoked_tool_probe",
        "delivery": "not_invoked_tool_probe",
        "persisted": persisted,
        "observed": observed,
    }


async def call_skill(settings: Settings, operation_id: str, arguments: dict[str, Any], run_id: str,
                     *, refresh: bool = False) -> ToolResult:
    return await skill_adapter_for(settings).call(
        operation_id, arguments, deadline=time.monotonic() + 25, run_id=run_id, refresh=refresh
    )


async def call_gateway(settings: Settings, transport: str, operation_id: str, arguments: dict[str, Any]) -> ToolResult:
    cls = MarketGatewayClient if transport == "mcp" else MarketGatewayHttpClient
    try:
        async with cls(settings) as gateway:
            # MCP context entry performs initialize + list_tools. A business call
            # is required before marking MCP usable.
            result = await gateway.call(operation_id, arguments, deadline=time.monotonic() + 25)
            if transport == "mcp":
                result.mcp_initialize = True
                result.mcp_list_tools = True
                result.mcp_real_call = True
            return result
    except Exception as exc:
        stage = getattr(exc, "stage", "stdio_init" if transport == "mcp" else "http_request")
        reason_codes = list(getattr(exc, "reason_codes", []) or [])
        if not reason_codes:
            if transport == "mcp" and stage in {"stdio_init", "initialize", "list_tools", "call_tool"}:
                reason_codes = [f"mcp_{stage}_error"]
            else:
                reason_codes = ["gateway_transport_error"]
        attempts = [{
            "stage": stage,
            "endpoint": getattr(exc, "endpoint", None),
            "attempt": getattr(exc, "attempt", 1),
            "elapsed_ms": getattr(exc, "elapsed_ms", None),
            "reason_codes": reason_codes,
        }]
        return ToolResult(tool=operation_id, operation_id=operation_id, arguments=arguments,
                          status="error", normalized=[], error=f"{type(exc).__name__}",
                          reason_codes=reason_codes,
                          attempts=attempts,
                          transport=transport,
                          mcp_initialize=False if transport == "mcp" else None,
                          mcp_list_tools=False if transport == "mcp" else None,
                          mcp_real_call=False if transport == "mcp" else None)


async def probe(settings: Settings) -> None:
    run_id = new_run_id()
    write({"kind": "registry", "run_id": run_id, "skills": registry_snapshot(),
           "capability_matrix": {cap: list(keys) for cap, keys in _capability_matrix().items()}})
    metrics: dict[tuple[str, str], CapabilityMetrics] = {}

    # 1) All ten Skills: direct HTTP/API, independent of MCP.
    skill_args = {
        "internal_iwencai_finance_query": {"query": "贵州茅台 2025年营业收入和净利润"},
        "internal_iwencai_event_query": {"query": "贵州茅台 2026年10月9日 业绩预告或异常原因"},
        "internal_iwencai_basicinfo_query": {"query": "贵州茅台 股票代码 上市日期"},
        "internal_iwencai_market_query": {"query": "600519 2026年10月9日 收盘价 涨跌幅"},
        "internal_iwencai_macro_query": {"query": "中国 2026年 CPI"},
        "internal_iwencai_industry_query": {"query": "白酒行业 2026年10月9日 涨跌幅"},
        "internal_iwencai_management_query": {"query": "贵州茅台 前十大股东"},
        "internal_iwencai_announcement_search": {"query": "贵州茅台 2026年10月 公告", "size": 5},
        "internal_iwencai_report_search": {"query": "贵州茅台 研报 2026年10月", "size": 5},
        "internal_iwencai_news_search": {"query": "贵州茅台 2026年10月9日 新闻", "size": 5},
    }
    for spec in P6_BASELINE_SKILLS:
        result = await call_skill(settings, spec.operation_id, skill_args[spec.operation_id], run_id)
        row = summary(result, run_id=run_id, skill=spec.name, transport="http_api", capability=spec.capabilities[0])
        write({"kind": "scenario", "scenario": "skill_probe", **row})
        key = (spec.name, "http_api")
        metrics.setdefault(key, CapabilityMetrics(spec.name, "同花顺问财"))
        metrics[key].add(RouteObservation(run_id, "同花顺问财", "http_api", spec.capabilities[0], result,
                                          float(result.duration_ms or 0)))

    # 2) Identical quote/sentiment/abnormal requests across HTTP, MCP and Skill.
    paired = [
        ("quote", "get_market_quotes", {"symbols": ["600519"]}, "internal_iwencai_market_query", {"query": "600519 2026年10月9日 最新价 涨跌幅"}),
        ("sentiment", "get_ashare_sentiment", {}, "internal_iwencai_market_query", {"query": "A股 2026年10月9日 上涨家数 下跌家数"}),
        ("abnormal_reasons", "get_stock_abnormal_reasons", {"symbol": "SH600519", "start": "2026-10-09", "end": "2026-10-09"}, "internal_iwencai_event_query", {"query": "600519 2026年10月9日 异常涨跌原因"}),
    ]
    for capability, gateway_tool, gateway_args, skill_tool, skill_args_one in paired:
        for transport in ("http", "mcp"):
            gateway_settings = settings.model_copy(update={"market_gateway_mode": transport})
            result = await call_gateway(gateway_settings, transport, gateway_tool, gateway_args)
            row = summary(result, run_id=run_id, skill=gateway_tool, transport=transport, capability=capability)
            write({"kind": "transport_comparison", "scenario": f"{capability}_{transport}", **row,
                   "mcp_tls_upstream": transport == "mcp" and any(x in result.reason_codes for x in ("gateway_tls_error", "gateway_upstream_error"))})
            key = (capability, transport)
            metrics.setdefault(key, CapabilityMetrics(capability, transport))
            metrics[key].add(RouteObservation(run_id, gateway_tool, transport, capability, result,
                                              float(result.duration_ms or 0)))
        skill_result = await call_skill(settings, skill_tool, skill_args_one, run_id)
        row = summary(skill_result, run_id=run_id, skill=skill_tool, transport="http_api", capability=capability)
        write({"kind": "transport_comparison", "scenario": f"{capability}_ths_skill", **row,
               "mcp_statement": "同花顺 Skill 数据链路可用；MCP TLS/上游问题仍未修复"})
        key = (capability, "http_api")
        metrics.setdefault(key, CapabilityMetrics(capability, "同花顺问财"))
        metrics[key].add(RouteObservation(run_id, "同花顺问财", "http_api", capability, skill_result,
                                          float(skill_result.duration_ms or 0)))

        # Empty/upstream abnormal-reasons outcomes are recorded explicitly when
        # the live run does not observe them; they must not be inferred from a
        # timeout or from a successful Skill response.
        for outcome in ("empty", "upstream_error"):
            write({"kind": "scenario", "scenario": f"{capability}_{outcome}", "run_id": run_id,
                   "skill": skill_tool, "transport": "http_api", "capability": capability,
                   "status": "not_observed", "datum_count": 0, "evidence_count": 0,
                   "reason_codes": ["not_observed"], "observed": False, "persisted": False,
                   "route": "primary", "critic_verdict": "not_invoked_tool_probe",
                   "finalize": "not_invoked_tool_probe", "delivery": "not_invoked_tool_probe"})

    # Refresh is an explicit upstream read. A failed refresh is kept as an
    # error and cannot revive an older partial cache entry.
    refresh_args = {"query": "600519 2026年10月9日 收盘价 涨跌幅"}
    runtime = ToolRuntime(settings)
    runtime.set_run_id(run_id)
    for refresh in (False, False, True):
        result = await runtime.execute(
            "internal_iwencai_market_query",
            refresh_args,
            set(),
            deadline=time.monotonic() + 25,
            refresh=refresh,
        )
        row = summary(result, run_id=run_id, skill="market", transport="http_api", capability="quote",
                      route="refresh" if refresh else "normal")
        write({"kind": "scenario", "scenario": "partial_refresh", **row})

    # 3) Trading-day and weekend semantics; a Skill query is never a weekend quote.
    for question, scenario in (("2026-10-09 A股发生了什么", "trading_day"), ("2026-10-11 A股发生了什么", "weekend_closed")):
        semantics = resolve_trading_date_semantics(question)
        result = await call_skill(settings, "internal_iwencai_market_query", {"query": question}, run_id)
        write({"kind": "date_semantics", "scenario": scenario, "run_id": run_id,
               "requested_date": semantics.requested_date, "market_closed": semantics.market_closed,
               "as_of_date": semantics.as_of_date, "reason": semantics.reason,
               **summary(result, run_id=run_id, skill="market", transport="http_api", capability="quote",
                         requested_date=semantics.requested_date, market_closed=semantics.market_closed)})

    write({"kind": "metrics", "run_id": run_id,
           "metrics": [item.summary() for item in metrics.values()],
           "run_complete": True, "probe_kind": "tool_probe", "production_ready": False})
    print(json.dumps({"run_id": run_id, "artifact": str(ARTIFACT), "production_ready": False}))


def _capability_matrix() -> dict[str, tuple[str, ...]]:
    from app.gateway.ths_skill_registry import THS_CAPABILITY_MATRIX

    return THS_CAPABILITY_MATRIX


def latest_complete_run(rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Select the newest *complete* run, never merely the newest run_id line.

    A process interruption can leave a registry/scenario tail after a newer
    run id.  Replay must use the newest group with a completion marker and all
    required scenarios, while retaining the original row order for audit.
    """
    by_run: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        run_id = row.get("run_id")
        if not run_id:
            continue
        if run_id not in by_run:
            by_run[run_id] = []
            order.append(run_id)
        by_run[run_id].append(row)
    for run_id in reversed(order):
        group = by_run[run_id]
        scenarios = {row.get("scenario") for row in group}
        complete = any(
            row.get("kind") == "metrics"
            and (row.get("run_complete") is True or "metrics" in row)
            for row in group
        )
        if complete and REQUIRED_SCENARIOS <= scenarios:
            return run_id, group
    raise AssertionError("no complete acceptance run in JSONL")


def replay() -> None:
    if not ARTIFACT.exists():
        raise SystemExit(f"artifact missing: {ARTIFACT}")
    all_rows = [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]
    text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("Bearer ", "Authorization:", "api_key", "synthetic-secret", "iiix_int_"):
        assert marker not in text, f"credential marker: {marker}"
    latest_run_id, rows = latest_complete_run(all_rows)
    failures = [row for row in rows if row.get("status") == "error" and row.get("datum_count") != 0]
    assert not failures, "error result created datum"
    for row in rows:
        if row.get("kind") in {"scenario", "transport_comparison", "date_semantics"}:
            assert row.get("critic_verdict") == "not_invoked_tool_probe"
            assert row.get("finalize") == "not_invoked_tool_probe"
            assert row.get("delivery") == "not_invoked_tool_probe"
        if row.get("status") == "error":
            assert row.get("evidence_count") == 0
    scenarios = {row.get("scenario") for row in rows}
    missing = sorted(REQUIRED_SCENARIOS - scenarios)
    assert not missing, f"missing scenarios: {missing}"
    weekend = [row for row in rows if row.get("scenario") == "weekend_closed" and row.get("run_id") == latest_run_id]
    assert weekend and all(row.get("requested_date") == "2026-10-11" and row.get("market_closed") is True for row in weekend)
    refresh_rows = [row for row in rows if row.get("scenario") == "partial_refresh" and row.get("run_id") == weekend[0].get("run_id")]
    assert len(refresh_rows) == 3
    assert refresh_rows[0].get("refresh_requested") is False and refresh_rows[0].get("cache_hit") is False
    if refresh_rows[0].get("status") in {"success", "partial"}:
        assert refresh_rows[1].get("cache_hit") is True
    else:
        # Error/empty responses are deliberately never cached; replay must
        # reflect the observed run instead of claiming a cache hit.
        assert refresh_rows[1].get("cache_hit") is False
    assert refresh_rows[2].get("refresh_requested") is True and refresh_rows[2].get("cache_hit") is False
    mcp = [row for row in rows if row.get("transport") == "mcp"]
    skill_success = [row for row in rows if row.get("transport") == "http_api" and row.get("evidence_created")]
    print(json.dumps({"rows": len(rows), "run_ids": sorted({row.get("run_id") for row in rows}),
                      "selected_latest_complete_run": latest_run_id,
                      "mcp_all_required_success": all(
                          any(row.get("scenario") == f"{capability}_mcp" and row.get("status") == "success"
                              and row.get("mcp_initialize") and row.get("mcp_list_tools") and row.get("mcp_real_call")
                              for row in rows)
                          for capability in ("quote", "sentiment", "abnormal_reasons")
                      ),
                      "mcp_business_success_any": any(row.get("status") == "success" for row in mcp),
                      "skill_evidence_success": bool(skill_success),
                      "not_observed": sorted({row.get("scenario") for row in rows if row.get("observed") is False}),
                      "production_ready": False, "probe_kind": "tool_probe"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "replay"), required=True)
    args = parser.parse_args()
    if args.mode == "replay":
        replay()
    else:
        asyncio.run(probe(Settings()))


if __name__ == "__main__":
    main()
