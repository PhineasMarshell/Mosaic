# A 股情绪面（舆情）接入执行计划 —— 交给子 Agent 直接落地

> 本文由主 Agent 基于**实测真值**编写（2026-10-06，main @ a15c209 + 工作区未提交的 MCP 语法修正，工作树有改动）。
> 所有 Gateway 事实均来自本机实跑（HTTP 直连 + MCP `list_tools`），不是文档推断；每处实测结论都标了取证方式。
> **执行时不必重新调研**；如实际代码与本文引用不符，以代码为准并在交付报告中说明差异。
> 执行纪律（环境、基线、commit 规则）沿用 `US_STOCK_INTEGRATION_PLAN.md` §1.5，此处不重复，开工前先读那一节。
> **执行顺序（已定）：本计划先做，`MARKET_GATEWAY_321_MIGRATION.md` 后做。** 两份都改 `app/gateway/tool_registry.py` 与 `WHITELIST_NO_SYMBOL`，严禁并行。

---

## 0. 目标与范围

**目标**：`sentiment`（舆情/情绪）分析面从「契约里有、节点未实现」变为**可用** —— 用户问「茅台现在市场情绪怎么样 / 大家怎么看」时，Agent 能规划情绪工具、拿到真实雪球讨论流与 A 股情绪指标、产出经 Evidence Gate → Reasoning → Critic 审计的结论。

**本计划做**：
- Phase A：A0 探针闸门 → 注册表改名（**仅情绪面相关条目**）→ 2 个新 INTERNAL 工具 + 2 个聚合 INTERNAL 工具 → `SentimentAnalystNode` → 配置 → 测试 → 文档

**本计划不做**（明确排除，勿顺手实现）：
- **其余 ~30 个工具从旧 operationId 改成 3.2.1 新名**（那是 `MARKET_GATEWAY_321_MIGRATION.md` 的范围，两件事分开做）
- 港股/美股情绪面（**本机无可用源**：`list_stock_discussions` 的 feed 对 00700/AAPL 无 discussion 数据；`/ashare-master/sentiment` 只覆盖 A 股）
- 浏览器抓取 / 页面渲染绕过 Gateway（反爬、渲染、合规三个长期包袱，方案级决策，需回到用户）
- LLM 情绪打分（本计划用**规则聚合**，不新增 LLM 调用点；见 §2-A4 说明）
- 富途/同花顺评论源（`_SENTIMENT_TOOLS` 注释里的旧设想，当前 Gateway 无这些端点）

---

## 1. 关键真值事实（已核实）

### 1.1 Gateway 3.2.1：讨论流真实可用，但**全部换名了**

| 事实 | 取证方式 |
|---|---|
| `GET /market/discussions` → **200**，返回 `{"source":"xueqiu","symbol":"SH600519","category":"discussion","page":1,"count":5,"items":[…],"max_page":100}` | HTTP 直连实测（`X-API-Key` header） |
| 旧路径 `/xueqiu/timeline` → **403 `{"detail":"path_not_exposed"}`**（不是权限问题，是路径已不存在） | HTTP 实测：4 个 symbol × 4 种 source 口径 × page 1/2 × count 1/20/50 全 403 |
| **新旧 OpenAPI 对比：43 条路径中 operationId 完全不变的有 0 条**（全部改名）；7 条路径消失、17 条新增 | 对比 `allowed_openapi.json`（33 条，旧）与 `%LOCALAPPDATA%\iiix\plugins\market-gateway\versions\3.2.1\extracted\api\openapi.json`（43 条，新） |
| MCP 通道：`list_tools()` → **40 个工具**（新名）；**但真实工具调用当前被 iiix 侧 OAuth 刷新超时挡住**（详见 §3 顶部的通道现状说明） | `MCP_ARGS="plugin serve market-gateway"` + 0.8.4 二进制实跑 |
| **HTTP 模式是当前唯一能真正执行工具调用的通道**（`MARKET_GATEWAY_MODE=http` + `X-API-Key`），本文所有 payload 真值都来自它 | HTTP 直连实测 |

**本计划涉及的工具新旧名对照**（只列情绪面要动的）：

| 旧 operationId（现注册表） | 新 operationId（3.2.1） | 路径 | 变化 |
|---|---|---|---|
| `public_sentiment_ashare_master_sentiment_get` | **`get_ashare_sentiment`** | `/ashare-master/sentiment`（**未变**） | 改名 |
| `public_limit_up_count_ashare_master_limit_up_count_get` | **`get_limit_up_count`** | `/ashare-master/limit-up/count`（未变） | 改名 |
| `public_limit_up_sectors_ashare_master_limit_up_sectors_get` | **`list_limit_up_sectors`** | `/ashare-master/limit-up/sectors`（未变） | 改名 |
| `public_limit_up_pool_ashare_master_limit_up_pool_get` | **`list_limit_up_stocks`** | `/ashare-master/limit-up/pool`（未变） | 改名 |
| `timeline_xueqiu_timeline_get` | **`list_stock_discussions`** | `/xueqiu/timeline` → **`/market/discussions`** | 改名 + 改路径 |
| （无） | **`list_stock_post_comments`** | **`/market/post-comments`** | **新增工具** |

### 1.2 `list_stock_discussions` 参数与响应实况（实测）

MCP `input_schema`（`list_tools()` 实测）：

