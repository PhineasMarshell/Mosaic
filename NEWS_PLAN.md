# 新闻面（NewsAnalyst）多源接入执行计划 —— 交给子 Agent 直接落地

> 本文由主 Agent 基于**实测真值**编写（2026-10-08；真值取证基线为 `main` @ `23b0aca`）。
> 所有信源事实均来自本机实跑（Python 直连 + HTTP 探针），不是文档推断；每处实测结论都标了取证方式。
> **执行时不必重新调研信源**；如实际代码与本文引用不符，以代码为准并在交付报告中说明差异。
> **执行纪律（必须遵守，有既往踩坑）**：
> 1. **必须用 `.venv/Scripts/python.exe`**（Windows 环境，不要用系统 python）。
> 2. 开工前先跑全量基线并记录数字（**双模式各跑一次**：`MARKET_GATEWAY_MODE=mcp` 与 `=http`；基线应为全绿 + 0 skipped，预期约 575 passed，以实测为准；若基线本身有红，**停下报告，不要在红基线上开工**）：
>    ```bash
>    .venv/Scripts/python.exe -m pytest . -q
>    .venv/Scripts/python.exe -m ruff check . --no-cache
>    .venv/Scripts/python.exe -m ruff format --check . --no-cache
>    ```
> 3. 测试**不得依赖真实网络/真实 LLM**：解析函数直测 + monkeypatch 假响应（参考 `tests/test_hk_northbound.py` 纯函数模式、`tests/test_news_search.py` 的 `monkeypatch.setattr(news_search, "DDGS", ...)` 工厂打桩模式）。既有例外：`tests/test_news_search.py::TestAsyncWrapper` 是真网冒烟用例（容忍失败，只断言不抛异常），保持原样。
> 4. 注意已知测试坑：`market_cache` 全局污染、monkeypatch 打在模块全局上、污染类测试在单用例内顺序执行；新测试文件的 fixture 隔离要自洽。
> 5. **三步验证全绿才能 commit**；commit message 用中文 + `feat:`/`test:`/`docs:` 前缀（对齐 `git log` 现有风格）；**不要 push**。
> 6. **与 `SENTIMENT_PLAN.md` 严禁并行**：两份计划都改 `app/gateway/tool_registry.py` 的 `ALL_TOOLS` 拼装行、docstring 工具总数（`tests/test_docs_contract.py` 守护）与 `app/config.py`。若届时 sentiment 计划已先落地，本文所有「44 → 47」的数字与 `ALL_TOOLS` 现状须按其交付结果重算后再动笔。
> 7. **依赖预置说明**：主 Agent 为本计划探针已把 `akshare==1.19.1` 装进 `.venv`（含 pandas 等）。子 Agent 仍必须把它落进 `requirements.txt` 与 `pyproject.toml`（见 A5），并跑 `pip check` 确认无依赖冲突。

---

## 0. 目标与范围

**目标**：news 分析面从「单一 DDGS 关键词搜索」升级为**多源新闻聚合** —— 4 路信源（东财个股新闻 / 财联社电报 / Google 资讯 RSS / DDGS）、跨源去重、时效排序、来源标注进 datum，让 Critic 的「孤证不立」审查有数据可依。用户问「茅台最近有什么消息 / 今天盘面有什么新闻 / 白酒板块出了什么事」时，Agent 能按问题类型选对工具、拿到带来源与时间的新闻证据。

**本计划做**：
- Phase A：A0 探针闸门 → A1 信源适配层（纯函数）→ A2 三个新 INTERNAL 聚合工具 + `news_search` TTL 分档改造 → A3 注册表 → A4 白名单 → A5 配置与依赖 → A6 `_make_digest` 覆写（事件归因）→ A7 Critic 新闻证据纪律 → A8 planner 指引 → A9 测试 → A10 文档

**本计划不做**（明确排除，勿顺手实现）：
- **Bing News RSS**：本机实测死亡（见 §1.5），不做
- **巨潮 cninfo 公告**：本机实测接口返回旧数据/日期过滤失效（见 §1.5），本期不做，留档待上游恢复
- **RSSHub / SearxNG 自建**：方案级引入（新增常驻服务），本期不做
- **GDELT / 其他英文聚合 API**：本期先以 Google RSS 覆盖英文口，GDELT 不做
- **trafilatura 全文下钻**（拿 URL 抽正文）：本期不做，新闻正文用信源自带的摘要/内容字段
- **LLM 新闻打分 / 事件抽取**：规则聚合，不新增 LLM 调用点（对齐 sentiment 计划的既定纪律）
- **浏览器抓取 / 渲染**：三个长期包袱（反爬、渲染、合规），不做

---

## 1. 关键真值事实（已核实）

### 1.1 信源矩阵实测总表（2026-10-08 本机，主 Agent 亲测）

| 信源 | 实测结果 | 耗时 | 取证方式 | 定位 |
|---|---|---|---|---|
| **akshare `stock_news_em(symbol='600519')`** | ✅ 10 条，字段 `关键词/新闻标题/新闻内容/发布时间/文章来源/新闻链接` | **0.1s** | `.venv` python 直调 akshare 1.19.1 | **主路（A股个股新闻）** |
| **akshare `stock_info_global_cls()`** | ✅ 20 条，字段 `标题/内容/发布日期/发布时间`（财联社电报，全市场快讯） | **0.2s** | 同上 | **主路（大盘/盘面快讯）** |
| **akshare `stock_info_a_code_name()`** | ✅ 5572 行（全A code↔name），字段 `code/name`；**带 tqdm 进度条噪音（18 页翻页）** | **5.5s** | 同上 | 名称桥（长缓存 24h） |
| **Google News RSS**（`news.google.com/rss/search?q=…&hl=zh-CN&gl=CN&ceid=CN:zh-Hans`） | ✅ 100 条/查询，RSS 2.0；title 带 `" - 来源"` 后缀（如 `贵州茅台：今日暂停！ - 东方财富`）；pubDate RFC822 | ~2s | httpx `trust_env=True`（走本机代理 `127.0.0.1:7897`） | **主路（通用兜底 + 港美股中文）** |
| **Google News RSS 直连（不走代理）** | ❌ `ConnectTimeout` | 10s 超时 | httpx `trust_env=False` | **代理硬依赖**，必须降级设计 |
| **DDGS**（现有 `ddgs` 库，`region='wt-wt'`） | ⚠️ 首测成功 3 条（date 为 ISO8601 绝对时间），**随后 5 连败** `No results found`（限流）；`region='cn-zh'` 两次均 `No results found` | — | 多轮重试对照 | **兜底源**（可弃，永不作为唯一源） |
| **Bing News RSS**（`www.bing.com` / `cn.bing.com` + `format=rss`） | ❌ 302 重定向到 `cn.bing.com` 首页 HTML（查询参数丢失），直连 cn.bing.com 同样返回首页 HTML | — | httpx `follow_redirects=True` | **剔除** |
| **cninfo 公告**（akshare `stock_zh_a_disclosure_report_cninfo`） | ⚠️ 接口通（字段 `代码/简称/公告标题/公告时间/公告链接`）但日期过滤返回 0 行、无过滤返回 **2023 年旧公告页** | 0.4s | 两轮对照实测 | **本期剔除** |

