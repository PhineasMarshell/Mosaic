"""Official finance/event/basicinfo/market/macro query2data adapters.

Keep dynamic field names, values, units and reporting dates; never count the
default quote/identity columns as financial or event evidence.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.iwencai_contracts import QUERY_TOOLS
from app.models.market import NormalizedDatum, ToolResult
from app.research.iwencai import SOURCE, _call_iwencai, _error
from app.research.trading_calendar import normalize_date

_CODE_FIELDS = (
    "股票代码",
    "证券代码",
    "基金代码",
    "指数代码",
    "转债代码",
    "债券代码",
    "期货代码",
    "期权代码",
    "合约代码",
    "产品代码",
    "代码",
)
_NAME_FIELDS = (
    "股票简称",
    "证券简称",
    "基金简称",
    "基金名称",
    "指数简称",
    "指数名称",
    "转债简称",
    "债券简称",
    "债券名称",
    "合约名称",
    "产品名称",
    "名称",
)
_QUOTE_FIELDS = {"最新价", "最新价格", "最新涨跌幅", "现价", "涨跌幅", "涨跌额", "成交量", "成交额", "开盘价", "收盘价"}
_EVENT_DATES = ("公告日期", "预告公告日期", "调研日期", "质押日期", "解禁日期", "增发上市日期", "股东大会召开日期")
_EMPTY_TEXT = {"", "--", "-", "暂无数据", "null", "none", "nan"}
_FINANCE_FIELDS = re.compile(
    r"收入|利润|收益率|毛利|净利|负债|资产|现金|成本|费用|每股|市盈率|市净率|市销率|股息|偿债|比率|增长率|周转|资本|商誉|研发|税|应收|应付|存货|估值|\b(?:ROE|ROA|EPS|EBITDA|PE|PB|PS)\b",
    re.IGNORECASE,
)
_EVENT_FIELDS = re.compile(
    r"业绩预|增发|配股|质押|解禁|调研|监管|问询|警示|预告|机构|接待|公告|事件|股东大会|限售|预增|预减|扭亏|处罚|违规|回购|分红|派息|股权登记"
)
_BASIC_FIELDS = re.compile(
    r"代码|简称|名称|上市|成立|所属|行业|管理人|托管|发行|费率|到期|期限|交割|行权|面值|票面|币种|交易所|交易市场|合约|乘数|最小|交易单位|保费|保额|保险|类型|申购|赎回|风险等级|地区|国家|注册|联系方式|地址|网址|负责人|法人|评级|基金经理|基准"
)
_MARKET_FIELDS = re.compile(
    r"价格|股价|最新价|现价|开盘|收盘|最高价|最低价|均价|涨跌|涨幅|跌幅|振幅|成交|换手|量比|资金流|净流入|净流出|大单|小单|主力|技术指标|均线|布林|(?<![A-Z])(?:MACD|RSI|KDJ|MA|EMA|SMA|BOLL)(?![A-Z])",
    re.IGNORECASE,
)
_MARKET_REQUESTS = {
    "价格": (
        r"股价|股票价格|ETF价格|指数价格|最新价|现价|收盘价|开盘价",
        r"价格|股价|最新价|现价|开盘|收盘|最高价|最低价|均价",
    ),
    "涨跌幅": (r"涨跌幅|涨幅|跌幅", r"涨跌|涨幅|跌幅"),
    "成交量": (r"成交量", r"成交量"),
    "成交额": (r"成交额", r"成交额"),
    "资金流向": (r"资金流向|主力资金|资金净流入|资金流入|资金流出|大单|小单", r"资金流|净流入|净流出|主力|大单|小单"),
    "MACD": (r"MACD", r"MACD"),
    "RSI": (r"RSI", r"RSI"),
    "KDJ": (r"KDJ", r"KDJ"),
    "均线": (r"均线", r"均线|移动平均|(?<![A-Z])(?:MA|EMA|SMA)(?![A-Z])"),
}
_MACRO_FIELDS = re.compile(
    r"GDP|CPI|PPI|PMI|LPR|M[012]|国内生产总值|物价|居民消费价格|生产者|利率|汇率|社会融资|社融|工业增加值|消费|投资|进口|出口|贸易|失业|就业|货币供应|外汇储备|经济增长|宏观",
    re.IGNORECASE,
)
_MANAGEMENT_FIELDS = re.compile(r"股本|股权|股东|户数|持股|持仓|持有|控股|控制人|实控人|质押|高管|董事|监事")
_INDUSTRY_IDENTITY = ("行业代码", "行业名称", "板块代码", "板块名称", "行业简称", "板块简称")
_CLASSIFICATION_FIELDS = ("行业分类", "分类体系", "行业标准", "行业层级")
_REFERENCE_REQUESTS = {
    "iwencai_industry": {
        **_MARKET_REQUESTS,
        "营业收入": (r"营业收入|营收", r"营业收入|营收"),
        "净利润": (r"净利润", r"净利润"),
        "估值": (r"估值|市盈率|市净率", r"估值|市盈率|市净率|PE|PB"),
        "排名": (r"排名", r"排名|排序"),
        "ROE": (r"ROE|净资产收益率", r"ROE|净资产收益率"),
        "现金流": (r"现金流", r"现金流"),
    },
    "iwencai_management": {
        "股东户数": (r"股东户数|股东人数", r"户数|股东人数"),
        "十大股东": (r"十大.*股东", r"持股|股东名称|大股东名称|流通股东"),
        "流通股东口径": (r"流通股东", r"流通股东|无限售.*股东"),
        "实控人": (r"实控人|实际控制人", r"实控人|实际控制人"),
        "股本": (r"股本", r"股本"),
        "质押": (r"质押", r"质押"),
    },
}
_REFERENCE_REQUESTS["iwencai_sector_selector"] = {
    **_REFERENCE_REQUESTS["iwencai_industry"],
    "板块类型": (r"板块类型|行业板块|概念板块|地域板块", r"板块类型|指数类型|行业分类|分类体系"),
}
_SCREENING_KEYS = {"iwencai_astock_selector", "iwencai_sector_selector"}
_BUSINESS_FIELDS = re.compile(
    r"主营|业务|产品|地区|客户|供应商|采购|销售|收入占比|成本占比|利润占比|毛利率|参控股|参股|子公司|股权投资|投资公司|被投资|重大合同|合同|持股比例"
)
_INSRESEARCH_FIELDS = re.compile(r"研报|评级|预测|预期|目标价|金股|ESG|星级|评分", re.IGNORECASE)
_COMPANY_REQUESTS = {
    "iwencai_business": {
        "主营业务": (r"主营业务|业务构成", r"主营|业务|产品|地区"),
        "收入占比": (r"收入占比", r"收入占比|营收占比"),
        "客户": (r"主要客户|客户", r"客户"),
        "供应商": (r"供应商", r"供应商|采购"),
        "参控股公司": (r"参控股|参股|子公司", r"参控股|参股|子公司"),
        "股权投资": (r"股权投资", r"股权投资|被投资|投资公司"),
        "重大合同": (r"重大合同|合同", r"合同"),
    },
    "iwencai_insresearch": {
        "评级": (r"评级|ESG|星级", r"评级|ESG|星级|评分"),
        "业绩预测": (r"业绩预测|盈利预测|预测.{0,12}(?:净利润|营收|EPS)", r"预测|预期"),
        "券商金股": (r"券商金股", r"金股"),
        "目标价": (r"目标价", r"目标价"),
    },
}
_INDEX_FIELDS = re.compile(r"点位|指数值|" + _MARKET_FIELDS.pattern, re.IGNORECASE)
_INDEX_REQUESTS = {
    **_MARKET_REQUESTS,
    "点位": (r"点位|多少点|指数值", r"点位|指数值|最新价|收盘价|价格"),
}
_SCREENING_REQUESTS = {
    **_MARKET_REQUESTS,
    "市盈率": (r"市盈率|(?<![A-Z])PE(?![A-Z])", r"市盈率|(?<![A-Z])PE(?![A-Z])"),
    "市净率": (r"市净率|(?<![A-Z])PB(?![A-Z])", r"市净率|(?<![A-Z])PB(?![A-Z])"),
    "ROE": (r"ROE|净资产收益率", r"ROE|净资产收益率"),
    "营业收入": (r"营业收入|营收", r"营业收入|营收"),
    "净利润": (r"净利润", r"净利润"),
    "行业概念": (r"行业|概念", r"行业|概念"),
    "技术形态": (r"突破|新高|多头|K线形态", r"突破|新高|多头|K线形态"),
}
_RESPONSE_METADATA = {
    "status",
    "error",
    "token",
    "query",
    "page",
    "limit",
    "code_count",
    "row_count",
    "chunks_info",
    "trace_id",
}


def _present(value: Any) -> bool:
    if value is None or value == [] or value == {}:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_TEXT
    return not isinstance(value, float) or math.isfinite(value)


def _symbol(row: dict) -> tuple[str | None, str | None]:
    code = next((str(row[k]).strip().upper() for k in _CODE_FIELDS if _present(row.get(k))), "")
    name = next((str(row[k]).strip() for k in _NAME_FIELDS if _present(row.get(k))), "")
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        number, exchange = code.split(".")
        code = exchange + number
    elif re.fullmatch(r"\d{1,5}\.HK", code):
        code = "HK" + code.split(".")[0].zfill(5)
    elif re.fullmatch(r"\d{5}", code):
        code = "HK" + code
    # Preserve explicitly returned contract/product identifiers too. Never
    # derive an identifier from the query or use a missing field as a code.
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9./:_-]{0,63}", code):
        code = ""
    return code or None, name or None


def _single_date(value: Any) -> str | None:
    return normalize_date(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _bounded(value: Any, limit: int, depth: int = 0) -> tuple[Any, bool]:
    if isinstance(value, str):
        return value[:limit], len(value) > limit
    if isinstance(value, (dict, list)):
        if depth >= 3:
            text = json.dumps(value, ensure_ascii=False)
            return text[:limit], True
        clipped = len(value) > 20
        if isinstance(value, dict):
            out = {}
            for key, item in list(value.items())[:20]:
                out[key], was_clipped = _bounded(item, limit, depth + 1)
                clipped |= was_clipped
        else:
            out = []
            for item in value[:20]:
                bounded, was_clipped = _bounded(item, limit, depth + 1)
                out.append(bounded)
                clipped |= was_clipped
        return out, clipped
    return value, False


def _datum_completeness(
    *,
    as_of_date: str | None,
    value: Any,
    instrument: str | None = None,
    instrument_scope: str | None = None,
    clipped: bool = False,
    scope: str | None = None,
) -> str:
    """单条 datum 的**字段完整性**，而不是"它来自分页响应"。

    P13 §三：``completeness`` 曾经写死为 ``"page"`` —— 那描述的是响应信封
    （分页 / 检索集合），与 datum 自身字段是否齐全无关。下游把它当作数据
    完整性判据时，会把日期、标的、来源、检索时间全部齐全的真实行情行误判
    为不可交付，于是"证据已采集但无法形成可审计交付"。这里只依据 datum
    自己是否具备判定事实所必需的字段来给结论：

    - 没有真实数据日期（不能用检索时间代替）→ ``partial``；
    - 值为空 → ``partial``；
    - 标的级数据缺少标的代码 → ``partial``；
    - 展示被截断（完整值只在 raw 里）→ ``partial``；
    - 静态属性（``scope="static"``）允许没有生效日期。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "partial"
    if clipped:
        return "partial"
    if scope != "static" and not as_of_date:
        return "partial"
    if (instrument_scope or "") == "instrument" and not instrument:
        return "partial"
    return "complete"