| 参数 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `symbol` | string | **required**，minLength 1，maxLength 12 | 「A股用 SH600519 / SZ000001 / BJ430047，港股可加市场前缀或直接 00700，美股直接 AAPL」 |
| `feed` | string | enum `all\|discussion\|trade\|news\|announcement\|research\|ir`，default **`discussion`** | 信息流类型 |
| `page` | integer | 1..**20**，default 1 | 翻页，注明「最多 20 页」 |
| `count` | integer | 1..**20**，default 10 | 每页条数 |
| `source` | string | `const "xueqiu"`，default `xueqiu` | 只支持雪球 |

**实测到的硬约束（与 schema 不一致，必须按实测写代码）**：

| 约束 | 实测值 | 取证 |
|---|---|---|
| `count=20` 实际每页只返回 **10 条** | 3 页各 10 条 | 实跑 page=1/2/3 |
| 响应里的 `max_page` 是 **100**，但 `page>20` → **422** | 服务端拒绝 | 实跑 page=21 |
| 单 symbol 实际可达上限 = **20 页 × 10 条 = 200 条** | — | 推算 + 分页无重叠验证（page1 ∩ page2 的 id = 0） |
| `feed=讨论`（中文）→ **422** | 必须用 enum 英文值 | 实跑 |
| `symbol` 校验失败示例：`600519` / `BTC/USDT` → 422「symbol='600519' 不是支持的证券代码；证券代码：A股用 SH600519 / SZ000001 / BJ430047，港股可加市场前缀或直接 00700，美股直接 AAPL」 | — | 实跑 |

**item 字段（30 条实测，全部 30/30 存在）**：
`id`, `title`, `text`, `published_at`, `author`, **`author_id`**（界面文档没写，实际有）, `like_count`, `reply_count`, `repost_count`, `url`。
`url` **30/30 非空**（形如 `https://xueqiu.com/9279954254/411373813`）→ **评论下钻对每条都可行**。

**互动数据稀疏（决定排序策略的实测）**：

| 字段 | 非零比例 | 非零值域 |
|---|---|---|
| `like_count` | **18/30 (60%)** | 1–10 |
| `reply_count` | **5/30 (17%)** | 1–6 |
| `repost_count` | **1/30 (3.3%)** | 1 |

**密度与文本长度**：30 条覆盖 `2026-10-06 14:34:04 → 18:32:30`（约 4.5 小时 ≈ **6.7 条/小时**）；`text` 长度 **min 17 / max 6027 / 中位 389**；`title` 30/30 非空（部分帖 title == text）。

样例（最短帖）：
```json
{"id":411373813,"title":"贵州茅台股价，白酒全面崩盘？","text":"贵州茅台股价，白酒全面崩盘？","published_at":1791281029000,"author_id":9279954254,"author":"别问问87f","reply_count":0,"like_count":1,"repost_count":0,"url":"https://xueqiu.com/9279954254/411373813"}
```

### 1.3 `list_stock_post_comments` 参数（实测 schema）

| 参数 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `post_url` | string | **required**，minLength 1，maxLength 256 | 「timeline.items[].url 返回的帖子完整 URL，原样传入，不要改造」 |
| `expand_replies` | boolean | default `false` | 「是否继续查看回复，获取楼中楼」 |
| `max_pages` | integer | 1..**20**，default 1 | 最多翻取评论页数 |
| `max_reply_requests` | integer | 1..**50**，default 20 | 最多触发楼中楼请求数 |
| `source` | string | `const "xueqiu"`，default `xueqiu` | — |

> A0 必须实测：`expand_replies=false` 与 `true` 的响应结构、单帖评论条数上限、`max_pages` 与 `max_reply_requests` 的真实效果。**不要在 A0 之前假设字段名**。

### 1.4 `get_ashare_sentiment` 的 datum 爆炸问题（本计划必须解决）

`get_ashare_sentiment` **无入参**（`input_schema.properties = {}`，`required = []`）。HTTP 响应形如 `{"source":"ashare-master","type":"qingxu","series":{…}}`。

**实测：一次调用 = 26,447 条 datum** —— `app/gateway/normalizer.py` 的 `_CONTAINER_KEYS = {"candles","data","items","rows","results","trades"}`（`app/gateway/normalizer.py:142`）不含 `series`，但 `_flatten` 仍把 `series.QX[]` 整个序列逐项展平成 datum（`type=qingxu`、`series.QX[0]=41`…）。

**这是现有缺陷**：26,447 条 datum 会灌爆 `market_cache` 与 reasoning 输入，且 `truncate()`（`app/graph/tool_runtime.py:260-283`）保留**末尾 200 条** —— 对「新→旧」的序列保住的是**最旧**那一段，语义正好反了。

**本计划的做法**：**不改 normalizer**（那是全局契约，风险外溢到 44 个工具），改为注册一个 INTERNAL 聚合工具 `internal_sentiment_index`，内部走 Gateway 拿到 raw，然后**自己在内部聚合**成 ≤5 条 datum（QL/QX 最新值 + 窗口统计 + 趋势方向）。`get_ashare_sentiment` 本身仍注册（供 A0/诊断/未来直查），但不作为情绪面的主入口。

### 1.5 仓库现状（本计划要改的位置）

