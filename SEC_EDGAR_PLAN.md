# SEC EDGAR 美股基本面接入执行计划（交给子 Agent 直接落地）

> 本文由主 Agent 基于仓库真值（2026-10-06，main @ 67c0e2e，工作树干净）编写，行号已按美股域接入后的现状核实。
> 执行纪律（环境、基线、commit 规则）沿用 `US_STOCK_INTEGRATION_PLAN.md` §1.5，此处不重复；先读那份的 §1.5 再开工。
> 如实际代码与本文引用不符，以代码为准并在交付报告中说明差异。

---

## 0. 目标与范围

**目标**：美股域补上基本面能力 —— 基本面 Analyst（category=`fundamental`）能通过 SEC EDGAR 拿到美股公司的营收/净利/EPS/毛利（最新年报+季报）与近期申报文件，进入既有 Evidence Gate → Reasoning → Critic 链路。

**本计划做**：
- Phase A：A0 探针闸门 → 研究模块 + 2 个 INTERNAL 工具 + critic 规则更新 + 配置项 + 测试 + 文档

**本计划不做**（明确排除）：
- 分析师预期 / consensus estimate / 财报日历 / guidance（EDGAR 没有这类数据，属未来另找源的方案级决策）
- 10-K/10-Q 全文抓取与解析（只取 XBRL 结构化数据 + 申报文件元信息）
- 宏观域、AIHOT（维持原计划排除项）

---

## 1. 关键真值事实（已核实）

### 1.1 仓库现状

| 事实 | 位置 |
|---|---|
| INTERNAL 分发：`_execute_internal` 按 `tool_name` 硬编码 if 分支；`internal_hk_northbound`（320 行）、`internal_hk_index`（343 行）、`news_search`（368 行，**带 market_cache 显式缓存 + `_make_cache_key`**，是带缓存的 INTERNAL 工具范本） | `app/graph/tool_runtime.py` |
| ToolResult 塑形：成功 `status=STATUS_SUCCESS, normalized=[...]`，失败 `status=STATUS_ERROR, normalized=[], error=...`，**不抛异常**（try/except 包裹、error 落 ToolResult） | 同上 322-341 行 |
| `_US_STOCK_PLACEHOLDERS`（us_klines/us_window）在 515 行；`ALL_TOOLS` 拼装在 605 行；`_NEWS_SEARCH` 在 548 行 | `app/gateway/tool_registry.py` |
| 模块 docstring 第 9 行写"所有 42 个"，有契约测试 `test_docs_contract.py::test_registry_docstring_tool_count_matches_actual` 守护 → **本计划 +2 后必须改为 44** | `app/gateway/tool_registry.py:9` |
| critic 的 `_DOMAIN_RULES["us_stock"]` 在 55 行，当前文本含"美股无基本面工具接入时，报告不得声称财务数据支撑"→ **接入后必须改写**（改为要求财务结论必须有 us_fundamentals 数据支撑） | `app/graph/nodes/critic.py` |
| `tests/test_us_stock_domain.py` 断言 `_DOMAIN_RULES["us_stock"]` 存在且非空（改文本不破坏该断言，但改完要跑确认） | `tests/test_us_stock_domain.py` |
| Settings 很小（75 行），pydantic-settings，env 名 = 字段名大写 | `app/config.py` |
| 研究模块范本：`app/research/hk_northbound.py`（httpx + `_build_params`/`_parse_*` 纯函数 + async fetch，无类）；测试范本：`tests/test_hk_northbound.py`（只测纯函数，不真连网） | `app/research/`、`tests/` |
| 当前基线：双模式各 540 passed + 0 skipped | 2026-10-06 主 Agent 亲测 |

### 1.2 SEC EDGAR API（文档级事实，A0 闸门必须实测）

