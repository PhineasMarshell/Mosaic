"""tests/test_tool_registry_multi_domain.py — 多域注册表测试。

T24 契约（本文件同时吸收 §4/T34 里归属注册表的断言修复）：
- ``registry_text(domains=...)`` 按 domain 过滤、**始终附加** health 工具
  （domain="unknown"），域过滤为空时**只返回 health**（不再 ``or ALL_TOOLS``
  静默回退全量，且必须留 warning）；
- hk_quote / hk_search 的 domain 是 ``hk_stock``、category 与 A 股同类工具对齐；
- BY_NAME 每个 operationId 只有一个**规范条目**（cross 优先，否则先注册者），
  复用同一 operationId 的占位条目显式暴露在 ``SHARED_BY_NAME``，
  不再"后注册者静默胜出"。
"""

import logging

import pytest

from app.gateway.tool_registry import (
    ALL_TOOLS,
    BY_KEY,
    BY_NAME,
    SHARED_BY_NAME,
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
        # T24：同一 operationId 可被多个域复用（hk/commodities 占位复用 quote/search/
        # klines/snapshot），复用条目显式进 SHARED_BY_NAME；BY_NAME 本身无重复键，
        # 且 规范条目 + 共享条目 必须恰好覆盖全部注册条目（不丢也不重）
        assert len(BY_NAME) == len({t.tool_name for t in ALL_TOOLS}), (
            "BY_NAME 不应有重复键（后注册者静默胜出已被 T24 废除）"
        )
        shared_total = sum(len(v) for v in SHARED_BY_NAME.values())
        assert len(BY_NAME) + shared_total == len(ALL_TOOLS), (
            f"BY_NAME({len(BY_NAME)}) + SHARED({shared_total}) 应等于 ALL_TOOLS({len(ALL_TOOLS)})"
        )

    def test_shared_by_name_canonical_resolution(self):
        """规范条目选取：cross 优先，否则先注册者（A 股原生工具）。"""
        # hk 占位复用 A 股 quote/search 的 operationId —— 规范条目是 A 股原生工具
        assert BY_NAME["get_market_quotes"].key == "quote"
        assert BY_NAME["get_market_quotes"].domain == "a_share"
        assert BY_NAME["search_stocks"].key == "search"
        # commodities 占位复用 cross 的 klines/snapshot —— 规范条目是 cross 通用工具
        assert BY_NAME["get_market_klines"].key == "klines"
        assert BY_NAME["get_market_klines"].domain == "cross"
        assert BY_NAME["get_market_snapshot"].domain == "cross"
        # 占位条目在 SHARED_BY_NAME 里可查
        assert {m.key for m in SHARED_BY_NAME["get_market_quotes"]} == {"hk_quote"}
        assert {m.key for m in SHARED_BY_NAME["get_market_snapshot"]} == {
            "commodity_silver",
            "commodity_platinum",
        }


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
        meta = resolve_tool_by_name("get_market_snapshot")
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
        # T34：旧断言 `A or B` 恒真——snapshot 与 klines 都不在 crypto 域，
        # 两个条件必须**同时**成立才说明 cross 工具确实没漏进来
        assert "snapshot" not in crypto_text and "klines" not in crypto_text

    def test_registry_ashare_domain_only(self):
        ashare_text = registry_text(domains=["a_share"])
        assert "sentiment" in ashare_text
        assert "overview" in ashare_text

    def test_registry_cross_domain(self):
        # cross 域包含 snapshot, klines 等跨域通用工具
        cross_text = registry_text(domains=["cross"])
        assert "snapshot" in cross_text
        assert "klines" in cross_text

    def test_us_stock_excludes_other_domains_and_includes_health(self):
        """T24：us_stock 尚未接入工具——旧实现 `or ALL_TOOLS` 静默回退成全量 40 个；
        现在必须只返回 health 工具（domain=unknown），绝不混入其它域。"""
        us_text = registry_text(domains=["us_stock"])
        lines = us_text.splitlines()
        assert lines, "us_stock 域不应返回空文本"
        assert not any("[a_share]" in line or "[crypto]" in line or "[hk_stock]" in line for line in lines)
        assert any("[unknown]" in line for line in lines), "health 工具应始终包含"

    def test_unknown_domain_returns_health_only_with_warning(self, caplog):
        """T24：未知域 → 只有 health + warning，绝不等于全量。"""
        with caplog.at_level(logging.WARNING, logger="app.gateway.tool_registry"):
            text = registry_text(domains=["bogus"])
        lines = text.splitlines()
        assert lines and all("[unknown]" in line for line in lines)
        assert "no tools for domains" in caplog.text

    def test_hk_stock_includes_hk_tools_and_health(self):
        """T24：hk 域包含 hk 工具（修完 domain 漂移后）且 health 始终在场。"""
        hk_text = registry_text(domains=["hk_stock"])
        assert "hk_northbound_daily" in hk_text
        assert "hk_quote" in hk_text and "[hk_stock]" in hk_text
        assert any("[unknown]" in line for line in hk_text.splitlines())


class TestHkToolDomain:
    """T24：HK 工具的 domain/category 漂移修复。"""

    def test_hk_quote_and_search_domain_is_hk_stock(self):
        assert resolve_tool("hk_quote").domain == "hk_stock"
        assert resolve_tool("hk_search").domain == "hk_stock"

    def test_hk_search_category_aligned_with_ashare_search(self):
        assert resolve_tool("hk_search").category == resolve_tool("search").category
        assert resolve_tool("hk_quote").category == resolve_tool("quote").category

    def test_hk_tools_not_leaked_into_ashare_domain(self):
        ashare_keys = {t.key for t in tools_by_domain("a_share")}
        assert "hk_quote" not in ashare_keys
        assert "hk_search" not in ashare_keys


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