| 事实 | 位置 |
|---|---|
| `AnalystName = Literal["technical","fundamental","moneyflow","news","sentiment"]` —— sentiment **已在契约里** | `app/graph/state.py:96` |
| `sentiment_enabled: bool = False`（注释「评论 MCP 就绪后置 true」）、`sentiment_max_comments: int = 500` | `app/config.py:64-66` |
| builder 对 sentiment 只打 warning「sentiment_enabled=true, but the sentiment node is not implemented; this setting will be ignored.」；图只注册 technical/fundamental/moneyflow + 可选 news | `app/graph/builder.py:105-108`（warning）、`builder.py:119-128`（注册）、`builder.py:142`（默认 fanout）、`builder.py:145-151`（`_fanout_map`） |
| `route_candidate_categories(settings)` 返回 `["technical","fundamental","moneyflow"]`，news 按开关追加；注释明确「在 builder.add_node("sentiment", ...) 前不能把它暴露为可路由目标，否则条件边会指向未知节点并导致建图失败」 | `app/graph/nodes/supervisor.py:25-37` |
| analyst 骨架：`MarketAnalystNode`（`category`、`WHITELIST_NO_SYMBOL`、`_execute_tools`、`_make_digest`）；`_execute_tools` **只执行 `state["route"]` 里 `analyst == self.category` 的 tool_calls** | `app/graph/nodes/analysts/base.py:35`、`:47-68`、`:181-186` |
| 最小子类范本：`class NewsAnalystNode(MarketAnalystNode): category = "news"`（全文 7 行） | `app/graph/nodes/analysts/news.py` |
| **无 `sentiment.py` 源码**（只有 `__pycache__` 残留） | `app/graph/nodes/analysts/` |
| `WHITELIST_NO_SYMBOL` 现含 `public_sentiment_ashare_master_sentiment_get`、4 个 limit_up 系列、`news_search`、`search_xueqiu_search_get`、`longhu_xueqiu_longhu_get`、`internal_hk_northbound`、`internal_hk_index` | `app/graph/nodes/analysts/base.py:47-68` |
| `_ASHARE_MASTERTOOLS` = 4 条，**category 全是 `technical`**（含 sentiment 那条） | `app/gateway/tool_registry.py:76-117` |
| `_ASHARE_MICRO` 含 `key=timeline` / `tool_name=timeline_xueqiu_timeline_get` / `http_path=/xueqiu/timeline` / category=technical / priority=low | `app/gateway/tool_registry.py:200-271` |
| `_SENTIMENT_TOOLS` 整块被注释（旧设想：xq_comments / futu_comments / ths_comments） | `app/gateway/tool_registry.py:587-597` |
| `ALL_TOOLS` 拼装里写着 `# + _SENTIMENT_TOOLS  # P4-5：评论 MCP 就绪后取消注释` | `app/gateway/tool_registry.py:631-644` |
| 注册表 docstring 第 9 行写「所有 44 个 Market Gateway Tool」，有契约测试守护 | `app/gateway/tool_registry.py:9`、`tests/test_docs_contract.py` |
| INTERNAL 分发：`_do_execute` 先 `resolve_tool_by_name` 再判 `http_method == "INTERNAL"` → `_execute_internal`；**内部工具必须在会话分支前 return，只用内部工具的 analyst 不得触发建连** | `app/graph/tool_runtime.py:313-330` |
| 内部工具范本（带缓存的）＝ `news_search`（TTL `news_search_ttl_seconds`）；SEC 范本＝`internal_us_fundamentals`（TTL 12h）/`internal_us_filings_recent`（TTL 3600）；模块级归一化辅助 `_normalize_us_fundamentals` / `_normalize_us_filings` / `_normalize_hk_entries` | `app/graph/tool_runtime.py` |
| 内部工具缺配置时的既有范式：`_SEC_EDGAR_CONTACT_MISSING = "SEC_EDGAR_CONTACT 未配置（SEC 公平访问政策要求声明访问身份，见 .env.example）"` | `app/graph/tool_runtime.py` |
| `truncate()` 保留**末尾** 200 条并置 partial | `app/graph/tool_runtime.py:260-283` |
| 缓存 TTL 表：`"public_sentiment_ashare_master_sentiment_get": SENTIMENT_TTL`、`"longhu_xueqiu_longhu_get": LONGHU_TTL` | `app/cache.py:181`、`:186` |
| normalizer 时间戳键 `_TIMESTAMP_KEYS = {"timestamp","time","date","datetime","ts"}`（**不含 `published_at`**）；元数据键 `_METADATA_KEYS = {"partial","status","note","source","source_used"}`；容器键 `_CONTAINER_KEYS` 含 `items`；`_LIST_CAP = 50`；`_SERIES_TOOL_HINTS`（`normalizer.py:152-159`）刻意**不含** timeline（新→旧流头部才是最新） | `app/gateway/normalizer.py:141-161` |
| 实测：`count=5` 的讨论流 → **55 条 datum**；`count=20`（实收 10 条）→ **105 条 datum** | 本会话实跑 `normalize_tool_result` |
| critic `_DOMAIN_RULES` 有 a_share/crypto/us_stock 三套；a_share 第一条即「如果报告声称市场情绪悲观/乐观，必须有 sentiment / limit_up_count 等数据支撑」 | `app/graph/nodes/critic.py:43-62` |
| SSE 节点标签 `"sentiment": ("tool_call", "舆情分析员采集中")` **已备好** | `app/main.py:248` |
| docs 已写：`docs/tools.md:10` `| sentiment | public_sentiment_ashare_master_sentiment_get | 情绪 |`；`docs/architecture.md:63`「可选节点：news / sentiment analyst 由配置开关控制」；`:128` `| sentiment | tool_call | 舆情分析员采集中 |`；README.md:44 同口径 | — |
| **必须改写的现有测试**：`test_sentiment_enabled_warns_and_does_not_crash_graph`（断言 `"sentiment node is not implemented" in caplog.text` 且 sentiment 不在 nodes 里）、`test_sentiment_category_not_present`（断言 `"sentiment" not in by_category`）、`test_us_stock_domain.py:103`（断言 `"sentiment"` 等不在 us 域）、`test_cache_multi_domain.py:23`（`_resolve_ttl("public_sentiment_ashare_master_sentiment_get") == SENTIMENT_TTL`） | `tests/test_graph_topology.py:319-329`、`tests/test_tool_registry.py:25-26`、`tests/test_us_stock_domain.py:103`、`tests/test_cache_multi_domain.py:23` |
| 其他引用 sentiment 的测试：`test_graph_nodes.py`、`test_graph_routing.py`、`test_gateway_reuse.py`、`test_evidence_gate.py`、`test_normalizer_multi_domain.py:14`、`test_normalizer_enhanced.py`、`test_tool_registry_multi_domain.py`、`test_reasoning_parsing.py`、`test_stream_endpoint.py`、`test_docs_contract.py:45`、`test_data_integrity.py:394`、`test_evidence_id.py`、`test_evidence.py:11-18` | `tests/` |
| HTTP 模式 = `MARKET_GATEWAY_MODE=http` + `X-API-Key`；MCP 模式 = `MCP_ARGS="plugin serve market-gateway"`（iiix ≥ 0.8.0，本机 0.8.4 已 `plugin verify` 通过） | `.env`、`app/config.py:18-23` |

