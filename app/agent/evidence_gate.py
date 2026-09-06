"""Evidence Gate — 代码层面的证据质量硬检查。

不依赖 LLM，直接根据 ToolResult 的状态判断当前证据是否具备基本可用性。

返回值传递给 EvidenceEvaluator，LLM 不能绕过此检查结果。
"""

from dataclasses import dataclass, field

from app.models.market import Status, ToolResult


@dataclass
class EvidenceGateResult:
    """
    证据门控的检查结果。

    has_evidence:
        是否存在至少一条 status=success 的有效证据。
        如果为 false，Evaluator 永远不应判定 sufficient=true。

    successful_tools:
        调用成功（status == "success"）的工具名集合。

    partial_tools:
        返回了部分数据（status == "partial"）的工具名集合。
        partial 数据可以作为辅助证据，但不能作为唯一核心证据。

    error_tools:
        调用失败的工具名及错误信息。

    reason:
        人类可读的门控原因说明。
    """

    has_evidence: bool = False
    successful_tools: list[str] = field(default_factory=list)
    partial_tools: list[str] = field(default_factory=list)
    error_tools: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""


def run_evidence_gate(results: list[ToolResult]) -> EvidenceGateResult:
    """
    对一组 ToolResult 做代码级证据质量检查。

    规则：
    - success 工具 → 有效证据来源
    - partial 工具 → 有数据但不完整，降低置信度
    - error 工具 → 记录错误，不作为证据
    - 没有任何 success/partial → has_evidence=False
    """

    gate = EvidenceGateResult()

    for result in results:
        if result.status == "success":
            gate.successful_tools.append(result.tool)
        elif result.status == "partial":
            gate.partial_tools.append(result.tool)
        elif result.status == "error":
            gate.error_tools.append({
                "tool": result.tool,
                "error": result.error or "unknown error",
            })

    # has_evidence = 至少有一个成功或部分成功的工具
    gate.has_evidence = bool(gate.successful_tools or gate.partial_tools)

    # 构建人类可读的原因
    reasons: list[str] = []
    if not gate.has_evidence:
        if results:
            errors = "; ".join(
                f"{e['tool']}: {e['error']}" for e in gate.error_tools
            )
            reasons.append(f"所有工具调用均失败: {errors}")
        else:
            reasons.append("未执行任何工具调用")
    else:
        if gate.partial_tools and not gate.successful_tools:
            reasons.append(
                "仅有 partial 数据返回，历史区间可能不完整"
            )
        elif gate.successful_tools and gate.partial_tools:
            reasons.append(
                "部分工具有完整数据，部分为 partial"
            )
        else:
            reasons.append(
                f"已有 {len(gate.successful_tools)} 个工具返回有效数据"
            )

    if gate.error_tools:
        reasons.append(
            f"{len(gate.error_tools)} 个工具调用失败"
        )

    gate.reason = " | ".join(reasons)
    return gate
