# 同花顺问财 Skill → Mosaic Tool

已接入十五个官方 Skill（均为 1.0.0）。Mosaic 直接调用 Skill 所使用的 OpenAPI，
无需在生产机安装 SkillHub CLI 或让 LLM 执行 Skill 的 Shell/Python 命令。

| 官方 Skill | Mosaic tool key | operationId | 类别 |
|---|---|---|---|
| announcement-search | announcement_search | internal_iwencai_announcement_search | news |
| hithink-finance-query | iwencai_finance | internal_iwencai_finance_query | fundamental |
| hithink-event-query | iwencai_events | internal_iwencai_event_query | news |
| hithink-basicinfo-query | iwencai_basicinfo | internal_iwencai_basicinfo_query | fundamental |
| report-search | iwencai_report_search | internal_iwencai_report_search | fundamental |
| hithink-market-query | iwencai_market | internal_iwencai_market_query | technical |
| news-search | iwencai_news_search | internal_iwencai_news_search | news |
| hithink-macro-query | iwencai_macro | internal_iwencai_macro_query | fundamental |
| hithink-industry-query | iwencai_industry | internal_iwencai_industry_query | fundamental |
| hithink-management-query | iwencai_management | internal_iwencai_management_query | fundamental |
| hithink-zhishu-query | iwencai_index | internal_iwencai_index_query | technical |
| hithink-astock-selector | iwencai_astock_selector | internal_iwencai_astock_selector | technical |
| hithink-sector-selector | iwencai_sector_selector | internal_iwencai_sector_selector | technical |
| hithink-business-query | iwencai_business | internal_iwencai_business_query | fundamental |
| hithink-insresearch-query | iwencai_insresearch | internal_iwencai_insresearch_query | fundamental |

## 配置与使用

在项目 `.env` 或部署环境中配置（真实 key 不提交到 Git）：

```dotenv
IWENCAI_BASE_URL=https://openapi.iwencai.com
IWENCAI_API_KEY=你的key
IWENCAI_ANNOUNCEMENT_TTL_SECONDS=900
IWENCAI_FINANCE_TTL_SECONDS=3600
IWENCAI_EVENT_TTL_SECONDS=900
IWENCAI_BASICINFO_TTL_SECONDS=86400
IWENCAI_MARKET_TTL_SECONDS=60
IWENCAI_INDEX_TTL_SECONDS=60
IWENCAI_ASTOCK_SELECTOR_TTL_SECONDS=60
IWENCAI_SECTOR_SELECTOR_TTL_SECONDS=60
IWENCAI_BUSINESS_TTL_SECONDS=3600
IWENCAI_INSRESEARCH_TTL_SECONDS=3600
IWENCAI_MACRO_TTL_SECONDS=3600
IWENCAI_INDUSTRY_TTL_SECONDS=60
IWENCAI_MANAGEMENT_TTL_SECONDS=3600
IWENCAI_REPORT_TTL_SECONDS=3600
IWENCAI_NEWS_TTL_SECONDS=300
```

