"""Deterministic required Skill calls for explicit reference-data questions."""

from __future__ import annotations

import re

from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.iwencai_contracts import IWENCAI_TOOL_KEYS
from app.gateway.tool_registry import resolve_tool
from app.models.research import ResearchPlan, ToolCallPlan


def _announcement_query(question: str) -> str:
    """Keep only explicit security/date constraints for the search Skill."""
    code = re.search(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", question, re.IGNORECASE)
    month = re.search(r"(20\d{2})\s*(?:年|[-/])\s*(0?[1-9]|1[0-2])\s*月?", question)
    parts = [code.group(0).upper() if code else ""]
    if month:
        parts.append(f"{month.group(1)}年{int(month.group(2))}月")
    parts.append("公告")
    return " ".join(part for part in parts if part)

_PATTERNS = {
    "iwencai_sector_selector": re.compile(
        r"选板块|板块筛选|筛选.{0,25}(?:板块|行业)|(?:板块|行业).{0,25}(?:筛选|符合|满足)|"
        r"(?:哪些|选出|挑选).{0,30}(?:板块|行业)|(?:板块|行业).{0,20}(?:涨幅前|涨幅最大|跌幅榜|资金流入排名)|"
        r"(?:涨幅前|涨幅最大|跌幅榜|资金净流入|资金流入).{0,20}(?:板块|行业)",
        re.IGNORECASE,
    ),
    "iwencai_business": re.compile(
        r"公司经营|经营数据|主营业务|业务构成|主要客户|客户名称|供应商|参控股公司|参股公司|子公司|股权投资|重大合同|业务类型|"
        r"产品.{0,12}(?:收入|占比)|地区.{0,12}(?:收入|占比)",
    ),
    "iwencai_insresearch": re.compile(
        r"机构研究|机构观点|机构评级|研报评级|投资评级|业绩预测|盈利预测|预测.{0,12}(?:净利润|营收|EPS)|"
        r"ESG|信用评级|主体评级|基金评级|星级评定|券商金股|目标价",
        re.IGNORECASE,
    ),
    "iwencai_index": re.compile(
        r"指数(?:数据|行情|价格|点位|涨跌|涨幅|跌幅|成交)|上证指数|深证成指|创业板指|沪深\s*300|"
        r"中证\s*(?:500|1000)|科创\s*50|上证\s*50|恒生指数|纳斯达克(?:指数)?|道琼斯(?:指数)?|标普\s*500",
        re.IGNORECASE,
    ),
    "iwencai_astock_selector": re.compile(
        r"选股|筛选.{0,20}(?:A\s*股|股票|个股)|(?:A\s*股|股票|个股).{0,20}筛选|问财选A股|"
        r"(?:选出|挑选|选一些).{0,30}(?:A\s*股|股票|个股)|哪些.{0,15}(?:A\s*股|股票|个股)|"
        r"(?:符合|满足).{0,80}(?:股票|个股)",
        re.IGNORECASE,
    ),
    "iwencai_industry": re.compile(
        r"行业(?:数据|估值|排名|财务|盈利|行情|市盈率|市净率|涨跌|涨幅)|板块(?:排名|数据|估值|行情|涨跌|涨幅)|"
        r"(?:行业|板块).{0,12}(?:估值|盈利|行情|排名|市盈率|市净率|营收|营业收入|净利润|涨|跌|怎么样)|"
        r"(?:银行业|保险业|制造业).{0,12}(?:盈利|估值|财务|行情|排名)",
        re.IGNORECASE,
    ),
    "iwencai_management": re.compile(
        r"股本|股权结构|股东(?!大会)|持股|持有人|实控人|实际控制人|控股|股权质押|高管|董事会|监事会",
    ),
    "iwencai_finance": re.compile(
        r"财务(?:数据|指标|报表)?|营业收入|营收|净利润|毛利率|净利率|负债率|负债情况|现金流|"
        r"净资产收益率|市盈率|市净率|市销率|赚了多少|(?<![A-Z])RO[EA](?![A-Z])",
        re.IGNORECASE,
    ),
    "iwencai_events": re.compile(
        r"业绩预告|业绩预增|业绩预减|扭亏|增发|配股|质押|解禁|机构调研|调研记录|"
        r"监管函|问询函|警示函|股东大会",
    ),
    "iwencai_basicinfo": re.compile(
        r"基本资料|基本信息|股票信息|基金资料|债券资料|合约信息|合约详情|费率|"
        r"上市日期|上市地点|(?:什么时候|何时)上市|基金管理人|发行主体|交割方式|到期日|"
        r"行权价|票面利率|所属行业|股票代码|证券代码|基金代码|指数成分股",
    ),
    "iwencai_market": re.compile(
        r"行情|股价|股票价格|ETF价格|指数价格|最新价|现价|收盘价|开盘价|涨跌幅|涨幅|跌幅|成交量|成交额|资金流向|资金净流入|资金流入|资金流出|"
        r"主力资金|大单|小单|技术指标|均线|布林|(?<![A-Z])(?:MACD|RSI|KDJ|BOLL)(?![A-Z])",
        re.IGNORECASE,
    ),
    "iwencai_macro": re.compile(
        r"宏观|国内生产总值|社会融资|社融|货币供应|工业增加值|社会消费品|固定资产投资|进出口|进口总额|出口总额|"
        r"居民消费价格|生产者价格|利率|汇率|(?<![A-Z0-9])(?:GDP|CPI|PPI|PMI|M[012]|LPR)(?![A-Z0-9])",
        re.IGNORECASE,
    ),
}


def required_query_keys(question: str, domain: str | None = None) -> list[str]:
    # Derivative funding rates are market data, not static product fees.
    basic_question = re.sub(r"资金费率|融资费率|永续费率|funding\s+rates?", "", question, flags=re.IGNORECASE)
    macro_question = re.sub(r"净利率|毛利率|票面利率", "", question)
    securities_explicit = bool(
        re.search(r"A股|港股|美股|股票|ETF|沪深|上证|恒生|纳斯达克|标普", question, re.IGNORECASE)
    )
    other_market = domain in {"crypto", "commodities"} or bool(
        re.search(
            r"(?<![A-Z0-9])(?:BTC|ETH|SOL|USDT)(?![A-Z0-9])|比特币|以太坊|加密|永续|黄金|白银|原油",
            question,
            re.IGNORECASE,
        )
    )
    index_query = bool(_PATTERNS["iwencai_index"].search(question)) and not bool(
        re.search(r"指数成分股|指数编制|指数基本资料", question)
        and not _PATTERNS["iwencai_market"].search(question)
    )
    stock_screening = bool(_PATTERNS["iwencai_astock_selector"].search(question)) and (
        bool(re.search(r"A\s*股", question, re.IGNORECASE))
        or domain not in {"hk_stock", "us_stock", "crypto", "commodities", "macro"}
        and not re.search(r"港股|美股|基金|ETF|比特币|加密|期货|指数", question, re.IGNORECASE)
    )
    individual_market = bool(re.search(r"股票|个股|ETF|(?<!\d)\d{6}(?!\d)", question, re.IGNORECASE))
    return [
        key
        for key, pattern in _PATTERNS.items()
        if pattern.search(
            basic_question if key == "iwencai_basicinfo" else macro_question if key == "iwencai_macro" else question
        )
        and (key != "iwencai_market" or not other_market or securities_explicit)
        and (key != "iwencai_index" or index_query)
        and (key != "iwencai_astock_selector" or stock_screening)
        and (key != "iwencai_market" or not index_query or individual_market)
    ]


def apply_required_iwencai_tools(
    plan: ResearchPlan,
    *,
    question: str,
    registry: str,
    max_steps: int,
    results: list | None = None,
) -> list[str]:
    """Prepend required calls, repair missing args, respect visible tools/budget.

    Prior calls (including failed calls) already satisfy the requirement to
    use the Skill. Further evidence collection stays under the existing Critic
    loop; it must not blindly repeat or relax the user's conditions.
    """
    required = required_query_keys(question, plan.intent.domain)
    plan.required_tool_keys = required
    forced = []
    remaining = list(plan.steps)
    for key in required:
        if f"- {key}:" not in registry:
            continue
        operation = resolve_tool(key).tool_name
        candidates = [s for s in remaining if s.tool_key == key]
        if not candidates and any(
            (r.get("operation_id") or r.get("tool")) == operation
            if isinstance(r, dict)
            else (getattr(r, "operation_id", None) or getattr(r, "tool", None)) == operation
            for r in results or []
        ):
            continue
        step = (
            candidates[0]
            if candidates
            else ToolCallPlan(
                tool_key=key,
                arguments={"query": question},
                purpose="用户明确查询对应数据，必须调用官方 Skill",
                priority="high",
            )
        )
        try:
            step.arguments = canonicalize_tool_arguments(operation, step.arguments)
        except ValueError:
            step.arguments = canonicalize_tool_arguments(operation, {"query": question})
        step.priority = "high"
        forced.append(step)
        if candidates:
            remaining.remove(step)
    # Preserve the user's full query for announcement retrieval. The planner
    # has previously reduced "600519 2026年10月公告" to an undated symbol
    # query, so the Skill returned unrelated older announcements.
    for step in [*forced, *remaining]:
        if step.tool_key == "announcement_search":
            operation = resolve_tool(step.tool_key).tool_name
            step.arguments = canonicalize_tool_arguments(
                operation, {**step.arguments, "query": _announcement_query(question)}
            )
    plan.steps = (forced + remaining)[:max_steps]
    prior_keys = {
        IWENCAI_TOOL_KEYS[op]
        for result in results or []
        if (
            op := (result.get("operation_id") or result.get("tool"))
            if isinstance(result, dict)
            else (getattr(result, "operation_id", None) or getattr(result, "tool", None))
        )
        in IWENCAI_TOOL_KEYS
    }
    planned = {s.tool_key for s in plan.steps}
    missing = [key for key in required if key not in planned and key not in prior_keys]
    return missing