### 1.6 当前基线

**双模式各 575 passed + 0 skipped，ruff check + format 全绿**（2026-10-06 主 Agent 亲测，含本工作区的 MCP 语法修正）。

---

## 2. Phase A：实施

### A0. 探针闸门 —— `scripts/verify_sentiment.py`（必须最先做）

风格仿 `scripts/verify_us_market.py`（143 行：`load_env()` 极简 .env 解析 + `probe_endpoint() -> (ok, note)` + 可用性表 + 闸门判定）与 `scripts/verify_xueqiu_timeline.py`（本工作区已有的一次性探针）。

**实测清单**（用 `MARKET_GATEWAY_HTTP_URL` + `X-API-Key` 直连，不要走应用层）：

1. `/market/discussions`：`SH600519` / `SZ000001` / `00700` / `AAPL` 各测，记录 items 数、`category` 值、`max_page`。
2. `feed` 全枚举：`all|discussion|trade|news|announcement|research|ir`，对 `SH600519` 各测一次，**记录每个 feed 的 items 数**（确认 `discussion` 是否最优、`all` 是否更全）。
3. 分页边界：`page` 1 / 5 / 20 / 21（21 应 422）、`count` 10 / 20 / 21（21 应 422）；确认「count=20 实收 10」是否稳定。
4. 时间跨度：翻满 20 页，记录**首条与末条的 `published_at`**，算「200 条能覆盖多久」——**这个数字决定 `sentiment_max_comments` 的默认值**。
5. `list_stock_post_comments`：取第 1 条帖子的 `url`，测 `expand_replies=false` / `true`，记录**响应字段名**、评论条数、`max_pages=1/5` 差异、耗时。
6. `get_ashare_sentiment`：记录响应体大小、**顶层结构**（`series` 下有哪些 key、每个 key 的数组长度）、最新值。
7. 涨停池 3 条：`get_limit_up_count` / `list_limit_up_sectors` / `list_limit_up_stocks`，记录条数与字段。
8. 域对照：对 `00700` / `AAPL` 测 `/market/discussions` 的 `discussion` feed，**记录是否真的无数据**（若港股/美股也有数据，交付报告里要如实推翻 §0 的排除结论）。

**闸门判定**：
- **通过**（`/market/discussions` 对 A 股返回真实讨论 + `post-comments` 可下钻 + `sentiment` 指数结构可解析）→ 继续 A1。
- **部分通过**（如 `post-comments` 不可用 / `feed=discussion` 无数据但 `all` 有）→ 按实测收缩能力，purpose 如实描述，交付报告注明。
- **失败**（讨论流不可达或返回非讨论数据）→ **停止 Phase A**，不提交实现代码，探针脚本可选留档，报告交回主 Agent。

### A1. 注册表改名 + 情绪面条目（`app/gateway/tool_registry.py`）

**只动情绪面相关条目，其余 ~30 条留给迁移计划。**

1. **`_ASHARE_MASTERTOOLS`（76-117 行）4 条全部改名**，并**把 sentiment 那条移出**：

```python
# 迁移到 3.2.1 operationId；sentiment 条目移到 _SENTIMENT_TOOLS
ToolMeta("limit_up_count", "get_limit_up_count", "A股当日涨停家数（活跃指标）",
         domain="a_share", priority="high", http_method="GET",
         http_path="/ashare-master/limit-up/count", category="technical"),
ToolMeta("limit_up_sectors", "list_limit_up_sectors", "A股当日涨停题材与板块分布（题材强度）",
         domain="a_share", priority="high", http_method="GET",
         http_path="/ashare-master/limit-up/sectors", category="technical"),
ToolMeta("limit_up_pool", "list_limit_up_stocks", "A股当日涨停股票明细（深入调查用）",
         domain="a_share", priority="medium", http_method="GET",
         http_path="/ashare-master/limit-up/pool", category="technical"),
```

> ⚠️ **`key` 保持不变**（`limit_up_count` / `limit_up_sectors` / `limit_up_pool`）—— Supervisor 的 `plan.steps` 按 `key` 引用，改 key 会波及提示词与既有测试；**只改 `tool_name`**。

2. **`key=timeline` 条目改名 + 改路径 + 改 category 为 `sentiment`**（原 200-271 行的 `_ASHARE_MICRO` 内）：