| 端点 | 用途 | 要点 |
|---|---|---|
| `https://www.sec.gov/files/company_tickers.json` | ticker→CIK 映射 | 全量约 1MB+，返回 `{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."},...}`；应一次拉取后模块级缓存（24h TTL），不能每次调用都拉 |
| `https://data.sec.gov/api/xbrl/companyconcept/CIK{10位}/us-gaap/{Tag}.json` | 单概念 XBRL 序列 | 返回该概念全部历史申报值（`units.USD` 数组，每项含 `start/end/val/fy/fp/form/filed`）；取 `form` 为 10-K/10-Q 的最新几条 |
| `https://data.sec.gov/submissions/CIK{10位}.json` | 申报文件元信息 | `filings.recent` 数组含 `form/filingDate/accessionNumber/primaryDocument`；过滤 `form ∈ {10-K, 10-Q, 8-K}` 取最近 10 份 |

**硬性要求（SEC 公平访问政策）**：所有请求必须带声明身份的 `User-Agent`（格式如 `"Mosaic market-intelligence research contact@example.com"`），限速 10 次/秒（Mosaic 低频使用碰不到）。**无合规 UA 会被 403**——A0 要实测"带 UA 200 / 不带 UA 403"以确认。

**已知 XBRL 坑（探针必须查清）**：不同公司营收用的 us-gaap tag 不一致——`Revenues`、`RevenueFromContractWithCustomerExcludingAssessedTax` 都常见；EPS 有 `EarningsPerShareDiluted`/`EarningsPerShareBasic`；毛利是 `GrossProfit`。**A0 必须对 AAPL、NVDA 逐个 tag 实测哪些存在**，据此定候选 tag 列表与回退顺序。

---

## 2. Phase A：接入实施

### A0. 探针闸门 —— `scripts/verify_sec_edgar.py`（仿 `verify_us_market.py` 风格，一次性验证工具）

1. 从 `.env`/环境读新配置项 `SEC_EDGAR_CONTACT`（见 A4；探针阶段可临时用环境变量传入，**不要把真实邮箱硬编码进脚本**）。
2. 实测清单：
   - `company_tickers.json`：200、可解析、含 AAPL/NVDA/TSLA 的 CIK；记录载荷大小。
   - `companyconcept`：对 AAPL（CIK 0000320193）与 NVDA（CIK 0001045810）实测 tag：`Revenues`、`RevenueFromContractWithCustomerExcludingAssessedTax`、`NetIncomeLoss`、`GrossProfit`、`EarningsPerShareDiluted`、`EarningsPerShareBasic`，输出"公司 × tag 存在性"表 + 各自载荷大小；确认 `units.USD` 最新 10-K/10-Q 条目结构。
   - `submissions`：AAPL 实测，确认 `filings.recent` 结构与 form 过滤可行性；记录载荷大小（全文较大，确认 httpx 可处理）。
   - UA 对照：同一请求不带 UA（或通用 UA）重试一次，记录状态码（预期 403）。
3. **闸门判定**：
   - **通过**（三类端点带合规 UA 均 200 且载荷可控、至少 2 家公司的核心 tag 候选齐全）→ 继续 A1。
   - **部分通过**（个别 tag 缺失/载荷偏大）→ 按实测收缩工具能力（如只做营收/净利/EPS），purpose 如实描述。
   - **失败**（合规 UA 仍 403/限流/结构不可解析）→ **停止**，删探针或仅提交探针作档案，报告交回。**不得换 yfinance 等第三方绕过**。

### A1. 研究模块 `app/research/sec_edgar.py`

风格完全对齐 `hk_northbound.py`（模块级常量 + `_build_*`/`_parse_*` 纯函数 + async fetch，无类）：

