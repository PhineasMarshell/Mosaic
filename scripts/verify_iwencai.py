"""Real Iwencai tool probes, distinct from Critic/Finalize acceptance.

Run from the repository root: python -m scripts.verify_iwencai "贵州茅台 分红公告" --size 3
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from app.agent.evidence_gate import run_evidence_gate
from app.config import get_settings
from app.gateway.iwencai_contracts import IWENCAI_TOOL_KEYS, SEARCH_TOOLS
from app.gateway.ths_skill_registry import THS_SKILL_REGISTRY
from app.graph.run_log import new_run_id
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai import OPERATION_ID


async def main() -> int:
    parser = argparse.ArgumentParser(description="验证同花顺问财工具真实链路")
    parser.add_argument("query")
    parser.add_argument("--size", type=int, default=3)
    parser.add_argument(
        "--tool",
        choices=list(IWENCAI_TOOL_KEYS.values()),
        default="announcement_search",
    )
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--expand-index", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--max-data-items", type=int, default=10, help="最多展示多少条datum，完整数据保留在ToolResult.raw"
    )
    parser.add_argument("--requested-date", help="独立Evidence Gate的目标日期；不改写query或证明Critic通过")
    parser.add_argument("--output", type=Path, help="追加秘密脱敏的JSONL工具探针记录")
    parser.add_argument("--raw-output-dir", type=Path, help="另存脱敏原始响应，以run_id命名；不代表研究报告持久化")
    args = parser.parse_args()
    operation = next(op for op, key in IWENCAI_TOOL_KEYS.items() if key == args.tool)
    arguments = (
        {"query": args.query, "size": args.size}
        if operation in SEARCH_TOOLS
        else {"query": args.query, "page": args.page, "limit": args.size, "expand_index": args.expand_index}
    )
    settings = get_settings()
    runtime = ToolRuntime(settings)
    run_id = new_run_id()
    runtime.set_run_id(run_id)
    result = await runtime.execute(
        operation,
        arguments,
        set(),
        refresh=True,
    )
    evidence = build_evidence([result])
    gate = run_evidence_gate([result], requested_date=args.requested_date)
    payload = json.loads(result.raw) if isinstance(result.raw, str) and result.raw.startswith("{") else {}
    raw_path = None
    raw_redacted = False
    if args.raw_output_dir and isinstance(result.raw, str):
        args.raw_output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = args.raw_output_dir / f"{run_id}.json"
        safe_raw = result.raw.replace(settings.iwencai_api_key, "[REDACTED]") if settings.iwencai_api_key else result.raw
        raw_redacted = safe_raw != result.raw
        raw_path.write_text(safe_raw, encoding="utf-8")
    record = {
        "run_id": run_id,
        "probe_kind": "tool_probe",
        "production_ready": False,
        "skill_id": result.skill_id,
        "skill_version": result.skill_version,
        "tool_key": args.tool,
        "operation_id": result.operation_id,
        "source_tool": result.source_tool,
        "capabilities": list(THS_SKILL_REGISTRY[operation].capabilities),
        "transport": result.transport,
        "endpoint": result.endpoint,
        "query": args.query,
        "arguments": result.arguments,
        "requested_date": args.requested_date,
        "status": result.status,
        "reason_codes": result.reason_codes,
        "error": result.error,
        "source": result.authority,
        "authority": result.authority,
        "coverage": result.coverage,
        "completeness": result.completeness,
        "note": result.note,
        "evidence_count": len(evidence),
        "datum_count": result.datum_count,
        "partial": result.partial,
        "latency_ms": result.duration_ms,
        "retrieved_at": result.retrieved_at,
        "datum_dates": sorted({d.as_of_date for d in result.normalized if d.as_of_date}),
        "instrument": sorted({d.instrument for d in result.normalized if d.instrument}),
        "instrument_name": sorted({d.instrument_name for d in result.normalized if d.instrument_name}),
        "data_sample_count": min(len(result.normalized), max(0, args.max_data_items)),
        "data_sample_truncated": len(result.normalized) > max(0, args.max_data_items),
        "refresh_requested": result.refresh_requested,
        "cache_hit": result.cache_hit,
        "upstream_row_count": len(payload.get("datas", [])),
        "upstream_code_count": payload.get("code_count"),
        "response_sha256": hashlib.sha256((result.raw or "").encode()).hexdigest()
        if isinstance(result.raw, str)
        else None,
        "raw_response_path": str(raw_path.resolve()) if raw_path else None,
        "raw_response_redacted": raw_redacted,
        "evidence_gate": {"has_evidence": gate.has_evidence, "reason_codes": gate.reason_codes},
        "critic_verdict": "not_invoked_tool_probe",
        "route": "direct_skill_tool_probe",
        "finalize": "not_invoked_tool_probe",
        "delivery": "not_invoked_tool_probe",
        "persisted": False,
        "attempts": result.attempts,
        "announcements": [
            {
                "title": d.value["title"],
                "published_at": d.timestamp,
                "url": d.value["url"],
                "instrument": d.instrument,
            }
            for d in result.normalized[: max(0, args.max_data_items)]
            if operation == OPERATION_ID
        ],
        "data": [
            {
                "metric": d.metric,
                "value": d.value,
                "unit": d.unit,
                "instrument": d.instrument,
                "instrument_name": d.instrument_name,
                "coverage": d.instrument_scope,
                "partial": d.partial or result.partial,
                "authority": d.authority,
                "retrieved_at": d.retrieved_at,
                "as_of_date": d.as_of_date,
                "source": d.source,
                "source_tool": result.source_tool,
            }
            for d in result.normalized[: max(0, args.max_data_items)]
            if operation != OPERATION_ID
        ],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if result.status in {"success", "partial"} and evidence else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
