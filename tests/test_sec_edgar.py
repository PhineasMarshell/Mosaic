"""tests/test_sec_edgar.py — SEC EDGAR 美股基本面接入测试。

SEC_EDGAR_PLAN §A6：
- 纯函数直测（对齐 test_hk_northbound 模式）：_pick_concept / _summarize_metric /
  _parse_recent_filings / _parse_ticker_map，不真连网；
- 注册表断言：us_stock 域含 2 条新工具（category=fundamental、INTERNAL、key 无冲突）；
- 分发接线：monkeypatch 打在 app.graph.tool_runtime 的导入名上（T17T 教训——
  打实例属性无效），并断言未触碰 Gateway（参考 test_gateway_reuse）；
- 配置门闩：SEC_EDGAR_CONTACT 为空 → 直接 error、不发起网络请求；
- critic 接线：_DOMAIN_RULES["us_stock"] 含 us_fundamentals 字样。

A0 实测（2026-10-06）背景：EPS 单位是 USD/shares；营收 tag 有新旧口径
（AAPL 的 Revenues 停在 2018、NVDA 的 RFCWC 停在 2022）→ _summarize_metric
按最新 end 选优而非顺序回退。
"""

from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.gateway.tool_registry import (
    ALL_TOOLS,
    BY_KEY,
    registry_text,
    resolve_tool_by_name,
    tools_by_domain,
)
from app.graph.nodes.critic import _DOMAIN_RULES
from app.graph.tool_runtime import ToolRuntime
from app.research.sec_edgar import (
    _build_headers,
    _parse_recent_filings,
    _parse_ticker_map,
    _pick_concept,
    _summarize_metric,
)

# ------------------------------------------------------------------ #
# 造数工厂                                                             #
# ------------------------------------------------------------------ #


def _units(entries: list[dict], unit: str = "USD") -> dict:
    return {unit: entries}


def _concept_entries() -> list[dict]:
    """一个概念的历史申报值：年报 2 条 + 季报 2 条 + 干扰条目。"""
    return [
        {
            "start": "2023-10-01",
            "end": "2024-09-28",
            "val": 100.0,
            "fy": 2024,
            "fp": "FY",
            "form": "10-K",
            "filed": "2024-11-01",
        },
        {
            "start": "2024-10-01",
            "end": "2025-09-27",
            "val": 200.0,
            "fy": 2025,
            "fp": "FY",
            "form": "10-K",
            "filed": "2025-10-31",
        },
        {
            "start": "2025-04-01",
            "end": "2025-06-28",
            "val": 30.0,
            "fy": 2025,
            "fp": "Q3",
            "form": "10-Q",
            "filed": "2025-08-01",
        },
        {
            "start": "2025-07-01",
            "end": "2025-09-27",
            "val": 40.0,
            "fy": 2025,
            "fp": "Q4",
            "form": "10-Q",
            "filed": "2025-10-31",
        },
        {
            "start": "2025-07-01",
            "end": "2025-09-27",
            "val": 999.0,
            "fy": 2025,
            "fp": "FY",
            "form": "8-K",
            "filed": "2025-10-01",
        },
    ]


# ------------------------------------------------------------------ #
# 纯函数：_build_headers / _parse_ticker_map                           #
# ------------------------------------------------------------------ #


class TestBuildHeaders:
    def test_contains_contact(self):
        headers = _build_headers("YourName you@example.com")
        assert "YourName you@example.com" in headers["User-Agent"]

    def test_ua_template_never_hardcodes_email(self):
        """UA 模板本身不得内置任何邮箱（SEC 身份声明只能来自配置）。"""
        import app.research.sec_edgar as mod

        assert "@" not in mod._EDGAR_UA_BASE