```python
ToolMeta(
    "discussions",
    "list_stock_discussions",
    "个股雪球讨论流（feed=discussion 为讨论帖；参数：symbol 必填如 SH600519，page 1-20，count 1-20 但每页实收 10 条）",
    domain="a_share",
    priority="high",
    http_method="GET",
    http_path="/market/discussions",
    category="sentiment",
),
```

**注意**：这里 `key` 从 `timeline` **改成 `discussions`** —— 与上面不同，因为「分时/时间线数据」的旧语义已不成立（旧 purpose 是错的），且新 key 更贴真实语义。**必须同步 grep `"timeline"` 字符串**（提示词、测试、文档里可能有引用），逐处处理。

3. **启用 `_SENTIMENT_TOOLS`**（替换 587-597 行的整块注释）：

```python
_SENTIMENT_TOOLS = [
    ToolMeta(
        "discussions",          # ← 从上一步的 _ASHARE_MICRO 移到这里
        "list_stock_discussions",
        "个股雪球讨论流（feed=discussion；symbol 必填如 SH600519，page 1-20）",
        domain="a_share", priority="high", http_method="GET",
        http_path="/market/discussions", category="sentiment",
    ),
    ToolMeta(
        "post_comments",
        "list_stock_post_comments",
        "帖子评论区下钻（post_url 必填，取自讨论流 items[].url，原样传入；expand_replies 取楼中楼）",
        domain="a_share", priority="medium", http_method="GET",
        http_path="/market/post-comments", category="sentiment",
    ),
    ToolMeta(
        "sentiment_index",
        "internal_sentiment_index",
        "A股情绪指数聚合（QX/QL 最新值 + 窗口统计 + 趋势），内部聚合、非原始序列",
        domain="a_share", priority="high", http_method="INTERNAL", http_path="",
        category="sentiment",
    ),
    ToolMeta(
        "internal_discussions",
        "internal_stock_discussions",
        "个股讨论流聚合（自动翻页 + 去重 + 按热度排序 + 文本截断；参数：symbol 必填，pages 1-20，top_k）",
        domain="a_share", priority="high", http_method="INTERNAL", http_path="",
        category="sentiment",
    ),
    ToolMeta(
        "internal_post_comments",
        "internal_post_comments",
        "帖子评论聚合（post_url 必填；参数：max_pages 1-20，expand_replies）",
        domain="a_share", priority="medium", http_method="INTERNAL", http_path="",
        category="sentiment",
    ),
    ToolMeta(
        "ashare_sentiment_raw",
        "get_ashare_sentiment",
        "A股原始情绪时间序列（0-100 市场情绪时序；返回序列极长，仅诊断/兜底用）",
        domain="a_share", priority="low", http_method="GET",
        http_path="/ashare-master/sentiment", category="sentiment",
    ),
]
```

4. **`ALL_TOOLS` 拼装**：把 `# + _SENTIMENT_TOOLS  # P4-5：评论 MCP 就绪后取消注释`（631-644 行）改为真正 `+ _SENTIMENT_TOOLS`。
5. **更新注册表 docstring 第 9 行的工具总数**（契约测试守护 `tests/test_docs_contract.py`）。
6. **`BY_NAME` 冲突检查**：`search_stocks` 等旧名条目仍在（迁移计划的范围），本次新增 `list_stock_discussions` / `list_stock_post_comments` / `get_ashare_sentiment` + 3 个 INTERNAL 名，**与既有名零冲突**；但 `discussions` 这个 key 若同时留在 `_ASHARE_MICRO` 就会 `BY_KEY` 冲突 —— **务必只注册一次**。

### A2. 讨论流/评论聚合内部工具（`app/graph/tool_runtime.py`）

新增 3 个 INTERNAL 分发分支（`_execute_internal` 里按 `tool_name` 硬编码 if，紧跟 `news_search` / SEC 分支之后）。**都必须 `return` 在会话分支之前**，只用内部工具的 analyst 不得触发 Gateway 建连。

#### A2.1 `internal_sentiment_index`

```
入参: 无
逻辑: ① 调 Gateway `get_ashare_sentiment`（无参）
      ② 拿 raw，自己聚合 → ≤5 条 datum:
         - sentiment_qx_latest   最新 QX 值 + timestamp
         - sentiment_ql_latest   最新 QL 值 + timestamp
         - sentiment_qx_stats    窗口内 min/max/mean/std（窗口由 window 参数定，默认全序列）
         - sentiment_trend       最近 N 点相对前 N 点的方向（up/down/flat + 变化幅度）
         - _meta                 {source, series_keys, series_len, source_url, fetched_at}
出参: ToolResult(status=success, normalized=[...], note=...)
TTL:  3600（情绪指数日频，1h 缓存足够）
```

**硬要求**：
- **绝不允许**把原始 `series` 整体塞进 `normalized`（这是本工具的**唯一存在理由**）。
- 若 `series` 结构与预期不符（A0 实测为准），**返回 `status=error` + 明确文案**，不得返回空 datum（`normalizer` 的不变式是「0 条 datum 判 error」，此处保持同样纪律）。
- 数值必须带 `timestamp`（normalizer 的 `_TIMESTAMP_KEYS` 不含业务字段，**在 datum 里显式给 `timestamp`**）。

#### A2.2 `internal_stock_discussions`

