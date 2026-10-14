# Tool Registry

工具按市场域和分析员类别筛选，避免把全部 63 个 Tool 无约束交给 Planner。

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
| announcement_search | internal_iwencai_announcement_search | 同花顺问财官方公告：A股/港股/基金/ETF；财报、分红、回购增持、重组等（query必填，size可选1..20；需IWENCAI_API_KEY） |
| iwencai_finance | internal_iwencai_finance_query | 官方财务指标：营收、净利润、ROE、负债率、现金流等；对应问题必须调用（query必填，page/limit可选，limit<=20） |
| iwencai_events | internal_iwencai_event_query | 官方事件：业绩预告、增发配股、质押、解禁、机构调研、监管函等；对应问题必须调用（参数同上） |
| iwencai_basicinfo | internal_iwencai_basicinfo_query | 官方基本资料：股票/基金/期货/债券等的上市、发行、费率、合约信息；对应问题必须调用（参数同上） |
| iwencai_report_search | internal_iwencai_report_search | 官方研报正文片段：机构、评级、观点和可用的目标价；query必填，size可选1..20；同文档片段不是多份研报 |
| iwencai_market | internal_iwencai_market_query | 官方股票/ETF/指数行情、资金流向和技术指标；对应证券行情问题必须调用；query、page/limit；保留实际日期和复权口径 |
| iwencai_news_search | internal_iwencai_news_search | 官方财经新闻检索，保留原媒体和发布日期；query、size；与现有DDGS的news_search是两个独立工具 |
| iwencai_macro | internal_iwencai_macro_query | 官方GDP/CPI/利率/社融等宏观指标；对应问题必须调用；query、page/limit；国家地区、统计期和单位保留 |
| iwencai_industry | internal_iwencai_industry_query | 官方行业估值、财务、盈利、行情及板块排名；对应问题必须调用；保留行业/指数身份、分类体系、字段日期和单位 |
| iwencai_management | internal_iwencai_management_query | 官方股本结构、股东户数、十大股东、实控人及质押；对应问题必须调用；股东名称/排名逐行保留，公告日不是持仓截止日 |
| iwencai_index | internal_iwencai_index_query | 官方指数点位、涨跌幅、成交量；对应指数问题必须调用；保留明确指数身份、字段日期及单位 |
| iwencai_astock_selector | internal_iwencai_astock_selector | 官方A股多条件选股；保留原query/日期/全部条件；候选页及分页不代表完整筛选验收 |
| iwencai_sector_selector | internal_iwencai_sector_selector | 官方板块筛选；保留行业/概念/地域分类、全部条件及每项日期；候选页始终partial |
| iwencai_business | internal_iwencai_business_query | 官方公司经营数据；保留公司与项目/客户/供应商身份、报告期及合同事件日 |
| iwencai_insresearch | internal_iwencai_insresearch_query | 官方机构评级与预测；保留机构、发布日、预测期及研报链接；机构观点不等于已实现事实 |

完整清单见 `app/gateway/tool_registry.py`（63 个逻辑工具，55 个唯一 operationId，覆盖 A股/Crypto/港股/大宗商品/美股/宏观/新闻/公告/资料）。

同花顺十五类工具共用 `IWENCAI_API_KEY`。配置、必用工具补齐、真实验证命令和能力边界见 [iwencai-tools.md](iwencai-tools.md)。

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