- `_EDGAR_UA_BASE`：UA 模板，运行时拼 `settings.sec_edgar_contact`。
- `_TICKER_TO_CIK` 惰性加载 + 模块级缓存（24h TTL，`time.monotonic` 记载入时刻；**不要**放进 market_cache——它是全 Agent 共享的行情缓存，1MB 映射表塞进去不合适）。
- `_REVENUE_TAGS`/`_EPS_TAGS` 候选列表（按 A0 实测排序），`_pick_concept(units, forms)` 纯函数：从 `units.USD` 过滤 `form ∈ {10-K, 10-Q}`、按 `end` 降序取最新年报值与最新季报值（年报取 form=10-K 最新一条，季报取 form=10-Q 最新一条）。
- `async def fetch_us_fundamentals(symbol: str) -> dict[str, Any]`：CIK 解析 → 并发（`asyncio.gather`）拉 3-4 个概念 → 归并为 `{"symbol", "cik", "revenue": {"annual": {...}, "quarterly": {...}}, "net_income": ..., "eps": ..., "gross_profit": ..., "source_urls": [...]}`，每个数值带 `form/end/filed` 溯源字段（可解释性是本项目定位）。单概念缺失 → 该键置 `None` + 附 caveat，**不整体失败**。
- `async def fetch_us_recent_filings(symbol: str, limit: int = 10) -> list[dict]`：form 过滤 10-K/10-Q/8-K，每项 `{form, filing_date, accession_no, document_url, primary_doc}`。
- httpx 超时/异常风格对齐 hk_northbound；**所有解析逻辑放纯函数**便于直测。

### A2. 注册表 `app/gateway/tool_registry.py`

在 `_US_STOCK_PLACEHOLDERS`（515 行）**同一个列表里追加**两条（域已存在、拼装式已含该列表，勿新建列表勿改 ALL_TOOLS）：

```python
ToolMeta(
    "us_fundamentals",
    "internal_us_fundamentals",
    "美股基本面（SEC EDGAR XBRL，必填: symbol=美股裸代码如 AAPL；返回营收/净利/EPS/毛利的最新年报与季报值，含申报文件溯源）",
    domain="us_stock",
    priority="high",
    http_method="INTERNAL",
    http_path="",
    category="fundamental",
),
ToolMeta(
    "us_filings_recent",
    "internal_us_filings_recent",
    "美股近期 SEC 申报（必填: symbol=美股裸代码；返回最近 10 份 10-K/10-Q/8-K 的类型/申报日/文件链接）",
    domain="us_stock",
    priority="medium",
    http_method="INTERNAL",
    http_path="",
    category="fundamental",
),
```

- **purpose 必须含全部必填参数**（上次冒烟教训：planner 只看 purpose 决定传参）。
- docstring 第 9 行计数 42 → 44。
- 同步核对 README/docs 中所有工具计数。

### A3. 分发 `app/graph/tool_runtime.py`

`_execute_internal` 增加两个分支，模式对齐 `internal_hk_northbound`（try/except → ToolResult，error 不抛出）：

- `internal_us_fundamentals`：**dispatch 层加 market_cache 缓存**（对齐 news_search 的 368 行模式，`_make_cache_key` + TTL 常量 12h——XBRL 数据日级更新）。
- `internal_us_filings_recent`：同上，TTL 1h（8-K 可能日内新增）。
- 两个分支开头先查 `settings.sec_edgar_contact`：为空 → 直接返回 `STATUS_ERROR`，error 写明"SEC_EDGAR_CONTACT 未配置（SEC 公平访问政策要求声明访问身份，见 .env.example）"，**不发起网络请求**。

### A4. 配置 `app/config.py` + `.env.example`

- Settings 增加字段 `sec_edgar_contact: str = ""`（env 名 `SEC_EDGAR_CONTACT`，建议值格式注释："YourName your@email.com"）。
- `.env.example` 增加对应条目与注释（说明 SEC 公平访问政策要求）。
- compose/部署契约测试不受影响（env_file 运行时注入，无需改 docker-compose.yml）。

### A5. Critic 规则 `app/graph/nodes/critic.py:55`

`us_stock` 规则改写（对齐现有风格：断言与数据绑定）：

```python
"us_stock": (
    "[美股审查规则]\n"
    "- 关于美股个股涨跌的结论必须有 us_klines / us_window 数据支撑\n"
    "- 财务类结论（营收/利润/EPS/估值）必须有 us_fundamentals 数据支撑，"
    "且不得混淆年报值与季报值\n"
    "- 不能在没有 K 线数据的情况下声称 '趋势突破/破位'"
),
```

### A6. 测试（新增 `tests/test_sec_edgar.py`）

