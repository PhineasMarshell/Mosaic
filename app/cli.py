"""CLI — Mosaic 命令行入口。

用法：

    python -m app.cli "今天A股发生了什么？"

或在交互式模式下运行：

    python -m app.cli
"""

import asyncio
import json
import re
import sys

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.logging_config import HumanFormatter, setup_logging


def _strip_evidence_tags(text: str) -> str:
    """清理文本中的 [evidence-xxx] 标签。"""
    if not isinstance(text, str):
        return text
    return re.sub(r'\[evidence-\d+(?:~\d+)?\]', '', text).rstrip()


def _clean_text_fields(report: dict) -> dict:
    """递归清理报告中所有文本字段内的 [evidence-xxx] 标签。"""
    for key in ("what_happened", "market_state", "title"):
        report[key] = _strip_evidence_tags(report.get(key, ""))
    for key in ("why", "strong_areas", "what_changed", "what_matters", "risks", "data_caveats"):
        cleaned = [_strip_evidence_tags(item) for item in (report.get(key) or [])]
        # 过滤空字符串（标签删完后可能留下纯空行）
        report[key] = [c.strip() for c in cleaned if c.strip()]
    return report


def render_report(report: dict, question: str) -> str:
    """将结构化报告渲染为人类可读的 Markdown 格式文本。"""
    lines = [f"# {report.get('title', '今日市场情报')}"]
    lines.append("")
    lines.append(f"**用户问题**: {question}")
    lines.append("")

    # Market State
    state_label = report.get("state_label", "Unknown")
    market_state = report.get("market_state", "")
    lines.append(f"> **State**: {state_label} · {market_state}")
    lines.append("")

    # What Happened
    what_happened = report.get("what_happened", "")
    if what_happened:
        lines.append("## What Happened")
        lines.append("")
        lines.append(what_happened)
        lines.append("")

    # Why
    why_items = report.get("why", [])
    if why_items:
        lines.append("## Why")
        lines.append("")
        for i, w in enumerate(why_items, 1):
            lines.append(f"{i}. {w}")
        lines.append("")

    # Evidence
    evidence_items = report.get("evidence", [])
    if evidence_items:
        lines.append("## Evidence")
        lines.append("")
        for e in evidence_items[:5]:  # 最多展示5条
            # New EvidenceItem format: id, source_tool, metric, value, note
            source = e.get("source_tool") or e.get("tool", "unknown")
            metric = e.get("metric", "")
            note = e.get("note", "")
            evidence_id = e.get("id", "?")
            # Build description from available fields
            parts = [f"[{evidence_id}]"]
            if metric:
                parts.append(f"**{metric}**")
            elif note:
                parts.append(note)
            else:
                parts.append(f"via {source}")
            lines.append(f"- {' '.join(parts)} — `{json.dumps(e.get('value', ''), ensure_ascii=False, default=str)[:120]}`")
        lines.append("")

    # Strong Areas
    strong = report.get("strong_areas", [])
    if strong:
        lines.append("## What's Moving")
        lines.append("")
        for item in strong:
            lines.append(f"- {item}")
        lines.append("")

    # What Changed
    changed = report.get("what_changed", [])
    if changed:
        lines.append("## What Changed")
        lines.append("")
        for item in changed:
            lines.append(f"- {item}")
        lines.append("")

    # What Matters
    matters = report.get("what_matters", [])
    if matters:
        lines.append("## What to Watch")
        lines.append("")
        for item in matters:
            lines.append(f"- {item}")
        lines.append("")

    # Risks
    risks = report.get("risks", [])
    if risks:
        lines.append("## Risks / Counter-Evidence")
        lines.append("")
        for item in risks:
            lines.append(f"- {item}")
        lines.append("")

    # Data Caveats
    caveats = report.get("data_caveats", [])
    if caveats:
        lines.append("## Data Caveats")
        lines.append("")
        for item in caveats:
            lines.append(f"- {item}")
        lines.append("")

    # Confidence
    confidence = report.get("confidence", "low")
    conf_colors = {"high": "🟢", "medium": "🟡", "low": "🔴"}
    lines.append(f"**Confidence**: {conf_colors.get(confidence, '')} {confidence.upper()}")
    lines.append("")

    # Tools used
    tools = report.get("used_tools", [])
    if tools:
        lines.append(f"*Tools called: {', '.join(tools)}*")

    return "\n".join(lines)


async def main(question: str | None = None) -> None:
    settings = get_settings()

    if not question:
        try:
            question = input("Mosaic > ").strip()
        except EOFError:
            print("No input provided.", file=sys.stderr)
            return
        except KeyboardInterrupt:
            print("\nBye.")
            return

    if not question:
        print("Please provide a question.", file=sys.stderr)
        return

    orchestrator = Orchestrator(settings)

    try:
        result = await orchestrator.run(question)
        report = result.report.model_dump()
        _clean_text_fields(report)
        rendered = render_report(report, question)

        # 显示缓存统计
        if result.cache_stats:
            stats = result.cache_stats
            print(f"\n[Cache: {stats.get('hits', 0)} hits / {stats.get('misses', 0)} misses ({stats.get('hit_rate_pct', 0)}%)]")
            print()

        print(rendered)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    setup_logging(level="INFO", json_format=False)
    question = " ".join(sys.argv[1:]) or None
    asyncio.run(main(question))
