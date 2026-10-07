"""tests/test_normalizer_f10.py — F10 数据解析和域名修复专项测试。"""

from app.gateway.normalizer import (
    _deep_flatten_value,
    _extract_f10_list_items,
    _is_eastmoney_f10_tool,
    infer_domain_from_tool,
    normalize_tool_result,
)
from app.gateway.tool_registry import resolve_tool_by_name


class TestDomainFix:
    """验证 tool_registry.py 修复后域名正确性。"""

    def test_quote_tool_domain_is_ashare(self):
        """修复后 get_market_quotes 的 domain 应为 a_share。"""
        meta = resolve_tool_by_name("get_market_quotes")
        assert meta.domain == "a_share"

    def test_search_tool_domain_is_ashare(self):
        """修复后 search_stocks 的 domain 应为 a_share。"""
        meta = resolve_tool_by_name("search_stocks")
        assert meta.domain == "a_share"

    def test_infer_domain_quote_is_ashare(self):
        """infer_domain_from_tool 对 quote 工具应返回 a_share。"""
        assert infer_domain_from_tool("get_market_quotes") == "a_share"

    def test_infer_domain_search_is_ashare(self):
        """infer_domain_from_tool 对 search 工具应返回 a_share。"""
        assert infer_domain_from_tool("search_stocks") == "a_share"

    def test_infer_domain_f10_finance_is_ashare(self):
        """infer_domain_from_tool 对 F10 finance 工具应返回 a_share。"""
        assert infer_domain_from_tool("get_company_finance") == "a_share"

    def test_infer_domain_f10_business_is_ashare(self):
        """infer_domain_from_tool 对 F10 business 工具应返回 a_share。"""
        assert infer_domain_from_tool("get_company_business") == "a_share"

    def test_infer_domain_f10_shareholders_is_ashare(self):
        """infer_domain_from_tool 对 F10 shareholders 工具应返回 a_share。"""
        assert infer_domain_from_tool("get_company_shareholders") == "a_share"

    def test_infer_domain_detail_is_ashare(self):
        """infer_domain_from_tool 对 detail 工具应返回 a_share。"""
        assert infer_domain_from_tool("get_company_detail") == "a_share"

    def test_hk_quote_key_still_resolves(self):
        """hk_quote key 仍能正确解析到 get_market_quotes。"""
        from app.gateway.tool_registry import BY_KEY

        meta = BY_KEY["hk_quote"]
        assert meta.tool_name == "get_market_quotes"


class TestDeepFlattenValue:
    """测试 _deep_flatten_value 的深层递归能力。"""

    def test_single_level_value(self):
        """单层 value 键：{"PB": {"value": 2.5}}"""
        obj = {"PB": {"value": 2.5}}
        result = _deep_flatten_value(obj)
        assert result == 2.5

    def test_deep_nesting_indicators(self):
        """深层嵌套：{"data": {"indicators": {"ROE": {"value": 12.5}}}}"""
        obj = {"data": {"indicators": {"ROE": {"value": 12.5, "unit": "%"}}}}
        result = _deep_flatten_value(obj)
        assert result == 12.5

    def test_deep_nesting_with_row(self):
        """带 row 键的深层嵌套：{"data": {"row": [{"name": "PB", "value": 2.5}]}}"""
        obj = {"data": {"row": [{"name": "PB", "value": 2.5}]}}
        result = _deep_flatten_value(obj)
        assert result == 2.5

    def test_multiple_depth(self):
        """三层嵌套：{"a": {"b": {"c": {"value": 42}}}}"""
        obj = {"a": {"b": {"c": {"value": 42}}}}
        result = _deep_flatten_value(obj)
        assert result == 42

    def test_string_number(self):
        """字符串形式的数字：{"value": "12.5"}"""
        obj = {"value": "12.5"}
        result = _deep_flatten_value(obj)
        assert result == 12.5

    def test_non_numeric_returns_original(self):
        """非数值返回原始对象：{"name": "ICBC"}"""
        obj = {"name": "ICBC"}
        result = _deep_flatten_value(obj)
        assert result == "ICBC"

    def test_int_value(self):
        """整数值：{"count": 100}"""
        obj = {"count": 100}
        result = _deep_flatten_value(obj)
        assert result == 100

    def test_empty_dict(self):
        """空 dict 返回空 dict"""
        obj = {}
        result = _deep_flatten_value(obj)
        assert result == {}

    def test_none_value(self):
        """None 值返回原始 dict（None 不是数值，不应被提取）。"""
        obj = {"value": None}
        result = _deep_flatten_value(obj)
        # None 不是数值，函数返回原始对象
        assert result == {"value": None}