```
入参: symbol(str, 必填) / pages(int, 默认 3, 1..20) / top_k(int, 默认 10)
       / feed(str, 默认 "discussion") / max_text_chars(int, 默认 800)
逻辑: ① 逐页调 `list_stock_discussions`（page=1..pages, count=20）
      ② 按 `id` 去重（跨页无重叠已实测，仍要去重防御）
      ③ 过滤 text 为空/仅空白的条目
      ④ **排序（用户已裁定：方案 1）**：
         key = (-(like_count or 0), -(reply_count or 0), -(repost_count or 0), -(published_at or 0))
         并列时 published_at 新的优先
      ⑤ 截断 text 到 max_text_chars（尾部加 "…"），**必须先截断再进 datum**
      ⑥ 产出 datum：
         - discussions_meta      {symbol, pages_fetched, total_before_dedup, kept, window_start, window_end, sort_rule}
         - discussion_top_{i}    i=1..top_k，value = {id,title,author,author_id,published_at,like_count,reply_count,repost_count,url,text}
         - discussions_stats     {like_total, like_nonzero, reply_nonzero, post_count, span_hours}
出参: ToolResult(status=success|partial, normalized=[...])
TTL:  900（讨论流变化快，15min）
partial 条件: 任何一页失败 → status=partial + note 写明第几页失败、成功几页
```

**硬要求**：
- `pages=1`（默认 3 页 = 30 条）时也要能工作。
- **必须**把 `published_at`（毫秒 epoch）当时间戳处理 —— 不要在 datum 里留裸数字而不解释单位。
- 排序规则写进 `discussions_meta.sort_rule`，让 Reasoning 与 Critic 看得见依据。
- **不要**把 200 条全部产出（top_k 默认 10 是有意的收敛）。

#### A2.3 `internal_post_comments`

```
入参: post_url(str, 必填, ≤256) / max_pages(int, 默认 1, 1..20) / expand_replies(bool, 默认 false)
       / top_k(int, 默认 20) / max_text_chars(int, 默认 500)
逻辑: ① 调 `list_stock_post_comments`
      ② 聚合：按 like_count 排序取 top_k，截断 text
      ③ 产出 datum：comments_meta {post_url,max_pages,expand_replies,total,kept}
                     comment_{i} {id?,author,text,like_count,reply_count,published_at}
                     comments_stats {count, like_total, author_uniq}
TTL:  900
```

> A0 实测的字段名以实际为准（§1.3 只是 schema，**响应字段名未实测**）。若响应没有 `like_count`，改为按时间排序并在 `comments_meta` 里写明。

#### A2.4 共用辅助（模块级纯函数，便于直测）

```python
def _sort_discussions(items: list[dict]) -> list[dict]: ...
def _dedup_by_id(items: list[dict]) -> list[dict]: ...
def _clip(text: str | None, limit: int) -> str: ...
def _aggregate_sentiment_series(series: dict, window: int | None) -> list[dict]: ...
```

**这些纯函数必须有单测**（参考 `tests/test_hk_northbound.py` 只测纯函数、不真连网的模式）。

### A3. 分析员节点（`app/graph/nodes/analysts/sentiment.py` 新建）

```python
class SentimentAnalystNode(MarketAnalystNode):
    """舆情/情绪分析员。

    只消费 category=sentiment 的工具；工具来自 supervisor 的 route
    （见 base.py:181-186），本节点不自行发现工具。
    """
    category = "sentiment"
```

**要点**：
- 主体逻辑全部继承 `MarketAnalystNode`（预算守卫、去重、Evidence 构建、异常兜底），**不要覆写 `_execute_tools`**。
- `_make_digest` 可覆写以产出更有信息量的摘要（如「情绪指数 41（下行）、讨论 30 条/18 条有赞、top1 帖：…」），但**保持 ≤200 字**（`base.py:298` 的既有约束）。
- **不新增 LLM 调用**（本计划用规则聚合）。若实施者认为必须 LLM 打分，**停下来问主 Agent**（涉成本与新增失败点）。

### A4. `WHITELIST_NO_SYMBOL`（`app/graph/nodes/analysts/base.py:47-68`）

去掉已废弃的 `public_sentiment_ashare_master_sentiment_get` / 旧 limit_up 三条（改名后旧名永不匹配），加入：

```
"get_ashare_sentiment",
"get_limit_up_count",
"list_limit_up_sectors",
"list_limit_up_stocks",
"internal_sentiment_index",
```

**不加** `list_stock_discussions` / `list_stock_post_comments` —— 它们**必须**有 symbol / post_url，走 symbol 守卫（`base.py:241-247`）才对。`internal_stock_discussions` **也不加**（comment 里写它 symbol 必填），但注意：`base.py:241` 的守卫是**按 `tool_name` 查表**，`internal_stock_discussions` 不在表里 → planner 不给 symbol 时会用问题里的 6 位代码补齐（这正是想要的）。

### A5. 配置（`app/config.py` + `.env.example`）

```python
#: 舆情分析员总开关（评论/讨论源已就绪）
sentiment_enabled: bool = False      # 保持默认 False；文档与 .env.example 说明如何开启

#: 单次讨论流聚合的默认翻页数与保留条数（替代旧 sentiment_max_comments 的"评论上限"语义）
sentiment_default_pages: int = 3
sentiment_top_k: int = 10
#: 单条讨论/评论正文进 datum 的最大字符数（实测 text 最长 6027 字，必须截断）
sentiment_max_text_chars: int = 800
#: 讨论流/评论缓存 TTL（秒）
sentiment_cache_ttl_seconds: int = 900
```

**`sentiment_max_comments: int = 500` 的处理**（需实施者二选一并说明）：
- (a) **删除**该字段 —— 新语义由 `sentiment_default_pages` × 10 + `sentiment_top_k` 表达；grep 确认无其他引用（现在 grep 应只在 `config.py` 出现）。
- (b) 保留并改语义为「一次调查内讨论+评论的 datum 总上限」，在 `ToolRuntime.execute` 侧做硬截断。