def _macro_datum(row: dict, metadata: dict, tool: str, row_index: int, retrieved: str) -> NormalizedDatum | None:
    indicator = row.get("macro_name") or row.get("指标名称") or row.get("指标")
    if not isinstance(indicator, str) or not _MACRO_FIELDS.search(indicator):
        return None
    value_field = next((f for f in ("指标值", "数值", "值") if _present(row.get(f))), None)
    if value_field is None:
        return None
    period = next((row[f] for f in ("时间", "日期", "统计期", "报告期") if _present(row.get(f))), None)
    stamp = _single_date(period)
    # Preserve month/year/quarter precision instead of inventing a day.
    period_known = (
        stamp is not None
        or isinstance(period, str)
        and bool(re.fullmatch(r"20\d{2}(?:年|[-/]?(?:0?[1-9]|1[0-2])月?|年[一二三四1-4]季度|Q[1-4])?", period))
    )
    country = row.get("国家")
    region = row.get("地区") or row.get("省份") or row.get("城市")
    unit = row.get("单位") or metadata.get(value_field, {}).get("unit")
    unit = unit if isinstance(unit, str) and unit else None
    series = row.get("macro_id")
    series = series if isinstance(series, str) and series else None
    bounded, clipped = _bounded(
        {
            "indicator": indicator,
            "value": row[value_field],
            "country": country,
            "region": region,
            "period": period,
            "frequency": row.get("周期"),
            "series_id": series,
        },
        800,
    )
    notes = ["统计期是经济指标所属期，不是发布日期或检索日期"]
    if not period_known:
        notes.append("上游统计期缺失或无法确认")
    if not country and not region:
        notes.append("上游未提供国家/地区")
    if not unit:
        notes.append("上游未单独提供单位，不推断单位或数值倍率")
    if period_known and not stamp:
        notes.append("统计期只提供年/月/季度粒度，未补造具体日期")
    if clipped:
        notes.append("字段展示已截断，完整返回保留于 raw")
    return NormalizedDatum(
        tool=tool,
        domain="macro",
        instrument=series,
        instrument_name=indicator,
        instrument_scope="macro_series" if series else "macro_indicator",
        metric=f"iwencai_macro[{row_index}].{indicator}",
        value=bounded,
        unit=unit,
        timestamp=stamp,
        as_of_date=stamp,
        source=SOURCE,
        authority=SOURCE,
        retrieved_at=retrieved,
        completeness=_datum_completeness(
            as_of_date=stamp, value=bounded, instrument=series,
            instrument_scope="macro_series" if series else "macro_indicator", clipped=clipped,
        ),
        partial=not period_known or not (country or region) or not unit,
        note="；".join(notes),
    )