class TestExtractF10ListItems:
    """测试 _extract_f10_list_items 列表名值对提取。"""

    def test_basic_name_value_list(self):
        """基本 name/value 列表提取。"""
        result = []
        obj = [{"name": "ROE", "value": 12.5}, {"name": "PE", "value": 15.2}]
        _extract_f10_list_items(
            obj,
            "data",
            result,
            _tool="get_company_finance",
            _domain="a_share",
            _status="success",
            _partial=False,
            _timestamp=None,
            _source=None,
        )
        metrics = {d.metric: d.value for d in result}
        assert "data[0].name" in metrics
        assert metrics["data[0].name"] == "ROE"
        assert "data[0].value" in metrics
        assert metrics["data[0].value"] == 12.5
        assert "data[1].name" in metrics
        assert metrics["data[1].name"] == "PE"
        assert metrics["data[1].value"] == 15.2

    def test_shareholder_list(self):
        """股东列表提取（mixed types）。"""
        result = []
        obj = [
            {"holder_name": "张三", "holding_pct": 15.5, "shares": 123456789},
            {"holder_name": "李四", "holding_pct": 10.0, "shares": 98765432},
        ]
        _extract_f10_list_items(
            obj,
            "data",
            result,
            _tool="get_company_shareholders",
            _domain="a_share",
            _status="success",
            _partial=False,
            _timestamp=None,
            _source=None,
        )
        metrics = {d.metric: d.value for d in result}
        # 字符串字段
        assert metrics["data[0].holder_name"] == "张三"
        assert metrics["data[1].holder_name"] == "李四"
        # 数值字段
        assert metrics["data[0].holding_pct"] == 15.5
        assert metrics["data[1].holding_pct"] == 10.0
        assert metrics["data[0].shares"] == 123456789
        assert metrics["data[1].shares"] == 98765432

    def test_non_list_returns_empty(self):
        """非列表输入返回空结果。"""
        result = []
        _extract_f10_list_items(
            "not a list",
            "data",
            result,
            _tool="test",
            _domain="a_share",
            _status="success",
            _partial=False,
            _timestamp=None,
            _source=None,
        )
        assert result == []

    def test_mixed_item_types(self):
        """混合类型列表（dict 和 non-dict）。"""
        result = []
        obj = [
            {"name": "ROE", "value": 12.5},
            "not a dict",
            {"name": "PE", "value": 15.2},
        ]
        _extract_f10_list_items(
            obj,
            "data",
            result,
            _tool="test",
            _domain="a_share",
            _status="success",
            _partial=False,
            _timestamp=None,
            _source=None,
        )
        # 只提取 dict 项
        names = [d.value for d in result if d.metric.endswith(".name")]
        assert names == ["ROE", "PE"]