class TestParseTickerMap:
    def test_parses_official_shape(self):
        payload = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Inc."},
        }
        mapping = _parse_ticker_map(payload)
        assert mapping == {"AAPL": 320193, "NVDA": 1045810}

    def test_normalizes_case_and_skips_junk(self):
        payload = {
            "0": {"cik_str": 1, "ticker": " aapl "},
            "1": {"ticker": "NO_CIK"},
            "2": {"cik_str": "not-int", "ticker": "BAD"},
            "3": "not-a-dict",
        }
        assert _parse_ticker_map(payload) == {"AAPL": 1}

    def test_non_dict_payload(self):
        assert _parse_ticker_map(None) == {}
        assert _parse_ticker_map([]) == {}


# ------------------------------------------------------------------ #
# 纯函数：_pick_concept（年报/季报选取、form 过滤、单位回退）           #
# ------------------------------------------------------------------ #


class TestPickConcept:
    def test_picks_latest_annual(self):
        picked = _pick_concept(_units(_concept_entries()), "10-K")
        assert picked["value"] == 200.0
        assert picked["end"] == "2025-09-27"
        assert picked["form"] == "10-K"
        assert picked["filed"] == "2025-10-31"

    def test_picks_latest_quarterly(self):
        picked = _pick_concept(_units(_concept_entries()), "10-Q")
        assert picked["value"] == 40.0
        assert picked["end"] == "2025-09-27"

    def test_filters_out_other_forms(self):
        # 只有 8-K 条目 → 10-K/10-Q 都取不到
        units = _units([{"end": "2025-09-27", "val": 1.0, "form": "8-K", "filed": "2025-10-01"}])
        assert _pick_concept(units, "10-K") is None
        assert _pick_concept(units, "10-Q") is None

    def test_eps_unit_fallback_usd_shares(self):
        """A0 实测：EPS 的单位键是 USD/shares 而非 USD。"""
        units = _units(
            [{"end": "2025-09-27", "val": 6.08, "form": "10-K", "filed": "2025-10-31"}],
            unit="USD/shares",
        )
        picked = _pick_concept(units, "10-K")
        assert picked is not None
        assert picked["value"] == 6.08

    def test_empty_or_missing_units(self):
        assert _pick_concept({}, "10-K") is None
        assert _pick_concept({"USD": []}, "10-K") is None
        assert _pick_concept({"USD": None}, "10-K") is None
        assert _pick_concept(None, "10-K") is None  # type: ignore[arg-type]

    def test_invalid_entries_skipped(self):
        units = _units(
            [
                {"end": "2025-09-27", "form": "10-K", "filed": "2025-10-31"},  # 缺 val
                {"end": "", "val": 1.0, "form": "10-K", "filed": "2025-10-31"},  # 缺 end
                "not-a-dict",
            ]
        )
        assert _pick_concept(units, "10-K") is None

    def test_prefers_usd_over_shares_when_both(self):
        units = {
            "USD/shares": [{"end": "2025-09-27", "val": 6.0, "form": "10-K", "filed": "2025-10-31"}],
            "USD": [{"end": "2025-09-27", "val": 100.0, "form": "10-K", "filed": "2025-10-31"}],
        }
        assert _pick_concept(units, "10-K")["value"] == 100.0


# ------------------------------------------------------------------ #
# 纯函数：_summarize_metric（候选 tag 回退/选优）                       #
# ------------------------------------------------------------------ #