**网络环境事实**：本机 shell 有 `HTTP_PROXY=HTTPS_PROXY=http://127.0.0.1:7897`、`NO_PROXY=localhost,127.0.0.1`。**akshare 国内源在代理环境下实测正常**（0.1–0.2s 返回），无需特殊处理；Google RSS 必须走代理。

### 1.2 DDGS 细节（决定改造方式的实测）

| 事实 | 取证 |
|---|---|
| `ddgs.news()` 返回 item 的 `date` 是 **ISO8601 绝对时间**（`'2026-10-07T19:47:00+00:00'`），不是相对时间 | 首测 3 条实跑 |
| item 字段固定 `['body','date','image','source','title','url']` | 实跑 `sorted(keys)` |
| `region='cn-zh'` 与 `'zh-cn'` 均 `No results found` / `RequestError`；**`region` 参数化放弃，保持现网 `wt-wt`**（中文 query 在 wt-wt 下实测能返回中文财经内容，首测 3 条来源均为搜狐） | 三组对照实跑 |
| **限流真实存在**：首测成功后紧接 5 连败，无退避恢复 | 连续 6 次实跑 |
| 现实现：`app/research/news_search.py:36-104`，`region="wt-wt"` 写死、`asyncio.to_thread` 包裹、空 query 防御分支、异常返回结构化错误（不抛） | 代码 + 既有测试 `tests/test_news_search.py` |

### 1.3 Google News RSS 细节

- URL 模板：`https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans`（英文口 `hl=en-US&gl=US&ceid=US:en`，A0 顺带复测一次英文口）。
- 解析要点（实测）：`<item>` 里 `title` 末尾 `" - 来源名"` 要剥离成独立 source 字段；`pubDate` 是 RFC822（`Mon, 05 Oct 2026 08:41:11 GMT`），用 `email.utils.parsedate_to_datetime`；`description` 是 HTML 片段，只取纯文本前 N 字做摘要；单查询 100 条，**必须在解析层截断**，不能全量进 datum。
- 响应是 XML（`<?xml` 开头）；HTTP 错误/重定向到 HTML 时 `ElementTree` 会抛 `ParseError` —— 解析函数必须把「非 XML」当「源失败」处理，不许把异常漏到聚合层之外。
- **用 stdlib `xml.etree.ElementTree` 解析，不引 feedparser**（少一个依赖；RSS 2.0 结构简单足够）。

### 1.4 akshare 三个接口的字段映射（写代码直接抄）

| 接口 | 返回列 | → NewsItem 映射 |
|---|---|---|
| `stock_news_em(symbol='600519')` | `关键词/新闻标题/新闻内容/发布时间/文章来源/新闻链接` | title=新闻标题, url=新闻链接, published_at=发布时间（`'2026-10-06 08:44:00'` naive）, source=文章来源, text=新闻内容 |
| `stock_info_global_cls()` | `标题/内容/发布日期/发布时间` | title=标题, text=内容, published_at=**发布日期+发布时间合成**（`'2026-10-08'`+`'14:00:48'`）, source='财联社电报', **无 url**（实测列里没有链接字段） |
| `stock_info_a_code_name()` | `code/name` | `dict[str, str]`（code 6 位无前缀 → 中文名） |

样例（实测第一条）：
```
stock_news_em:   新闻标题='贵州茅台：今日暂停！' 文章来源='证券时报网' 发布时间='2026-10-06 08:44:00'
                 新闻链接='http://finance.eastmoney.com/a/202610063888722080.html'
stock_info_global_cls: 标题='国庆假期1546.2万人次出入境 日均220.9万人次' 发布日期='2026-10-08' 发布时间='14:00:48'
```

**跨源一致性实证**：`stock_news_em` 头条 `贵州茅台：今日暂停！` 与 Google RSS 头条（剥后缀）**一字不差** —— 跨源标题去重有真实命中场景，`news_stats.multi_source_titles` 统计有牙。

**akshare 使用纪律**：同步阻塞（`asyncio.to_thread` 包裹，同 ddgs 模式）；模块顶部 try-import（`AKSHARE = None` 降级模式，同 `news_search.py:19-22` 的 DDGS 模式）；东财系上游有反爬，**只在低频 + 长 TTL 缓存路径下使用**（本计划全部内建）；`stock_info_a_code_name` 的 tqdm 进度条噪音：导入 akshare 前 `os.environ.setdefault("TQDM_DISABLE", "1")` 尽力抑制，若实测无效（tqdm 版本不支持）接受噪音并在交付报告注明，不阻塞。

### 1.5 已剔除信源的死亡记录（防止将来重蹈，勿凭印象复活）

| 信源 | 死因（实测） | 复活条件 |
|---|---|---|
| Bing News RSS | `format=rss` 302 → `cn.bing.com` 首页 HTML，查询参数全部丢失 | 微软恢复 RSS 输出（短期无望） |
| cninfo（`stock_zh_a_disclosure_report_cninfo`） | `start_date/end_date` 过滤返回 0 行；无过滤返回 2023 年旧公告页（分页定位旧档） | akshare 修复该接口或改用其他公告通道；公告类信源价值高，值得后续单独 A0 复测 |

### 1.6 仓库现状锚点（本计划要动的位置，行号为 `main @ 23b0aca` 实测）

