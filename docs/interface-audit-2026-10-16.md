# Mosaic 实际接口巡检

测试日期：2026-10-16，北京时间 15:48 至 16:22。结论适用于本次环境、代表性参数与测试窗口。

## 结论

反复出现的问题集中在三个层面：MCP 到 Data Gateway 的传输不稳定、部分网关上游持续失败、THS 主路由及本地工具返回的数据不满足业务契约。模型基础调用和多个新闻源在本次补测中正常。

| 优先级 | 问题 | 实测证据 | 对结论的影响 |
| --- | --- | --- | --- |
| 高 | MCP 传输反复超时 | 有效参数的 71 次独立会话探测中，30 次 error，其中 19 次明确为 TLS handshake timeout；指数报价对照一次超时、一次 29.5 秒成功，同期 HTTP 为 0.7 至 1.6 秒 | 获取行情和兜底数据的耗时、成功率不稳定 |
| 高 | 涨停与市场宽度主路由返回错误类型的数据 | `get_limit_up_count` 返回全 A 最新价/涨跌幅；`list_limit_up_sectors` 返回 empty_normalized；`get_ashare_sentiment` 缺上涨/下跌家数；加日期后仍复现 | 即使调用成功，也无法补足报告证据 |
| 高 | 涨停兜底数据缺日期或必要结构 | 直接网关涨停数能返回 count=110，但没有观察日期；板块接口返回 code/plate/concept，没有目标排名计数结构及日期 | `fallback_provenance_insufficient`，不能认定为当天证据 |
| 高 | 盘口、成交、异动及美股雪球行情持续失败 | `get_market_orderbook`、`list_market_trades`、`get_stock_abnormal_reasons`、AAPL K 线/窗口 HTTP 各两次 502 | 对应数据不可用；502 本身不能定位到源站内部哪一环 |
| 高 | 自写港股工具空数据却报 success | `internal_hk_northbound`、`internal_hk_index` 各两次零业务数据；源 URL 为 404，修正路径和恒指代码后能取到 K 线 | 掩盖底层故障，误导后续判断 |
| 中 | 龙虎榜上游鉴权/限流 | HTTP 502 后复测为 429；响应明确称“雪球 token 已失效或触发配额限制(400016)，刷新后仍不可用” | 当前调用链无法提供龙虎榜 |
| 中 | 港股代码与报价接口不兼容 | HK00700 在 HTTP/MCP 都失败；hk00700 也为 422；00700 HTTP 200 但空响应 | 代码搜索可用不代表报价可用 |
| 中 | 新闻主路由丢失原工具的时间范围语义 | 原生财联社返回当天电报；生产主路由改为问财通用新闻，混有 9 月及更早日期 | success 不代表“当日电报”或“近一周新闻” |

“71 次 / 30 次 / 19 次”是多种接口与异常复测构成的样本统计，不是随机采样的可用率。首次共享会话中的排队超时没有计入这组独立会话数字，错误衍生品参数也已排除。

## 覆盖与方法

- 项目注册的 **55 个唯一 operation** 全部执行，覆盖 **63 个逻辑注册键**对应的操作：32 个网关、15 个 THS Skill、8 个原生工具。逻辑别名未逐一重复执行。
- MCP `initialize` / `list_tools` 成功，实际目录中 **40 个工具全部调用**，其中 10 个尚未注册到项目。两个 health 操作仅在 HTTP 提供。
- 额外执行 16 个主路由操作、10 个生产 runtime 操作、跨市场参数变体、5 个原生新闻/代码表数据源、2 次模型 JSON 调用及港股路径对照。
- 六组有效产物共 **297 条记录，其中 290 条为实际探测尝试记录**。其余是目录检查、未暴露或待补测占位。一次记录可能包含初始化、重试或多次源请求，不能等同于 290 个网络请求。
- 使用项目现有开源客户端 `httpx`、MCP SDK、OpenAI 兼容客户端，以及项目自己的 adapter、normalizer、ToolRuntime。未替换业务实现。
- 异常通常复测两次；对照探测与健康单次样本在表中单独注明。MCP 复测使用独立连接，避免前一调用取消后影响后续队列。Windows 原生管道启动受限后，项目已有 blocking bridge 能完成 MCP 初始化与调用。
- 参数涵盖 SH600519、SH000001/SH000300/SZ399006、HK00700、AAPL、BTC/USDT、ETH 和金银铂。历史窗口为 2026-10-08 至 2026-10-15，非所有标的、日期、分页及边界组合。
- 本地 `/health`、`/` 返回 200。曾请求 `/api/brief/morning`，客户端 15 秒超时；源码显示该 GET 会执行研究并落库，因此不能据此判定服务故障，也未反复触发。该次请求可能继续在服务端执行。`/api/brief/evening`、`/api/ask`、`/api/ask/stream` 未做新的完整研究验收，原始失败日志另作证据。
- 诊断只新增脚本与报告；没有修复、回滚业务代码，也没有主动改动配置或清理用户数据。

