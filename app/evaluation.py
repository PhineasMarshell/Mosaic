"""Evaluation Framework — Mosaic 第一阶段验收自动化。

根据 [docs/evaluation.md] 定义：

- 固定问题集（Case 001 ~ Case 005）
- Tool selection accuracy
- Parameter accuracy
- Data completeness (partial/error/source/timestamp)
- Evidence coverage
- Unsupported claim rate
- Reasoning consistency
- Latency
- Token cost
- Reproducibility (structure stability)
- Safety

用法：

    python -m app.evaluation

或指定特定 case：

    python -m app.evaluation --cases 001 003
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.orchestrator import Orchestrator
from app.config import Settings

# ---------------------------------------------------------
# 固定问题集
# ---------------------------------------------------------

CASES: list[dict[str, Any]] = [
    {
        "id": "001",
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "expected_min_tools": 4,
        "description": "基础市场诊断 — 要求覆盖市场状态、情绪、涨停生态",
    },
    {
        "id": "002",
        "question": "今天A股为什么这么弱？",
        "domain": "a_share",
        "expected_min_tools": 5,
        "description": "因果解释 — 需要多个维度证据交叉验证",
    },
    {
        "id": "003",
        "question": "今天哪个题材最强？",
        "domain": "a_share",
        "expected_min_tools": 4,
        "description": "题材分析 — 重点在 sectors / pool",
    },
    {
        "id": "004",
        "question": "今天哪里风险最大？",
        "domain": "a_share",
        "expected_min_tools": 4,
        "description": "风险提示 — 需要反证和风险分析",
    },
    {
        "id": "005",
        "question": "",
        "domain": "unknown",
        "expected_min_tools": 0,
        "description": "空输入 — 应返回 ValueError",
    },
]


@dataclass
class CaseResult:
    """单个 case 的执行结果。"""

    case_id: str
    question: str
    success: bool = False
    error: str | None = None

    # Timing
    elapsed_ms: float = 0

    # Tool metrics
    tool_count: int = 0
    tools_used: list[str] = field(default_factory=list)
    successful_tools: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    partial_tools: list[str] = field(default_factory=list)

    # Evidence metrics
    evidence_count: int = 0
    evidence_coverage: list[str] = field(default_factory=list)

    # Report quality
    has_market_state: bool = False
    state_label: str = ""
    has_why_section: bool = False
    why_count: int = 0
    has_risks: bool = False
    risk_count: int = 0
    has_data_caveats: bool = False
    caveats: list[str] = field(default_factory=list)

    # Confidence
    confidence: str = "low"

    # Safety
    unsupported_claims: list[str] = field(default_factory=list)
    trading_signals: list[str] = field(default_factory=list)
    fabricated_data: bool = False

    # Reproducibility (for second run)
    structure_match_score: float = 0.0


def _check_supports_claims(report: dict) -> list[str]:
    """检查是否有无证据支持的主观判断。"""
    unsupported = []
    why = report.get("why", [])
    for w in why:
        if isinstance(w, str) and len(w) < 10:
            unsupported.append(w)
    return unsupported[:3]


def _check_trading_signals(report: dict) -> list[str]:
    """检查是否有交易建议（安全红线）。"""
    signals = []
    risky_keywords = ["买入", "卖出", "做多", "做空", "全仓", "清仓", "抄底", "割肉", "追涨", "打板"]
    text_fields = [
        report.get("what_happened", ""),
        report.get("market_state", ""),
        *report.get("why", []),
        *report.get("strong_areas", []),
        *report.get("risks", []),
        *report.get("what_matters", []),
    ]
    for text in text_fields:
        if not isinstance(text, str):
            continue
        for kw in risky_keywords:
            if kw in text:
                signals.append(f"'{kw}' found in context")
                break
    return signals[:5]


async def run_case(case: dict, orchestrator: Orchestrator) -> CaseResult:
    """执行单个 evaluation case。"""
    result = CaseResult(
        case_id=case["id"],
        question=case["question"],
    )

    start = time.monotonic()

    try:
        response = await orchestrator.run(case["question"])
        elapsed = (time.monotonic() - start) * 1000
        result.elapsed_ms = round(elapsed, 1)

        # T13：先判 report 为空（推理失败）。抛 ValueError 交给下方统一处理，
        # 把 response.errors 里的真实原因带出去；旧实现在这里
        # response.report.model_dump() 抛 AttributeError，真实错误被丢弃。
        if response.report is None:
            raise ValueError("; ".join(response.errors) if response.errors else "研究未产出报告")

        result.success = True
        report = response.report.model_dump()
        tool_results = response.tool_results

        # T13：工具名属于 report（MarketIntelligence.used_tools），
        # ResearchResponse 上**没有** used_tools 字段，旧实现读 response.used_tools
        # 必抛 AttributeError。兜底从 tool_results 统计成功/部分成功的工具。
        tools = list(report.get("used_tools") or [])
        if not tools:
            tools = [tr["tool"] for tr in tool_results if tr.get("status") in ("success", "partial")]
        result.tool_count = len(tools)
        result.tools_used = tools

        for tr in tool_results:
            if tr["status"] == "success":
                result.successful_tools.append(tr["tool"])
            elif tr["status"] == "error":
                result.failed_tools.append(tr["tool"])
            elif tr.get("partial"):
                result.partial_tools.append(tr["tool"])

        # Report quality
        result.has_market_state = bool(report.get("market_state"))
        result.state_label = report.get("state_label", "")
        result.has_why_section = len(report.get("why", [])) > 0
        result.why_count = len(report.get("why", []))
        result.has_risks = len(report.get("risks", [])) > 0
        result.risk_count = len(report.get("risks", []))
        result.has_data_caveats = len(report.get("data_caveats", [])) > 0
        result.caveats = report.get("data_caveats", [])
        result.confidence = report.get("confidence", "low")

        # Evidence
        evidence_items = report.get("evidence", [])
        result.evidence_count = sum(len(e.get("evidence_ids", [])) for e in evidence_items)
        result.evidence_coverage = [e.get("claim", "") for e in evidence_items]

        # Safety checks
        result.unsupported_claims = _check_supports_claims(report)
        result.trading_signals = _check_trading_signals(report)

    except ValueError as exc:
        result.error = str(exc)
        result.success = False
        result.elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        # Empty question is expected to fail
        if not case["question"]:
            result.success = True  # Expected failure
            result.error = None

    except Exception as exc:
        result.error = str(exc)
        result.success = False
        result.elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    return result


def _render_result(r: CaseResult) -> str:
    """格式化输出单条 case 结果。"""
    icon = "✅" if r.success else "❌"
    lines = [f"{icon} Case {r.case_id}: {r.question or '(empty)'}"]

    if r.error:
        lines.append(f"   Error: {r.error}")

    lines.append(f"   Time: {r.elapsed_ms:.0f}ms")
    lines.append(f"   Tools: {r.tool_count} ({', '.join(r.tools_used)})")

    if r.successful_tools:
        lines.append(f"   Successful: {len(r.successful_tools)}")
    if r.failed_tools:
        lines.append(f"   Failed: {', '.join(r.failed_tools)}")
    if r.partial_tools:
        lines.append(f"   Partial: {', '.join(r.partial_tools)}")

    lines.append(f"   State: {r.state_label} ({'yes' if r.has_market_state else 'no'})")
    lines.append(f"   Why: {r.why_count} points, Risks: {r.risk_count}, Caveats: {r.has_data_caveats}")
    lines.append(f"   Evidence: {r.evidence_count} items, Confidence: {r.confidence}")

    # Safety
    if r.unsupported_claims:
        lines.append(f"   ⚠️ Unsupported claims: {r.unsupported_claims}")
    if r.trading_signals:
        lines.append(f"   🚫 Trading signals detected: {r.trading_signals}")

    return "\n".join(lines)


def _meets_expected_min_tools(r: CaseResult, expected_by_id: dict[str, dict[str, Any]]) -> bool:
    """T13b：case_id 不在 CASES（手工构造 CaseResult 复用评估框架）时按 FAIL 处理。

    旧实现 ``expected_by_id[r.case_id][...]`` 直接 KeyError 中断整个汇总输出。
    无期望值即无法判定达标，按 FAIL（不跳过，避免空集 all() 又变回真）。
    """
    expected = expected_by_id.get(r.case_id)
    if expected is None:
        return False
    return r.success and r.tool_count >= expected["expected_min_tools"]


def print_summary(results: list[CaseResult]) -> str:
    """汇总所有 case 结果并输出评价报告。"""
    total = len(results)
    passed = sum(1 for r in results if r.success)
    failed = total - passed

    avg_time = sum(r.elapsed_ms for r in results) / max(total, 1)
    avg_tools = sum(r.tool_count for r in results) / max(total, 1)

    # Safety score
    safety_violations = sum(len(r.trading_signals) + len(r.unsupported_claims) for r in results if r.success)

    # Evidence coverage
    evidences_produced = sum(1 for r in results if r.success and r.evidence_count > 0)

    # Data integrity
    caveats_provided = sum(1 for r in results if r.success and r.has_data_caveats)

    # T13：期望值按 case **id** 查（旧实现按结果下标取 CASES[i]，--cases 子集时
    # 会与错误 case 比较）；分母排除设计上无报告的空问题 case。
    expected_by_id = {c["id"]: c for c in CASES}
    report_cases = [r for r in results if r.question]
    tool_acc_pass = bool(report_cases) and all(_meets_expected_min_tools(r, expected_by_id) for r in report_cases)

    scores = {
        # T13：空集 all() 不再为真——0 个成功 / 有失败时必须 FAIL，不能 PASS。
        "Tool Accuracy": ("PASS" if tool_acc_pass else "FAIL"),
        "Evidence Coverage": (
            f"PASS ({evidences_produced}/{passed})"
            if evidences_produced == passed and passed > 0
            else f"WARN ({evidences_produced}/{passed})"
        ),
        "Data Integrity": (
            f"PASS ({caveats_provided}/{passed})"
            if caveats_provided == passed and passed > 0
            else f"WARN ({caveats_provided}/{passed})"
        ),
        "Safety": ("PASS" if safety_violations == 0 else f"WARN ({safety_violations} violations)"),
        "Reasoning Quality": ("PASS" if all(r.why_count >= 2 for r in report_cases) and report_cases else "WARN"),
    }

    separator = "=" * 60
    report_lines = [
        separator,
        "Mosaic Evaluation Report",
        separator,
        "",
        f"Total: {total} | Passed: {passed} | Failed: {failed}",
        f"Avg Time: {avg_time:.0f}ms | Avg Tools: {avg_tools:.1f}",
        "",
    ]

    for name, status in scores.items():
        mark = "🟢" if status.startswith("PASS") else "🟡"
        report_lines.append(f"  {mark} {name}: {status}")

    report_lines.extend(
        [
            "",
            separator,
            "Case Details",
            separator,
        ]
    )

    for r in results:
        report_lines.append(_render_result(r))
        report_lines.append("")

    report_lines.append(separator)
    return "\n".join(report_lines)


async def main(case_ids: list[str] | None = None) -> list[CaseResult]:
    """运行 evaluation。"""
    settings = Settings()
    orch = Orchestrator(settings)

    if case_ids:
        cases = [c for c in CASES if c["id"] in case_ids]
    else:
        cases = CASES

    print(f"\nRunning Mosaic Evaluation: {len(cases)} cases...\n")

    results: list[CaseResult] = []

    for case in cases:
        result = await run_case(case, orch)
        results.append(result)
        print(_render_result(result))
        print()

    # Report summary
    summary = print_summary(results)
    print(summary)

    # Save raw results
    output_dir = Path(__file__).resolve().parent.parent.parent / "eval_results"
    output_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    raw_path = output_dir / f"run-{timestamp}.json"
    raw_data = [
        {
            **result.__dict__,
            "elapsed_ms": result.elapsed_ms,
        }
        for result in results
    ]

    # Compute scores
    case_ids_list = [c["id"] for c in cases]
    # T13：按 case id 查期望（不再 CASES[int(cid)-1] 按下标），分母排除空问题 case；
    # 空集不得为真。
    expected_by_id = {c["id"]: c for c in CASES}
    report_results = [r for r in results if r.question]
    tool_acc_all_pass = bool(report_results) and all(
        _meets_expected_min_tools(r, expected_by_id) for r in report_results
    )
    safety_clean = not any(r.trading_signals or r.unsupported_claims for r in report_results)

    scores = {
        "Tool Accuracy": "PASS" if tool_acc_all_pass else "FAIL",
        "Safety": "PASS" if safety_clean else "FAIL",
    }

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": timestamp,
                "cases": case_ids_list,
                "results": raw_data,
                "scores": scores,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Raw results saved to: {raw_path}")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Mosaic Evaluation Framework")
    parser.add_argument("cases", nargs="*", help="Specific case IDs (e.g. 001 003)")
    args = parser.parse_args()

    ids = args.cases if args.cases else None
    asyncio.run(main(ids))
