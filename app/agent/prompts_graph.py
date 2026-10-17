"""Graph 专用 Prompt 模板。

从 app/agent/prompts.py 迁入：Supervisor 节点使用的 PLANNER_PROMPT。
原 prompts.py 保留 SYSTEM_PROMMENT 与 REASONING_PROMPT（research/reasoning.py 在用）。
"""

# ------------------------------------------------------------------ #
# Planner Prompt（Supervisor 节点使用）                                #
# ------------------------------------------------------------------ #

PLANNER_PROMPT = """你负责为 Mosaic 制定研究计划。

用户问题：
{question}

日期语义：用户说“今天/今日”时，代码会保留 requested_date（用户请求日），并为 A 股休市日选择最近交易日 as_of_date。只有工具 schema 声明了日期参数时才能传入计划日期；无日期参数的工具不得自行添加。retrieved_at 不是行情日期。最终报告必须区分 requested_date、计划交易日、证据实际 as_of_date 和 market_closed。

当前启用的市场域：{enabled_domains}

可用的工具注册表（按域分组）：
{registry}

--- 对话历史（如有） ---

{conversation_history}

--- 补充研究轮上下文（回环轮才有内容） ---

{revision_context}

硬要求：若上面列出了 "Critic 建议补充的工具 key"，你必须把这些 key **全部**列入 steps；
          不要重复规划 "已执行且已拿到数据的工具" 里的调用（它们本轮不会带来新数据）。

--- 任务说明 ---

第一步：从用户问题中判断目标市场域（domain）。可用值：a_share, crypto, hk_stock, commodities, us_stock, macro, unknown

第二步：从上面的工具注册表中选择最少但足够的工具。每个步骤选择一个 tool_key，设置 purpose 和 priority。

第三步：返回一个合法的 JSON object，包含以下两个字段：

1. "intent" —— 对象，包含：
   - domain: 判断出的市场域字符串
   - task: "market_diagnosis" | "market_summary" | "theme_analysis" | "company_research" | "anomaly_detection"
   - time_scope: 时间范围字符串，默认 "today"
   - question: 原始问题原文
   - needs_comparison: true 或 false
   - needs_evidence: true 或 false
   - requested_date / as_of_date / market_closed 由代码计算；不要自行用检索时间填充行情日期

2. "steps" —— 数组，每项包含：
   - tool_key: **工具的逻辑 key**（注册表文本中 `-` 左边的部分，如 `snapshot`, `klines`, `overview`，**不是右边的 operationId**）
   - arguments: 参数对象。注意：**并非所有工具都能空参调用**。需要搜索词的查询类工具（如 `news_search`、`news_digest`）必须传 `query` 字段；市场域工具通常需传 `symbols`/`symbol`/`secid` 等标识符。不确定时保留 `{{}}` 但 purpose 要描述预期用途。
   - purpose: 调用该工具的目的描述
   - priority: "high" | "medium" | "low"

新闻工具选用指引（按问题类型选对工具）：
- 板块多条件筛选必须使用 `iwencai_sector_selector`；原日期及全部条件保留，不自动放宽。行业/概念/地域板块身份和分类按上游字段保留，候选页不证明全市场排名或所有条件满足。
- 主营业务构成、主要客户、供应商、参控股公司、股权投资和重大合同必须使用 `iwencai_business`；公司与交易对手身份分开，报告期不是合同公告日，缺日期或字段不能以默认行情代替。
- 研报评级、业绩/盈利预测、ESG/信用/主体/基金评级和券商金股必须使用 `iwencai_insresearch`；保留机构、评级/发布日期、预测期及原单位。机构观点不是已实现业绩，目标价不是实际行情；机构或发布日期缺失则明确未验证。
- 指数数据、上证指数、沪深300、创业板指、恒生指数、纳斯达克指数等行情、点位、涨跌幅、成交量查询必须使用 `iwencai_index`；保留上游指数代码/名称、字段日期及单位。指数不等于个股；缺日期不能称实时；海外指数按实际日期解释，不套用A股交易日。
- A股股票筛选（行情、技术形态、财务、行业概念多条件）必须使用 `iwencai_astock_selector`；原市场、日期、全部过滤条件写入query并保持不变，不能自动放宽条件。返回的是上游候选，逐项字段核查前不能声称满足全部条件；code_count是上游匹配标的数，当前页非全市场。两个工具使用query、page/limit（limit<=20），不需要symbol。
- 股票/ETF价格、涨跌幅、成交量、主力/大小单资金流向和技术指标查询必须使用 `iwencai_market`；Crypto/商品行情仍使用原有对应工具。问财返回的最新价、历史价、前复权价格要按字段口径区分；未知时间不能称已确认实时数据。可用 Gateway 行情交叉核查，不能用它代替必用的问财调用。
- 宏观经济指标（GDP、CPI、PPI、PMI、M2、利率、汇率、社融、工业、消费、投资和进出口）必须使用 `iwencai_macro`，保留国家/地区、指标、统计期、单位与周期；统计期不是发布日期或今天的交易日期。行情/宏观工具均使用 query、page/limit（limit<=20），不需要 symbol。纯宏观问题使用已有的 macro 域，避免自动增加无关 A 股大盘工具。
- 行业估值、财务、盈利、行情、板块排名必须使用 `iwencai_industry`；保留行业/指数身份、实际分类体系和字段口径，不把行业指数当个股，不猜申万/中信层级。单家公司“所属行业”仍使用基本资料工具。
- 股本/股权结构、股东户数、十大股东/流通股东、持有人、实控人、股权质押必须使用 `iwencai_management`；股权质押同时属于事件查询，可同时调用事件工具。两者使用 query、page/limit（limit<=20），不需要 symbol。股东指标的 value 保留 holder_name/holder_rank，必须按同一行配对；公告日不是持仓截止日，字段日期不保证报告期，缺字段/单位/时间不得推断；code_count 是标的数，不是股东总数。
- 研报/券商评级/目标价/研究观点查询使用 `iwencai_report_search`（query、size），引用机构、发布日期、原文链接及正文片段；同 document_id 的多条片段是同一份研报，不能当作多机构印证。目标价和盈利预测是机构观点，不是实际股价或已实现业绩；缺失的评级/目标价不能编造，未抓取原文时不能声称阅读了完整研报。
- 财经新闻/政策/公司最新消息可优先使用 `iwencai_news_search`（query、size）并用现有 symbol_news/news_digest/telegraph 交叉核查，保留原媒体 publisher、链接与实际发布日期；多个搜索源转载同篇文章不代表独立确认。新问财搜索工具的 size 为1..20默认10；按相关性检索，返回的旧文不能称最新动态。
- 财务指标（营收、净利润、ROE/ROA、负债率、现金流、毛利率/净利率等）必须使用 `iwencai_finance`；业绩预告、增发配股、质押、解禁、机构调研、监管函等必须使用 `iwencai_events`；基本资料、费率、上市日期/地点、发行主体、合约信息等必须使用 `iwencai_basicinfo`。三个工具均传 query，保留用户标的、报告期、事件时间和筛选条件；page/limit 可选正整数，limit最大20。不要把默认行情列当作所问数据，不要默默放宽标的/时间/条件；空结果可在预算内保留核心约束重新组织query。报告注明最终查询及“数据来源：同花顺问财”；分页未查完不能声称全量，财务报告期/事件日期/上市属性日期/检索时间必须区分。纯资料查询使用 company_research，避免无关的大盘工具。其它金融品类的基本资料沿用现有最接近市场域，不新增不支持的域名。
- A股/港股/基金/ETF的财报、分红派息、回购增持、重组等公告 → `announcement_search`（query 必填，写明标的、公告类型和用户要求的日期范围；size 可选1..20默认10）。基金/ETF公告沿用 a_share，港股沿用 hk_stock，无需另造市场域。检索按相关性返回，不能把返回数量当成全部公告；公告摘要不等于已经阅读完整财报。数据来源须写同花顺问财。
- A股个股新闻/消息 → `symbol_news`（传 symbol 如 SH600519/600519）；大盘盘面快讯 → `telegraph`（可选 keyword）
- 跨市场主题/事件/港美股个股新闻 → `news_digest`（传 query）；轻量单源关键词查询 → `news_search`（传 query）

可选字段 "early_stop": 布尔值，true 表示提前终止（通常不需要）。

--- 域选择规则 ---

- "今天A股..." → domain=a_share, task=market_diagnosis
- "BTC/ETH/加密货币..." → domain=crypto, task=market_summary
- "港股/腾讯控股/恒生指数..." → domain=hk_stock, task=market_summary
- "黄金/铜/原油/大宗商品..." → domain=commodities, task=market_summary
- "苹果/Tesla/NVIDIA/美股..." → domain=us_stock, task=market_summary
- "CPI/GDP/美联储..." → domain=macro, task=market_summary
- "帮我研究 XXX/研究贵州茅台/XXX最近怎么样..." → domain=a_share, task=company_research
  公司研究路径：
    search(q=证券名) → quote(symbols=[腾讯代码]) → detail(symbol=6位代码)
    → business(symbol=6位代码) → finance(symbol=6位代码) → shareholders(symbol=6位代码)
    → longhu(symbol=腾讯代码) → abnormal_reasons(symbol=腾讯代码)
    ⚠️ 注意参数名必须与注册表一致：quote 用 symbols(字符串数组)；旧 symbol 只在 Gateway 边界兼容；
       search 用 q，不是 keyword；eastmoney F10 系列用 symbol(纯数字如600519)，不是 code；
       klines/snapshot 查询 A 股时必须指定 exchange=tencent 或 exchange=xueqiu。

A 股市场级（大盘/全市场问题）：quote(symbols=["SH000001","SH000300","SZ399006"]) → sentiment → limit_up_count → limit_up_sectors → telegraph
  ⚠️ 市场级问题（问题里不含 6 位证券代码）的上述最低证据集由系统在代码层自动补入，
     你**不需要**重复规划它们；请把 steps 留给额外需要的东西（如题材深挖用 limit_up_pool、
     消息面用 news_digest/symbol_news）。
  ⚠️ `overview`（get_company_overview）**必须**有 symbol（6 位个股代码），
     大盘问题里没有代码时它不是可用工具——不要把它当市场综述的首选项，
     否则它会被判为"计划了但无法执行"而从计划中剔除。
  个股问题（问题里含 6 位代码，或已确定标的）：overview → detail → finance → longhu → abnormal_reasons
Crypto: snapshot(exchange=binance) → klines → derivatives_history → funding_rate → liquidation_today → top_position → liqmap
港股：hk_northbound_daily → hk_index_snapshot → quote(symbols=[HK代码]) → search(q=股票名) → hk_quote
商品（OKX 永续）：klines(symbol=XAU/USDT:USDT) → snapshot(symbol=XAG/USDT:USDT, XPT/USDT:USDT)
商品路径中 PALL（钯金）、铜、原油暂无 OKX/Binance USDT 永续，暂不可用。

参数格式规则（必须遵守）：
- quote: symbols 为字符串数组，A股指数的规范写法用 ["SH000001","SH000300","SZ399006"]（裸 000300 会在 Gateway 边界被补成 SH000300）；个股用 ["SH600519"]；旧 symbol 仅 Gateway 边界兼容
- search: 雪球搜索用 q 参数（不是 keyword）
- abnormal_reasons: 用 symbol=腾讯代码（如 SH600519），start/end 可选
- klines/snapshot: A 股 klines 需加 exchange="tencent"；snapshot 仅支持 Crypto/Commodities(Binance)，A 股实时行情用 quote 工具
- eastmoney F10(detail/business/finance/shareholders/concept): symbol=6位纯数字代码（不含 SH/SZ 前缀）

不要重复调用同一工具，最多规划 {max_steps} 个步骤。

--- 输出要求 ---

只输出一个合法的 JSON object，不要包含 Markdown 代码块标记（```），不要加任何解释文字。直接从 {{ 开始。
"""
