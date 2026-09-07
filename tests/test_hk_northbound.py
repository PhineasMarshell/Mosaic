"""测试港股通北向资金数据获取模块。"""

import json

import pytest

from app.research.hk_northbound import (
    _build_params,
    _parse_index,
    _parse_northbound,
)


# ------------------------------------------------------------------ #
# _build_params                                                        #
# ------------------------------------------------------------------ #


class TestBuildParams:
    def test_returns_required_keys(self):
        params = _build_params("HK.960036")
        assert "secid" in params
        assert params["secid"] == "HK.960036"
        assert params["klt"] == "101"
        assert "fields1" in params
        assert "fields2" in params

    def test_custom_days(self):
        params = _build_params("SH.960052", days=10)
        assert params["cut"] == 10

    def test_all_secids_valid(self):
        from app.research.hk_northbound import _SECID_MAP
        for key, secid in _SECID_MAP.items():
            assert "." in secid  # Eastmoney secid format requires dot
            assert len(secid.split(".")) == 2  # market.code


# ------------------------------------------------------------------ #
# _parse_northbound                                                    #
# ------------------------------------------------------------------ #


class TestParseNorthbound:
    def test_parses_single_day(self):
        raw = {
            "data": {
                "hqData": [
                    ["2024-09-01", "38000.00", "-15.32", "38500.00", "37900.00", "100"],
                ]
            }
        }
        result = _parse_northbound(raw)
        assert len(result) == 1
        assert result[0]["date"] == "2024-09-01"
        assert result[0]["net_inflow_billion_cny"] == -15.32
        assert result[0]["direction"] == "outflow"

    def test_parses_multiple_days(self):
        raw = {
            "data": {
                "hqData": [
                    ["2024-09-01", "38000.00", "10.50", "38500.00", "37900.00", "100"],
                    ["2024-09-02", "39000.00", "-5.20", "39500.00", "38800.00", "120"],
                    ["2024-09-03", "40000.00", "8.30", "40200.00", "39800.00", "110"],
                ]
            }
        }
        result = _parse_northbound(raw)
        assert len(result) == 3
        assert result[0]["net_inflow_billion_cny"] == 10.50
        assert result[0]["direction"] == "inflow"
        assert result[1]["direction"] == "outflow"
        assert result[2]["direction"] == "inflow"

    def test_empty_data(self):
        assert _parse_northbound({}) == []
        assert _parse_northbound({"data": None}) == []
        assert _parse_northbound({"data": {"hqData": None}}) == []
        assert _parse_northbound({"data": {"hqData": []}}) == []

    def test_short_items_skipped(self):
        raw = {
            "data": {
                "hqData": [
                    ["2024-09-01"],  # Too short, should be skipped
                    ["2024-09-02", "x", "y", "z", "w", "v", "5.0"],  # index error on item[2]
                    ["2024-09-03", "a", "100.00", "b", "c", "d", "e"],
                ]
            }
        }
        result = _parse_northbound(raw)
        # Only the valid third entry survives
        assert len(result) == 1
        assert result[0]["date"] == "2024-09-03"

    def test_rounds_values(self):
        raw = {
            "data": {
                "hqData": [
                    ["2024-09-01", "38000.00", "10.55555", "38500.00", "37900.00", "100"],
                ]
            }
        }
        result = _parse_northbound(raw)
        assert result[0]["net_inflow_billion_cny"] == 10.56  # rounded to 2dp


# ------------------------------------------------------------------ #
# _parse_index                                                         #
# ------------------------------------------------------------------ #


class TestParseIndex:
    def test_parses_latest(self):
        raw = {
            "data": {
                "hqData": [
                    ["2024-09-01", "18000.00", "19500.00", "19800.00", "19400.00", "0.50"],
                    ["2024-09-02", "18500.00", "19800.00", "20000.00", "19600.00", "1.54"],
                ]
            }
        }
        result = _parse_index(raw)
        assert result is not None
        assert result["date"] == "2024-09-02"
        assert result["close"] == 19800.00
        assert result["change_pct"] == 1.54

    def test_empty_data(self):
        assert _parse_index({}) is None
        assert _parse_index({"data": None}) is None
        assert _parse_index({"data": {"hqData": []}}) is None
