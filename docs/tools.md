# Tool Registry

第一阶段不是把全部 44 个 Tool 无约束交给 Planner。

核心逻辑工具：

| key | operationId | 用途 |
|---|---|---|
| overview | get_company_overview | 全市场 |
| sentiment | get_ashare_sentiment | 情绪 |
| limit_up_count | get_limit_up_count | 涨停数量 |
| limit_up_sectors | list_limit_up_sectors | 题材 |
| limit_up_pool | list_limit_up_stocks | 涨停池 |
| quote | get_market_quotes | 行情 |
| longhu | get_stock_longhu | 龙虎榜 |
| abnormal_reasons | get_stock_abnormal_reasons | 异动 |
| us_klines | get_market_klines | 美股历史K线(雪球通道, 裸代码) |
| us_window | get_market_window | 美股复盘时间窗(雪球通道) |
| us_fundamentals | internal_us_fundamentals | 美股基本面：营收/净利/EPS/毛利年报+季报(SEC EDGAR XBRL 直连，需 SEC_EDGAR_CONTACT) |
| us_filings_recent | internal_us_filings_recent | 美股近期 SEC 申报：最近 10 份 10-K/10-Q/8-K(直连，需 SEC_EDGAR_CONTACT) |

完整清单见 `app/gateway/tool_registry.py`（44 个工具，覆盖 A股/Crypto/港股/大宗商品/美股）。

后续新增 Tool 时：

1. 在 Registry 增加业务 key。
2. 添加 purpose/domain/priority。
3. Planner Prompt 自动可见。
4. 不修改 Orchestrator 主流程。