def _reference_datums(row, metadata, tool, logical_key, row_index, retrieved, text_limit):
    """Keep industry identity and holder row context attached to each metric.

    Publication dates remain context, never become holding snapshot dates.
    Provider query-date labels remain distinct from the published dates.
    """
    industry = logical_key in {"iwencai_industry", "iwencai_sector_selector"}
    if logical_key == "iwencai_sector_selector" and not any(
        _present(row.get(key)) for key in (*_INDUSTRY_IDENTITY, "指数代码", "指数简称", "指数名称")
    ):
        return []  # A default stock table cannot establish sector identity.
    instrument, name = _symbol(row)
    classification = {k: row[k] for k in _CLASSIFICATION_FIELDS if _present(row.get(k))}
    if industry:
        classification.update({key: row[key] for key in ("板块类型", "指数类型") if _present(row.get(key))})
    if industry:
        code = row.get("行业代码") or row.get("板块代码") or row.get("指数代码")
        instrument = str(code).strip() if _present(code) else None
        name = (
            row.get("行业名称")
            or row.get("板块名称")
            or row.get("行业简称")
            or row.get("板块简称")
            or row.get("指数简称")
            or row.get("指数名称")
        )
        scope = "industry_index" if row.get("指数代码") else "industry"
        # Unexpected constituent securities remain individual securities.
        if not name and not instrument:
            instrument, name = _symbol(row)
            scope = "instrument" if instrument else "unverified_industry"
    else:
        # Generic 名称 in expanded holder rows identifies the shareholder,
        # and must never serve as the company name.
        _, name = _symbol({k: v for k, v in row.items() if k != "名称"})
        scope = "instrument" if instrument else "unverified_instrument"
    holder = row.get("股东名称") or row.get("持有人名称")
    if not holder and (
        any(_present(row.get(k)) for k in ("持股数量", "持股比例", "持股变动类型"))
        or re.search(r"股东|持有人", str(metadata.get("名称", {}).get("index_name", "")))
    ):
        holder = row.get("名称")
    context_dates = {
        k: v
        for k, v in row.items()
        if any(t in k for t in ("公告日期", "报告期", "截止日期", "统计日期", "持仓日期", "变动日期")) and _present(v)
    }
    snapshot = next(
        (_single_date(row.get(k)) for k in ("报告期", "截止日期", "统计日期", "持仓日期") if _single_date(row.get(k))),
        None,
    )
    entries = []
    for field, value in row.items():
        base = field.split("[")[0]
        column = metadata.get(field, {})
        semantic = field + " " + str(column.get("index_name", ""))
        if not _present(value) or base in _RESPONSE_METADATA or base in _CLASSIFICATION_FIELDS:
            continue
        if not industry and base in {"公告日期", "报告期", "截止日期", "统计日期", "持仓日期", "变动日期"}:
            continue  # dates are context, not standalone ownership metrics
        if base in (*_CODE_FIELDS, *_NAME_FIELDS, *_INDUSTRY_IDENTITY, "序号") and not (
            not industry and base == "名称" and holder
        ):
            continue  # identity/rank retained in each structured metric below
        if base == "排名" and not industry:
            continue
        if industry:
            if not (
                _FINANCE_FIELDS.search(semantic)
                or _MARKET_FIELDS.search(semantic)
                or re.search(r"估值|盈利|排名|排序|板块类型|指数类型", semantic)
            ):
                continue
            date_scope = "static" if re.search(r"板块类型|指数类型", semantic) else (
                "market"
                if _MARKET_FIELDS.search(semantic) or re.search(r"估值|市盈率|市净率|市销率", semantic)
                else "reporting_period"
            )
        else:
            if not _MANAGEMENT_FIELDS.search(semantic) or base in _QUOTE_FIELDS:
                continue
            date_scope = "snapshot"
        stamp = _single_date(column.get("timestamp")) or normalize_date(field)
        if date_scope != "market":
            stamp = stamp or snapshot
        structured = {
            "value": value,
            "indicator": column.get("index_name") or field,
            "date_scope": date_scope,
            "field_date": stamp,
            "source_dates": context_dates,
        }
        if industry:
            structured.update(industry_name=name, classification=classification, rank=row.get("排名"), entity_kind="sector")
        elif re.search(r"持股|持仓|持有|股东名称|大股东", semantic) or base == "名称":
            structured.update(holder_name=holder, holder_rank=row.get("排名"))
        bounded, clipped = _bounded(structured, max(1, text_limit))
        unit = column.get("unit")
        unit = unit if isinstance(unit, str) and unit else None
        notes = ["字段日期按上游保留；公告日期不是报告期/持仓截止日，检索日期不替代数据日期"]
        if not stamp:
            notes.append("上游未提供该指标的数据日期")
        if industry and not classification:
            notes.append("上游未确认行业分类体系，不推断申万/中信或层级")
        if not unit:
            notes.append("上游未单独提供单位，不推断持股比例倍率或股数单位")
        if clipped:
            notes.append("字段展示已截断，完整返回保留于 raw")
        entries.append(
            NormalizedDatum(
                tool=tool,
                domain="reference"
                if industry
                else "hk_stock"
                if instrument and instrument.startswith("HK")
                else "a_share"
                if instrument
                else "reference",
                instrument=instrument,
                instrument_name=name,
                instrument_scope=scope,
                metric=f"{logical_key}[{row_index}].{field}",
                value=bounded,
                unit=unit,
                timestamp=stamp,
                as_of_date=stamp,
                retrieved_at=retrieved,
                source=SOURCE,
                authority=SOURCE,
                completeness=_datum_completeness(
                    as_of_date=stamp, value=bounded, instrument=instrument,
                    instrument_scope=scope, clipped=clipped,
                ),
                partial=not stamp
                or not unit
                or (not (instrument or name) if industry else not instrument)
                or (industry and not classification),
                note="；".join(notes),
            )
        )
    return entries


