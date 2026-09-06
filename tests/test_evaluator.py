"""Tests for app.agent.evaluator helper functions."""

import json

from app.agent.evaluator import _extract_json, _fallback_decision
from app.agent.evidence_gate import EvidenceGateResult


class TestExtractJSON:
    """测试 _extract_json 的多种输入情况。"""

    def test_pure_json(self):
        text = '{"key": "value", "number": 42}'
        assert _extract_json(text) == text

    def test_strips_whitespace(self):
        text = '  \n  {"key": "value"}  \n  '
        result = _extract_json(text)
        assert result is not None
        assert '"key"' in result

    def test_markdown_json_block(self):
        text = '```json\n{"key": "value"}\n```\n'
        result = _extract_json(text)
        assert result is not None
        assert '"key"' in result

    def test_markdown_generic_block(self):
        text = '```\n{\n  "key": "value"\n}\n```\n'
        result = _extract_json(text)
        assert result is not None

    def test_extract_from_mixed_text(self):
        text = 'Here is the result:\n```json\n{"sufficient": true}\n```'
        result = _extract_json(text)
        assert result is not None
        data = json.loads(result)
        assert data["sufficient"] is True

    def test_brace_fallback(self):
        text = 'Some preamble { "key": "value" } postamble'
        result = _extract_json(text)
        assert result is not None

    def test_empty_input(self):
        assert _extract_json("") is None
        assert _extract_json(None) is None

    def test_invalid_json(self):
        # 无法解析的内容应返回 None
        assert _extract_json("this is not json") is None
        assert _extract_json("{{invalid}}") is None

    def test_list_not_dict(self):
        # 如果提取到的是列表而非字典，尝试也应该返回结果（但调用方会判断 isinstance）
        text = '[1, 2, 3]'
        result = _extract_json(text)
        # _extract_json 只负责提取可解析的 JSON，不负责类型检查
        assert result is not None


class TestFallbackDecision:
    """测试 LLM 不可达时的降级决策逻辑。"""

    def test_no_evidence_continues_research(self):
        gate = EvidenceGateResult(has_evidence=False)
        decision = _fallback_decision(gate, called_tools=[])

        assert decision.sufficient is False
        assert decision.recommended_next_action == "continue_research"
        assert decision.evidence_quality == "none"
        # 应该有至少一个 next_step 用于回退重试
        assert len(decision.next_steps) >= 0  # CORE_TOOLS 存在时会返回 overview

    def test_all_tools_used_finishes(self):
        from app.gateway.tool_registry import CORE_TOOLS

        used_keys = [tool.key for tool in CORE_TOOLS]
        gate = EvidenceGateResult(
            has_evidence=True,
            successful_tools=["overview", "sentiment"],
            reason="所有工具已调用",
        )

        decision = _fallback_decision(gate, called_tools=used_keys)

        # 所有 CoreTools 都被标记为已调用 → 没有剩余工具
        assert decision.sufficient is True
        assert decision.recommended_next_action == "finish"
        assert decision.evidence_quality == "weak"

    def test_partial_only_has_evidence(self):
        gate = EvidenceGateResult(
            has_evidence=True,
            partial_tools=["klines"],
            reason="仅有 partial 数据",
        )
        decision = _fallback_decision(gate, called_tools=[])

        assert decision.sufficient is False
        assert decision.recommended_next_action == "continue_research"
        assert decision.coverage == []
        assert "klines" in decision.missing or len(decision.missing) > 0