| 事实 | 位置 |
|---|---|
| `news_search` INTERNAL 分支：缓存键 → `_search_news` → `_extract_news_entries` → 成功才 `market_cache.set(ttl=settings.news_search_ttl_seconds)` | `app/graph/tool_runtime.py:438-476` |
| `_execute_internal` 分发（新分支加在 `news_search` 分支之后、`internal_us_fundamentals` 之前） | `app/graph/tool_runtime.py:390` 起 |
| 内部工具范本：`internal_us_fundamentals`（SEC 门闩 + TTL 12h）、`internal_us_filings_recent`（TTL 3600） | `app/graph/tool_runtime.py:478` 起、`:34/:36` |
| `_resolve_ttl` 里 news_search 条目返回 `settings.news_search_ttl_seconds` | `app/cache.py:177`、`:169` |
| 配置：`news_enabled: bool = False`、`news_search_ttl_seconds: int = 21600` | `app/config.py:85-87` |
| `.env.example` **无任何 NEWS_ 条目**（README:364-365 有说明但示例文件缺失，本计划补齐） | `.env.example` |
| 注册表：`_NEWS_SEARCH` 单条（key=news_search, domain=cross, category=news, INTERNAL）；`ALL_TOOLS` 拼装行 `+ _NEWS_SEARCH` | `app/gateway/tool_registry.py:578-591`、`:649` |
| 注册表 docstring「所有 44 个 Market Gateway Tool」，`tests/test_docs_contract.py` 守护 | `app/gateway/tool_registry.py:9` |
| **T24 规则**：`BY_NAME` 构建时 cross 域条目优先抢占同 operationId 规范位，其余进 `SHARED_BY_NAME` —— 新工具 tool_name 必须全局唯一（本计划三个新名无冲突） | `app/gateway/tool_registry.py:656-674` |
| `registry_text(domains)` **严格等值过滤**（不自动带 cross；unknown/health 始终附加） —— a_share 域新闻工具天然对 us_stock/crypto 问题不可见 | `app/gateway/tool_registry.py:698-717` |
| Supervisor：显式域时 `registry_text(domains=[explicit_domain, "cross"])`；`route_candidate_categories` 在 `news_enabled` 时追加 `"news"` | `app/graph/nodes/supervisor.py:92-95`、`:33-34` |
| `WHITELIST_NO_SYMBOL` 现含 `news_search`（T8 语义：无 symbol 也要真正执行） | `app/graph/nodes/analysts/base.py:47-68`（news_search 在 `:62`） |
| `_make_digest` 基类实现 + **≤200 字硬约束** | `app/graph/nodes/analysts/base.py:293-305` |
| `NewsAnalystNode` 仅 7 行（`category = "news"`），无 digest 覆写 | `app/graph/nodes/analysts/news.py` |
| Critic `_DOMAIN_RULES` 按域组织（a_share/crypto/us_stock 三套），**无新闻证据纪律、无跨域通用规则注入点** | `app/graph/nodes/critic.py:43-62`、`_get_domain_rules` |
| 图接线：news 节点注册/fanout/edge 三处（`news_enabled` 控制） | `app/graph/builder.py:123-125`、`:150-151`、`:163-164` |
| planner 提示词已有 query 字段使用说明（`news_search` 为例） | `app/agent/prompts_graph.py:43` |
| `extract_news_entries` 产出的 datum `domain="media"`（新工具保持同口径） | `app/research/news_search.py:140-195` |
| `truncate()` 保留**末尾** 200 条 datum | `app/graph/tool_runtime.py:260-283` |
| 既有测试：`tests/test_news_no_symbol.py`（T8 回归，monkeypatch `app.graph.tool_runtime._search_news`）、`tests/test_news_search.py`（T34 打桩 + P4-3 缓存 + 1 个真网冒烟）、`tests/test_tool_runtime_ttl.py:26,47`（引用 `news_search_ttl_seconds`） | `tests/` |
| docs：`docs/tools.md` 核心表**无 news 行**；`docs/architecture.md:51`（NewsAnalyst 行）、`:63`（可选节点）、`:127`（SSE 标签）；README `:364-365`（env）、`:444/:456`（结构树）、`:485`（测试清单）、`:538`（工具列表「news_search — DDGS 新闻舆情搜索（跨域通用）」） | docs |

### 1.7 当前基线

双模式各 **575 passed + 0 skipped**，ruff check + format 全绿（上一会话实测；子 Agent 开工先复测记录）。

---

## 2. Phase A：实施

### A0. 探针闸门 —— `scripts/verify_news.py`（必须最先做）

风格仿 `scripts/verify_sentiment.py` / `verify_us_market.py`（`load_env()` 极简 .env 解析 + `probe() -> (ok, note)` + 可用性表 + 闸门判定）。主 Agent 2026-10-08 已实测一轮（§1.1 即结果），子 Agent 开工时**必须复测**（尤其 DDGS 限流恢复情况与代理在线情况）。

**实测清单**（每项最多重试 3 次、超时 20s）：
1. `stock_news_em('600519')`：条数、字段名是否仍是 §1.4 的六列。
2. `stock_info_global_cls()`：条数、四列字段。
3. `stock_info_a_code_name()`：行数量级（>5000）、首载耗时。
4. Google RSS 中文口（`贵州茅台`）+ 英文口（`AAPL`）：是否 XML、条数、第一条 title/pubDate。
5. Google RSS **代理断开对照**：子进程里清掉 `HTTP_PROXY/HTTPS_PROXY` 再测一次，确认 `ConnectTimeout` 仍复现（降级路径的设计前提）。
6. DDGS（wt-wt）：连测 3 次，记录成功次数（预期 0–1 次，限流是常态而非异常）。

**闸门判定**：
- **通过**：akshare 两路（1+2）可用 且 Google RSS（4）可用 → 全量实施。
- **部分通过**：akshare 两路可用、Google RSS 不可用（如代理故障）→ 砍掉 Google 源开工（`internal_symbol_news` 退化为东财单源、`internal_news_digest` 退化为 DDGS 单源 + partial 语义），交付报告注明。
- **失败**：akshare 两路全挂 → **停止 Phase A**，报告交回主 Agent（主路死亡，没有开工价值）。

### A1. 信源适配层 —— `app/research/news_sources/`（新包，纯函数优先）

```
app/research/news_sources/
├── __init__.py          # re-export 公开 API
├── types.py             # NewsItem + 各源失败异常
├── google_rss.py        # URL 构造 / httpx 抓取 / stdlib XML 解析
├── akshare_sources.py   # try-import akshare；三个接口的抓取与字段映射
├── ddgs_source.py       # 包 _do_news_search → NewsItem（date ISO→datetime）
└── aggregate.py         # dedup / rank / clip / symbol 归一（纯函数，全部可直测）
```

**`types.py`**：
```python
@dataclass
class NewsItem:
    title: str
    url: str = ""                 # 电报无 url，允许空
    published_at: datetime | None = None   # 统一 aware UTC 或 naive（见下）
    source: str = ""              # 东财=文章来源；Google=剥离后缀；电报='财联社电报'；DDGS=source 字段
    text: str = ""
```
时间统一约定：**解析即归一到 naive 本地时间字符串可比形式**（东财/电报实测就是 naive `'YYYY-MM-DD HH:MM:SS'`；Google RFC822 转 naive 本地；DDGS ISO 带 tz 转 naive 本地）。排序用 `datetime`，None 沉底。归一函数 `_to_naive(dt)` 必须是纯函数并有单测（tz-aware → `astimezone().replace(tzinfo=None)`）。

**`google_rss.py`**：
- `build_url(query: str, *, hl="zh-CN", gl="CN", ceid="CN:zh-Hans") -> str`（纯）
- `fetch(query, *, timeout, max_items=20) -> list[NewsItem]`（async；`httpx.AsyncClient(trust_env=True)` 走环境代理；`follow_redirects=True`；**取回即截断到 max_items**，100 条不外泄）
- `parse_rss(content: bytes, *, max_items=20) -> list[NewsItem]`（**纯**：ElementTree；title 末尾 `" - X"` 剥离；pubDate `parsedate_to_datetime`；description 去标签取纯文本；非 XML / 无 channel → 抛 `GoogleRssError`）

