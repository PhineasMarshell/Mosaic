"""T36 文档契约 — 文档里写的必须和代码实际行为一致。

背景（T36）：审计要求"不再存在'文档承诺、代码没有'的项"。本文件把几条最容易漂的
承诺做成**跨代码/文档的机械交叉断言**：一边读真实实现（``build_evidence`` 生成的
evidence id、``_MAX_EVIDENCE_ITEMS``、``Verdict`` 字面量、``ALL_TOOLS`` 数量、
``build_response_from_state`` 的 anomalies 来源、``pyproject``/``requirements`` 的
mcp 版本区间），一边读 ``docs/architecture.md`` / ``README.md`` /
``app/agent/prompts.py`` 的文字，断言两者对得上。

把文档改回旧说法（或让代码漂走），对应用例必红。

mock 范围声明：**不 mock 任何东西**——只读源码/文档文本，加上一次真实的
``build_evidence`` 调用（纯内存，不触网、不落库）。因此不覆盖"文档描述的运行时
行为是否真的正确"（那要跑 LLM），只保证"文档里的数字与字面量和代码一致"。
"""

import re
import tomllib
from pathlib import Path

import pytest

from app.gateway.tool_registry import ALL_TOOLS
from app.models.market import ToolResult
from app.research.evidence import _MAX_EVIDENCE_ITEMS, build_evidence

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
PROMPTS = (REPO_ROOT / "app" / "agent" / "prompts.py").read_text(encoding="utf-8")
REGISTRY = (REPO_ROOT / "app" / "gateway" / "tool_registry.py").read_text(encoding="utf-8")
CRITIC = (REPO_ROOT / "app" / "graph" / "nodes" / "critic.py").read_text(encoding="utf-8")
RESPONSE = (REPO_ROOT / "app" / "models" / "response.py").read_text(encoding="utf-8")
DOCS = ARCHITECTURE + README


# ── 1. evidence id 格式（T4 / T36）──────────────────────────────


def test_prompt_evidence_id_example_matches_real_generated_format():
    """prompts.py 的 id 示例必须是 build_evidence 真正生成的格式，不是旧的 evidence-NNN。"""
    evidence = build_evidence(
        [
            ToolResult(
                tool="get_ashare_sentiment",
                arguments={},
                status="error",
                error="upstream unavailable",
            )
        ],
        id_prefix="technical",
    )
    real_id = evidence[0].id
    assert re.fullmatch(r"technical-\d{3}", real_id), f"build_evidence 生成的 id 格式变了：{real_id!r}"
    assert "evidence-001" not in PROMPTS, (
        "app/agent/prompts.py 仍在用旧的 evidence-001 示例，实际格式是 {analyst category}-NNN（T4 之后）"
    )
    assert real_id in PROMPTS, f"prompts.py 的示例 id 应与真实生成的 {real_id!r} 一致，否则 LLM 会被教错格式"


# ── 2. 工具总数（T36）───────────────────────────────────────────


def test_registry_docstring_tool_count_matches_actual():
    """tool_registry 模块 docstring 声明的工具数必须等于 len(ALL_TOOLS)。"""
    declared = re.search(r"所有 (\d+) 个 Market Gateway Tool", REGISTRY)
    assert declared, "tool_registry.py 模块 docstring 里的工具数声明被删了"
    assert int(declared.group(1)) == len(ALL_TOOLS), (
        f"tool_registry docstring 说 {declared.group(1)} 个工具，实际 {len(ALL_TOOLS)} 个"
    )


# ── 3. Critic 内部 error 语义（T11 / T36）───────────────────────


def test_docs_document_critic_internal_error_verdict():
    """文档必须写清 Critic 的内部 error 裁决，且代码里确实存在这个 verdict。"""
    assert '"error"' in CRITIC and "Verdict" in CRITIC, "critic.py 已经没有内部 error 裁决了，文档需同步"
    for name, text in (("docs/architecture.md", ARCHITECTURE), ("README.md", README)):
        assert "error" in text, f"{name} 未提及 Critic 的 error 裁决"
    assert "安全终止" in ARCHITECTURE and "安全终止" in README, "两份文档都应写明 error 裁决是安全终止，而不是当成 pass"