1. **纯函数直测**（对齐 test_hk_northbound 模式）：`_pick_concept` 年报/季报选取、form 过滤、缺单位/空数组；filings form 过滤与 limit；tag 候选回退顺序（首个缺失自动换下一个）。
2. **注册表断言**：us_stock 域含 `us_fundamentals`/`us_filings_recent`，category=fundamental、http_method=INTERNAL、key 无冲突；`registry_text(domains=["us_stock"])` 含两条新工具；docstring 计数 == `len(ALL_TOOLS)`（既有契约测试兜底）。
3. **分发接线**（monkeypatch 模块全局，对齐 T17T 教训——打在 `app.graph.tool_runtime` 的导入名上）：patch `app.research.sec_edgar.fetch_us_fundamentals` 返回假数据 → ToolRuntime 走 INTERNAL 分支拿到 normalized 条目、**未触碰 Gateway**（参考 `test_gateway_reuse.py::test_internal_only_route_never_touches_gateway`）；patch 抛异常 → STATUS_ERROR 且 error 非空。
4. **配置门闩**：`sec_edgar_contact=""` → 分支直接返回 error、不发起网络请求（patch fetch 断言未被调用）。
5. **critic 接线**：`_DOMAIN_RULES["us_stock"]` 非空且含 "us_fundamentals" 字样。

### A7. 文档真值同步

- `README.md`：§支持的市场域美股行（基本面从"无"改为"SEC EDGAR XBRL：营收/净利/EPS/毛利 + 近期申报"）；§扩展新市场域；**Internal Tools 小节**（原列 2 个 hk 工具 → 加上 news_search 与本计划 2 个，共 5 个——news_search 现在似乎没列，核实后一并补真）。
- `docs/tools.md`：工具计数与 us_* 条目行。
- `.env.example`（见 A4）。
- 本文件文末追加"§执行记录"。

---

## 3. Phase B：验证与提交

1. 双模式全量 pytest + `ruff check . --no-cache` + `ruff format --check . --no-cache` 全绿（基线 540 → 预期 +8~14 条）。
2. 冒烟（有 key 条件下）：`MARKET_GATEWAY_MODE=http` 起服务，问 "苹果公司最近一个季度营收和利润怎么样"，确认 planner 选中 `us_fundamentals`、Evidence 含 EDGAR 数值与溯源、报告产出。无 key/无 SEC_EDGAR_CONTACT 则跳过并注明（探针已充当数据面验证）。
3. commit 拆分建议：
   - `feat: 美股基本面接入 SEC EDGAR —— us_fundamentals/us_filings_recent 内部工具 + SEC_EDGAR_CONTACT 配置 + critic 规则改写`
   - `test: SEC EDGAR 解析纯函数与 INTERNAL 分发测试`
   - `docs: README/tools 美股基本面真值同步 + 执行记录落档`
   - `chore: A0 探针脚本 verify_sec_edgar.py`
4. 交付报告：A0 实测表（公司 × tag 存在性 + 载荷大小 + UA 对照）、基线前后数字（双模式）、文件清单、commit 列表、遗留限制（哪些 tag 哪些公司缺失）、与计划真值的偏差。

## 4. 停止条件

1. A0 闸门失败（合规 UA 仍 403 / 结构不可解析）。
2. 基线本身有红。
3. 需要引入第三方依赖（yfinance/finnhub/sec-api 等）、修改 `ToolResult` 结构、或动 LangGraph 拓扑 —— 方案级变更，交回主 Agent。
4. `companyconcept` 对主流公司（AAPL/NVDA/TSLA 任一）营收+净利+EPS 三个核心概念全部缺 tag，无法注册有意义的 us_fundamentals。

---

## 7. 执行记录（2026-10-06，子 Agent 执行）

### A0 探针闸门实测（scripts/verify_sec_edgar.py）

- `company_tickers.json`：HTTP 200，780 KB，10434 个条目；AAPL=320193 / NVDA=1045810 / TSLA=1318605 全部命中。
- `companyconcept`（HTTP 200 = 存在）：