解释表格时须区分：**返回数据**表示调用取得内容；**契约缺口**表示目标字段、日期、标的或结构不能满足当前消费者；**未采集**是服务声明无覆盖；**超时**只说明超过此次探测预算。不能把全部 partial 当成接口宕机。

## 32 个网关操作

HTTP 列以直接请求结果为准。MCP 列综合首轮及独立会话复测，初次共享会话超时不单独用来证明接口故障。

| operation | HTTP 结果 | MCP 结果 | 业务解释 |
| --- | --- | --- | --- |
| `get_ashare_sentiment` | 两次返回 | TLS 超时后返回 | 原始 series 含 QX、SZ、XD 等，当前归一化未满足 market_breadth 契约，不能直接断言上游没有市场宽度数据 |
| `get_limit_up_count` | 两次 partial | 两次 partial | count=110 存在，但缺日期；契约标注 mcp_missing_date |
| `list_limit_up_sectors` | 两次 partial | partial 后 TLS 超时 | code/plate/concept 与“板块涨停数量排名”结构不匹配，缺日期 |
| `list_limit_up_stocks` | 两次 partial | 初次超时，独立复测 partial | 网关有涨停池字段，日期/契约不满足；不同于 THS 主路由仅有行情字段 |
| `get_company_overview` | 两次返回 partial | 两次探测超时，含独立会话 | HTTP 有 rows 和日期字段，归一化来源/日期契约仍不完整 |
| `get_company_detail` | 两次 partial | 独立复测返回 partial | 有融资/股东等内容，嵌套日期未充分映射到业务 datum |
| `get_company_business` | 两次 partial | 独立复测返回 partial | 原始主营构成有 REPORT_DATE；归一化日期缺失 |
| `get_company_concepts` | 两次返回 | 独立复测返回 | 有板块/核心题材对象，但当前 sector_theme 契约不满足 |
| `get_company_finance` | 两次返回 | 独立复测返回 | 原始 REPORT_DATE 等字段存在；不要把所有元数据无日期当成接口失败 |
| `get_company_shareholders` | 两次返回 | 独立复测返回 | 原始 END_DATE 等存在，嵌套对象的归一化日期不完整 |
| `get_company_survey` | 两次返回 | 两次探测超时，含独立会话 | 网关返回公司概况 jbzl 等；THS 路由却映射为 events，语义不同 |
| `get_market_quotes` | A 股/指数返回，指数对照重复成功 | 指数有成功也有超时；个股样本未成功 | 当前日期、价格等可归一化；港股另见变体表 |
| `get_stock_longhu` | 502、429 | 超时/TLS 超时 | 429 明确指出雪球 token 失效或配额限制 |
| `get_stock_abnormal_reasons` | 两次 502 | 超时、源站响应不完整 | runtime 亦失败或耗尽工具预算 |
| `get_market_orderbook` | 两次 502 | 超时/TLS 超时 | 本次反复失败 |
| `list_market_trades` | 两次 502 | 超时/TLS 超时 | 本次反复失败 |
| `list_stock_discussions` | 两次返回 | 独立复测返回 | 帖子 published_at 未完整进入归一化日期；可取得真实帖子 URL |
| `search_stocks` | 两次返回 | 独立复测 TLS 超时 | 标的元数据不要求行情日期，HTTP 本身可用 |
| `get_market_klines` | BTC 样本两次返回 | 独立复测返回 | candle 时间戳存在；外围元数据无日期不应判定整批失败 |
| `get_market_snapshot` | BTC 样本返回 | 独立复测返回 | 日期与来源完整；银/铂另见变体表 |
| `get_market_window` | BTC 样本两次返回 | 独立复测返回 | 有 candle 时间戳；元数据与业务数据需分开判断 |
| `list_exchanges` | 两次返回 | 独立复测 TLS 超时 | HTTP 元数据查询可用 |
| `get_derivatives_history` | 修正为 BTCUSDT 后两次返回 | 合法参数一次返回、一次 TLS 超时 | 初始 BTC/USDT:USDT 导致 422 属测试参数错误，已纠正；funding[].t 仍需正确映射 |
| `list_hyperliquid_symbols` | 两次 partial | 独立复测 partial | 列表可返回；无日期或范围限制不代表连接失败 |
| `get_hyperliquid_liquidation_map` | 两次返回 | 两次返回 | 有仓位记录；createTime 不应未经解释就当作本次行情日期 |
| `get_hyperliquid_top_position` | 两次 partial | 两次 partial | 有仓位记录，日期/完整性限制 |
| `get_hyperliquid_user_count` | 两次 partial | TLS 超时后 partial | 原始 dateTime 存在，归一化日期缺口 |
| `list_hyperliquid_vaults` | 两次返回 | 两次返回 | 有 vault 数据，结构/日期需按业务消费 |
| `get_crypto_liquidation_today` | 两次返回 | 两次返回 | 有清算数据，归一化未给业务日期 |
| `list_crypto_funding_rates` | 两次返回 | TLS 超时后返回 | 有费率数据；nextFundingTime 是下一结算时间，不自动等于观察日期 |
| `get_service_health` | 两次 200 | 目录不提供 | health 不需要金融观察日期 |
| `get_market_health` | 两次 200 | 目录不提供 | health 不需要金融观察日期 |