# ── 4. 证据条数上限（T6 / T36）──────────────────────────────────


def test_docs_evidence_cap_matches_constant():
    """文档写的证据条数上限必须等于 _MAX_EVIDENCE_ITEMS。"""
    assert _MAX_EVIDENCE_ITEMS == 80, f"上限常量变成 {_MAX_EVIDENCE_ITEMS} 了，测试与文档需同步"
    assert re.search(r"硬上限\s*80", DOCS), "docs/README 未记录「证据链硬上限 80」这条承诺"
    assert "build_evidence" in DOCS, "文档应指明截断发生在 build_evidence"


# ── 5. anomalies 由代码填（D3 / T36）────────────────────────────


def test_docs_claim_anomalies_is_code_filled_and_code_agrees():
    """文档承诺"anomalies 由代码填"，代码必须真的在 build_response_from_state 里调 detect_anomalies。"""
    assert "detect_anomalies" in RESPONSE, "build_response_from_state 不再调用 detect_anomalies，文档需同步"
    assert 'report["anomalies"]' in RESPONSE or "report.anomalies" in RESPONSE, "anomalies 覆盖写入的代码形态变了"
    for name, text in (("docs/architecture.md", ARCHITECTURE), ("README.md", README)):
        assert "detect_anomalies" in text, f"{name} 未说明报告的 anomalies 字段由 detect_anomalies 代码填充（D3）"
        assert "anomalies" in text and "表" in text, f"{name} 应区分报告的 anomalies 字段与存储的 anomalies 表"


# ── 6. Market Memory 的真实落盘位置（T36，P5 迁移遗留）──────────


@pytest.mark.parametrize(
    "stale_path",
    ["~/.mosaic/memory/daily", "~/.mosaic/memory/anomalies", "~/.mosaic/memory/research"],
)
def test_docs_do_not_promise_removed_mosaic_home_paths(stale_path: str):
    """`~/.mosaic/memory/...` 三个目录在 P5 之后已不存在，文档不得再当现有路径承诺。"""
    for name, text in (("docs/architecture.md", ARCHITECTURE), ("README.md", README)):
        assert stale_path not in text, f"{name} 仍在承诺已不存在的路径 {stale_path}（实际是 memory/memory.db）"


def test_docs_point_at_the_real_sqlite_location():
    """文档必须指向代码真实使用的 memory/memory.db 与四张表。"""
    storage = (REPO_ROOT / "app" / "memory" / "storage.py").read_text(encoding="utf-8")
    assert '"memory" / "memory.db"' in storage, "storage.py 的默认落盘路径变了，文档需同步"
    for table in ("daily_states", "anomalies", "research_records", "conversations"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in storage, f"表 {table} 不存在了"
        assert table in ARCHITECTURE, f"docs/architecture.md 的存储表清单缺 {table}"
    assert "memory/memory.db" in ARCHITECTURE and "memory/memory.db" in README, "两份文档都应指向 memory/memory.db"


# ── 7. MCP 依赖版本区间（D1 / T36）──────────────────────────────


def test_docs_mcp_version_range_matches_manifests():
    """文档写的 mcp 版本区间必须与 pyproject.toml 和 requirements.txt 完全一致。"""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = next(dep for dep in pyproject["project"]["dependencies"] if dep.startswith("mcp"))
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert spec == "mcp>=2,<3", f"pyproject 的 mcp 依赖变成 {spec} 了，文档需同步"
    assert spec in requirements, "requirements.txt 与 pyproject 的 mcp 区间不一致（T12b 已修，别回退）"
    assert spec in DOCS, f"docs/README 未记录 MCP 依赖区间 {spec}（D1）"
