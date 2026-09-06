from app.models.evidence import Evidence
from app.models.market import ToolResult


def build_evidence(results: list[ToolResult]) -> list[Evidence]:
    evidence: list[Evidence] = []
    counter = 1

    for result in results:
        for datum in result.normalized:
            evidence.append(
                Evidence(
                    id=f"evidence-{counter:03d}",
                    source_tool=result.tool,
                    domain=datum.domain,
                    metric=datum.metric,
                    value=datum.value,
                    timestamp=datum.timestamp,
                    source=datum.source,
                    status=datum.status,
                    partial=datum.partial,
                    note=datum.note or result.error,
                )
            )
            counter += 1

    # 即使一个 Tool 只有原始响应，也必须留下证据链。
    for result in results:
        if not result.normalized:
            evidence.append(
                Evidence(
                    id=f"evidence-{counter:03d}",
                    source_tool=result.tool,
                    metric="tool_status",
                    value=result.error or result.status,
                    status=result.status,
                    partial=result.partial,
                )
            )
            counter += 1

    return evidence