获取 key：[同花顺官方 SkillHub](https://www.iwencai.com/skillhub)。应用重启后加载新配置。
缺少 key/认证失败时返回明确错误，不生成证据；其它工具仍可使用。
十五类工具共用这一组配置；TTL 设为0可关闭对应本地缓存。项目使用 `.env`；按用户要求另外安装用户级 SkillHub CLI，并在 PowerShell profile 从本地 DPAPI 凭据加载环境变量，不向 profile 或代码写入明文密钥。

## 板块筛选、公司经营与机构研究（第五批）

三个官方 Skill 均为直连 HTTPS `POST /v1/query2data`；共用固定 query/page/limit/is_cache/expand_index 契约、鉴权、64hex trace、有限预算/重试与失败零证据不变式，不依赖 iiix/MCP。官方文档的自动简化/放宽条件建议不在 Mosaic 中执行。

- 板块筛选必须使用 `iwencai_sector_selector`，只接受明确板块/行业/指数身份，保留实际分类。上游候选始终partial；所有过滤条件未逐项核查。实测limit=3返回63行，标准化只保留前3行，不推断全市场排名或数量。
- 经营数据必须使用 `iwencai_business`，每项保留公司、项目名称/分类标准或客户/供应商/子公司上下文。数值分别关联原报告期或合同事件日，公告日期不替代经营报告期；默认价格不算经营数据。
- 机构评级/预测必须使用 `iwencai_insresearch`，区分机构观点、研报发布日、预测期、评级及目标价；保留原研报链接。缺机构或明确发布日时不能支持对应claim；错误预测年度、未注明真实机构、将预测当已实现利润仍阻断。
- 2026-10-12实测机构研究打开或关闭expand_index均返回40000行/code_count=1；标准化最多保留200行，Evidence Builder最多80条，完整原响应另存本地。只有“公告日期”且元数据标为通用交易日期时不据此补造研报发布日，保持partial/`missing_publication_date`。

三个Skill已安装到用户 `.agents/skills`；复用现有SkillHub及DPAPI profile配置。完整真实run_id、限制和验证见 [第五批接入报告](iwencai-sector-business-research-report.md)，记录为 `memory/iwencai-sector-business-research-probes.jsonl`。工具成功不代表Critic/Finalize或MCP通过，继续 `production_ready=false`。

```powershell
python -m scripts.verify_iwencai "2026年10月9日 行业板块 涨跌幅大于1% 主力资金净流入大于0 市盈率小于30" --tool iwencai_sector_selector --size 3 --requested-date 2026-10-09 --output memory/iwencai-sector-business-research-probes.jsonl --raw-output-dir .tmp/iwencai-p5/responses
python -m scripts.verify_iwencai "300033 同花顺 2025年12月31日 主营业务构成 收入占比" --tool iwencai_business --size 3 --output memory/iwencai-sector-business-research-probes.jsonl --raw-output-dir .tmp/iwencai-p5/responses
python -m scripts.verify_iwencai "600519 贵州茅台 2026年 盈利预测 预测净利润 预测机构 研报日期" --tool iwencai_insresearch --size 3 --no-expand-index --output memory/iwencai-sector-business-research-probes.jsonl --raw-output-dir .tmp/iwencai-p5/responses
python -m scripts.replay_iwencai_probes memory/iwencai-sector-business-research-probes.jsonl
```

## 指数与选A股（第四批）

官方安装包的 `SKILL.md` 与 `scripts/cli.py` 已核对。二者都是直接 HTTPS API，固定 `POST /v1/query2data`，输入为 query/page/limit/is_cache/expand_index，输出为 datas/code_count/chunks_info 和动态 columns。Bearer 鉴权、1.0.0 Skill headers、64hex trace、30秒官方CLI默认超时；Mosaic沿用现有有限预算/重试策略和脱敏错误格式，不依赖 iiix/MCP。

- 指数问题必须调用 `iwencai_index`，只从明确的指数代码/简称/名称字段建立身份；不从 query 猜代码，不把默认股票表当指数。海外 `.IXIC`、`^GSPC` 等上游结构化代码原样保留。指数 datum 使用 reference 域、index coverage，不能支持个股股价结论。
- 点位、涨跌幅、成交量按每项 metadata timestamp 或字段日期保留；原单位为点/元/手时分别保留，不换算为其它口径。无日期的最新字段不借用同一行历史日期；海外指数不套用A股休市语义。
- A股多条件筛选必须调用 `iwencai_astock_selector`，保留用户原query、日期、市场和全部过滤条件。空结果不自动放宽条件；对应字段未返回时不以默认报价证明ROE/技术形态等条件。
- 选股返回上游候选页，筛选条件未逐项独立验证时始终partial并记录 `screening_conditions_not_verified`。估值字段按market日期，年报字段按reporting_period分别校验；不存在用报告期替代交易日期的路径。
- 2026-10-12实测选股请求limit=3返回156行。记录 `upstream_page_size_exceeded`，标准化只保留前3行，原响应保存在ToolResult.raw；不声称分页已可靠或候选满足全部条件。仅有数据接口成功不代表Critic/Finalize或MCP通过。

用户级安装位置：`C:\Users\Phineas\.iwencai-skillhub`（CLI 2026.3.13），`C:\Users\Phineas\.agents\skills\hithink-zhishu-query` 和 `hithink-astock-selector`。Windows以 `.local\bin\iwencai-skillhub-cli.cmd` 包装官方Python CLI；profile位于 `Documents\PowerShell\Microsoft.PowerShell_profile.ps1`，凭据以当前用户DPAPI加密存放。官方Bash安装器未在Windows直接运行，仅安装CLI及指定的两个Skill。

真实工具探针与安全边界见 [指数/选股接入记录](iwencai-index-selector-report.md)，结构化记录为 `memory/iwencai-index-selector-probes.jsonl`。Critic/Finalize/Delivery均标 `not_invoked_tool_probe`，研究持久化为false；P6仍为 `production_ready=false`。

```powershell
iwencai-skillhub-cli --version
python -m scripts.verify_iwencai "上证指数 2026年10月9日 收盘价 涨跌幅 成交量" --tool iwencai_index --requested-date 2026-10-09 --output memory/iwencai-index-selector-probes.jsonl
python -m scripts.verify_iwencai "2026年10月9日 A股 涨跌幅大于5% 市盈率小于30 非ST 股票" --tool iwencai_astock_selector --size 3 --requested-date 2026-10-09 --output memory/iwencai-index-selector-probes.jsonl
```

## 研报、行情、新闻与宏观（第二批）

所有工具均为内部直连、`cross` 域、无 symbol 参数。新闻在 `NEWS_ENABLED=false` 时由 technical 执行。
原 DDGS `news_search` 保持独立；官方新闻的逻辑 key 是 `iwencai_news_search`，避免同名混淆。

研报与新闻使用 `query`（非空、最多2000字符）和 `size`（默认10，整数1..20），固定
`POST /v1/comprehensive/search`，分别固定频道 `report`、`news` 与 `app_id=AIME_SKILL`。
发送各自 Skill ID、1.0.0、normal调用类型及每次尝试独立的64hex trace。
成功要求 `status_code=0` 与官方 `data` 列表；未知参数、模型自选 channel/endpoint 均拒绝。

- 原响应文本保存于 `raw`，展示层另保留标题、正文片段、原URL、发布日期、上游证券信息。
- 新闻保留 `extra.real_publish_source` 优先的原媒体 `publisher`；没有证券代码时保留检索口径，不从标题猜代码。
- 研报保留明确返回的机构、作者、评级、目标价及币种；缺失保持未知，不从摘要推算目标价或补造币种。
- `document_id` 来自 uid 或原链接，`chunk_index` 来自 para_index。同文档多个片段不能算多份研报，亦不能当作多机构交叉确认；未获取全文时只称摘要/片段。
- 评级、目标价和盈利预测是机构观点，不能当作实际股价或已实现业绩。证据匹配会拒绝用研报片段支持实际股价论断。
- 新闻按相关性命中，F10页面、转载、旧文章都可能出现；日期以实际返回为准，不能凭“最新”查询词把旧文称为最新动态。
- 缺日期或存在无效记录时标partial；空结果、结构/业务失败不生成证据。抓取日期不替代发布日期。

行情与宏观使用与第一批 query2data 相同的参数：`query`、`page`、`limit`、`is_cache`、`expand_index`，固定
`POST /v1/query2data`，分别发送 `hithink-market-query`、`hithink-macro-query`。刷新绕过本地缓存并发送 `is_cache=0`。

- 行情只收集价格、涨跌幅、量额、资金流向及技术指标。查询资金流向/RSI等却只返回默认价格时，不把价格当目标证据；多项指标缺部分时明确标partial及缺失项。
- 每个行情字段分别使用自身元数据 timestamp 或字段日期，不把同行的收盘日期套到“最新涨跌幅/主力资金”等无日期字段上。元数据中的前复权等口径保留在证据说明中；未知时间不能称已确认实时行情。
- 宏观使用实际返回的指标名称、数值、国家/地区、统计期、周期、单位及 macro_id；不要求证券代码。指标值为0仍有效。宏观证据使用 `macro_series`/`macro_indicator` 口径。
- 日、月、年、季度统计期保留原粒度；仅年/月/季度时不补造具体日期。统计期不是发布日期或交易日，经济统计期不与今天的行情日期比较。缺国家地区/统计期/单位时标partial，不推断数值倍率。
- `macro` 是项目已有的研究域，本批启用为默认研究目标，也支持 `/api/ask` 显式指定 `domain=macro`；宏观工具仍可跨域调用。

规划提示词要求证券行情和宏观数据使用对应工具；代码按常见指标词补齐 `iwencai_market`/`iwencai_macro`，沿用单轮工具预算不足的显式终止机制。
Crypto/商品行情不强制转给问财；净利率/毛利率、债券票面利率不误判为宏观利率。
研报和新闻由 Planner 按问题选用，可配合其它源核查。规则匹配未覆盖的口语表达仍依赖 Planner。

可直接在界面询问，或执行适配器验收：

```powershell
python -m scripts.verify_iwencai "贵州茅台 研报" --tool iwencai_report_search --size 3
python -m scripts.verify_iwencai "贵州茅台 最新价 涨跌幅 成交量 主力资金净流入" --tool iwencai_market --size 3
python -m scripts.verify_iwencai "贵州茅台 最新新闻" --tool iwencai_news_search --size 3
python -m scripts.verify_iwencai "中国 2025年 GDP" --tool iwencai_macro --size 3
```

2026-10-12真实适配器验收：

| 工具 | 实际结果 |
|---|---|
| 研报 | success；3条正文片段来自同一份诚通证券报告，保留2026-09-18发布日期、机构、评级和链接，未声称返回3份报告或已验收目标价字段 |
| 行情 | partial；12项行情/资金字段，收盘/成交等日期为2026-10-12；涨跌幅、主力流向等4项未给日期，未补造时间 |
| 新闻 | success；3条命中包含F10页面、转载新闻，保留实际媒体/日期/链接；未提供唯一证券代码，仍为检索口径 |
| 宏观 | success；中国2025年GDP一条序列，统计期2025-12-31、周期年、单位元，保留原数值和series ID |

仅完成真实API/适配器及模拟LLM规划与执行的验证，未声称完成真实LLM与界面的端到端验收。
SkillHub CLI已在项目本地存在；本批因其Windows临时目录权限问题，直接从CLI元数据的官方包地址取得源文件核对，不在运行时执行供应商脚本。
来源：[研报包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=report-search)、
[行情包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-market-query)、
[新闻包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=news-search)、
[宏观包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-macro-query) 的 `SKILL.md`、搜索类 `references/api.md` 及随包脚本。

## 行业与股东股本（第三批）

宏观工具复用已有接入，没有重复添加。新增两个 query2data 工具均属于 cross 域、fundamental 类，无需 symbol；
共享现有凭证、参数校验、预算截止、重试、缓存及刷新机制。行业默认缓存60秒（含行情），股东股本3600秒，均可设为0。
Supervisor 按行业数据/估值/排名/盈利/行情和股东/股本/实控人等常见表达补齐必用工具，提示词也要求对应问题必须调用。
单家公司“所属行业”继续使用基本资料；股东大会走事件工具；股权质押同时要求事件和股东股本工具。

- 行业数据保留实际行业/板块/指数代码、名称与分类体系；`881273.TI` 这类行业指数使用 industry_index 口径，不能当个股。不从问句推断申万/中信体系或行业层级。
- 行业财务数据的报告期与行情/估值的市场日期分别核对。最新价不能借用同一行的财报期或估值日期；无日期时不声称实时。
- 股东指标的结构化 value 保留同一行的 holder_name、holder_rank 与原始 source_dates；通用“名称”按上游语义识别股东，不当公司简称。零持股/比例保留，不能把不同股东行合并成同一个持有人。
- 公告日不是持仓截止日。字段 metadata timestamp/字段日期与明确报告期按上游保留，原公告日期另存；如上游用查询日标记股东户数，也不据此推断该日完成了新的披露。
- 股东股本使用 snapshot 日期口径，不按今日行情日期判作过期；行业逐字段区分 market/reporting_period。报告期、公告日和查询日不能互换。
- 没有明确单位、持仓日期或行业分类时标partial，不推断股数单位或持股比例倍率。检索时间不补数据日期。
- 代码/名称/默认价格不能满足所问的收入、股东户数等指标；缺部分请求字段时说明缺失项，仅有无关字段时不生成目标证据。“流通股东”需保留对应口径，不能用大股东表冒充。
- 一只股票可能展开十大股东多行，code_count=1 不等于只有1个股东，也不保证查询覆盖全部持有人。标准化保留最多200行，证据展示沿用项目80条上限；完整成功响应保留于 raw。

重启项目后可直接询问“白酒行业市盈率是多少？”、“贵州茅台股东户数和前十大股东有哪些？”、“中国2025年GDP是多少？”。
适配器验收命令：

```powershell
python -m scripts.verify_iwencai "白酒行业 市盈率 营业收入 净利润" --tool iwencai_industry --size 3
python -m scripts.verify_iwencai "贵州茅台 股东户数 前十大股东 实际控制人" --tool iwencai_management --size 3
python -m scripts.verify_iwencai "中国 2025年 GDP" --tool iwencai_macro --size 3
```

2026-10-12真实API/适配器验证：行业返回白酒指数881273.TI及2026-10-09市盈率，收入和净利润缺失，价格/涨跌幅缺日期、分类体系未知，结果为partial；
股东返回10行持有人与排名、股东户数和实控人，标准化90项、证据展示80条，原公告日2026-08-15、户数字段标2026-10-12，部分持股指标缺日期/单位，结果为partial；
宏观复核为success，中国2025年GDP统计期2025-12-31、单位元、周期年。
此验收验证了真实上游和适配器；规划/分析员回归使用模拟LLM，未进行真实LLM与界面的端到端验收。
本批最终全量回归1326项通过；改动范围Ruff检查、三个新增/扩展查询文件的格式检查和 `git diff --check` 通过。

官方依据：[行业Skill包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-industry-query)、
[股东股本Skill包](http://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-management-query) 的 `SKILL.md` 与 `scripts/cli.py`。
SkillHub CLI 已在本地存在；沿用官方包直接核对方式，生产运行不依赖供应商脚本或全局 shell profile。

## 财务、事件与基本资料

三个新工具均属于 `cross` 域、无需 `symbol`，可传完整自然语言条件。
财务和资料由 fundamental 执行；事件由 news 执行，`NEWS_ENABLED=false` 时沿用已有 technical 路由。
官方声明基本资料覆盖股票、指数、基金、期货、期权、转债、债券、理财、保险等；
这是上游资料查询能力，不增加这些品类的行情或交易接口。支持情况以实际返回为准。

```json
{"tool_key":"iwencai_finance","arguments":{"query":"贵州茅台 2025年年报 营业收入 净利润 ROE","page":1,"limit":10},"purpose":"核查财务指标","priority":"high"}
```

- `query` 必填，非空、最多2000字符；保留标的、报告期/事件时间及筛选条件。
- `page` 默认1，范围1..10000；`limit` 默认10，范围1..20，均支持整数或整数字符串。
- `is_cache` 默认true；`expand_index` 默认true，支持布尔值或字符串0/1/true/false。未知参数拒绝。
- 固定 `POST /v1/query2data`；这五个参数按官方协议发送为字符串。每个工具发送自己的 Skill ID、1.0.0版本与 trace；重试标记为retry。
- 固定解析 `datas` 与 `columns`，从实际字段/指标名称、单位和时间生成证据；保留原始响应文本于 `raw`。不把代码、简称或默认最新价当作财务/事件证据。
- 财务日期表示报告期，不作为当天行情的观察日期；事件日期来自明确事件日期字段，或有事件字段及 DATE 元数据支持的“变动日期/事件日期”；不使用上市日期或检索时间替代事件日期。
- 基本资料的上市/到期日期是属性值，其观察日期保持未知；零值保留，空值/“--”过滤。结构化值保留，展示截断有提示。
- `code_count` 按上游标的数解释，分页提示遵循 `page * limit < code_count`。`expand_index` 可把一个标的展开为多行；保留最多200行，超过的留于raw并标记partial。同标的多事件时不据 `code_count` 声称事件全集，`row_count` 不被当成总事件数。
- 缺代码、财务/事件日期、分页未查完或展开口径不明时返回partial。空结果、仅默认行情、业务失败不生成目标证据；不能据此声称没有业绩预告或调研等事件。
- 刷新同时绕过本地缓存并发送 `is_cache=0`。共享有限重试与预算截止；认证/业务失败不重试。

规划提示词要求对应问题使用这些工具；`app/graph/iwencai_plan.py` 还在 Supervisor 规划后按常见中文关键词及ROE/ROA补齐调用，置于其它步骤前，并修复缺失参数。
这不是任意表达的语义分类器；未命中规则的表达由 Planner 选择工具。过去已调用过的工具不盲目重复，补查由 Critic 控制。
如果单轮工具预算连必须调用的工具都容纳不了，显式安全终止并记录 `required_tool_budget_insufficient`，不把漏调工具当作已完成。

界面可直接询问：

- “贵州茅台2025年营业收入、净利润和ROE是多少？”
- “宁德时代限售解禁的日期和股数？”
- “贵州茅台上市日期和上市地点？”
- “510300基金资料和管理费率？”

真实适配器验证（绕过缓存，不调用LLM，不打印密钥/原始响应）：

```powershell
python -m scripts.verify_iwencai "贵州茅台 2025年年报 营业收入 净利润 ROE" --tool iwencai_finance --size 3
python -m scripts.verify_iwencai "宁德时代 限售解禁 解禁日期" --tool iwencai_events --size 3
python -m scripts.verify_iwencai "贵州茅台 股票代码 上市日期 上市地点" --tool iwencai_basicinfo --size 3
```

2026-10-12实测：财务返回6项带2025-12-31报告期与元/%单位的指标；基本资料返回5项代码/简称/上市日期/地点/板块。
解禁查询返回宁德时代两行事件（2025-11-20、2026-09-16），字段元数据支持解禁股数、金额、比例和股份来源；
`code_count=1`、`row_count=2`，保留实际日期并标为partial以提示事件覆盖未知。
“贵州茅台 最近一年 机构调研”仅返回代码/行情，适配器返回无有效事件数据；不认定该时间内没有调研。
这些是实际API/适配器验收，加上模拟LLM的规划/分析员回归，未声称完成真实LLM与界面的端到端验收。

协议依据：[财务Skill包](https://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-finance-query)、
[事件Skill包](https://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-event-query)、
[资料Skill包](https://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=hithink-basicinfo-query) 中的 `SKILL.md` 和 `scripts/cli.py`。

## 公告查询

规划器逻辑 key 为 `announcement_search`，operationId 为 `internal_iwencai_announcement_search`。
属于 `cross` 域、`news` 类，不要求 `symbol`；`NEWS_ENABLED=false` 时沿用现有机制由 technical 执行。
公告查询覆盖官方声明的 A股、港股、基金、ETF。项目暂没有独立基金/ETF研究域，
基金/ETF公告查询沿用 `a_share`；港股沿用 `hk_stock`。不扩展这些标的的行情或交易能力。

参数只有 `query` 和 `size`：

```json
{"tool_key":"announcement_search","arguments":{"query":"贵州茅台 2026年 分红公告","size":10},"purpose":"核查分红公告","priority":"high"}
```

`query` 为非空自然语言字符串（最多2000字符），写清标的、公告类型、时间范围。
`size` 默认为10，项目限制为整数1..20。日期表达放在 query 中，不添加未经官方声明的 start/end 等字段。
可在界面询问“查贵州茅台2026年的分红公告”“查腾讯控股最近的回购公告”“查510300 ETF的分红公告”。

真实链路验证（绕过缓存，不调用 LLM，不打印密钥或大响应）：

```powershell
python -m scripts.verify_iwencai "贵州茅台 分红公告" --size 3
```

## 官方协议与证据边界

- 固定 `POST /v1/comprehensive/search`；固定 `channels=["announcement"]`、`app_id="AIME_SKILL"`。
- Bearer 认证；完整发送官方 `X-Claw-*` 标识，版本 `1.0.0`，每次尝试生成独立64位十六进制 trace ID。
- HTTP成功之外还必须满足 `status_code=0`、`data` 为公告列表。空结果/结构异常/业务失败不生成证据。
- 成功结果的 `ToolResult.raw` 保存原始响应文本，字段和顺序不改写；标准化层另提取公告标题、摘要、原公告URL、发布时间、上游实际标的。
- 发布时间优先使用 `publish_date`，否则解析 `publish_time`（Unix秒/毫秒，以UTC+8展示）。缺失时保持未知，绝不用抓取时间冒充发布日期。
- 只承认上游 `stock_infos` 中合法的证券代码，过滤 `MOUTAI80` 等内部标识，不从 query 猜出证券身份。
- 公告未提供唯一可确认证券代码或发布日期时标记 `partial` 并提示核对，不能把这些命中当作指定标的已验证的公告。
- 返回按相关性排列，旧公告可能排在新公告前。自然语言时间条件是检索请求，不能保证严格过滤或覆盖所有最新公告；回答必须依据返回的实际发布日期。
- `completeness=search_results` 表示检索命中子集，不代表完整财报或全部公告。摘要可能截断，提示中明确标注；未抓取PDF全文。
- 公告证据保留结构化链接及摘要供报告引用，来源标注“同花顺问财”，不能当作实时价格证据。
- 复用项目的有限重试、预算截止、缓存和刷新机制。认证/业务失败不重试；限流/上游故障可按预算有限重试。

协议核对来源（2026-10-12）：[官方安装入口](https://www.iwencai.com/skillhub/static/0.0.4/download_and_install.sh)，
其CLI元数据指向的[公告Skill包](https://ms.10jqka.com.cn/gateway/market/api/v1/skills/square/download?name=announcement-search)
内 `SKILL.md`、`references/api.md`、`scripts/announcement_search.py`。
本次真实请求验证了HTTP 200 / `status_code=0`及3条贵州茅台公告、原上交所PDF链接。
另测“腾讯控股 回购公告”返回空结果，“510300 ETF 分红公告”命中港交所公告且缺少可确认证券代码，
后者标记为部分结果。官方声明覆盖这些市场不代表每个查询都能正确命中；本次不声称所有市场/公告类型已通过验收。

## 继续包装其它 Skill

每个Skill先核对官方包的请求合同、版本及真实返回，再显式添加注册表条目和对应适配器，
补齐参数校验、来源/时间/标的映射、错误与空结果处理及链路验证。
共用 `IWENCAI_BASE_URL` / `IWENCAI_API_KEY` 和项目运行时；不同Skill保留各自的固定协议。
Skill通常是提示词、脚本与文档的集合，安装本身不会自动产生Mosaic可调用的tool。