def _company_reference_datums(row, metadata, tool, logical_key, row_index, retrieved, text_limit):
    """Keep company/relationship identity and opinion dates distinct.

    A forecast horizon is never its publication date. Counterparty/institution
    names in expanded rows are never used to fill the queried company name.
    """
    research = logical_key == "iwencai_insresearch"
    instrument, name = _symbol({key: value for key, value in row.items() if key != "名称"})
    pattern = _INSRESEARCH_FIELDS if research else _BUSINESS_FIELDS
    dates = {
        key: value for key, value in row.items()
        if re.search(r"日期|报告期|截止日|预测年度|预测期", key) and _present(value)
    }
    institution = next((row[key] for key in ("评级机构", "研究机构", "机构名称", "券商名称", "证券公司") if _present(row.get(key))), None)
    institution = institution.strip() if isinstance(institution, str) and institution.strip() else None
    publication = next((date for key in ("评级日期", "研报日期", "报告日期", "发布日期", "发布日", "报告发布日期") if (date := _single_date(row.get(key)))), None)
    reporting_period = next((date for key in ("报告期", "报告期截止日", "截止日期", "统计日期") if (date := _single_date(row.get(key)))), None)
    entries = []
    for field, value in row.items():
        base = field.split("[")[0]
        if base in (*_CODE_FIELDS, *_NAME_FIELDS, *_RESPONSE_METADATA, "序号", "排名") or not _present(value):
            continue
        column = metadata.get(field, {})
        semantic = field + " " + str(column.get("index_name", ""))
        if not pattern.search(semantic) or base in dates or base in {"评级机构", "研究机构", "机构名称", "券商名称", "证券公司"}:
            continue
        field_date = _single_date(column.get("timestamp")) or normalize_date(field)
        if research:
            date_scope, stamp = "event", publication
            forecast = bool(re.search(r"预测|预期", semantic))
            kind = "forecast" if forecast else "target_price" if "目标价" in semantic else "recommendation" if "金股" in semantic else "rating" if re.search(r"评级|ESG|星级|评分", semantic, re.IGNORECASE) else "research_report"
            target = row.get("预测年度") or row.get("预测期") or (row.get("报告期截止日") if forecast else None)
            if forecast and not target:
                target = next(iter(re.findall(r"20\d{2}年?", semantic)), None)
            context = {
                "data_kind": "institutional_opinion", "opinion_kind": kind,
                "institution_name": institution, "publication_date": publication,
                "target_period": target, "source_dates": dates,
                "report_title": row.get("研报") or row.get("研报标题") or row.get("报告标题"),
                "report_url": row.get("研报链接") or row.get("报告链接"),
            }
        else:
            contract = "合同" in semantic
            date_scope = "event" if contract else "reporting_period"
            stamp = field_date or (
                next((date for key in ("合同签订日期", "合同公告日期") if (date := _single_date(row.get(key)))), None)
                if contract else reporting_period
            )
            context = {
                "data_kind": "company_operations", "source_dates": dates,
                "relationship": {key: row[key] for key in ("客户名称", "供应商名称", "子公司名称", "参控股公司名称", "被投资公司名称", "业务类型", "产品名称", "地区", "主营构成", "主营业务构成", "项目名称", "分类标准", "名称", "分类") if _present(row.get(key))},
            }
        structured, clipped = _bounded(
            {"value": value, "indicator": column.get("index_name") or field, "date_scope": date_scope,
             "field_date": field_date, **context}, max(1, text_limit),
        )
        unit = column.get("unit") if isinstance(column.get("unit"), str) and column["unit"] else None
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        notes = ["原始字段日期、报告期与事件日期分别保留，检索日期不替代任何业务日期"]
        if research:
            notes.append("机构观点；预测/预期不代表已实现业绩，目标价不是实际行情，预测期不是发布日期")
        if not instrument or not name:
            notes.append("公司代码/名称不完整，不从查询或对手名称补造")
        if not stamp:
            notes.append("上游未提供可确认的发布日期" if research else "上游未提供该经营字段所属期/合同事件日期")
        if research and not institution:
            notes.append("上游未提供评级/研究机构，不推断机构来源")
        if clipped:
            notes.append("字段展示已截断，完整返回保留于 raw")
        entries.append(NormalizedDatum(
            tool=tool, domain="hk_stock" if instrument and instrument.startswith("HK") else "a_share" if instrument and re.fullmatch(r"(?:SH|SZ|BJ)?\d{6}", instrument) else "reference",
            instrument=instrument, instrument_name=name, instrument_scope="instrument" if instrument else "unverified_instrument",
            metric=f"{logical_key}[{row_index}].{field}", value=structured, unit=unit,
            timestamp=stamp, as_of_date=stamp, source=SOURCE, authority=SOURCE, retrieved_at=retrieved,
            completeness=_datum_completeness(
                as_of_date=stamp, value=structured, instrument=instrument,
                instrument_scope="instrument" if instrument else "unverified_instrument", clipped=clipped,
            ),
            partial=clipped or not instrument or not name or not stamp or (numeric and not unit) or (research and not institution),
            note="；".join(notes),
        ))
    return entries