**`akshare_sources.py`**：
- 顶部 `try: import akshare as ak except ImportError: ak = None`；`_akshare_available()` 同 DDGS 模式；不可用时 fetch 抛 `AkshareUnavailableError`（聚合层转为「源失败」）。
- `fetch_stock_news(symbol6: str, *, timeout=30) -> list[NewsItem]`：`to_thread(ak.stock_news_em, symbol=symbol6)` → 按 §1.4 映射。
- `fetch_telegraph(*, timeout=15) -> list[NewsItem]`：`stock_info_global_cls()` → 标题/内容/日期+时间合成。
- `fetch_code_name_map(*, timeout=60) -> dict[str, str]`：`stock_info_a_code_name()` → `dict(zip(df['code'], df['name']))`。
- akshare 内部用 requests，超时参数不透传，超时控制放 `asyncio.wait_for` 外层兜底（`to_thread` + `wait_for` 组合；超时后线程仍在跑但不阻塞事件循环，可接受，注释说明）。

**`ddgs_source.py`**：`fetch_ddgs(query, *, max_results=8, time_limit="d", timeout=20)`：复用 `_do_news_search`（不重复实现限流防御），date 解析 `datetime.fromisoformat`（容错 `'YYYY-MM-DD'` 与 None）。

**`aggregate.py`**（全部纯函数，A9 必测）：
```python
def symbol_to_akshare6(symbol: str) -> str | None:
    # SH600519/SZ000001/BJ430047/600519/sh600519 → '600519'；其他 → None
def dedup_news(items: list[NewsItem]) -> tuple[list[NewsItem], int]:
    # 一级键：url 非空时 url 相等即重复；二级键：标题归一（lower + 去空白标点）相等即重复；返回 (去重后, 重复数)
def rank_news(items: list[NewsItem]) -> list[NewsItem]:
    # published_at 降序，None 沉底；stable
def clip(text: str | None, limit: int) -> str:  # 截断加 '…'
def multi_source_count(items: list[NewsItem]) -> int:
    # 标题归一后出现于 ≥2 个不同 source 的条数（critic 交叉验证的抓手）
```

### A2. 三个新 INTERNAL 工具 + `news_search` TTL 分档（`app/graph/tool_runtime.py`）

新分支全部加在 `news_search` 分支（`:438-476`）之后、`internal_us_fundamentals` 之前；**都必须 `return` 在会话分支之前**（只用内部工具的 analyst 不得触发 Gateway 建连）。datums 的 `domain` 一律 `"media"`（对齐 `extract_news_entries` 现口径）。

#### A2.1 `internal_symbol_news`（A 股个股新闻聚合，主路）

```
入参: symbol(str, 必填, 'SH600519'/'600519' 均可) / top_k(int, 默认 8, 1..15) / include_google(bool, 默认 True)
逻辑: ① symbol_to_akshare6 归一；失败 → status=error + 明确文案（不猜）
      ② code_name 表取名称：market_cache 键 'internal:code_name_map'（TTL 24h）；
         表取不到 → 用 6 位码当 query 的一部分，不阻塞
      ③ asyncio.gather 并发：fetch_stock_news(symbol6) + （include_google 时）google_rss.fetch(名称 or 6位码)
         每源 try/except，单源失败记 sources_failed，其余继续
      ④ 合池 dedup_news → rank_news → top_k → clip(text, 800)
      ⑤ datum：
         news_meta       {symbol, symbol6, name, sources_ok[], sources_failed[], total_before_dedup, kept,
                          window_start, window_end, multi_source_titles}
         news_top_{i}    i=1..top_k：value={title, url, source, published_at, text}（url 可空，电报同款语义）
         news_stats      {per_source: {源: 条数}, multi_source_titles}
      ⑥ status：两源全失败=error；恰一源失败=partial（note 写明哪路挂了）；否则 success
TTL:  900（settings.news_symbol_ttl_seconds）
```

#### A2.2 `internal_market_telegraph`（财联社电报快讯）

```
入参: top_k(int, 默认 15, 1..30) / keyword(str, 可选, 标题或内容包含即保留)
逻辑: fetch_telegraph() → keyword 过滤（空 keyword 不过滤）→ 时序倒序取 top_k → clip(400)
datum: telegraph_meta {total, kept, keyword, window_start, window_end}
       telegraph_{i}   {title, text, published_at, source='财联社电报'}（无 url）
TTL:  300（快讯变化快，settings.news_telegraph_ttl_seconds）
```

#### A2.3 `internal_news_digest`（跨市场主题/事件聚合，cross 域）

```
入参: query(str, 必填) / top_k(int, 默认 8, 1..15) / time_limit(str, 默认 'd', d/w/m)
逻辑: ① asyncio.gather：google_rss.fetch(query) + ddgs_source.fetch_ddgs(query, timelimit=time_limit)
         DDGS 失败自动重试 1 次（限流实证，只重试一次不许更多）
      ② 合池 dedup → rank → top_k → clip(800)
      ③ datum 结构与 internal_symbol_news 同款（meta/stats/top_{i}），meta.query 必写
      ④ status：全挂=error；一源挂=partial；否则 success
TTL:  900（settings.news_digest_ttl_seconds）
```

#### A2.4 `news_search` TTL 分档改造（现有分支小改）

`time_limit` 分档设缓存 TTL：`d → news_ttl_day_seconds(1800)`、`w → news_ttl_week_seconds(21600)`、`m → news_ttl_month_seconds(86400)`（「今天的新闻」缓存 6 小时是错误语义，实测发现并修正）。**删除旧键 `news_search_ttl_seconds`**（更少歧义，对齐 sentiment 计划 A5 的推荐处理方式），同步四处引用：`app/config.py:87`、`app/cache.py:177`（`_resolve_ttl` 改返回 week 档默认）、`app/graph/tool_runtime.py:466`、`tests/test_tool_runtime_ttl.py:26,47`、`README.md:365`。

### A3. 注册表（`app/gateway/tool_registry.py`）

`_NEWS_SEARCH` 块（`:578-591`）扩成 `_NEWS_TOOLS` 四条（或并列追加三条，风格自洽即可）：

```python
ToolMeta("symbol_news", "internal_symbol_news",
         "A股个股新闻聚合（东财个股新闻+Google资讯，跨源去重带来源；symbol 必填如 SH600519/600519）",
         domain="a_share", priority="high", http_method="INTERNAL", http_path="", category="news"),
ToolMeta("telegraph", "internal_market_telegraph",
         "财联社电报快讯（全市场最新电报流；可选 keyword 过滤标题/内容）",
         domain="a_share", priority="high", http_method="INTERNAL", http_path="", category="news"),
ToolMeta("news_digest", "internal_news_digest",
         "多源新闻聚合搜索（Google资讯+DDGS 双源去重；query 必填，不限市场域）",
         domain="cross", priority="medium", http_method="INTERNAL", http_path="", category="news"),
```

- `ALL_TOOLS` 拼装行（`:649`）追加；docstring `:9` 工具总数 **44 → 47**；`tests/test_docs_contract.py` 同步。
- T24 复核：三个新 tool_name 全局唯一，无 `BY_NAME` 冲突（主 Agent 已核，落地时再跑一遍 §A1 的 BY_KEY/BY_NAME 检查命令即可）。
- 域可见性推论（`registry_text` 严格等值）：a_share 两条只对 A 股问题可见；`news_digest` 与 `news_search` 一样 cross 全域可见 —— **这是有意设计**，港美股/加密问题走 digest。