## MCP 额外 10 个工具

| operation | 实测结果 | 解释 |
| --- | --- | --- |
| `get_ashare_capital_flow` | 返回 not_collected | 服务明确未采集，不能用于资金流结论 |
| `get_latest_ashare_capital_flow` | TLS 超时后返回 not_collected | 有传输问题，也缺数据覆盖 |
| `get_latest_ashare_holders` | 返回 not_collected | 无持有人数据覆盖 |
| `get_latest_ashare_reductions` | 返回 not_collected | 无减持数据覆盖 |
| `get_latest_ashare_unlocks` | TLS 超时后返回 not_collected | 无解禁数据覆盖 |
| `list_ashare_holders` | 返回 not_collected | 无数据覆盖 |
| `list_ashare_reductions` | 返回 not_collected | 无数据覆盖 |
| `list_ashare_unlocks` | 超时，独立复测 TLS 超时 | 未取得可判定覆盖的响应 |
| `verify_upstream` | 两次成功返回 | 仅证明这个校验操作能响应，不代表所有上游均健康 |
| `list_stock_post_comments` | 真实帖子 URL 下两次成功响应 | 完成评论接口调用；此响应验证连通性，不能据非空包装对象保证存在评论条目 |

## 15 个 THS Skill

下表使用明确查询直接调用 Skill adapter，不经过同名旧操作的主路由。