**推荐 (a)**（更少歧义）。若选 (b) 必须补测试。

### A6. 图接线（4 处，缺一处就建图失败或路由不到）

1. `app/graph/builder.py:105-108`：**删除** sentiment 的「not implemented」warning 分支。
2. `app/graph/builder.py:119-128`：按 `settings.sentiment_enabled` 注册 `builder.add_node("sentiment", SentimentAnalystNode(settings))`。
3. `app/graph/builder.py:142` 默认 fanout **不动**（默认仍返回 technical/fundamental/moneyflow；sentiment 由 supervisor 显式路由）。
4. `app/graph/builder.py:145-151` `_fanout_map` 加 `"sentiment"` 映射。
5. `app/graph/nodes/supervisor.py:25-37` `route_candidate_categories(settings)`：`sentiment_enabled` 为真时追加 `"sentiment"`。
   **注释必须同步更新**（原文「Sentiment 节点尚未实现…否则条件边会指向未知节点并导致建图失败」已过时）。
6. **域约束（重要）**：情绪工具全是 `domain="a_share"`。`sentiment` 只对 A 股问题有意义 —— 在 supervisor prompt（`app/agent/prompts_graph.py`）里写明「sentiment 只适用于 A 股/沪深京标的；港股/美股问题不要分配 sentiment」。**不要**在 `route_candidate_categories` 里做域判断（那是 planner 的职责，函数签名里没有 domain）。

### A7. Critic 规则（`app/graph/nodes/critic.py:43-62`）

`_DOMAIN_RULES["a_share"]` 第一条已是「声称情绪悲观/乐观必须有 sentiment / limit_up_count 数据支撑」—— **微调为**明确列出情绪面的可用证据名：

> 「如果报告声称市场情绪（悲观/乐观/亢奋/恐慌/分歧），必须有 `sentiment_index`（QX/QL 指标）或 `discussions`（讨论流热度与倾向）或 `limit_up_count` 的数据支撑；只有新闻标题或讨论帖数量时不构成情绪结论。讨论流采样有限（≤200 条、时间窗数小时），**不得**把它当作全市场情绪的代表。」

另加一条**采样免责**规则（讨论流是样本，不是总体）。

### A8. 测试

**必须改写的现有测试**：
| 文件:行 | 现状 | 改成 |
|---|---|---|
| `tests/test_graph_topology.py:319-329` | `test_sentiment_enabled_warns_and_does_not_crash_graph`：断言 warning 文案 + sentiment 不在 nodes | 拆成两个：`sentiment_enabled=False` → 无 sentiment 节点、无 warning；`=True` → 有 sentiment 节点、图可编译、fanout/条件边正常 |
| `tests/test_tool_registry.py:25-26` | `test_sentiment_category_not_present`：断言 `"sentiment" not in by_category` | 反转：断言 `set(by_category["sentiment"])` 恰为 6 条且全是 a_share |
| `tests/test_cache_multi_domain.py:23` | `_resolve_ttl("public_sentiment_ashare_master_sentiment_get") == SENTIMENT_TTL` | 改为新 operationId（或新内部工具名） |
| `tests/test_us_stock_domain.py:103` | 断言 `sentiment` 等 key 不在 us 域 | 保持（情绪工具确实全是 a_share），但要确认新 key（`discussions`/`post_comments`/`sentiment_index`）也不在 us 域 |

**必须新增**（`tests/test_sentiment_pipeline.py`）：
1. 注册表：情绪面 6 条存在、全 `domain=="a_share"`、`category=="sentiment"`；`BY_NAME` 无重复；`registry_text(domains=["a_share"])` 含 `discussions`；`registry_text(domains=["us_stock"])` **不含**任何情绪 key。
2. 纯函数：`_dedup_by_id` / `_clip` / `_sort_discussions`（**并列规则必须测**：likes 相同按 reply 排；再相同按 published_at 新的优先）/ `_aggregate_sentiment_series`（正常 + 结构异常 → error）。
3. 聚合工具：monkeypatch Gateway 返回假 payload → `internal_stock_discussions` datum 数 ≤ top_k+2、`internal_sentiment_index` datum 数 ≤ 5、`internal_post_comments` 结构正确；**一页失败 → status=partial**。
4. 图：`sentiment_enabled=True` 时 `route_candidate_categories` 含 sentiment；Supervisor 为 A 股情绪问题能路由到 sentiment（可用假 planner 输出）。
5. 域隔离：A 股问题可路由 sentiment；`question="苹果股价"` 的注册表文本里无情绪工具。
6. 回归：`sentiment_enabled=False` 时行为与今日**逐字节一致**（无 sentiment 节点、无 warning、fanout 不变）。

**测试纪律**（沿用既有惯例）：不得依赖真实网络/真实 LLM；注意 market_cache 全局污染（新文件 fixture 自洽隔离）；monkeypatch 打在模块全局上。

### A9. 文档真值同步