### A4. `WHITELIST_NO_SYMBOL`（`app/graph/nodes/analysts/base.py:47-68`）

**只补两条**：`"internal_news_digest"`（query 型）、`"internal_market_telegraph"`（可空参）。
**不加** `internal_symbol_news` —— 它必须有 symbol，走 symbol 守卫（`base.py:241-247` 会从问题里的 6 位代码自动补齐，这正是想要的）。

### A5. 配置与依赖（`app/config.py` + `.env.example` + `requirements.txt` + `pyproject.toml`）

```python
news_enabled: bool = False            # 不变（默认关，.env 打开）
news_ttl_day_seconds: int = 1800      # news_search time_limit=d
news_ttl_week_seconds: int = 21600    # =w（原 21600 语义延续）
news_ttl_month_seconds: int = 86400   # =m
news_symbol_ttl_seconds: int = 900
news_telegraph_ttl_seconds: int = 300
news_digest_ttl_seconds: int = 900
news_code_name_ttl_seconds: int = 86400
news_max_text_chars: int = 800
news_source_timeout_seconds: int = 15
```

- 删 `news_search_ttl_seconds`（A2.4）。
- `.env.example` 补 `NEWS_*` 全段（现文件无任何 NEWS_ 条目）。
- `requirements.txt` 与 `pyproject.toml` 的 `dependencies` 列表都加 `akshare>=1.19`；**不加 feedparser**（stdlib 解析）。装完跑 `.venv/Scripts/python.exe -m pip check`。

### A6. `_make_digest` 覆写（`app/graph/nodes/analysts/news.py`）

规则版事件归因（**无 LLM**）：从 `results` 的 datum 统计 `news_top_*`/`telegraph_*` 条数、`sources_ok/failed`、`multi_source_titles`、`window_start/end`，产出形如：
`个股新闻8条(东财+Google,2条双源确认,最新14:00);电报15条(截止14:32)` 的摘要，**≤200 字硬约束**（`base.py:298`），超长截断。只覆写 `_make_digest`，不碰 `_execute_tools`。

### A7. Critic 新闻证据纪律（`app/graph/nodes/critic.py`）

新闻规则是**跨域**的，但 `_DOMAIN_RULES` 按域组织（`:43-62`）。两个方案，子 Agent 读 `critic.py` 全文后**按代码实况择一**并在交付报告注明选型：
- (a) 新增模块级 `_NEWS_RULES` 常量，在 critic prompt 组装处（`_get_domain_rules` 的调用点）**无条件附加**；
- (b) 退化方案：三域规则各追加一行（机械但零结构风险）。

规则文本（两方案同文）：
> 「涉及新闻/事件/消息面的论断必须引用 news_* 或 telegraph_* 证据，并标注时间与来源；单一来源的传闻性表述必须明示『单一来源，未交叉确认』；标题相同且出现于 ≥2 个独立来源（news_meta.multi_source_titles）方可称『已证实』；数条新闻只是样本，不得据此推断全市场情绪或长期趋势。」

### A8. planner 指引（`app/agent/prompts_graph.py`）

在 `:43` 的 query 字段说明附近追加 news 工具选用指引（3 行以内）：
「A股个股新闻/消息 → `symbol_news`（传 symbol）；大盘盘面快讯 → `telegraph`；跨市场主题/事件/港美股个股新闻 → `news_digest`（传 query）；轻量单源关键词查询 → `news_search`。」
supervisor 的 `route_candidate_categories` 已含 news（`news_enabled` 时），**不动**。

### A9. 测试矩阵

**新文件 `tests/test_news_pipeline.py`**（fixture 自洽，`market_cache` clear）：
1. 纯函数：`build_url`；`parse_rss`（正常 XML 样例 / title 后缀剥离 / RFC822 时间 / 非 XML bytes → `GoogleRssError` / max_items 截断）；`symbol_to_akshare6`（SH/SZ/BJ/裸 6 位/小写/非法）；`_to_naive`（tz-aware / naive / None）；`dedup_news`（url 同 / 标题归一同 / 均不同 → 重复计数）；`rank_news`（None 沉底、stable）；`clip`；`multi_source_count`。
2. akshare 适配层：monkeypatch 模块全局 `ak`（返回构造的小 DataFrame，pandas 已随 akshare 装上）→ 字段映射正确；`ak=None` → `AkshareUnavailableError`。
3. 聚合工具：monkeypatch 各 fetch → `internal_symbol_news`（两源成功 datum 数 ≤ top_k+2、meta 完整、multi_source 统计正确；**东财成功+Google 抛错 → partial**；两源全抛 → status=error 且 normalized 为空，对齐「0 条 datum 判 error」的既有纪律）；`internal_market_telegraph`（keyword 过滤 / 空参）；`internal_news_digest`（DDGS 首败重试 1 次成功 → success；两源挂 → error）。
4. TTL：`news_search` 三档取值；三个新工具 TTL 与配置键对上。
5. 注册表：4 条 news 工具存在、域/category 正确；`registry_text(domains=['a_share'])` 含 `symbol_news`/`telegraph`、不含 `news_digest` 之外的越域键；`registry_text(domains=['us_stock'])` **不含** a_share 两条、含 cross 两条；`BY_KEY`/`BY_NAME` 无重复。
6. 路由：`news_enabled=True` 时 `route_candidate_categories` 含 `news`（仿 `test_graph_routing` 既有模式）。

**必须改的既有测试**：`tests/test_tool_runtime_ttl.py:26,47`（旧键 → week 档新键）。
**必须保持通过的既有测试**：`tests/test_news_no_symbol.py` 两用例（monkeypatch 点 `app.graph.tool_runtime._search_news` 不变即不受影响）；`tests/test_news_search.py` 全部（含真网冒烟）；`tests/test_docs_contract.py`（数字 44→47 改后绿）。

### A10. 文档真值同步

| 文件 | 改什么 |
|---|---|
| `docs/tools.md` | 核心表补 4 行（news_search 现在就没列，一并补上：news_search/symbol_news/telegraph/news_digest） |
| `docs/architecture.md:51` | NewsAnalyst 行改「多源新闻聚合（东财个股 + 财联社电报 + Google 资讯 + DDGS，跨源去重、来源标注），开关控制」 |
| `docs/architecture.md:63` | 「可选节点」行补「news 关闭时 news 类工具归入 technical（现状语义保持）」——若现状本就如此则原文已准，勿改 |
| `README.md:364-365` | env 行：TTL 三档新键替换旧键 |
| `README.md:444/:456` | 结构树补 `app/research/news_sources/` 包与 news.py 注释更新 |
| `README.md:485` | 测试清单补 `test_news_pipeline.py` |
| `README.md:538` | 工具列表：news_search 描述更新 + 3 条新工具；总工具数 44 → 47（若 sentiment 计划已先行，按其交付数字累加） |
| `app/gateway/tool_registry.py:9` | docstring 总数 |

---

## 3. Phase B：验证与提交