| operation | 实测结果 | 数据限制 |
| --- | --- | --- |
| `internal_iwencai_finance_query` | 返回 6 个带日期指标 | 2025 年年报日期为 2025-12-31 |
| `internal_iwencai_event_query` | 原查询两次 empty_normalized；替代调研查询也两次为空 | 这些查询未取得有效事件字段，不推广为所有事件永远不可用 |
| `internal_iwencai_basicinfo_query` | 两次 partial，有代码/名称/行业 | 无日期；静态实体信息可用范围与行情证据不同 |
| `internal_iwencai_market_query` | 两次 partial，有行情/成交额 | 请求含家数，但结果缺涨停/上涨/下跌家数；部分行情有日期 |
| `internal_iwencai_macro_query` | CPI 同比返回且有日期 | 2026 年 9 月观测 |
| `internal_iwencai_industry_query` | 两次 partial | 有部分带日期指标，不能据此确认涨停计数排名 |
| `internal_iwencai_management_query` | 两次 partial，有股东记录 | 归一化记录没有报告期日期 |
| `internal_iwencai_announcement_search` | 返回 5 条带日期公告 | 查询范围内结果，不保证完整公告全集 |
| `internal_iwencai_report_search` | 返回 5 条带日期研报 | 搜索结果覆盖有限 |
| `internal_iwencai_news_search` | 返回 5 条当天新闻 | 显式带日期的直接查询可取到当天结果 |
| `internal_iwencai_index_query` | 两次 partial，其中 9/12 个业务 datum 有日期 | 3 个指数的涨跌幅、成交额、成交量带日期，最新价缺日期；带日期子集不应全部作废 |
| `internal_iwencai_astock_selector` | 两次 partial | 涨停筛选条件未经验证，结果仅最新价/涨跌幅，缺日期 |
| `internal_iwencai_sector_selector` | 两次 partial | 条件验证、分页和日期限制；部分涨跌幅带日期 |
| `internal_iwencai_business_query` | 两次 partial，56 个业务 datum 均有日期 | 展开行总量/覆盖范围未知；不是无数据 |
| `internal_iwencai_insresearch_query` | 复杂查询两次 gateway_business_error；简化查询两次返回 partial | “贵州茅台 研报评级”能返回评级，但缺发布日期；属于查询依赖与日期缺口 |

## 8 个自写原生工具及其底层源

这里关闭 THS 主路由后执行，避免“测试内部工具”实际只打到问财。

| operation | 实测结果 | 解释 |
| --- | --- | --- |
| `internal_hk_northbound` | 两次 success，业务数据为 0 | 源请求错误被吞掉，runtime 未检查空结果 |
| `internal_hk_index` | 两次 success，业务数据为 0 | 同上，路径及代码问题见下文 |
| `internal_us_fundamentals` | 两次返回 revenue/net_income/eps/gross_profit | SEC 可访问；日期留在嵌套值而未体现为统一 datum 日期 |
| `internal_us_filings_recent` | 两次返回 10 条申报 | SEC 可访问；归一化日期存在缺口 |
| `internal_symbol_news` | 返回 8 条带日期新闻 | 原生个股新闻链可用 |
| `internal_news_digest` | partial 后复测成功 | 复测返回 8 条带日期新闻 |
| `internal_market_telegraph` | 两次返回当天电报 | 探针曾把 telegraph_meta 算入无日期业务项；人工复核确认新闻项有日期 |
| `news_search` | 一次成功，复测报 TLS 协商异常 | 错误为 Bing 请求的 `SelectedUnofferedKxGroup`；后续 DDGS 底层复测恢复，属于间歇问题 |

| 直接底层源 | 补测结果 | 耗时 |
| --- | --- | --- |
| 东财个股新闻 / AKShare | 10 条，全部有日期 | 0.12 秒 |
| 财联社电报 / AKShare | 16 条，全部有日期 | 0.14 秒 |
| Google News RSS | 5 条，全部有日期 | 1.00 秒 |
| DDGS news | 5 条，全部有日期 | 1.21 秒 |
| 全 A 代码名称表 / AKShare | 5,577 项 | 5.58 秒 |

## 主路由与运行时

