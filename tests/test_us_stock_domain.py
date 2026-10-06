"""tests/test_us_stock_domain.py — 美股域注册表与域过滤测试。

US_STOCK_INTEGRATION_PLAN §A3：
- A0 实测（2026-10-06）：雪球通道 klines/window 对美股裸代码可用，
  snapshot 服务端 422「不支持实时快照」→ 只注册 us_klines / us_window（2 条）；
- 注册表含 us_stock 域条目、key 无重复、http_path 与 allowed_openapi.json 匹配；
- T24 行为回归：registry_text(domains=["us_stock"]) 含 us_* 条目 + health 工具，
  不含其他域工具；
- critic 接线：_DOMAIN_RULES["us_stock"] 存在且非空。

注意：不写任何需要真实 Gateway 的集成测试（网络依赖违反测试纪律）；
A0 的探针 scripts/verify_us_market.py 是一次性验证工具，不进测试套件。
"""

import json
from pathlib import Path

import pytest

from app.gateway.tool_registry import (
    ALL_TOOLS,
    registry_text,
    resolve_tool,
    tools_by_domain,
)
from app.graph.nodes.critic import _DOMAIN_RULES
from app.models.research import DEFAULT_DOMAINS, MarketDomain

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_allowed_openapi() -> dict:
    """allowed_openapi.json 头尾带 markdown 代码围栏，解析前剥掉。"""
    raw = (_REPO_ROOT / "allowed_openapi.json").read_text(encoding="utf-8")
    return json.loads(raw.strip().strip("`").strip())


def _us_stock_tools():
    return tools_by_domain("us_stock")


class TestUsStockRegistry:
    """注册表含美股域条目，且与 allowed 端点匹配。"""

    def test_us_stock_domain_has_entries(self):
        # A0 部分通过：snapshot 不支持雪球通道，只注册 klines + window（2 条）
        tools = _us_stock_tools()
        assert len(tools) == 2, f"期望 2 个美股工具（us_klines/us_window），实际 {[t.key for t in tools]}"
        assert {t.key for t in tools} == {"us_klines", "us_window"}

    def test_us_stock_keys_not_duplicated(self):
        keys = [t.key for t in ALL_TOOLS]
        us_keys = [t.key for t in _us_stock_tools()]
        assert len(us_keys) == len(set(us_keys)), "美股工具 key 重复"
        assert len(keys) == len(set(keys)), "美股条目导致全局 key 冲突"

    def test_http_path_matches_allowed_openapi(self):
        """us_* 条目的 http_method/http_path 必须是 allowed_openapi.json 里真实存在的 POST 端点。"""
        spec = _load_allowed_openapi()
        for tool in _us_stock_tools():
            op = spec.get("paths", {}).get(tool.http_path, {}).get(tool.http_method.lower(), {})
            assert op, f"{tool.key}: {tool.http_method} {tool.http_path} 不在 allowed_openapi.json 中"
            assert op.get("operationId") == tool.tool_name, (
                f"{tool.key}: operationId {tool.tool_name} 与 allowed 端点的 {op.get('operationId')} 不一致"
            )

    def test_no_snapshot_tool_registered(self):
        """A0 实测：/market/snapshot 不支持 exchange=xueqiu，不得注册美股快照工具。"""
        keys = {t.key for t in _us_stock_tools()}
        assert "us_snapshot" not in keys

    def test_docstring_tool_count_matches_all_tools(self):
        """模块 docstring 计数必须与 len(ALL_TOOLS) 一致（新增条目后同步 +2）。"""
        import re

        import app.gateway.tool_registry as registry

        declared = re.search(r"所有 (\d+) 个 Market Gateway Tool", registry.__doc__)
        assert declared, "tool_registry.py 模块 docstring 的工具数声明丢失"
        assert int(declared.group(1)) == len(ALL_TOOLS)


class TestUsStockDomainFilter:
    """T24 行为回归：域过滤不为空且只含本域 + health。"""

    def test_registry_text_contains_us_tools_and_health(self):
        text = registry_text(domains=["us_stock"])
        assert "us_klines" in text
        assert "us_window" in text
        # health 工具（domain=unknown）始终附加
        assert "health" in text
        assert "market_health" in text

    def test_registry_text_excludes_other_domains(self):
        text = registry_text(domains=["us_stock"])
        # 其他域的代表性工具不得出现
        for other_key in ("sentiment", "limit_up_count", "funding_rate", "commodity_gold"):
            assert other_key not in text, f"us_stock 域过滤泄漏了其他域工具: {other_key}"

    def test_registry_text_no_empty_fallback_warning(self, caplog):
        """us_stock 已有工具，不应触发『域过滤为空』的 warning 分支。"""
        import logging

        with caplog.at_level(logging.WARNING, logger="app.gateway.tool_registry"):
            registry_text(domains=["us_stock"])
        assert "no tools for domains" not in caplog.text


class TestResolveUsStockTool:
    """resolve_tool 各字段正确。"""

    @pytest.mark.parametrize(
        ("key", "tool_name", "http_path", "category", "priority"),
        [
            ("us_klines", "klines_market_klines_post", "/market/klines", "technical", "high"),
            ("us_window", "window_market_window_post", "/market/window", "technical", "medium"),
        ],
    )
    def test_resolve_fields(self, key, tool_name, http_path, category, priority):
        meta = resolve_tool(key)
        assert meta.key == key
        assert meta.tool_name == tool_name
        assert meta.domain == "us_stock"
        assert meta.http_method == "POST"
        assert meta.http_path == http_path
        assert meta.category == category
        assert meta.priority == priority


class TestCriticUsStockRules:
    """critic 美股审查规则接线。"""

    def test_domain_rules_contains_us_stock(self):
        assert "us_stock" in _DOMAIN_RULES
        rules = _DOMAIN_RULES["us_stock"]
        assert rules, "us_stock 审查规则为空"
        # 规则必须引用美股工具 key（断言与数据绑定）
        assert "us_klines" in rules
        assert "us_window" in rules


class TestMarketModelUsStock:
    """防回归：us_stock 必须在 MarketDomain 与 DEFAULT_DOMAINS 中。"""

    def test_market_domain_literal_contains_us_stock(self):
        args = getattr(MarketDomain, "__args__", ())
        assert "us_stock" in args, "MarketDomain Literal 丢了 us_stock"

    def test_default_domains_contains_us_stock(self):
        assert "us_stock" in DEFAULT_DOMAINS
