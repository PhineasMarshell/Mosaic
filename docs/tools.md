# Tool Registry

第一阶段不是把全部 47 个 Tool 无约束交给 Planner。

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
| news_search | news_search | DDGS 新闻舆情搜索(跨域通用，**单源兜底**；query 必填) |
| symbol_news | internal_symbol_news | A股个股新闻聚合：东财个股新闻 + Google 资讯，跨源去重带来源(a_share 域；symbol 必填) |
| telegraph | internal_market_telegraph | 财联社电报快讯：全市场最新电报流(a_share 域；可选 keyword 过滤标题/内容) |
| news_digest | internal_news_digest | 多源新闻聚合搜索：Google 资讯 + DDGS 双源去重(cross 域；query 必填，不限市场域) |

完整清单见 `app/gateway/tool_registry.py`（47 个工具，覆盖 A股/Crypto/港股/大宗商品/美股/新闻）。

新闻工具的已知边界（2026-10-08 实测）：

- DDGS **限流是常态**（首测成、随后 5 连败）：只作兜底，失败自动重试 1 次，永不作为唯一源。
- Google 资讯 RSS **硬依赖本机代理**（直连 ConnectTimeout）：代理断开时该源记 `sources_failed`，
  聚合工具降级为 `status=partial`，东财/电报主路照常出 datum。
- 财联社电报**无链接字段**（实测列里没有 url）：datum 允许空 url，Critic 不得因缺 url 判无效。
- `news_meta.multi_source_titles` 统计**去重前合池**中「标题归一后出现于 ≥2 个独立来源」的条数，
  是 Critic 判断「已证实」的抓手（去重后会恒为 0，因此不能在后置池上算）。

后续新增 Tool 时：

1. 在 Registry 增加业务 key。
2. 添加 purpose/domain/priority。
3. Planner Prompt 自动可见。
4. 不修改 Orchestrator 主流程。
