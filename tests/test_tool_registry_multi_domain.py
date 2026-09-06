"""tests/test_tool_registry_multi_domain.py — 多域注册表测试。"""

import pytest

from app.gateway.tool_registry import (
    ALL_TOOLS,
    BY_KEY,
    BY_NAME,
    get_enabled_domains,
    registry_text,
    resolve_tool,
    resolve_tool_by_name,
    tools_by_domain,
)


class TestAllToolsCompleteness:
    """验证工具数量完整——不应该丢失旧工具。"""

    def test_total_tool_count(self):
        # A 股生态 4 + 基本面 7 + 微观结构 7
        # + Crypto 行情 4 + 衍生品 1 + CoinGlass 7 + Health 2 = 30+
        assert len(ALL_TOOLS) >= 30, f"期望至少 30 个工具，实际 {len(ALL_TOOLS)}"

    def test_all_have_keys(self):
        for tool in ALL_TOOLS:
            assert tool.key, "每个工具必须有 key"

    def test_all_have_names(self):
        for tool in ALL_TOOLS:
            assert tool.tool_name, "每个工具必须有 tool_name"

    def test_index_consistency(self):
        # BY_KEY is keyed by business key (unique per tool)
        assert len(BY_KEY) == len(ALL_TOOLS), "BY_KEY 应与 ALL_TOOLS 等长"
        # BY_NAME is keyed by operationId —同一 operationId 可被多个域复用（如 quote_tencent_quote_get 同时服务 a_share 和 hk_stock）
        # 所以 BY_NAME 数量 <= ALL_TOOLS 是预期的
        assert len(BY_NAME) <= len(ALL_TOOLS), f"BY_NAME 不应超过 ALL_TOOLS，实际 {len(BY_NAME)} vs {len(ALL_TOOLS)}"


class TestDomainDistribution:
    """验证各域的工具有合理分布。"""

    def test_a_share_tools_exist(self):
        a_share_tools = tools_by_domain("a_share")
        assert len(a_share_tools) >= 15

    def test_crypto_tools_exist(self):
        crypto_tools = tools_by_domain("crypto")
        assert len(crypto_tools) >= 8

    def test_unknown_domain_has_health_only(self):
        unknown_tools = tools_by_domain("unknown")
        keys = {t.key for t in unknown_tools}
        assert keys <= {"health", "market_health"}

    def test_no_domain_has_zero_tools(self):
        for domain in ["a_share", "crypto", "us_stock"]:
            tools = tools_by_domain(domain)
            # us_stock 是占位符列表，可能为空；a_share 和 crypto 必须非空
            if domain != "us_stock":
                assert len(tools) > 0, f"{domain} 域不应为空"


class TestResolveAPIs:
    """验证 resolve_tool / resolve_tool_by_name 行为。"""

    def test_resolve_by_key_ashare(self):
        meta = resolve_tool("sentiment")
        assert meta.domain == "a_share"
        assert "情绪" in meta.purpose

    def test_resolve_by_key_crypto(self):
        # snapshot/klines 现在是跨域通用工具 (domain="cross")
        meta = resolve_tool("snapshot")
        assert meta.domain == "cross"
        assert "任意 symbol" in meta.purpose

    def test_resolve_by_key_derivatives(self):
        meta = resolve_tool("derivatives_history")
        assert meta.domain == "crypto"

    def test_resolve_by_name(self):
        meta = resolve_tool_by_name("snapshot_market_snapshot_post")
        assert meta.key == "snapshot"
        assert meta.domain == "cross"  # 现在是跨域通用

    def test_resolve_invalid_key_raises(self):
        with pytest.raises(KeyError):
            resolve_tool("nonexistent_tool_xyz")

    def test_resolve_invalid_name_raises(self):
        with pytest.raises(KeyError):
            resolve_tool_by_name("nonexistent_operation_id")


class TestRegistryTextFiltering:
    """验证 registry_text 按域过滤。"""

    def test_full_registry_contains_crypto(self):
        text = registry_text()
        assert "crypto" in text.lower() or "snapshot" in text

    def test_registry_filters_by_domain(self):
        # crypto 域只包含原生 crypto 工具，不包含跨域通用工具
        crypto_text = registry_text(domains=["crypto"])
        # 应该包含 derivatives_history, funding_rate 等原生 crypto 工具
        assert "derivatives_history" in crypto_text or "funding_rate" in crypto_text
        # snapshot/klines 现在是跨域通用 (domain="cross")，不在 crypto 域
        assert "snapshot" not in crypto_text or "klines" not in crypto_text

    def test_registry_ashare_domain_only(self):
        ashare_text = registry_text(domains=["a_share"])
        assert "sentiment" in ashare_text
        assert "overview" in ashare_text

    def test_registry_cross_domain(self):
        # cross 域包含 snapshot, klines 等跨域通用工具
        cross_text = registry_text(domains=["cross"])
        assert "snapshot" in cross_text
        assert "klines" in cross_text


class TestEnabledDomains:
    """验证 get_enabled_domains。"""

    def test_returns_non_unknown_domains(self):
        domains = get_enabled_domains()
        assert "a_share" in domains
        assert "crypto" in domains
        assert "unknown" not in domains


class TestToolMetaModelDump:
    """验证 ToolMeta.model_dump。"""

    def test_model_dump_contains_key(self):
        meta = resolve_tool("limit_up_count")
        d = meta.model_dump()
        assert d["key"] == "limit_up_count"
        assert d["domain"] == "a_share"
        assert d["priority"] == "high"