class TestIsEastmoneyF10Tool:
    """测试 _is_eastmoney_f10_tool 函数。"""

    def test_finance_tool(self):
        assert _is_eastmoney_f10_tool("get_company_finance") is True

    def test_business_tool(self):
        assert _is_eastmoney_f10_tool("get_company_business") is True

    def test_shareholders_tool(self):
        assert _is_eastmoney_f10_tool("get_company_shareholders") is True

    def test_concept_tool(self):
        assert _is_eastmoney_f10_tool("get_company_concepts") is True

    def test_survey_tool(self):
        assert _is_eastmoney_f10_tool("get_company_survey") is True

    def test_detail_tool(self):
        assert _is_eastmoney_f10_tool("get_company_detail") is True

    def test_overview_tool(self):
        assert _is_eastmoney_f10_tool("get_company_overview") is True

    def test_non_f10_tool(self):
        assert _is_eastmoney_f10_tool("get_market_quotes") is False

    def test_non_f10_market_tool(self):
        assert _is_eastmoney_f10_tool("get_market_klines") is False


class TestF10NormalizeResult:
    """测试 F10 数据的 normalize_tool_result 输出。"""

    def test_deeply_nested_indicators(self):
        """多个兄弟指标必须全部保留，且 metric 可辨识、unit 合并进 value datum。"""
        raw = {
            "source": "eastmoney",
            "section": "finance",
            "symbol": "601398",
            "data": {
                "indicators": {
                    "ROE": {"value": 12.5, "unit": "%"},
                    "PE": {"value": 15.2, "unit": "x"},
                    "PB": {"value": 2.5, "unit": "x"},
                }
            },
        }
        result = normalize_tool_result("get_company_finance", {"symbol": "601398"}, raw)
        assert result.status == "success"
        by_metric = {d.metric: d for d in result.normalized}
        # T2：以前整块 flatten 只保留第一个指标，PE/PB 永久丢失。
        assert by_metric["data.indicators.ROE.value"].value == 12.5
        assert by_metric["data.indicators.PE.value"].value == 15.2
        assert by_metric["data.indicators.PB.value"].value == 2.5
        # T2：同级 unit 合并进 value datum，不再产出独立的 .unit 字符串指标。
        assert by_metric["data.indicators.ROE.value"].unit == "%"
        assert by_metric["data.indicators.PE.value"].unit == "x"
        assert not any(m.endswith(".unit") for m in by_metric)
        assert len(result.normalized) >= 3

    def test_single_indicator_still_works(self):
        """单指标形状 {"data":{"indicators":{"PB":{"value":2.5}}}} 仍能提取且保留指标名。"""
        raw = {"data": {"indicators": {"PB": {"value": 2.5}}}}
        result = normalize_tool_result("get_company_finance", {"symbol": "601398"}, raw)
        assert result.status == "success"
        by_metric = {d.metric: d.value for d in result.normalized}
        assert by_metric["data.indicators.PB.value"] == 2.5

    def test_name_value_list(self):
        """name/value 列表应提取出可读指标。"""
        raw = {
            "source": "eastmoney",
            "section": "business",
            "symbol": "601398",
            "data": [
                {"name": "company_name", "value": "工商银行"},
                {"name": "industry", "value": "银行业"},
                {"name": "revenue", "value": 523456.78},
            ],
        }
        result = normalize_tool_result("get_company_business", {"symbol": "601398"}, raw)
        assert result.status == "success"
        metrics = {d.metric: d.value for d in result.normalized}
        # _extract_f10_list_items 提取 key name 为 metric 名
        # data[0].name = "company_name", data[0].value = "工商银行"
        assert any("data[0].name" in m for m in metrics)
        assert metrics.get("data[0].name") == "company_name"
        assert any("data[0].value" in m for m in metrics)
        assert metrics.get("data[0].value") == "工商银行"
        # revenue 是数值字段，应被提取为 float
        assert any("data[2].value" in m for m in metrics)
        assert metrics.get("data[2].value") == 523456.78

    def test_domain_is_ashare_for_f10(self):
        """F10 工具的 domain 应为 a_share。"""
        raw = {"data": {"test": 1}}
        result = normalize_tool_result("get_company_finance", {"symbol": "601398"}, raw)
        assert result.normalized[0].domain == "a_share"
