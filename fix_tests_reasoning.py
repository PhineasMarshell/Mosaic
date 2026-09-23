# Rewrite key sections of tests/test_reasoning_parsing.py to match new evidence integrity semantics
import re

with open("tests/test_reasoning_parsing.py", "r", encoding="utf-8") as f:
    content = f.read()

lines = content.split("\n")
result_lines = []
skip_until_next_def = False
in_function_replacement = None
replacement_code = []

i = 0
while i < len(lines):
    line = lines[i]

    # --- Replace test_new_format_with_partial_flag ---
    if 'def test_new_format_with_partial_flag(self):' in line:
        skip_until_next_def = True
        replacement_code = [
            '    def test_new_format_with_partial_flag(self):',
            '        """新格式必须提供匹配的原始 Evidence 才能通过验证层。"""',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [',
            '            {"id": "e-100", "source_tool": "derivatives", "domain": "crypto",',
            '             "metric": "open_interest", "value": 8.5,',
            '             "status": "success"}',
            '        ]',
            '        original = [Evidence(id="e-100", source_tool="derivatives", domain="crypto",',
            '                            metric="open_interest", value=8.5,',
            '                            status="success", partial=False)]',
            '        result = _parse_evidence(raw, original)',
            '        assert len(result) == 1',
            '        assert result[0].id == "e-100"',
            '        assert result[0].status == "success"',
            '',
        ]
        result_lines.extend(replacement_code)
        i += 1
        continue

    # --- Replace class TestParseEvidenceNewFormat entirely ---
    if 'class TestParseEvidenceNewFormat:' in line:
        skip_until_next_def = True
        replacement_code = [
            'class TestParseEvidenceNewFormat:',
            '    """新格式必须有匹配的原始 Evidence，否则条目被丢弃（审计第 7 项）。"""',
            '',
            '    def test_new_format_preserves_all_fields(self):',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [',
            '            {',
            '                "id": "e-100",',
            '                "source_tool": "test_tool",',
            '                "domain": "crypto",',
            '                "metric": "fundingRate",',
            '                "value": 0.25,',
            '                "timestamp": "2025-09-05T14:00:00Z",',
            '                "source": "coinglass",',
            '                "status": "success",',
            '                "partial": False,',
            '                "note": "High funding rate",',
            '            }',
            '        ]',
            '        original = [',
            '            Evidence(id="e-100", source_tool="test_tool", domain="crypto",',
            '                     metric="fundingRate", value=0.25, timestamp="2025-09-05T14:00:00Z",',
            '                     source="coinglass", status="success", partial=False)',
            '        ]',
            '        result = _parse_evidence(raw, original)',
            '',
            '        assert len(result) == 1',
            '        item = result[0]',
            '        assert isinstance(item, EvidenceItem)',
            '        assert item.id == "e-100"',
            '        assert item.source_tool == "test_tool"',
            '        assert item.metric == "fundingRate"',
            '        assert item.value == 0.25',
            '        assert item.partial is False',
            '',
            '    def test_unknown_id_is_dropped(self):',
            '        """无匹配原始证据 → 丢弃而非编造。"""',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [{"id": "e-nonexistent", "metric": "price", "value": 100}]',
            '        result = _parse_evidence(raw, [])',
            '        assert len(result) == 0',
            '',
            '    def test_llm_value_overridden_by_real_evidence(self):',
            '        """LLM 说的 value=999 被真实数据覆盖。"""',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [{"id": "e-100", "value": 999}]',
            '        original = [Evidence(id="e-100", value=0.25)]',
            '        result = _parse_evidence(raw, original)',
            '        assert len(result) == 1',
            '        assert result[0].value == 0.25',
            '',
            '    def test_missing_id_is_dropped(self):',
            '        """没有 id 的新格式条目无法匹配 → 丢弃。"""',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [{"metric": "price", "value": 100}]',
            '        result = _parse_evidence(raw, [Evidence(id="x")])',
            '        assert len(result) == 0',
            '',
            '    def test_unknown_keys_ignored(self):',
            '        from app.research.reasoning import _parse_evidence',
            '',
            '        raw = [',
            '            {"id": "e-200", "extra_key": "should_be_ignored", "metric": "test"}',
            '        ]',
            '        original = [Evidence(id="e-200", source_tool="t", metric="test")]',
            '        result = _parse_evidence(raw, original)',
            '        assert len(result) == 1',
            '        assert hasattr(result[0], "extra_key") is False',
            '',
        ]
        result_lines.extend(replacement_code)
        i += 1
        continue

    # --- Replace test_validate_with_new_evidence_format ---
    if 'def test_validate_with_new_evidence_format(self):' in line:
        skip_until_next_def = True
        replacement_code = [
            '    def test_validate_with_new_evidence_format(self):',
            '        ev = [',
            '            Evidence(id="e-001", source_tool="quote", domain="a_share",',
            '                     metric="price", value=3800.0, status="success"),',
            '            Evidence(id="e-002", source_tool="sentiment", domain="a_share",',
            '                     metric="risk_on", value=0.3, status="partial", partial=True),',
            '        ]',
            '        payload = self._base_payload(',
            '            evidence=[',
            '                {"id": "e-001", "source_tool": "quote", "domain": "a_share",',
            '                 "metric": "price", "value": 999.0, "status": "success"},',
            '                {"id": "e-002", "source_tool": "sentiment", "domain": "a_share",',
            '                 "metric": "risk_on", "value": 0.9, "status": "wrong_status"},',
            '            ]',
            '        )',
            '        payload = self._ensure_lists(payload, original_evidence=ev)',
            '        model = MarketIntelligence.model_validate(payload)',
            '        assert len(model.evidence) == 2',
            '        assert model.evidence[0].id == "e-001"',
            '        # LLM 说 value=999，但真实值是 3800',
            '        assert model.evidence[0].value == 3800.0',
            '        assert model.evidence[1].status == "partial"',
            '',
        ]
        result_lines.extend(replacement_code)
        i += 1
        continue

    # Handle skipping old code blocks
    if skip_until_next_def:
        if line.strip().startswith('def ') and 'def test_' in line or 'class Test' in line:
            skip_until_next_def = False
            # Process this line normally in next iteration - don't add to result yet
            # But we already added replacement above
            pass
        else:
            # Skip this line (part of old function/class)
            i += 1
            continue

    result_lines.append(line)
    i += 1

with open("tests/test_reasoning_parsing.py", "w", encoding="utf-8") as f:
    f.write("\n".join(result_lines))
print(f"Updated: wrote {len(result_lines)} lines")