class TestSummarizeMetric:
    def test_picks_fresher_tag_not_first_candidate(self):
        """A0 实测形态：首选 tag 存在但陈旧（AAPL Revenues 停在 2018），
        必须让位给数据更新的候选 tag。"""
        stale = {"units": _units([{"end": "2018-09-29", "val": 265.0, "form": "10-K", "filed": "2018-11-05"}])}
        fresh = {
            "units": _units(
                [
                    {"end": "2025-09-27", "val": 416.0, "form": "10-K", "filed": "2025-10-31"},
                    {"end": "2025-06-28", "val": 85.0, "form": "10-Q", "filed": "2025-08-01"},
                ]
            )
        }
        result = _summarize_metric({"Revenues": stale, "RevenueFromContractWithCustomerExcludingAssessedTax": fresh})
        assert result is not None
        assert result["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert result["annual"]["value"] == 416.0
        assert result["quarterly"]["value"] == 85.0

    def test_first_candidate_wins_on_tie(self):
        """两个 tag 数据同样新时取候选列表中靠前者（稳定可解释）。"""
        units_a = {"units": _units([{"end": "2025-09-27", "val": 1.0, "form": "10-K", "filed": "2025-10-31"}])}
        units_b = {"units": _units([{"end": "2025-09-27", "val": 2.0, "form": "10-K", "filed": "2025-10-31"}])}
        result = _summarize_metric({"TagA": units_a, "TagB": units_b})
        assert result["tag"] == "TagA"

    def test_failed_payloads_tolerated(self):
        """单概念拉取失败（None）不整体失败，剩下的候选顶上。"""
        units = {
            "units": _units(
                [
                    {"end": "2025-09-27", "val": 5.0, "form": "10-K", "filed": "2025-10-31"},
                    {"end": "2025-09-27", "val": 1.2, "form": "10-Q", "filed": "2025-10-31"},
                ]
            )
        }
        result = _summarize_metric({"EarningsPerShareDiluted": None, "EarningsPerShareBasic": units})
        assert result is not None
        assert result["tag"] == "EarningsPerShareBasic"
        assert result["annual"]["value"] == 5.0
        assert result["quarterly"]["value"] == 1.2

    def test_all_missing_returns_none(self):
        assert _summarize_metric({"A": None, "B": None}) is None
        assert _summarize_metric({}) is None

    def test_no_annual_entry_returns_none(self):
        """只有 10-Q 没有任何 10-K → 无法确定口径，视为该组不可用。"""
        units = {"units": _units([{"end": "2025-09-27", "val": 1.0, "form": "10-Q", "filed": "2025-10-31"}])}
        assert _summarize_metric({"TagA": units}) is None


# ------------------------------------------------------------------ #
# 纯函数：_parse_recent_filings（form 过滤与 limit）                    #
# ------------------------------------------------------------------ #


def _submissions_payload() -> dict:
    return {
        "filings": {
            "recent": {
                "form": ["10-Q", "8-K", "S-1", "10-K", "8-K", "10-Q"],
                "filingDate": ["2026-08-01", "2026-07-30", "2026-07-01", "2025-10-31", "2026-07-20", "2026-05-01"],
                "accessionNumber": [
                    "0001-26-001",
                    "0001-26-002",
                    "0001-26-003",
                    "0001-25-004",
                    "0001-26-005",
                    "0001-26-006",
                ],
                "primaryDocument": ["q.htm", "k8.htm", "s1.htm", "k10.htm", "k85.htm", "q2.htm"],
            }
        }
    }


class TestParseRecentFilings:
    def test_filters_forms_and_limits(self):
        filings = _parse_recent_filings(_submissions_payload(), cik=320193, limit=10)
        assert [f["form"] for f in filings] == ["10-Q", "8-K", "10-K", "8-K", "10-Q"]
        assert len(filings) == 5  # S-1 被过滤

    def test_limit(self):
        filings = _parse_recent_filings(_submissions_payload(), cik=320193, limit=2)
        assert len(filings) == 2
        assert filings[0]["accession_no"] == "0001-26-001"

    def test_document_url_shape(self):
        filings = _parse_recent_filings(_submissions_payload(), cik=320193, limit=1)
        url = filings[0]["document_url"]
        assert url == "https://www.sec.gov/Archives/edgar/data/320193/000126001/q.htm"

    def test_missing_primary_doc_tolerated(self):
        payload = _submissions_payload()
        payload["filings"]["recent"]["primaryDocument"] = ["", "k8.htm", "s1.htm", "k10.htm", "k85.htm", "q2.htm"]
        filings = _parse_recent_filings(payload, cik=1, limit=1)
        assert filings[0]["document_url"] is None
        assert filings[0]["primary_doc"] is None

    def test_malformed_payload(self):
        assert _parse_recent_filings(None, cik=1) == []
        assert _parse_recent_filings({}, cik=1) == []
        assert _parse_recent_filings({"filings": {}}, cik=1) == []


# ------------------------------------------------------------------ #
# 注册表断言                                                           #
# ------------------------------------------------------------------ #


class TestRegistry:
    def test_us_stock_domain_contains_new_tools(self):
        tools = {t.key: t for t in tools_by_domain("us_stock")}
        for key in ("us_fundamentals", "us_filings_recent"):
            assert key in tools
            meta = tools[key]
            assert meta.domain == "us_stock"
            assert meta.category == "fundamental"
            assert meta.http_method == "INTERNAL"
            assert meta.http_path == ""

    def test_new_tools_purpose_contains_required_params(self):
        """planner 只看 purpose 传参 —— purpose 必须含全部必填参数 symbol。"""
        for key in ("us_fundamentals", "us_filings_recent"):
            purpose = BY_KEY[key].purpose
            assert "symbol" in purpose, f"{key} 的 purpose 缺少必填参数 symbol"

    def test_resolve_by_operation_id(self):
        assert resolve_tool_by_name("internal_us_fundamentals").key == "us_fundamentals"
        assert resolve_tool_by_name("internal_us_filings_recent").key == "us_filings_recent"

    def test_no_key_conflict(self):
        keys = [t.key for t in ALL_TOOLS]
        assert len(keys) == len(set(keys)), "新工具导致全局 key 冲突"

    def test_registry_text_contains_new_tools(self):
        text = registry_text(domains=["us_stock"])
        assert "us_fundamentals" in text
        assert "us_filings_recent" in text


# ------------------------------------------------------------------ #
# 分发接线（monkeypatch 打在 tool_runtime 的导入名上）                  #
# ------------------------------------------------------------------ #


class _RecordingGateway:
    """记录实例化的假 Gateway —— 内部工具不得触碰它。"""

    instances: list["_RecordingGateway"] = []

    def __init__(self, settings):
        _RecordingGateway.instances.append(self)


def _make_runtime() -> ToolRuntime:
    # unique symbol per test 避免 market_cache 全局污染串台
    return ToolRuntime(Settings(sec_edgar_contact="Mosaic research admin@example.com"))


class TestDispatchWiring:
    @pytest.mark.asyncio
    async def test_us_fundamentals_happy_path_never_touches_gateway(self, monkeypatch):
        _RecordingGateway.instances = []
        fake = AsyncMock(
            return_value={
                "symbol": "AAPL",
                "cik": 320193,
                "revenue": {"tag": "Revenues", "annual": {"value": 1.0}, "quarterly": None},
                "net_income": None,
                "eps": None,
                "gross_profit": None,
                "caveats": ["net_income: 候选 tag [...] 均无有效 10-K/10-Q 数据"],
                "source_urls": ["https://data.sec.gov/x"],
            }
        )
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_fundamentals", fake)

        runtime = _make_runtime()
        runtime._gateway_class = lambda: _RecordingGateway  # type: ignore[method-assign]
        result = await runtime.execute("internal_us_fundamentals", {"symbol": "AAPL_FRESH_X"}, set())

        assert result.status == "success"
        assert result.error is None
        metrics = {e.metric for e in result.normalized}
        assert "revenue" in metrics
        assert "_caveats" in metrics and "_meta" in metrics
        assert all(e.domain == "us_stock" for e in result.normalized)
        assert all(e.source == "sec_edgar_xbrl" for e in result.normalized)
        fake.assert_awaited_once_with("AAPL_FRESH_X")
        # 内部工具不得建连 Gateway
        assert _RecordingGateway.instances == []

    @pytest.mark.asyncio
    async def test_us_fundamentals_exception_becomes_error(self, monkeypatch):
        async def boom(symbol):
            raise RuntimeError("EDGAR down")

        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_fundamentals", boom)
        runtime = _make_runtime()
        result = await runtime.execute("internal_us_fundamentals", {"symbol": "AAPL_ERR_X"}, set())
        assert result.status == "error"
        assert result.normalized == []
        assert "EDGAR down" in result.error

    @pytest.mark.asyncio
    async def test_us_filings_recent_happy_path_never_touches_gateway(self, monkeypatch):
        _RecordingGateway.instances = []
        fake = AsyncMock(
            return_value=[
                {
                    "form": "10-Q",
                    "filing_date": "2026-08-01",
                    "accession_no": "0001-26-001",
                    "document_url": "https://www.sec.gov/Archives/edgar/data/1/000126001/q.htm",
                    "primary_doc": "q.htm",
                }
            ]
        )
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_filings_recent", fake)

        runtime = _make_runtime()
        runtime._gateway_class = lambda: _RecordingGateway  # type: ignore[method-assign]
        result = await runtime.execute("internal_us_filings_recent", {"symbol": "AAPL_FIL_X"}, set())

        assert result.status == "success"
        assert len(result.normalized) == 1
        entry = result.normalized[0]
        assert entry.metric == "sec_filing_10-Q"
        assert entry.value["accession_no"] == "0001-26-001"
        assert entry.source == "sec_edgar_submissions"
        assert _RecordingGateway.instances == []

    @pytest.mark.asyncio
    async def test_us_filings_recent_exception_becomes_error(self, monkeypatch):
        async def boom(symbol, limit=10):
            raise RuntimeError("submissions down")

        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_filings_recent", boom)
        runtime = _make_runtime()
        result = await runtime.execute("internal_us_filings_recent", {"symbol": "AAPL_FIL_ERR_X"}, set())
        assert result.status == "error"
        assert "submissions down" in result.error

    @pytest.mark.asyncio
    async def test_same_args_hit_dispatch_cache(self, monkeypatch):
        """分发层缓存（对齐 news_search 模式）：同参数两次执行底层只拉 1 次。"""
        fake = AsyncMock(
            return_value={
                "symbol": "MSFT_CACHE_XYZ",
                "cik": 1,
                "revenue": {"tag": "Revenues", "annual": {"value": 1.0}, "quarterly": None},
                "caveats": [],
                "source_urls": [],
            }
        )
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_fundamentals", fake)
        runtime = _make_runtime()
        args = {"symbol": "MSFT_CACHE_XYZ"}
        r1 = await runtime.execute("internal_us_fundamentals", args, set())
        r2 = await runtime.execute("internal_us_fundamentals", args, set())
        assert fake.await_count == 1
        assert r1.status == "success"
        assert r2.status == "success"


# ------------------------------------------------------------------ #
# 配置门闩                                                             #
# ------------------------------------------------------------------ #


class TestContactGate:
    @pytest.mark.asyncio
    async def test_empty_contact_returns_error_without_network(self, monkeypatch):
        fake = AsyncMock()
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_fundamentals", fake)
        fake_filings = AsyncMock()
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_filings_recent", fake_filings)

        runtime = ToolRuntime(Settings(sec_edgar_contact=""))
        for tool_name, fake_fetch in (
            ("internal_us_fundamentals", fake),
            ("internal_us_filings_recent", fake_filings),
        ):
            result = await runtime.execute(tool_name, {"symbol": "AAPL"}, set())
            assert result.status == "error"
            assert result.normalized == []
            assert "SEC_EDGAR_CONTACT" in result.error
            fake_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_symbol_returns_error_without_network(self, monkeypatch):
        fake = AsyncMock()
        monkeypatch.setattr("app.graph.tool_runtime._fetch_us_fundamentals", fake)
        runtime = _make_runtime()
        result = await runtime.execute("internal_us_fundamentals", {}, set())
        assert result.status == "error"
        assert "symbol" in result.error
        fake.assert_not_awaited()


# ------------------------------------------------------------------ #
# critic 接线                                                          #
# ------------------------------------------------------------------ #


class TestCriticWiring:
    def test_us_stock_rules_reference_fundamentals(self):
        rules = _DOMAIN_RULES["us_stock"]
        assert rules
        assert "us_fundamentals" in rules
        assert "us_klines" in rules
        assert "us_window" in rules