| 旧 operation | THS 主路由表现 | 生产 runtime 样本 |
| --- | --- | --- |
| `get_market_quotes` | 指数目标被路由到 market；两次 empty_normalized | MCP fallback 成功取得带当天日期的指数报价 |
| `get_market_klines` | A 股补测两次均未满足 kline 契约；返回单日字段，不能证明已提供请求的整段序列 | 未另做生产 runtime 补测 |
| `get_ashare_sentiment` | 缺 market_breadth 必要字段；显式日期对照仍缺 | 失败/工具期限耗尽 |
| `get_limit_up_count` | 返回全 A 最新价/涨跌幅，缺 count；显式日期仍相同 | partial，fallback 日期及字段条件不满足 |
| `list_limit_up_stocks` | 最新价/涨跌幅，缺有效 pool 结构与日期 | partial，fallback 未解决证据缺口 |
| `list_limit_up_sectors` | 两次 empty_normalized；显式日期仍为空 | 失败/工具期限耗尽 |
| `get_company_overview` | 无标的原样本泛化返回，不能代表正常个股调用；增加 SH600519 后两次仍未满足 quote 契约 | 未单独执行 |
| `get_company_finance` | 返回 6 个 2026-06-30 财务指标 | 未单独执行 |
| `get_company_business` | 被映射为 basicinfo，仅返回代码/名称/行业 | 未单独执行；与已有 business Skill 能力不等价 |
| `get_company_concepts` | 返回行情字段，缺 sector_theme | 未单独执行 |
| `get_company_shareholders` | 股东字段可返回，但缺日期 | 未单独执行 |
| `get_company_survey` | 映射 events，返回 empty_normalized | 未单独执行；网关原接口提供公司概况 |
| `internal_market_telegraph` | 泛化新闻，日期从 9 月延伸到当天 | 同样返回混合日期新闻 |
| `internal_symbol_news` | 有新闻，但包含 2025 年日期 | success，不能等同于最新个股新闻 |
| `internal_news_digest` | 有新闻，包含 2025 年及更早于近一周的日期 | success，未保持 time_limit=w 的效果 |
| `news_search` | 与 digest 类似 | success，时间范围语义不足 |

另测 `get_stock_abnormal_reasons` 的生产 runtime，错误/超时与网关上游失败一致。

## 参数与路径对照

| 对照 | 结果 | 能证明什么 |
| --- | --- | --- |
| HK 报价 `HK00700` | HTTP/MCP 各两次无法识别代码 | 项目使用的港股格式不能被当前报价接口消费 |
| HK 报价 `hk00700` / `00700` | 分别为 422 / HTTP 200 空响应 | 简单改大小写或去前缀未解决 |
| HK 搜索 `00700` | HTTP 返回，MCP 超时后返回 | 搜索通道可访问，不能代替报价验证 |
| A 股 K 线 `SH600519` / xueqiu | HTTP 两次返回，MCP TLS 超时/预算超时 | 数据有 candle 字段，但当前 kline 契约仍未满足 |
| 美股 K 线及窗口 `AAPL` / xueqiu | HTTP 分别两次 502，MCP 源站响应不完整/TLS 超时 | 本次所测美股雪球路径失败，不代表所有美股供应商失败 |
| 金 K 线 `XAU/USDT:USDT` | HTTP/MCP 均返回 | 仍需检查日期/外围元数据归一化 |
| 银 snapshot `XAG/USDT:USDT` | HTTP 成功，MCP 两次 TLS 超时 | 更支持 MCP 传输不稳定 |
| 铂 snapshot `XPT/USDT:USDT` | HTTP/MCP 成功 | 本次可取得数据 |
| 衍生品 `BTC/USDT:USDT` 改为 `BTCUSDT` | 原始 422 消失；HTTP 连续成功 | 原始 422 是测试输入问题，已从故障判定排除 |
| 东财 `/klines/get` + `HK.800300` | 404，空 body | 现有源路径错误 |
| 东财 `/kline/get` + `HK.800300` | 200，rc=102，data=null | 仅修改路径不够 |
| 东财 `/kline/get` + `100.HSI` | 200，rc=0，有 K 线 | 有效路径和恒指 secid 对照成功；请求数量限制未获服务端严格执行 |
| 东财 `/kline/get` + `100.HSTECH` | 200，rc=100，data=null | 没有验证恒生科技的正确 secid，不能把这组猜测直接当修复 |

## 可定位的本地问题