1. 全量双模式：`.venv/Scripts/python.exe -m pytest . -q`，分别在 `MARKET_GATEWAY_MODE=mcp` 与 `=http` 下跑，记录数字（基线 575，新增后应 575+N）。
2. `ruff check . --no-cache` + `ruff format --check . --no-cache` 全绿。
3. **真链路 e2e**（不许只看 mock 单测；HTTP/mcp 模式均可，内部工具不走 Gateway）：
   - `internal_symbol_news(symbol='SH600519')`：status + datum 数 + 耗时；
   - `internal_market_telegraph(top_k=10)`：同上；
   - `internal_news_digest(query='白酒板块')`：同上（DDGS 大概率限流，partial 属正常，如实记录）；
   - **代理断开降级实测**（本计划的核心设计验证）：子 shell 清掉 `HTTP_PROXY/HTTPS_PROXY` 再跑一次 `internal_symbol_news` → 必须呈现 status=partial + sources_failed 含 google、东财源照常出 datum。在子 shell 里 `env -u` 清理，**严禁污染当前 shell 环境**；
   - `.venv/Scripts/python.exe -m pip check` 干净。
4. `git commit` 按 Phase 分提交（A1 适配层 / A2-A4 工具与注册表 / A5 配置依赖 / A6-A8 分析与提示词 / A9 测试可并入对应提交 / A10 文档），中文 + `feat:`/`test:`/`docs:` 前缀。**不要 push**。
5. 在本文档追加「执行记录」小节，必须包含四块：① A0 复测表（含原始输出片段与耗时）② 实施摘要（改了哪些文件、新增/改写的测试）③ **与本文档真值的偏差**（逐条列出计划写错或代码与描述不符之处）④ 测试数字（改动前/后，双模式）。

---

## 4. 停止条件（遇到就停，回报主 Agent，不要自行改方案）

1. A0 复测 akshare 两路（个股新闻 + 电报）全挂。
2. akshare 返回字段与 §1.4 差异大到无法写映射（如列改名/结构换型）。
3. 需要引入 akshare 之外的新依赖（feedparser/trafilatura 等）或新 LLM 调用点。
4. 需要改 `normalizer.py` 全局契约（`_CONTAINER_KEYS`/`_LIST_CAP`/`truncate()` 语义）才能完成。
5. `BY_KEY`/`BY_NAME` 与既有条目真实冲突且无法用 key 区分。
6. 基线本身有红。

---

## 5. 已知边界（写进交付报告与文档，不要隐瞒）

| 边界 | 事实 |
|---|---|
| DDGS 限流是常态 | 首测成、五连败（§1.2）；A0 复测 9 次全败。定位兜底，带 1 次重试，永不作为唯一源；**已决策接受「Google 单源 + partial」语义**（见 §7.8-1） |
| Google RSS 代理硬依赖 | 直连 ConnectTimeout 实测（§1.1）；代理挂 → 该源 partial 降级（Phase B 必须实测这条路径） |
| DDGS region 参数化已放弃 | cn-zh/zh-cn 实测均无结果，保持 wt-wt |
| Bing RSS / cninfo 公告已剔除 | 死亡记录见 §1.5；公告信源价值高，后续可单独复测 cninfo |
| akshare 上游漂移风险 | 约 45% 接口打东财，有反爬；本计划全部低频 + TTL 缓存路径；接口挂 → 结构化 error/partial，不炸 |
| 个股新闻单次 10 条 | `stock_news_em` 实测 10 条（文档称 20，以实测为准），无翻页参数 |
| 电报单次 20 条 | `stock_info_global_cls` 实测 20 条，无翻页参数 |
| 名称表首载 5.5s | 5572 行、18 页翻页（tqdm 噪音尽力抑制）；24h TTL 摊销，仅首次慢 |
| 新闻 ≠ 情绪 | news 面与 sentiment 面边界：新闻样本不得当全市场情绪（critic 规则已写）；雪球讨论流情绪面归 `SENTIMENT_PLAN.md` 管 |
| 电报无 url | 实测无链接字段，datum 允许空 url，Critic 不得因缺 url 判无效 |

---

## 6. 待主 Agent/用户确认的开放问题

1. TTL 档位数值（day 1800 / week 21600 / month 86400；symbol 900 / telegraph 300 / digest 900）是否合意 —— 子 Agent 按此实现，用户后续可只调 `.env`。
2. A7 critic 规则注入点选 (a) 通用附加 还是 (b) 三域各加一行 —— 子 Agent 按代码实况定，报告注明。
3. `internal_news_digest` 的 DDGS `time_limit` 默认值（现定 `d`，与 news_search 对齐）。
4. `include_google` 是否值得暴露给 planner（现为实现层参数，planner 不可见）。

---

## 7. 执行记录

> 子 Agent 落地，2026-10-08。基线 `main @ 23b0aca`，本文档为唯一权威规格。

### 7.1 A0 复测表（`.venv/Scripts/python.exe scripts/verify_news.py`，2026-10-08 本机）

**闸门判定：通过**（akshare 两路 + Google RSS 双语口可用）→ 全量实施。

| # | 探针 | 结果 | 条数/证据 | 耗时 | 与 §1.1 计划真值比对 |
|---|---|---|---|---|---|
| 1 | akshare `stock_news_em('600519')` | ✅ OK | 10 条，六列齐全；首条 `贵州茅台：今日暂停！` / 证券时报网 / `2026-10-06 08:44:00` | **976ms** | 一致（计划 0.1s，实测略慢但同量级） |
| 2 | akshare `stock_info_global_cls()` | ✅ OK | 20 条，四列齐全；`发布日期` 实测为 **`datetime.date(2026,10,8)` 对象**（非字符串） | **173ms** | 条数字段一致，**日期类型是计划未写明的新事实**（已按 date/str 双兼容实现） |
| 3 | akshare `stock_info_a_code_name()` | ✅ OK | 5572 行；首行 `{'code':'000001','name':'平安银行'}` | **7641ms** | 一致（计划 5.5s） |
| 4a | Google RSS 中文口（贵州茅台） | ✅ OK | 100 条；首条 `贵州茅台：今日暂停！ - 东方财富`，pubDate `Mon, 05 Oct 2026 00:54:02 GMT` | **1339ms** | 一致 |
| 4b | Google RSS 英文口（AAPL） | ✅ OK | 100 条；首条 `Why Is Apple Stock (AAPL) Trending Higher in Premarket Today, Oct. 7? - TipRanks` | **910ms** | 计划要求顺带复测，通过 |
| 5 | Google RSS **断代理对照**（子进程清 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`） | ✅ ConnectTimeout 复现 | `stdout='EXC ConnectTimeout'` | **15253ms** | 一致，降级路径设计前提成立 |
| 6 | DDGS（wt-wt）连测 3 次 ×3 轮 | ❌ **0/9 全败** | `#1:DDGSException #2:DDGSException #3:TimeoutException` | 4995 / 5415 / 14315ms | **比计划更差**（计划预期 0–1 次成功）：限流比主 Agent 首测更严重，已按「兜底源、失败重试 1 次、partial 属正常」处理 |

原始输出片段：