def _index_or_screening_datums(
    row: dict, metadata: dict, tool: str, logical_key: str, row_index: int, retrieved: str, text_limit: int,
) -> list[NormalizedDatum]:
    """Retain typed row identity and per-field scope, never validate filters from prose."""
    index = logical_key == "iwencai_index"
    if index:
        # A stock/default quote table is not proof of an index identity.
        if not any(_present(row.get(key)) for key in ("指数代码", "指数简称", "指数名称")):
            return []
        identity_row = {key: row[key] for key in ("指数代码", "指数简称", "指数名称") if key in row}
        instrument, name = _symbol(identity_row)
        # Official overseas identifiers may start with '.', '^' or '@'. Retain
        # the structured identifier verbatim; never infer it from the name.
        raw_index_code = str(row.get("指数代码") or "").strip().upper()
        if not instrument and re.fullmatch(r"[.\^@][A-Z0-9][A-Z0-9./:_-]{0,62}", raw_index_code):
            instrument = raw_index_code
    else:
        if not any(_present(row.get(key)) for key in ("股票代码", "证券代码", "股票简称", "证券简称")):
            return []
        instrument, name = _symbol(row)
        if instrument and not re.fullmatch(r"(?:SH|SZ|BJ)?\d{6}", instrument):
            return []  # A-stock selection must not promote overseas rows.
    entries = []
    for field, value in row.items():
        base = field.split("[")[0]
        if base in (*_CODE_FIELDS, *_NAME_FIELDS, "序号", "排名", *_RESPONSE_METADATA) or not _present(value):
            continue
        column = metadata.get(field, {})
        semantic = field + " " + str(column.get("index_name", ""))
        market_field = bool((_INDEX_FIELDS if index else _MARKET_FIELDS).search(semantic))
        finance_field = not index and bool(_FINANCE_FIELDS.search(semantic))
        static_field = not index and bool(re.search(r"所属行业|所属概念|行业分类|概念板块", semantic))
        technical_field = not index and bool(re.search(r"突破|新高|多头|K线形态|涨停状态|跌停状态", semantic))
        if not (market_field or finance_field or static_field or technical_field):
            continue
        valuation_field = bool(re.search(r"市盈率|市净率|市销率|市值|估值|\b(?:PE|PB|PS)\b", semantic, re.IGNORECASE))
        scope = "reporting_period" if finance_field and not (market_field or valuation_field) else "static" if static_field else "market"
        stamp = _single_date(column.get("timestamp")) or normalize_date(field)
        unit = column.get("unit") if isinstance(column.get("unit"), str) and column["unit"] else None
        bounded, clipped = _bounded(
            {"value": value, "indicator": column.get("index_name") or field, "date_scope": scope,
             "field_date": stamp, "entity_kind": "index" if index else "equity"},
            max(1, text_limit),
        )
        notes = ["数据日期及单位按上游字段保留；检索时间不替代行情日期或报告期"]
        if not instrument or not name:
            notes.append("上游代码/名称映射不完整，不从查询补全")
        if not stamp and scope != "static":
            notes.append("上游未提供该字段的数据日期")
        if index:
            notes.append("指数点位和成交量属于该指数原口径，不代表个股价格或全市场统计")
        else:
            notes.append("上游选股候选；返回本行字段不代表所有过滤条件已逐项验证")
        if clipped:
            notes.append("字段展示已截断，完整返回保留于 raw")
        entries.append(NormalizedDatum(
            tool=tool, domain="reference" if index else "a_share", instrument=instrument, instrument_name=name,
            instrument_scope=("index" if index else "instrument") if instrument else "unverified_instrument",
            metric=f"{logical_key}[{row_index}].{field}", value=bounded, unit=unit,
            timestamp=stamp, as_of_date=stamp, retrieved_at=retrieved, source=SOURCE, authority=SOURCE,
            completeness=_datum_completeness(
                as_of_date=stamp, value=bounded, instrument=instrument,
                instrument_scope=("index" if index else "instrument") if instrument else "unverified_instrument",
                clipped=clipped, scope=scope,
            ),
            partial=clipped or not instrument or not name or (not stamp and scope != "static") or (not unit and scope != "static"),
            note="；".join(notes),
        ))
    return entries


