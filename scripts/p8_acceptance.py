"""P8 real runtime acceptance for THS-first / MCP-controlled fallback.

The probe intentionally records metadata and response summaries only.  It
never writes API keys or raw provider bodies to the JSONL artifact.  A probe
is evidence about the observed transport; it is not a claim that production
delivery is ready.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.agent.evidence_gate import run_evidence_gate
from app.config import Settings, get_settings
from app.gateway.ths_skill_adapter import skill_adapter_for
from app.graph.run_log import new_run_id
from app.graph.tool_runtime import ToolRuntime
from app.models.market import ToolResult
from app.research.evidence import build_evidence
from app.research.trading_calendar import resolve_trading_date_semantics

ROOT = Path(__file__).parents[1]
ARTIFACT = ROOT / "memory" / "p8-acceptance.jsonl"

SKILL_PROBES = {
    "index": ("internal_iwencai_index_query", {"query": "2026年10月9日 上证指数 点位 涨跌幅"}),
    "astock_selector": (
        "internal_iwencai_astock_selector",
        {"query": "2026年10月9日 筛选A股 涨跌幅大于1% 市盈率小于30 非ST", "limit": 5},
    ),
    "sector_selector": (
        "internal_iwencai_sector_selector",
        {"query": "2026年10月9日 行业板块 涨跌幅大于1% 主力资金净流入大于0", "limit": 5},
    ),
    "business": ("internal_iwencai_business_query", {"query": "600519 2025年 主营业务构成 主要客户 供应商"}),
    "insresearch": ("internal_iwencai_insresearch_query", {"query": "600519 2026年 业绩预测 机构评级 目标价"}),
}

ROUTED_SCENARIOS = {
    "trading_day_today_ashare": ("get_ashare_sentiment", {"date": "2026-10-09"}),
    "weekend_today_ashare": ("get_ashare_sentiment", {"date": "2026-10-11"}),
    "quote": ("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}),
    "kline": ("get_market_klines", {"symbol": "600519", "start": "2026-10-01", "end": "2026-10-09"}),
    "limit_up_pool": ("list_limit_up_stocks", {"date": "2026-10-09"}),
    "limit_up_sectors": ("list_limit_up_sectors", {"date": "2026-10-09"}),
    "news": ("news_search", {"query": "600519 2026年10月9日 新闻", "max_results": 5, "time_limit": "w"}),
    "announcement": ("internal_iwencai_announcement_search", {"query": "600519 2026年10月 公告", "size": 5}),
}

REQUIRED_SCENARIOS = set(SKILL_PROBES) | set(ROUTED_SCENARIOS) | {
    "ths_success",
    "ths_401",
    "ths_timeout",
    "ths_empty",
    "mcp_tls",
    "mcp_http_5xx",
    "mcp_upstream_timeout",
    "deadline",
}


def write(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def raw_summary(result: ToolResult) -> dict[str, Any]:
    raw = result.raw
    if not isinstance(raw, str):
        return {"present": False, "payload_chars": 0, "sha256": None, "top_level_keys": []}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    return {
        "present": True,
        "payload_chars": len(raw),
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "top_level_keys": sorted(payload) if isinstance(payload, dict) else [],
        "upstream_row_count": len(payload.get("datas", payload.get("data", []))) if isinstance(payload, dict) and isinstance(payload.get("datas", payload.get("data", [])), list) else None,
        "upstream_code_count": payload.get("code_count") if isinstance(payload, dict) else None,
    }


def summary(result: ToolResult, run_id: str, scenario: str, *, requested_date: str | None = None) -> dict[str, Any]:
    evidence = build_evidence([result]) if result.status != "error" else []
    gate = run_evidence_gate([result], requested_date=requested_date)
    datums = result.normalized
    return {
        "kind": "scenario",
        "scenario": scenario,
        "run_id": run_id,
        "probe_kind": "tool_probe",
        "production_ready": False,
        "status": result.status,
        "error": result.error,
        "reason_codes": result.reason_codes,
        "datum_count": len(datums),
        "evidence_count": len(evidence),
        "evidence_created": bool(evidence),
        "skill_id": result.skill_id,
        "skill_version": result.skill_version,
        "source_tool": result.source_tool or result.tool,
        "endpoint": result.endpoint,
        "transport": result.transport,
        "primary_source": result.primary_source,
        "fallback_source": result.fallback_source,
        "source_switch": result.source_switch,
        "fallback_reason": result.fallback_reason,
        "coverage": result.coverage,
        "completeness": result.completeness,
        "partial": result.partial,
        "requested_date": requested_date,
        "datum_dates": sorted({d.as_of_date for d in datums if d.as_of_date}),
        "instrument": sorted({d.instrument for d in datums if d.instrument}),
        "retrieved_at": result.retrieved_at,
        "latency_ms": result.duration_ms,
        "attempts": result.attempts,
        "upstream_status": result.upstream_status,
        "raw_response_summary": raw_summary(result),
        "evidence_gate": {"has_evidence": gate.has_evidence, "reason_codes": gate.reason_codes},
        "critic_verdict": "not_invoked_tool_probe",
        "finalize": "not_invoked_tool_probe",
        "delivery": "not_invoked_tool_probe",
        "persisted": False,
    }


async def probe(settings: Settings) -> None:
    run_id = new_run_id()
    observed_results: list[ToolResult] = []
    for scenario, (operation, arguments) in SKILL_PROBES.items():
        result = await skill_adapter_for(settings).call(
            operation, arguments, deadline=time.monotonic() + 25, refresh=True, run_id=run_id
        )
        observed_results.append(result)
        write(summary(result, run_id, scenario, requested_date="2026-10-09"))

    runtime = ToolRuntime(settings)
    runtime.set_run_id(run_id)
    for scenario, (operation, arguments) in ROUTED_SCENARIOS.items():
        requested_date = str(arguments.get("date") or arguments.get("end") or "") or None
        result = await runtime.execute(operation, arguments, set(), deadline=time.monotonic() + 25, refresh=True)
        observed_results.append(result)
        write(summary(result, run_id, scenario, requested_date=requested_date))

    # These rows are explicit acceptance gates.  They are marked observed only
    # when the live result contains the corresponding diagnostic; no timeout or
    # 401 is inferred from a different failure class.
    observed_codes = {
        "ths_success": {"success"},
        "ths_401": {"ths_invalid_credentials"},
        "ths_timeout": {"gateway_timeout"},
        "ths_empty": {"empty_normalized", "empty_gateway_response"},
        "mcp_tls": {"gateway_tls_error", "mcp_stdio_init_error", "mcp_initialize_error"},
        "mcp_http_5xx": {"http_5xx", "gateway_upstream_error"},
        "mcp_upstream_timeout": {"mcp_upstream_timeout", "gateway_timeout"},
        "deadline": {"deadline_exhausted"},
    }
    observed_results.append(
        await runtime.execute(
            "get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set(),
            deadline=time.monotonic() + 5, refresh=True,
        )
    )
    for scenario, codes in observed_codes.items():
        match = next(
            (r for r in observed_results if (r.status == "success" and "success" in codes) or set(r.reason_codes) & codes),
            None,
        )
        if match is None:
            write({"kind": "scenario", "scenario": scenario, "run_id": run_id, "probe_kind": "tool_probe", "production_ready": False,
                   "status": "not_observed", "observed": False, "datum_count": 0, "evidence_count": 0,
                   "reason_codes": ["not_observed"], "critic_verdict": "not_invoked_tool_probe",
                   "finalize": "not_invoked_tool_probe", "delivery": "not_invoked_tool_probe", "persisted": False})
        else:
            write(summary(match, run_id, scenario))

    trading = resolve_trading_date_semantics("2026-10-09 A股发生了什么")
    weekend = resolve_trading_date_semantics("2026-10-11 A股发生了什么")
    write({"kind": "date_semantics", "scenario": "date_semantics", "run_id": run_id,
           "trading_day": trading.__dict__, "weekend": weekend.__dict__, "production_ready": False})
    write({"kind": "metrics", "run_id": run_id, "required_scenarios": sorted(REQUIRED_SCENARIOS),
           "run_complete": True, "probe_kind": "tool_probe", "production_ready": False})
    print(json.dumps({"run_id": run_id, "artifact": str(ARTIFACT), "production_ready": False}, ensure_ascii=False))


def replay() -> None:
    rows = [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "empty P8 artifact"
    text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("Bearer ", "Authorization:", "synthetic-secret", "iiix_int_", "IWENCAI_API_KEY="):
        assert marker not in text, f"credential marker: {marker}"
    runs = {}
    for row in rows:
        runs.setdefault(row.get("run_id"), []).append(row)
    selected = next((run_id for run_id in reversed(list(runs)) if any(r.get("kind") == "metrics" and r.get("run_complete") for r in runs[run_id])), None)
    assert selected, "no complete P8 run"
    current = runs[selected]
    scenarios = {row.get("scenario") for row in current}
    assert REQUIRED_SCENARIOS <= scenarios, sorted(REQUIRED_SCENARIOS - scenarios)
    for row in current:
        if row.get("status") == "error":
            assert row.get("datum_count") == row.get("evidence_count") == 0
            assert row.get("partial") is not True
        if row.get("scenario") in REQUIRED_SCENARIOS and row.get("status") == "not_observed":
            assert row.get("observed") is False
    print(json.dumps({"run_id": selected, "rows": len(current),
                      "not_observed": sorted(row.get("scenario") for row in current if row.get("status") == "not_observed"),
                      "production_ready": False, "probe_kind": "tool_probe"}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "replay"), required=True)
    args = parser.parse_args()
    if args.mode == "probe":
        asyncio.run(probe(get_settings()))
    else:
        replay()


if __name__ == "__main__":
    main()
