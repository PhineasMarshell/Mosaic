"""T1 回归：空载荷 / 裸标量不得被归一化成「成功证据」。

背景：上游用「200 + 空 body / {} / 纯文本」表示查不到数据时，旧逻辑只把
``raw is None`` 当失败；空容器产出 0 条 datum 却仍是 success，裸标量被
``str()`` 包成 metric="response" 的"证据"，导致 Evidence Gate 在零数据点下
报 has_evidence=True。

本文件的每个用例都满足「把对应生产代码改坏就会变红」。
"""

import pytest

from app.agent.evidence_gate import run_evidence_gate
from app.gateway.normalizer import normalize_tool_result
from app.research.evidence import build_evidence

TOOL = "get_market_quotes"


@pytest.mark.parametrize("raw", [{}, [], "", "Server busy", 0, False])
def test_empty_or_scalar_payload_is_error(raw):
    r = normalize_tool_result(TOOL, {}, raw)
    assert r.status == "error"
    assert r.normalized == []
    assert r.error


def test_none_payload_is_error():
    r = normalize_tool_result(TOOL, {}, None)
    assert r.status == "error"
    assert r.normalized == []


def test_scalar_error_keeps_original_text():
    """标量报错时 error 里应带上原文，便于定位是上游报错文本还是真空响应。"""
    r = normalize_tool_result(TOOL, {}, "Server busy, try again later")
    assert r.status == "error"
    assert "Server busy" in r.error


def test_metadata_only_dict_is_error():
    """整块 dict 只含元数据键（note/...）→ 通用路径产出 0 条 datum，
    兜底不变式必须把它强制成 error，而不是 success。"""
    r = normalize_tool_result(TOOL, {}, {"note": "upstream has nothing"})
    assert r.status == "error"
    assert r.normalized == []


def test_evidence_gate_rejects_empty_payloads():
    results = [normalize_tool_result(TOOL, {}, raw) for raw in ({}, [], "Server busy")]
    gate = run_evidence_gate(results)
    assert gate.has_evidence is False
    assert gate.successful_tools == []
    assert gate.partial_tools == []
    assert len(gate.error_tools) == 3


def test_build_evidence_does_not_fake_success_for_empty():
    """空载荷经 build_evidence 后不得出现 value="success"/"partial" 的伪证据；
    失败结果只能以 Failed: 标记呈现。"""
    results = [normalize_tool_result(TOOL, {}, raw) for raw in ({}, "Server busy")]
    evidence = build_evidence(results)
    assert evidence, "失败结果仍应留下 Failed 标记，便于排查"
    for e in evidence:
        assert e.status == "error"
        assert str(e.value).startswith("Failed:")
        assert e.value not in ("success", "partial")


def test_nonempty_payload_still_succeeds():
    """对照组：真实有数据的载荷不受影响。"""
    r = normalize_tool_result(TOOL, {}, {"price": 12.5})
    assert r.status == "success"
    assert len(r.normalized) == 1