def normalize_query_data(raw: str, tool: str, arguments: dict, *, text_limit: int = 800) -> ToolResult:
    logical_key, _skill_id = QUERY_TOOLS[tool]

    def error(message, code):
        return _error(arguments, message, code, tool=tool)

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return error("同花顺问财接口返回非 JSON 响应", "gateway_invalid_response")
    if not isinstance(payload, dict):
        return error("同花顺问财接口返回结构异常", "gateway_invalid_response")
    # The official example omits status_code; observed production includes it.
    # When present, a failure flag must override any accompanying datas.
    if "status_code" in payload and (type(payload["status_code"]) is not int or payload["status_code"] != 0):
        return error("同花顺问财接口业务状态非成功", "gateway_business_error")
    if payload.get("error") or payload.get("success") is False:
        return error("同花顺问财接口返回业务错误", "gateway_business_error")
    rows = payload.get("datas")
    if not isinstance(rows, list):
        return error("同花顺问财接口缺少官方 datas 列表", "gateway_invalid_response")
    columns = payload.get("columns")
    metadata = (
        {c["key"]: c for c in columns if isinstance(c, dict) and isinstance(c.get("key"), str)}
        if isinstance(columns, list)
        else {}
    )
    total = payload.get("code_count")
    if isinstance(total, str) and total.isdecimal():
        total = int(total)
    total_valid = type(total) is int and total >= 0
    # Official pagination is by code_count; expand_index can return multiple
    # event rows for one security. row_count is observed rows, not a total.
    has_more = total_valid and arguments["page"] * arguments["limit"] < total
    identities = [_symbol(row)[0] for row in rows if isinstance(row, dict)]
    expanded = len(rows) > arguments["limit"] or (total_valid and len(rows) > total)
    expanded |= len([s for s in identities if s]) > len({s for s in identities if s})
    retrieved = datetime.now(UTC).isoformat()
    entries = []
    rejected = 0
    row_limit = min(200, arguments["limit"]) if logical_key in _SCREENING_KEYS else 200
    for row_index, row in enumerate(rows[:row_limit], 1):
        if not isinstance(row, dict):
            rejected += 1
            continue
        if logical_key == "iwencai_macro":
            macro = _macro_datum(row, metadata, tool, row_index, retrieved)
            if macro is not None:
                entries.append(macro)
            else:
                rejected += 1
            continue
        if logical_key in _REFERENCE_REQUESTS:
            reference = _reference_datums(row, metadata, tool, logical_key, row_index, retrieved, text_limit)
            entries.extend(reference)
            rejected += not reference
            continue
        if logical_key in _COMPANY_REQUESTS:
            typed = _company_reference_datums(row, metadata, tool, logical_key, row_index, retrieved, text_limit)
            entries.extend(typed)
            rejected += not typed
            continue
        if logical_key in {"iwencai_index", "iwencai_astock_selector"}:
            typed = _index_or_screening_datums(row, metadata, tool, logical_key, row_index, retrieved, text_limit)
            entries.extend(typed)
            rejected += not typed
            continue
        instrument, name = _symbol(row)
        domain = (
            "hk_stock"
            if instrument and instrument.startswith("HK")
            else "a_share"
            if instrument and re.fullmatch(r"(?:SH|SZ|BJ)?\d{6}", instrument)
            else "reference"
        )
        event_date = next((date for key in _EVENT_DATES if (date := _single_date(row.get(key)))), None)
        event_fields_present = any(
            _present(value) and _EVENT_FIELDS.search(field + " " + str(metadata.get(field, {}).get("index_name", "")))
            for field, value in row.items()
        )
        event_date_fields = set(_EVENT_DATES)
        if event_fields_present:
            event_date_fields.update(
                field for field in ("变动日期", "事件日期") if metadata.get(field, {}).get("type") == "DATE"
            )
            event_date = event_date or next(
                (
                    date
                    for field in ("变动日期", "事件日期")
                    if field in event_date_fields and (date := _single_date(row.get(field)))
                ),
                None,
            )
        before = len(entries)
        for field, value in row.items():
            base_field = field.split("[")[0]
            if (
                (logical_key != "iwencai_market" and base_field in _QUOTE_FIELDS)
                or base_field in {"序号", "排名"}
                or base_field in _RESPONSE_METADATA
            ):
                continue
            if logical_key != "iwencai_basicinfo" and base_field in (*_CODE_FIELDS, *_NAME_FIELDS):
                continue
            if not _present(value):
                continue
            column = metadata.get(field, {})
            semantic_field = field + " " + str(column.get("index_name", ""))
            if logical_key == "iwencai_finance" and not (
                _FINANCE_FIELDS.search(semantic_field) or column.get("label") == "财务报表"
            ):
                continue
            if logical_key == "iwencai_events" and not (
                _EVENT_FIELDS.search(semantic_field)
                or (event_date and base_field in {*event_date_fields, "方式", "地点", "内容"})
            ):
                continue
            if logical_key == "iwencai_basicinfo" and not _BASIC_FIELDS.search(semantic_field):
                continue
            if logical_key == "iwencai_market" and not _MARKET_FIELDS.search(semantic_field):
                continue
            if logical_key == "iwencai_basicinfo":
                # Listing/maturity dates are static property values, not the
                # observation date for all the product's other attributes.
                stamp = None
            else:
                stamp = _single_date(column.get("timestamp")) or normalize_date(field)
                if logical_key == "iwencai_events":
                    stamp = _single_date(value) if base_field in event_date_fields else stamp or event_date
            normalized_value, clipped = _bounded(value, max(1, text_limit))
            notes = []
            if clipped:
                notes.append("字段展示已截断，完整返回保留于 raw")
            if not instrument:
                notes.append("上游未提供可确认的标的代码")
            if logical_key == "iwencai_basicinfo":
                notes.append("静态资料；上市/到期日期属于属性值，检索时间不代表属性生效日期")
            elif stamp is None:
                notes.append("上游未给出该字段的数据日期，不能用检索时间代替")
            if logical_key == "iwencai_market" and column.get("index_name"):
                notes.append(f"上游指标：{column['index_name']}；保留原始复权口径与时间粒度")
            entries.append(
                NormalizedDatum(
                    tool=tool,
                    domain=domain,
                    instrument=instrument,
                    instrument_name=name,
                    metric=f"{logical_key}[{row_index}].{field}",
                    value=normalized_value,
                    unit=column.get("unit") if isinstance(column.get("unit"), str) and column["unit"] else None,
                    timestamp=stamp,
                    as_of_date=stamp,
                    retrieved_at=retrieved,
                    source=SOURCE,
                    authority=SOURCE,
                    instrument_scope="instrument" if instrument else "unverified_instrument",
                    completeness=_datum_completeness(
                        as_of_date=stamp,
                        value=normalized_value,
                        instrument=instrument,
                        instrument_scope="instrument" if instrument else "unverified_instrument",
                        clipped=clipped,
                        scope="static" if logical_key == "iwencai_basicinfo" else None,
                    ),
                    note="；".join(notes) or None,
                )
            )
        if len(entries) == before:
            rejected += 1
    missing_market_fields = []
    if logical_key in {"iwencai_index", "iwencai_astock_selector"}:
        requests = _INDEX_REQUESTS if logical_key == "iwencai_index" else _SCREENING_REQUESTS
        requested = {
            name: fields for name, (query, fields) in requests.items()
            if re.search(query, arguments["query"], re.IGNORECASE)
        }
        semantics = " ".join(d.metric + " " + str(d.value.get("indicator", "")) for d in entries)
        missing_market_fields = [name for name, fields in requested.items() if not re.search(fields, semantics, re.IGNORECASE)]
        if requested and len(missing_market_fields) == len(requested):
            entries = []
    if logical_key in _REFERENCE_REQUESTS:
        requested = {
            name: fields
            for name, (query, fields) in _REFERENCE_REQUESTS[logical_key].items()
            if re.search(query, arguments["query"], re.IGNORECASE)
        }
        semantics = " ".join(d.metric + " " + str(d.value.get("indicator", "")) for d in entries)
        missing_market_fields = [
            name for name, fields in requested.items() if not re.search(fields, semantics, re.IGNORECASE)
        ]
        if requested and len(missing_market_fields) == len(requested):
            entries = []
    if logical_key in _COMPANY_REQUESTS:
        requested = {
            name: fields for name, (query, fields) in _COMPANY_REQUESTS[logical_key].items()
            if re.search(query, arguments["query"], re.IGNORECASE)
        }
        semantics = " ".join(d.metric + " " + str(d.value.get("indicator", "")) for d in entries)
        missing_market_fields = [name for name, fields in requested.items() if not re.search(fields, semantics, re.IGNORECASE)]
        if requested and len(missing_market_fields) == len(requested):
            entries = []
    if logical_key == "iwencai_market":
        requested = {
            name: fields
            for name, (query, fields) in _MARKET_REQUESTS.items()
            if re.search(query, arguments["query"], re.IGNORECASE)
        }
        semantics = " ".join(
            d.metric + " " + str(metadata.get(d.metric.split(".", 1)[1], {}).get("index_name", "")) for d in entries
        )
        missing_market_fields = [
            name for name, fields in requested.items() if not re.search(fields, semantics, re.IGNORECASE)
        ]
        if requested and len(missing_market_fields) == len(requested):
            entries = []  # Default prices cannot satisfy a funds/technical query.
    if not entries:
        return error(
            "未返回有效目标字段（代码/简称不算指标，默认行情不算财务/事件/宏观数据）；可保留标的与时间重新查询，或访问 https://www.iwencai.com/unifiedwap/chat",
            "empty_normalized",
        )
    partial = has_more or rejected > 0 or expanded or len(rows) > 200 or not total_valid or bool(missing_market_fields)
    partial |= any(
        d.partial
        if logical_key == "iwencai_macro" or logical_key in _REFERENCE_REQUESTS or logical_key in _COMPANY_REQUESTS or logical_key in {"iwencai_index", "iwencai_astock_selector"}
        else d.instrument is None or (logical_key != "iwencai_basicinfo" and d.timestamp is None)
        for d in entries
    )
    count_label = "上游记录数" if logical_key == "iwencai_macro" else "上游标的数"
    note = f"数据来源：{SOURCE}；第{arguments['page']}页，返回{len(rows)}行；{count_label} code_count={total if total_valid else '未知'}。最终查询：{arguments['query']}"
    if logical_key == "iwencai_market":
        note += "；数据日期/复权口径按字段提供，缺时间字段不作为已确认实时行情；最新价与历史字段不能混用日期"
        if missing_market_fields:
            note += "；未返回所问字段：" + "、".join(missing_market_fields)
    if logical_key in _REFERENCE_REQUESTS:
        note += "；保留每行身份与原日期；公告日、字段日期和报告期不能互换"
        if missing_market_fields:
            note += "；未返回所问字段：" + "、".join(missing_market_fields)
    if logical_key in {"iwencai_index", "iwencai_astock_selector"}:
        note += "；代码/名称必须由上游结构化字段配对；每项指标保留实际日期和原单位"
        if missing_market_fields:
            note += "；未返回所问字段：" + "、".join(missing_market_fields)
    if logical_key in _SCREENING_KEYS:
        # No automatic filter evaluator exists yet. A returned candidate page
        # is useful field evidence, not proof that every requested filter held.
        partial = True
        note += "；仅为上游筛选候选页，筛选条件未逐项独立验证，code_count不代表全市场数量"
    if logical_key in _COMPANY_REQUESTS:
        note += "；经营关系、机构观点及对应日期按结构化字段保留，默认行情不算目标数据"
        if missing_market_fields:
            note += "；未返回所问字段：" + "、".join(missing_market_fields)
    if has_more:
        note += f"；还有后续页，可使用 page={arguments['page'] + 1} 查询，不得声称当前页覆盖全部结果"
    if rejected:
        note += "；部分行没有有效目标字段"
    if expanded:
        note += "；上游行数超出limit或存在展开行，页码不保证完整覆盖" if logical_key in {"iwencai_index", *_SCREENING_KEYS} else (
            "；同一标的可能展开多行，股东总数及覆盖范围未知，code_count 不代表股东总数"
            if logical_key == "iwencai_management"
            else "；同一标的可能展开多条经营/研报记录，记录总数及覆盖范围未知，code_count 不代表业务或研报总数"
            if logical_key in _COMPANY_REQUESTS
            else "；同一标的可能展开多行，事件总数及覆盖范围未知，code_count 不代表事件总数"
        )
    if logical_key in _SCREENING_KEYS and len(rows) > row_limit:
        note += f"；标准化仅保留前{row_limit}行，完整上游响应保留于 raw"
    if len(rows) > 200:
        note += "；标准化最多保留200行，其余行保留于 raw"
    if partial:
        note += "；结果存在分页、字段或来源时间覆盖限制"
    return ToolResult(
        tool=tool,
        operation_id=tool,
        tool_key=logical_key,
        arguments=arguments,
        raw=raw,
        status="partial" if partial else "success",
        partial=partial,
        normalized=entries,
        retrieved_at=retrieved,
        authority=SOURCE,
        completeness="partial" if partial else "page",
        reason_codes=(
            ["screening_conditions_not_verified"]
            + (["upstream_page_size_exceeded"] if len(rows) > arguments["limit"] else [])
            if logical_key in _SCREENING_KEYS else []
        ) + (["expanded_rows_coverage_unknown"] if expanded and logical_key in _COMPANY_REQUESTS else [])
        + (["missing_requested_fields"] if missing_market_fields and logical_key in _COMPANY_REQUESTS else [])
        + (["missing_institution_provenance"] if logical_key == "iwencai_insresearch" and any(not d.value.get("institution_name") for d in entries) else [])
        + (["missing_publication_date"] if logical_key == "iwencai_insresearch" and any(not d.value.get("publication_date") for d in entries) else []),
        note=note,
    )


async def query_data(
    settings: Settings,
    tool: str,
    arguments: dict,
    *,
    deadline: float | None = None,
    refresh: bool = False,
) -> ToolResult:
    _logical_key, skill_id = QUERY_TOOLS[tool]
    arguments = canonicalize_tool_arguments(tool, arguments)
    return await _call_iwencai(
        settings,
        tool,
        arguments,
        skill_id=skill_id,
        endpoint="/v1/query2data",
        body={
            "query": arguments["query"],
            "page": str(arguments["page"]),
            "limit": str(arguments["limit"]),
            "is_cache": "1" if arguments["is_cache"] and not refresh else "0",
            "expand_index": "true" if arguments["expand_index"] else "false",
        },
        normalizer=lambda raw: normalize_query_data(raw, tool, arguments, text_limit=settings.news_max_text_chars),
        deadline=deadline,
        retry_header=True,
    )