```
[1.akshare个股新闻] OK  10条 cols=['关键词','新闻标题','新闻内容','发布时间','文章来源','新闻链接'] 首条标题='贵州茅台：今日暂停！' ... (976ms)
[2.akshare电报]     OK  20条 cols=['标题','内容','发布日期','发布时间'] 首条标题='' 日期=datetime.date(2026, 10, 8) (173ms)
[3.akshare全A名称表] OK  5572行 首行={'code': '000001', 'name': '平安银行'} (7641ms)
[4a.GoogleRSS中文]   OK  100条 首条title='贵州茅台：今日暂停！ - 东方财富' (1339ms)
[4b.GoogleRSS英文]   OK  100条 首条title='Why Is Apple Stock (AAPL) Trending Higher...' (910ms)
[5.GoogleRSS断代理对照] OK  ConnectTimeout 复现（降级前提成立）stdout='EXC ConnectTimeout' (15253ms)
[6.DDGS(wt-wt)x3]   FAIL 连测3次成功0/3 #1:DDGSException #2:DDGSException #3:DDGSException (4995ms)
闸门判定：通过（akshare 两路 + Google RSS 可用）→ 全量实施
```

### 7.2 实施摘要

**新增文件**

| 文件 | 内容 |
|---|---|
| `app/research/news_sources/types.py` | `NewsItem` dataclass、`_to_naive` 时间归一纯函数、`GoogleRssError`/`AkshareUnavailableError`/`DdgsSourceError` |
| `app/research/news_sources/google_rss.py` | `build_url`（纯）、`parse_rss`（纯，stdlib ElementTree）、`fetch`（async，httpx `trust_env=True` 走代理） |
| `app/research/news_sources/akshare_sources.py` | `fetch_stock_news`/`fetch_telegraph`/`fetch_code_name_map`，`to_thread`+`wait_for` 兜底，`TQDM_DISABLE` 抑制进度条 |
| `app/research/news_sources/ddgs_source.py` | `fetch_ddgs` 薄封装，复用 `_do_news_search` 的限流防御 |
| `app/research/news_sources/aggregate.py` | 纯函数：`symbol_to_akshare6`/`normalize_title`/`dedup_news`/`rank_news`/`clip`/`multi_source_count` |
| `scripts/verify_news.py` | A0 探针闸门脚本（6 项，含子进程断代理对照） |
| `tests/test_news_pipeline.py` | A9 测试矩阵，**63 用例** |

**改写文件**：`app/graph/tool_runtime.py`（三个聚合工具 + `_build_news_datums`/`_news_status`/`_news_window` + news_search TTL 分档）、`app/cache.py`（`_resolve_ttl` 新增 `arguments` 参数）、`app/config.py`（删旧键 + 9 个新键）、`.env.example`（补全 `NEWS_*` 全段）、`app/gateway/tool_registry.py`（`_NEWS_SEARCH`→`_NEWS_TOOLS` 四条，docstring 44→47）、`app/graph/nodes/analysts/base.py`（白名单 +2）、`app/graph/nodes/analysts/news.py`（`_make_digest` 覆写）、`app/graph/nodes/critic.py`（`_NEWS_RULES`）、`app/agent/prompts_graph.py`（工具选用指引）、`requirements.txt`/`pyproject.toml`（`akshare>=1.19`）、`docs/tools.md`/`docs/architecture.md`/`README.md`。

**改写的既有测试**：`tests/test_tool_runtime_ttl.py`（旧键→week 档）、`tests/test_tool_registry.py`（news 类 1→4 条）、`tests/test_gateway_tool_names_321.py`（44→47 / 37→40）、`tests/test_news_pipeline.py`（新增）。

### 7.3 与计划真值的偏差（逐条）

**代码缺陷（4 个，均由本次 A9 测试暴露并修复，非计划预期内）**

1. **`fetch_ddgs` 直接 `await` 同步函数** —— 计划 A1 写「复用 `_do_news_search`」，实现写成 `asyncio.wait_for(_do_news_search(...))`，而 `_do_news_search` 是**同步**函数 → 运行时必抛 `'dict' object can't be awaited`，DDGS 源 100% 失败。修复：改 `asyncio.to_thread(_do_news_search, ...)`。测试日志实证 `WARNING news_search.py:94 ... 'dict' object can't be awaited`。
2. **`multi_source_titles` 在去重后池上统计恒为 0** —— 计划 A2.1 只说「multi_source_titles 统计」，未指明池。`dedup_news` 会把同标题跨源条目折叠成一条（只留先出现者），在去重后池上算，该数字**永远为 0**，Critic 的「≥2 源才算已证实」抓手彻底失效。修复：`multi_source_count` 改在**去重前合池**上算，经 `_build_news_datums(multi_source_titles=...)` 参数传入 stats 与 meta。**e2e 实测该数字为 2，有牙**。
3. **`parse_rss` 的 description 用 `findtext` 丢嵌套标签正文** —— 计划 A1 只说「description 去标签取纯文本」。`findtext` 只返回第一个文本节点，`<description>内容<b>摘要</b></description>` 只拿到 `内容`。修复：新增 `_element_text()` 用 `itertext()` 取全文本，title/link/pubDate 同步加固。
4. **`_resolve_ttl` 覆盖 news_search TTL 分档（A2.4 失效）** —— 计划 A2.4 说「`_resolve_ttl` 改返回 week 档默认」，但 `execute()` 在 `_do_execute` 之后会用 `_resolve_ttl` 对**同一 key 再 set 一次**（`tool_runtime.py:361-363`），把 d/m 两档刚写进去的分档 TTL 又覆盖回 week 21600 → **分档形同失效**。修复：`_resolve_ttl(tool, settings, arguments)` 新增 `arguments` 形参，按 `time_limit` 返回对应档位（§2 处 A2.4 的设计前提在此与既有 execute 缓存机制冲突，计划未预见）。

**事实补充（计划未写明）**

5. akshare `stock_info_global_cls()` 的 `发布日期` 实测返回 **`datetime.date` 对象**而非字符串（§1.4 记为 `'2026-10-08'`）。实现已按 date/str 双类型兼容，并加测试覆盖两种。
6. DDGS 限流比计划预期更严重：本轮 **0/9** 全败（计划预期 0–1/3 成功）。不影响闸门，但意味着 `news_digest` 在现网大概率长期 partial——已按兜底源定位处理。
7. `TQDM_DISABLE=1` 对 `stock_info_a_code_name()` 的 tqdm 噪音**抑制有效**（探针输出无进度条噪音）。
8. `registry_text` 的域过滤是**严格等值**（§1.6 已记），因此 `registry_text(domains=['us_stock'])` **不含** cross 的 `news_search`/`news_digest`——需由 Supervisor 显式传 `['us_stock','cross']` 才可见。A9 原测试期望「us_stock 单独可见 cross 两条」与代码实况不符，已按代码改写测试并在测试内注明调用形态。

**选型记录**