| tag | AAPL | NVDA | 载荷（AAPL/NVDA，KB） | 备注 |
|---|---|---|---|---|
| Revenues | 200 | 200 | 2.2 / 41.3 | AAPL 最新 10-K 停在 2018-09-29（陈旧） |
| RevenueFromContractWithCustomerExcludingAssessedTax | 200 | 200 | 17.9 / 4.9 | NVDA 侧停在 2022-01-30（陈旧） |
| NetIncomeLoss | 200 | 200 | 49.6 / 45.8 | 两家均为现行口径（AAPL FY2025、NVDA FY2026） |
| GrossProfit | 200 | 200 | 49.7 / 44.6 | 同上 |
| EarningsPerShareDiluted | 200 | 200 | 47.6 / 43.7 | 单位是 `USD/shares`，不是 `USD` |
| EarningsPerShareBasic | 200 | 200 | 47.4 / 43.5 | 同上 |

- `submissions`（AAPL）：HTTP 200，160 KB，`filings.recent` 1000 条，form 过滤 10-K/10-Q/8-K 正常取到最近 10 份。
- UA 对照：同一 companyconcept 请求不带 UA → HTTP 403（合规 UA → 200），符合 SEC 公平访问政策预期。
- 判定：**通过**（三类端点带合规 UA 均 200、载荷可控、AAPL/NVDA 核心概念齐全）。

### 实施摘要

- A1：`app/research/sec_edgar.py` —— 与 hk_northbound 同风格（模块常量 + 纯函数 + async fetch）。ticker→CIK 模块级缓存 24h TTL；`_pick_concept` 支持 `USD`/`USD/shares` 单位回退；候选 tag 全并发拉取后**按最新 10-K end 日期选优**（非顺序回退，原因见下"偏差"）；单概念缺失 → 键置 None + caveat。
- A2：注册表 +2 条（us_fundamentals / us_filings_recent，category=fundamental、INTERNAL），docstring 42 → 44。
- A3：`tool_runtime._execute_internal` +2 分支，market_cache 缓存 12h / 1h，`SEC_EDGAR_CONTACT` 空值门闩（不发网络请求）。
- A4：`Settings.sec_edgar_contact`（默认空）+ `.env.example` 条目。
- A5：critic `us_stock` 规则改写（财务结论必须有 us_fundamentals 支撑、不得混淆年报/季报）。
- A6：`tests/test_sec_edgar.py` 35 条（纯函数 / 注册表 / 分发接线 / 门闩 / critic）；分发测试 monkeypatch 打在 `app.graph.tool_runtime` 的导入名上。
- A7：README（工具表 44、美股行、Internal Tools 5 条）、docs/tools.md、`.env.example`、本节。

### 与计划真值的偏差

1. **营收 tag 选取策略**：计划 §A1 写"候选列表按 A0 实测排序 + 首个缺失自动换下一个"。实测发现两家公司的两个营收 tag **都存在但各有一边陈旧**（AAPL 的 Revenues 停在 2018、NVDA 的 RFCWC 停在 2022），纯"缺失才回退"会取到 7 年前的旧值。改为：组内候选全并发拉取、按最新 10-K `end` 日期选优（缺失回退仍是其特例），`_summarize_metric` 纯函数可直测。
2. **EPS 单位**：计划 §1.2/§A1 写"过滤 `units.USD`"，实测 EPS 概念的单位键是 `units["USD/shares"]`。`_pick_concept` 按 `_UNIT_KEYS` 优先级（USD → USD/shares）选单位键。
3. **计划未覆盖的既有测试**：`tests/test_us_stock_domain.py` 断言 us_stock 域**恰好 2 条**且所有 us 条目都有真实 POST 端点，与本计划 +2 条 INTERNAL 冲突 → 已同步更新（计数 2→4；OPENAPI 端点断言跳过 INTERNAL 条目）。
4. **冒烟**：执行环境无 LLM key，按计划 §3-2 跳过端到端冒烟（A0 探针已充当数据面验证）。

### 测试数字

- 基线（开工前）：双模式各 540 passed + 0 skipped；ruff check / format 全绿。
- 收尾：见 git log 对应提交前的最后一次全量运行（双模式 575 passed + 0 skipped；ruff 全绿）。