| 文件 | 改什么 |
|---|---|
| `docs/tools.md:10` | 情绪面从 1 条变 6 条，工具名列新名；表头行数同步 |
| `docs/architecture.md:52` | `SentimentAnalyst` 一行：从「评论爬取 + LLM 打分，依赖评论 MCP」改为「个股雪球讨论流 + A股情绪指数聚合，规则聚合，无 LLM」 |
| `docs/architecture.md:63` | 「可选节点：news / sentiment analyst 由配置开关控制」保留，但补「sentiment 仅 A 股」 |
| `docs/architecture.md:128` | SSE 标签行保持（`app/main.py:248` 已一致） |
| README.md:44 | 同口径 |
| README.md 工具表（170 行附近） | A 股市场生态一行数字与描述；总工具数 **44 → 49**（净增 5：`post_comments`、`sentiment_index`、`internal_sentiment_index`、`internal_stock_discussions`、`internal_post_comments`；`discussions` 由 `timeline` 改名而来，不计数）；**注明 3.2.1 改名迁移尚未完成**（诚实标注） |
| `app/gateway/tool_registry.py:9` | docstring 工具总数 |

---

## 3. Phase B：验证与提交

> **⚠️ 2026-10-06 实测的通道现状（务必先读）**：
> - **HTTP 模式（`MARKET_GATEWAY_MODE=http` + `MARKET_GATEWAY_API_KEY`）是当前唯一能真正执行工具调用的通道**，本次所有真值（讨论流、sentiment 指数、涨停池）都由它取得。
> - **MCP 模式当前只能握手、不能执行调用**：`iiix plugin serve market-gateway` 能 `initialize()` + `list_tools()`（40 个工具），但任何真实 tool call 返回
>   `{"code":"gateway_error","message":"刷新 OAuth Token: ... Post \"https://logto.x.iiix.dev/oidc/token\": context deadline exceeded"}`
>   ；同一时刻 `iiix plugin verify market-gateway` 也在 `client_auth_ms` 后失败（有时曾通过）。**本机 Python 能 0.8s 打通该 URL**，属 iiix CLI（Go）侧的网络/代理路径问题，与本仓库代码无关。
> - 因此 B3 的 e2e **在 HTTP 模式下跑**；mcp 模式只验「握手 + list_tools + 通道自检」，**不要把 MCP 真实 tool call 设为验收门槛**（否则会被环境卡住）。这是**已知边界**，要写进交付报告。

1. 全量双模式：`.venv/Scripts/python.exe -m pytest . -q`，分别在 `MARKET_GATEWAY_MODE=mcp` 与 `=http` 下跑，记录数字（基线 **575**）。
2. `ruff check . --no-cache` + `ruff format --check . --no-cache` 全绿。
3. **真链路 e2e**（不许只看 mock 单测）：HTTP 模式下真调 `internal_sentiment_index` / `internal_stock_discussions` / `internal_post_comments` 各一次，记录 status + datum 数 + 耗时；**datum 数必须在上限内**。
4. `git commit` 按 Phase 分提交，中文 + `feat:`/`test:`/`docs:` 前缀（对齐 git log 风格）。**不要 push**。
5. 在本文档追加「执行记录」小节（对齐 `SEC_EDGAR_PLAN.md` §7 格式）：A0 实测表、实施摘要、与计划真值的偏差、测试数字。

---

## 4. 停止条件（遇到就停，回报主 Agent，不要自行改方案）

1. A0 闸门失败（讨论流不可达或结构无法解析）。
2. `list_stock_post_comments` 响应结构与 §1.3 schema 差异大到无法写解析（如没有稳定主键）。
3. 改名后发现 `BY_KEY` / `BY_NAME` 与既有条目**真实冲突**且无法用 `key` 区分。
4. 需要改 `normalizer.py` 的全局契约（`_CONTAINER_KEYS` / `_LIST_CAP` / `truncate()` 语义）才能完成 —— 这是方案级变更，回用户。
5. 需要引入新依赖 / 新 LLM 调用点 / 浏览器抓取。
6. 基线本身有红（576 个测试里出现失败）。

---

## 5. 已知边界（写进交付报告与文档，不要隐瞒）

| 边界 | 事实 |
|---|---|
| 无实时讨论快照 | `/market/discussions` 是分页信息流，不是实时推送；时间窗实测约 6.7 条/小时 |
| 采样有限 | 单 symbol 最多 20 页 × 10 条 = **200 条**；`count` 上限 20 但每页实收 10 条 |
| `max_page` 字段不可信 | 响应写 100，实际 >20 报 422 |
| 互动数据稀疏 | 60% 有赞、17% 有回复、3.3% 有转发 → 热度排序信号弱，必须写明排序规则 |
| 情绪面仅 A 股 | 港股/美股无可用情绪源 |
| `get_ashare_sentiment` 原始序列极长 | 26,447 条 datum，必须用 `internal_sentiment_index` 聚合后再用 |
| 改名迁移未完成 | 注册表里约 30 个工具仍是旧 operationId，**线上调用会失败**（见 `MARKET_GATEWAY_321_MIGRATION.md`） |
| 上游筹码数据域为空 | `/ashare/chips/*`（8 条，含股东/减持/解禁/股本）已在 3.2.1 暴露且参数结构正确，但实测 5 个标的 `coverage_status=not_collected`、`record=null`，采集侧未入库 → 本轮不注册。**情绪面可用源仅：讨论流 + 情绪指数 + 涨停池/板块 + `news_search`** |

---

## 6. 待主 Agent/用户确认的开放问题

1. `sentiment_max_comments` 删除还是改语义（§2-A5 推荐删除）。
2. `internal_*` 三个工具的 TTL 取值（本计划拟 `sentiment_index` 3600、讨论/评论 900）。
3. 是否需要在 supervisor prompt 里显式列「情绪面问题示例」（如「大家怎么看」「市场情绪怎么样」）以提升路由命中率。

---

## 7. 执行记录

（待子 Agent 落地后按 `SEC_EDGAR_PLAN.md` §7 格式追加）
