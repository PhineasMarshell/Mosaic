"""Real MCP/LLM and network SSE probes; summaries contain no raw payloads."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.graph import run_log
from app.graph.tool_runtime import ToolRuntime
from app.models.market import ToolResult
from app.research.evidence import build_evidence

ARTIFACT = Path(__file__).parents[1] / "memory" / "p5-real-acceptance.jsonl"


def write(row):
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


class Capture(logging.Handler):
    def emit(self, record):
        try:
            row = json.loads(record.getMessage())
        except ValueError:
            return
        write({"kind": "event", **row})


def tool_summary(result):
    datums = result.normalized
    return {
        "tool": result.tool, "status": result.status, "partial": result.partial,
        "datum_count": len(datums), "evidence_count": len(build_evidence([result])),
        "reason_codes": result.reason_codes, "cache_hit": result.cache_hit,
        "refresh_requested": result.refresh_requested,
        "source": sorted({d.source for d in datums if d.source}),
        "instrument": sorted({d.instrument for d in datums if d.instrument}),
        "as_of_date": result.as_of_date, "retrieved_at": result.retrieved_at,
        "datum_dates": sorted({d.as_of_date for d in datums if d.as_of_date}),
        "source_datum_count": result.source_datum_count or len(datums), "source_partial": result.source_partial,
        "duration_ms": result.duration_ms,
        "attempts": getattr(result, "attempts", []),
        "upstream_status": getattr(result, "upstream_status", None),
    }


def safe_response(data):
    out = {key: run_log.sanitize({key: value})[key] for key, value in data.items()
           if key not in {"question", "conversation_id", "tool_results", "report", "errors", "delivery_reason"}}
    out["tools"] = [tool_summary(ToolResult(**tool))
                    for tool in data.get("tool_results", [])]
    report = data.get("report")
    out["claims"] = [run_log.sanitize(claim) for claim in (report or {}).get("claims", [])]
    out["persisted"] = data.get("persisted") is True
    return out


async def probe(settings):
    runtime = ToolRuntime(settings)
    run_id = run_log.new_run_id()
    async with (MarketGatewayClient(settings) if settings.market_gateway_mode == "mcp"
                else MarketGatewayHttpClient(settings)) as gateway:
        if settings.market_gateway_mode == "mcp":
            health = await gateway.call("verify_upstream", {}, deadline=time.monotonic() + 20)
            write({"kind": "scenario", "scenario": "mcp_smoke", "run_id": run_id,
                   "tool_count": len(gateway.tools), "tool": tool_summary(health), "observed": True})
        for name, args, seconds, scenario in [
            ("get_market_quotes", {"symbols": ["688981"]}, 20, "real_unmapped_stock"),
            ("get_market_quotes", {"symbols": ["600519"]}, 20, "code_only"),
            ("get_stock_abnormal_reasons", {"symbol": "SH688981", "start": "2026-09-09", "end": "2026-10-09"}, 25, "abnormal_reasons_probe"),
            ("get_stock_abnormal_reasons", {"symbol": "SH600519", "start": "2026-10-09", "end": "2026-10-09"}, 25, "abnormal_reasons_probe"),
            ("get_stock_abnormal_reasons", {"symbol": "SH688981"}, .02, "abnormal_reasons_timeout"),
            ("get_ashare_sentiment", {}, .02, "gateway_deadline"),
        ]:
            started = time.monotonic()
            result = await gateway.call(name, args, deadline=started + seconds)
            write({"kind": "scenario", "scenario": scenario, "run_id": run_id,
                   "observed": True, "tool": tool_summary(result),
                   "elapsed_ms": round((time.monotonic() - started)*1000, 1)})
    async with runtime.gateway_session():
        signatures = set()
        for refresh in (False, False, True):
            started = time.monotonic()
            result = await runtime.execute("get_ashare_sentiment", {}, signatures if refresh else set(),
                                           deadline=started + 40, refresh=refresh)
            write({"kind": "scenario_attempt", "scenario": "partial_refresh", "run_id": run_id,
                   "observed": True, "tool": tool_summary(result),
                   "elapsed_ms": round((time.monotonic() - started)*1000, 1)})


async def research(settings, scenario, phase):
    from app.agent.orchestrator import Orchestrator
    questions = {"trading_day": "2026-10-09 A 股发生了什么？", "weekend_today": "今天 A 股发生了什么？",
                 "normal_quote": "2026-10-09 688981 的报价是多少？只回答已返回报价及其观测日期，不做比较或原因分析。"}
    result = await Orchestrator(settings).run(questions[scenario], domain="a_share")
    result.persisted = False  # Orchestrator-only probe never invokes persistence.
    run_log.log_delivery(result.model_dump(), delivery_status=result.delivery_status, persisted=False, sink="acceptance")
    data = result.model_dump(mode="json")
    write({"kind": "scenario", "scenario": scenario, "phase": phase, "observed": True,
           "run_id": result.run_id, "final_response": safe_response(data),
           "claims": [run_log.sanitize(c) for c in data.get("report", {}).get("claims", [])] if data.get("report") else [],
           "tools": [tool_summary(ToolResult(**tool))
                     for tool in data.get("tool_results", [])], "elapsed_ms": result.elapsed_ms,
           "delivery": result.delivery_status, "persisted": False, "budget_seconds": settings.research_budget_seconds})
    print(json.dumps({"run_id": result.run_id, "delivery": result.delivery_status,
                      "elapsed_ms": result.elapsed_ms, "reason_codes": result.reason_codes}))


async def partial_refresh(settings):
    """Observe production working-copy truncation separately from source partial."""
    runtime = ToolRuntime(settings)
    run_id = run_log.new_run_id()
    async with runtime.gateway_session():
        for refresh in (False, False, True):
            started = time.monotonic()
            result = await runtime.execute("get_ashare_sentiment", {}, set(),
                                           deadline=started + 30, refresh=refresh)
            runtime.truncate(result)
            write({"kind": "scenario_attempt", "scenario": "partial_refresh", "phase": "after",
                   "run_id": run_id, "observed": True, "coverage_origin": "local_truncation",
                   "tool": tool_summary(result), "elapsed_ms": round((time.monotonic()-started)*1000, 1)})
    print(json.dumps({"run_id": run_id, "scenario": "partial_refresh"}))


async def sse(scenario, url="http://127.0.0.1:8015"):
    import httpx
    question = "今天 A 股发生了什么？" if scenario == "weekend_today" else "2026-10-09 688981 的报价是多少？只回答已返回报价及其观测日期，不做比较或原因分析。"
    started = time.monotonic()
    events = []
    async with httpx.AsyncClient(trust_env=False, timeout=330) as client:
        health = (await client.get(url.rstrip("/") + "/health")).json()
        server_budget = health["research_budget_seconds"]
        async with client.stream("POST", url.rstrip("/") + "/api/ask/stream",
                                 json={"question": question, "domain": "a_share"}) as response:
            response.raise_for_status()
            event = None
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    events.append({"event": event, "step": data.get("step"), "at_ms": round((time.monotonic()-started)*1000, 1)})
                    if event == "result":
                        write({"kind": "scenario", "scenario": "sse_real", "question_scenario": scenario,
                               "phase": "after", "budget_seconds": server_budget,
                               "run_id": data.get("run_id"), "observed": True,
                               "final_response": safe_response(data), "events": events,
                               "elapsed_ms": round((time.monotonic()-started)*1000, 1)})
                        print(json.dumps({"run_id": data.get("run_id"), "delivery": data.get("delivery_status"), "events": events}))


async def sync():
    import httpx

    started = time.monotonic()
    async with httpx.AsyncClient(trust_env=False, timeout=185) as client:
        health = (await client.get("http://127.0.0.1:8015/health")).json()
        response = await client.post("http://127.0.0.1:8015/api/ask", json={"domain": "a_share",
            "question": "2026-10-09 688981 的报价是多少？只回答已返回报价及其观测日期，不做比较或原因分析。"})
        data = response.json()
        write({"kind": "scenario", "scenario": "sync_real", "phase": "after", "observed": True,
               "run_id": data.get("run_id"), "http_status": response.status_code,
               "budget_seconds": health["research_budget_seconds"],
               "final_response": safe_response(data), "elapsed_ms": round((time.monotonic()-started)*1000, 1)})
        print(json.dumps({"run_id": data.get("run_id"), "delivery": data.get("delivery_status")}))


def replay():
    artifact_text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "Authorization:", "api_key", "iiix_int_"):
        assert marker not in artifact_text, f"credential marker: {marker}"
    rows = [json.loads(line) for line in artifact_text.splitlines()]

    def check_summary(value):
        if isinstance(value, dict):
            assert not {"raw", "normalized"} & value.keys(), "raw Gateway payload in artifact"
            for child in value.values():
                check_summary(child)
        elif isinstance(value, list):
            for child in value:
                check_summary(child)

    check_summary(rows)
    required = {"trading_day", "weekend_today", "real_unmapped_stock", "code_only", "abnormal_reasons_success",
                "abnormal_reasons_timeout", "abnormal_reasons_empty", "partial_refresh", "gateway_deadline",
                "mcp_smoke", "sse_real", "sync_real"}
    assert required <= {row.get("scenario") for row in rows}
    unobserved = set()
    for row in rows:
        tools = ([row["tool"]] if "tool" in row else []) + row.get("tools", [])
        tools += (row.get("final_response") or {}).get("tools", [])
        for tool in tools:
            if tool["status"] == "error" and row.get("phase") != "before":
                assert tool["datum_count"] == tool["evidence_count"] == 0
            if tool.get("refresh_requested"):
                assert tool["cache_hit"] is False
        if row.get("observed") is False:
            unobserved.add(row.get("scenario"))
        final = row.get("final_response") or {}
        if final.get("delivery_status") in {"degraded", "blocked", "failed"}:
            assert final.get("persisted") is False
        if row.get("scenario") == "sse_real":
            terminal = row["final_response"]
            assert terminal["delivery_status"] in {"verified", "degraded", "blocked", "failed"}
            assert len([e for e in row["events"] if e["event"] == "result"]) == 1
            assert terminal["run_id"] == row["run_id"]
            assert row["events"][-1]["at_ms"] < row.get("budget_seconds", 180)*1000
            deliveries = [r for r in rows if r.get("event") == "delivery" and r.get("run_id") == row["run_id"]]
            assert deliveries and deliveries[-1]["delivery_status"] == terminal["delivery_status"]
    normal = [row for row in rows if row.get("scenario") in {"normal_quote", "sync_real", "sse_real"}
              and (row.get("final_response") or {}).get("delivery_status") in {"verified", "degraded"}
              and row.get("elapsed_ms", float("inf")) < row.get("budget_seconds", 180)*1000]
    assert normal, "no real audited data chain completed within budget"
    partial_reads = [row for row in rows if row.get("coverage_origin") == "local_truncation"]
    assert len(partial_reads) >= 3
    fresh, cached, refreshed = partial_reads[-3:]
    assert all(row["tool"]["partial"] and row["tool"]["source_partial"] is False for row in (fresh, cached, refreshed))
    assert cached["tool"]["cache_hit"] and cached["tool"]["attempts"] == []
    assert fresh["tool"]["retrieved_at"] == cached["tool"]["retrieved_at"]
    assert refreshed["tool"]["refresh_requested"] and not refreshed["tool"]["cache_hit"]
    assert refreshed["tool"]["retrieved_at"] != fresh["tool"]["retrieved_at"]
    assert refreshed["tool"]["attempts"]
    terminal_fields = {"run_id", "stage", "exception_type", "reason_codes", "elapsed_ms", "remaining_budget",
                       "delivery_status", "final_audit_status", "persisted"}
    for row in rows:
        if row.get("scenario") in {"sync_real", "sse_real"}:
            assert terminal_fields <= row["final_response"].keys()
    print(json.dumps({"rows": len(rows), "artifact": str(ARTIFACT), "audited_runs": [r["run_id"] for r in normal],
                      "not_observed": sorted(unobserved), "production_ready": False}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["probe", "partial-refresh", "research", "sse", "sync", "replay"], required=True)
    parser.add_argument("--scenario", default="weekend_today")
    parser.add_argument("--phase", default="after")
    parser.add_argument("--url", default="http://127.0.0.1:8015")
    args = parser.parse_args()
    if args.mode == "replay":
        replay()
        return
    logging.getLogger("app.graph.run_log").setLevel(logging.INFO)
    logging.getLogger("app.graph.run_log").addHandler(Capture())
    settings = Settings()
    if args.mode in {"probe", "partial-refresh", "research"}:
        print(json.dumps({"mode": settings.market_gateway_mode, "budget": settings.research_budget_seconds,
                          "llm_timeout": settings.llm_timeout_seconds}))
    if args.mode == "probe":
        asyncio.run(probe(settings))
    elif args.mode == "partial-refresh":
        asyncio.run(partial_refresh(settings))
    elif args.mode == "sse":
        asyncio.run(sse(args.scenario, args.url))
    elif args.mode == "sync":
        asyncio.run(sync())
    else:
        asyncio.run(research(settings, args.scenario, args.phase))


if __name__ == "__main__":
    main()