9. **A7 选方案 (a)**：新增模块级 `_NEWS_RULES`，在 `critic.py` prompt 组装处**无条件附加**（不塞进 `_DOMAIN_RULES`）——理由：`unknown` 域的 `_get_domain_rules` 返回空串，放进按域组织的结构会丢规则。
10. `app/graph/tool_runtime.py:349` 有一处日志被误硬编码为 `logger.info("Cache hit for internal_symbol_news")`（公共 execute 路径），已改回 `logger.info("Cache hit for %s", tool_name)`。
11. 计划 §1.6 提到 `truncate()` 保留末尾 200 条 —— 新增 datum 均 ≤ top_k+3 条，远未触及，未改 `normalizer.py` 全局契约。**未新增任何 akshare 之外的依赖、未新增 LLM 调用点、未触碰 SENTIMENT_PLAN.md 范围。**

### 7.4 测试数字（双模式）

| 阶段 | `MARKET_GATEWAY_MODE=mcp` | `MARKET_GATEWAY_MODE=http` |
|---|---|---|
| 计划基线（§1.7，主 Agent 上一会话实测） | 575 passed + 0 skipped | 575 passed + 0 skipped |
| 本次改动后最终 | **691 passed, 0 failed, 0 skipped**（12.5s） | **691 passed, 0 failed, 0 skipped**（11.9s） |
| 净增 | +116 | +116 |

- `ruff check . --no-cache` → `All checks passed!`
- `ruff format --check . --no-cache` → `128 files already formatted`
- `.venv/Scripts/python.exe -m pip check` → `No broken requirements found.`
- `tests/test_news_pipeline.py` 单文件：63 passed，**不触真实网络、不打真实 LLM**（akshare 三接口、Google httpx、DDGS 全部 monkeypatch 模块全局；`market_cache` 用 autouse fixture 每例清空）。
- 中途状态留档：接手时（前一 Agent 留下的 A9 草稿）双模式实测为 **6 failed / 683 passed**，其中 4 项是真实实现缺陷、1 项是 `test_gateway_tool_names_321` 计数未同步、3 项是测试期望写错（TTL 档位断言误把 `time_limit` 当 `deadline` 位置参数传入、跨源同题 fixture 标题不同导致 multi_source 误判、registry 域过滤期望与代码实况不符）。

### 7.5 Phase B 真链路 e2e（`MARKET_GATEWAY_MODE=http`，内部工具不走 Gateway）

| # | 调用 | status | datum 数 | 耗时 | 要点 |
|---|---|---|---|---|---|
| 1 | `internal_symbol_news(symbol='SH600519', top_k=8)` | **success** | 10 | 7071ms | `sources_ok=[eastmoney,google]`，`total_before_dedup=30`，`duplicates_removed=2`，`kept=8`，**`multi_source_titles=2`**，`per_source={金融界:1,新浪财经:3,证券时报网:3,第一财经:1}` |
| 2 | `internal_market_telegraph(top_k=10)` | **success** | 11 | **150ms** | `total=11 → kept=10`，时间窗 `16:06→15:49`，url 字段按电报语义省略 |
| 3 | `internal_news_digest(query='白酒板块')` | **partial** | 10 | 3951ms | `sources_ok=[google]`、`sources_failed=[ddgs]`（DDGS 0/9 限流，**如实属预期**），`multi_source_titles=2` |
| 4 | **断代理降级**（子 shell `cmd /c "set HTTP_PROXY=&& set HTTPS_PROXY=&& …"`，未污染当前 shell） | **partial** | 10 | 11096ms | `sources_failed=['google']`、`sources_ok=['eastmoney']`，**东财源照常出 10 条 datum**，与计划 §3-3 要求完全一致 |

第 1、3 项的 `multi_source_titles=2` 是 §7.3-2 修复的直接价值证明：修复前该字段恒为 0（Critic 无法据此判断「已证实」），修复后真实链路上有非零命中。

### 7.7 §6 开放问题的落地作答

1. **TTL 档位数值**（day 1800 / week 21600 / month 86400；symbol 900 / telegraph 300 / digest 900）——已按此实现并全部落 `.env`，用户可只调 `.env`。注意：day 档已因 §7.3-4 的修复而**真正生效**（修复前恒为 week）。
2. **A7 注入点**——选 **(a) 模块级 `_NEWS_RULES` + prompt 组装处无条件附加**，理由见 §7.3-9。
3. **`news_digest` 的 DDGS `time_limit` 默认值**——保持 **`d`**（与 news_search 对齐），非法档位回落 `d`。
4. **`include_google` 是否暴露给 planner**——**不暴露**（已决策，见 §7.8-5）。该开关只能减信息量（关掉后 `multi_source_titles` 在结构上恒为 0），不能加信息量；降级决策应由降级逻辑（partial）表达，而不是交给规划层。

### 7.8 遗留问题

1. ~~DDGS 长期不可用~~ —— **已决策（用户裁定，2026-10-08）：接受「Google 单源 + partial」语义。** DDGS 维持兜底源定位（失败重试 1 次、永不作为唯一源），`news_digest` 在现网大概率长期 `status=partial`，属**预期行为而非故障**。后续若要换兜底源，须先重跑 `scripts/verify_news.py` 复测候选信源再改实现。
2. **Bing News RSS / cninfo 公告**仍为死亡记录（§1.5），未复活；公告类信源价值高，值得后续单独 A0 复测。
3. `stock_info_a_code_name()` 首载 7.6s 靠 24h TTL 摊销；服务重启后的首次 `symbol_news` 调用会因此变慢（实测 7.1s 含此开销），可接受但需知晓。
4. **「主动关源」与「降级失败」在 datum 上不可区分**（本次讨论发现，未修）：`include_google=False` 时 `sources_failed=[]`、`status=success`，只靠 `sources_ok` 少一项区分不了「Google 从没被试过」与「Google 试了并成功」；且 digest 不打印 `sources_failed`。现状 `include_google` 默认 `True` 且不对 planner 暴露，不会触发。若将来开放该参数，建议同时在 `news_meta` 补 `google_skipped` 标记，否则 Critic 的新闻证据纪律会读到歧义证据。
5. **`include_google` 不对 planner 暴露**（已决策，理由见 §7.7-4 的修正版）：该开关只能减信息量——关掉后 `sources_ok` 只剩东财，`multi_source_titles` 在结构上恒为 0，跨源确认能力整个消失。补充说明：上一轮曾表述为「planner 会倾向关掉它」，该表述不准确——它当前不在注册表 purpose 文案里，planner 无从看到；真实理由是「只能减信息量、不能加信息量的开关，暴露给规划层无正收益」，而其唯一可见成本只是延迟（断代理实测 11s vs 东财单独 7s）。

### 7.9 提交记录（本地，未 push）

```
8437087 feat: A0/A1 新闻面多源信源适配层 —— …（A0 探针闸门通过）
ad970ee feat: A2-A4 三个新闻聚合内部工具 + 注册表/白名单 —— …
606efd5 feat: A5 新闻面配置与依赖 —— …
4a490c9 feat: A6-A8 新闻分析面归因与证据纪律 —— …
3bd2589 test: A9 多源新闻测试矩阵 —— …（63 用例）
289f35f docs: A10 文档真值同步 —— …（工具总数 44→47）
```
