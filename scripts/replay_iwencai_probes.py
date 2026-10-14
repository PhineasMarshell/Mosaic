"""Replay tool-probe artifacts and current normalization, never delivery audit."""

import argparse
import hashlib
import json
from pathlib import Path

from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.ths_skill_registry import THS_SKILL_BY_KEY
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data


def replay(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "No tool probe records"
    latest = {}
    raw_verified = 0
    for row in rows:
        assert row["run_id"] and row["transport"] == "http_api"
        assert row["critic_verdict"] == row["finalize"] == row["delivery"] == "not_invoked_tool_probe"
        assert row["persisted"] is False
        assert row["refresh_requested"] is True and row["cache_hit"] is False
        if row["status"] == "error":
            assert row["datum_count"] == row["evidence_count"] == 0
        else:
            assert row["status"] in {"success", "partial"}
            assert row["datum_count"] > 0 and row["evidence_count"] > 0
        if raw_path := row.get("raw_response_path"):
            raw = Path(raw_path).read_bytes()
            assert not row.get("raw_response_redacted"), "Original hash cannot validate a redacted response"
            assert hashlib.sha256(raw).hexdigest() == row["response_sha256"]
            raw_verified += 1
        latest[row["tool_key"]] = row
    normalized_replayed = []
    unobserved = []
    for key, row in latest.items():
        spec = THS_SKILL_BY_KEY[key]
        raw_path = row.get("raw_response_path")
        if not raw_path or spec.endpoint != "/v1/query2data":
            unobserved.append(f"{key}:raw_normalization_not_observed")
            continue
        result = normalize_query_data(
            Path(raw_path).read_text(encoding="utf-8"), spec.operation_id,
            canonicalize_tool_arguments(spec.operation_id, row["arguments"]),
        )
        assert result.status == row["status"] and result.partial == row["partial"]
        assert result.datum_count == row["datum_count"]
        assert len(build_evidence([result])) == row["evidence_count"]
        assert sorted({d.as_of_date for d in result.normalized if d.as_of_date}) == row["datum_dates"]
        assert sorted({d.instrument for d in result.normalized if d.instrument}) == row["instrument"]
        normalized_replayed.append(row["run_id"])
    return {
        "records": len(rows), "raw_hashes_verified": raw_verified,
        "latest_normalization_replayed": normalized_replayed,
        "not_observed": unobserved + ["full_research_critic_finalize_delivery", "sync_sse_delivery", "upstream_reliability"],
        "probe_kind": "tool_probe", "delivery_acceptance": "not_invoked_tool_probe", "production_ready": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    print(json.dumps(replay(parser.parse_args().artifact), ensure_ascii=False))


if __name__ == "__main__":
    main()