1. [hk_northbound.py](D:/Mosaic/app/research/hk_northbound.py:30) 使用 `/api/qt/stock/klines/get`；正确路径对照为 `/kline/get`。代码中的 HK.800300 等标识还需按源约定修正。循环捕获异常后返回空对象，导致故障没有向上暴露。
2. [tool_runtime.py](D:/Mosaic/app/graph/tool_runtime.py:888) 的两个港股分支，在结果为空时仍构造 `STATUS_SUCCESS`。这解释了“success 但没有业务数据”。
3. [ths_skill_registry.py](D:/Mosaic/app/gateway/ths_skill_registry.py:136) 将指数报价等都送往 market，把 company_business 映射为 basicinfo、company_survey 映射为 events，把原生新闻入口映射为通用 news。能力名称相近不等于返回内容等价。
4. [source_routing.py](D:/Mosaic/app/gateway/source_routing.py:60) 的泛化查询依赖传入日期；未传时生成“涨停家数”等短查询。不过显式日期对照也未补齐关键字段，因此只补日期还不够。
5. [iwencai_query.py](D:/Mosaic/app/research/iwencai_query.py:62) 的 market 字段筛选/请求映射未明确覆盖“涨停家数、上涨家数、下跌家数”等所需聚合指标。这是能力注册与本地适配不一致的可检查点；本次没有保存完整上游原文，不能进一步断言具体字段是在上游未返回还是全部在本地被过滤。
6. 多个网关有 `REPORT_DATE`、`END_DATE`、`published_at`、`dateTime`、`funding[].t` 等原始时间字段，统一业务 datum 未正确承接。这类问题应修正具体 payload 的语义适配，不能用“抓取时间就是数据时间”填补。

## 与本次无法交付结论的关系

原始日志的 run_id 为 `f73e92b3a77b`。最终 4 个 claim 中 3 个指数涨跌幅通过，另 1 个引用全 A 无日期行情的 claim 未通过，reason_codes 含 unknown_date、missing_as_of_date、partial_only。涨停家数与板块缺口也未补齐，最后 `delivery_status=blocked`、`final_audit_status=research_exhausted`。

该日志在结束时仍有约 613 秒，`research_round_count=1`。因此不能解释成整个墙钟预算已经耗尽；还涉及轮次/研究退出条件和证据审计。接口巡检已经复现其中的关键数据缺口，但没有重新跑一遍完整研究来验证最终交付。

配置中的模型 `qwen3.7-flash` 通过项目同样的 JSON 请求参数做了两次最小调用，分别约 4.9 秒和 7.2 秒，均返回正确 JSON。它证明当前模型基础连通和 JSON 模式可用，不证明长研究提示、工具选择或报告质量已经验收。

建议处理顺序：先修 THS 能力映射/目标字段与港股空成功，再解决网关日期契约和 MCP 传输；雪球 400016/502 需要检查网关上游凭据、配额与服务。最后用一份完整研究报告验证补证、轮次退出和交付。

## 复核产物

- [主巡检明细 JSONL](D:/Mosaic/tmp/interface-audit-20261016-final/results.jsonl)、[主巡检 CSV](D:/Mosaic/tmp/interface-audit-20261016-final/summary.csv)、[MCP 工具目录](D:/Mosaic/tmp/interface-audit-20261016-final/mcp_inventory.json)
- [合法参数、替代查询及评论补测](D:/Mosaic/tmp/interface-audit-20261016-details/results.jsonl)
- [港股路径与 secid 对照](D:/Mosaic/tmp/interface-audit-20261016-hk-control/results.jsonl)
- [原生新闻源补测](D:/Mosaic/tmp/interface-audit-20261016-sources/results.jsonl)
- [模型 JSON 补测](D:/Mosaic/tmp/interface-audit-20261016-llm/results.jsonl)
- [带日期查询、合法标的及港股格式补测](D:/Mosaic/tmp/interface-audit-20261016-routing-controls/results.jsonl)
- [主诊断脚本](D:/Mosaic/scripts/audit_live_interfaces.py)、[对照脚本](D:/Mosaic/scripts/audit_live_interface_details.py)

主巡检的自动 classification 是排查线索，不是最终人工结论。其 comments 待测占位已由补测解决，衍生品初始错误输入已由合法参数补测纠正，metadata/telegraph_meta 的日期告警也已人工复核。早期 `interface-audit-20261016-full` 因 MCP SDK 错误属性读取而作废，不纳入结论；`validated` 已被 `final` 接续，未重复计数。`details` 在晨报超时处中止，但此前记录有效，缺失的原生源检查已由 `sources` 完成。

脚本经 Ruff 格式化与检查通过。未运行全套应用单元测试，因为本轮未修改业务代码。
