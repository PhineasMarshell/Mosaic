# Market Gateway 3.2.1 改名迁移 + MCP 通道自检执行计划

> 本文由主 Agent 基于**实测真值**编写（2026-10-06）。
> **`SENTIMENT_PLAN.md` 与本文是两件独立的事**：情绪面接入只改情绪相关条目（6 条），本文负责**其余全部工具的 operationId 迁移**与**通道健壮性**。
> 两文可以并行执行，但**同一个文件（`app/gateway/tool_registry.py`）会有交叉**，建议串行：先做情绪面，再做本文；或反之。**不要同时开工。**
> 执行纪律沿用 `US_STOCK_INTEGRATION_PLAN.md` §1.5。

---

## 0. 背景：这是一次真实事故，不是理论风险

2026-10-06 验收 8 个未 push commit 时发现：**Mosaic 在默认配置（`MARKET_GATEWAY_MODE=mcp`）下，全部 Gateway 工具 100% 失败**，而 575 个测试全绿（mock 掩盖了链路层）。

两个独立根因：

| # | 根因 | 证据 |
|---|---|---|
| 1 | `.env` 指向 iiix **0.5.0** + 旧命令 `mcp serve market-gateway`；**`iiix mcp` 子命令在 0.8.0 已被删除**，改用 `iiix plugin serve <project-code>` | 0.8.4 实跑 `iiix mcp serve market-gateway` → `iiix: iiix mcp 已在 0.8.0 删除；请改用 iiix plugin`；Mosaic 侧 `MCPConnectionError: MCP initialize (handshake) failed: Connection closed` |
| 2 | 上游 market-gateway 升到 **3.2.1** 后**全部 operationId 改名**，Mosaic 注册表里 40 个旧 operationId 全部失效 | 新旧 OpenAPI 对比：43 条路径中 operationId **完全不变的有 0 条**；`ToolRuntime.execute("get_market_klines")` → `KeyError: Tool is not allowed by registry` |

**根因 1 已在主 Agent 工作区修复**（`app/config.py:20-23` 默认值、`.env`、`.env.example`、README、architecture.md），并已实测通过：

```
$env:MCP_ARGS="plugin serve market-gateway"  +  0.8.4 二进制
→ list_tools OK: 40 个工具 (44ms)
→ iiix plugin verify market-gateway → {"status":"passed", ...}
```

**根因 2 就是本文的主体工作。**

---

## 1. 关键真值事实（已核实）

### 1.1 通道自检命令

| 命令 | 用途 | 实测输出（正常态） |
|---|---|---|
| `iiix whoami` | 登录态 | `{"email":"…","display_name":"…","system_role":"MEMBER","status":"ACTIVE","user_id":"…"}` |
| `iiix plugin status` | 本地已装插件（**读本地 manifest，不走网络**） | `[{"project_code":"market-gateway","current":{"version":"3.2.1","sha256":"a5ed5cdd…","installed_at":"2026-10-06T09:59:23Z"}}]` |
| `iiix plugin verify market-gateway` | **登录态 + 上游连通性一起验**（推荐自检命令） | `{"status":"passed","timing":{"client_auth_ms":5090,"gateway_upstream_ms":183.9,…}}` |
| `iiix plugin info market-gateway` | 插件详情（需登录） | — |

**失败态的实际文案**（写进错误提示时用原文）：

| 场景 | 文案 |
|---|---|
| 登录失效（网络可达） | `iiix: 登录已失效，请重新执行 iiix login` |
| 登录失效（读凭据失败） | `iiix: 读取当前账号时取得调用凭据: 登录已失效，请重新执行 iiix login` |
| OAuth 发现文档不可达 | `iiix: 读取当前账号时取得调用凭据: 读取 OAuth 发现文档: Get "https://logto.x.iiix.dev/oidc/.well-known/openid-configuration": context deadline exceeded (Client.Timeout exceeded while awaiting headers)` |
| 用旧命令 | `iiix: MCP 已停用: 服务器目录已移除或当前账号无权使用` |
| 工具调用时登录失效 | `{"code":"gateway_error","message":"登录已失效，请重新执行 iiix login"}`（**参数校验先于鉴权**，所以参数错误仍会先返回 422） |

### 1.2 完整新旧 operationId 映射表（40 条）

> 「路径」列：**未变** = 路径没动，只改名；否则标注新路径。
> 「处置」列：改名 / 改名+改路径 / **删除（端点消失）** / 保持。

| # | 现注册表 `tool_name`（旧） | 3.2.1 新 `tool_name` | 路径 | 处置 |
|---|---|---|---|---|
| 1 | `public_sentiment_ashare_master_sentiment_get` | `get_ashare_sentiment` | 未变 | 改名（**情绪面计划已处理**） |
| 2 | `public_limit_up_count_ashare_master_limit_up_count_get` | `get_limit_up_count` | 未变 | 改名（同上） |
| 3 | `public_limit_up_sectors_ashare_master_limit_up_sectors_get` | `list_limit_up_sectors` | 未变 | 改名（同上） |
| 4 | `public_limit_up_pool_ashare_master_limit_up_pool_get` | `list_limit_up_stocks` | 未变 | 改名（同上） |
| 5 | `overview_eastmoney_overview_get` | `get_company_overview` | 未变 | 改名 |
| 6 | `detail_eastmoney_detail_get` | `get_company_detail` | 未变 | 改名 |
| 7 | `business_eastmoney_f10_business_get` | `get_company_business` | 未变 | 改名 |
| 8 | `concept_eastmoney_f10_concept_get` | `get_company_concepts` | 未变 | 改名 |
| 9 | `finance_eastmoney_f10_finance_get` | `get_company_finance` | 未变 | 改名 |
| 10 | `shareholders_eastmoney_f10_shareholders_get` | `get_company_shareholders` | 未变 | 改名 |
| 11 | `survey_eastmoney_f10_survey_get` | `get_company_survey` | 未变 | 改名 |
| 12 | `quote_tencent_quote_get`（A 股 `quote`） | `get_market_quotes` | `/tencent/quote` → **`/market/quotes`** | 改名 + 改路径 |
| 13 | `longhu_xueqiu_longhu_get` | `get_stock_longhu` | `/xueqiu/longhu` → **`/market/longhu`** | 改名 + 改路径 |
| 14 | `abnormal_reasons_xueqiu_abnormal_reasons_get` | `get_stock_abnormal_reasons` | `/xueqiu/abnormal-reasons` → **`/market/abnormal-reasons`** | 改名 + 改路径 |
| 15 | `orderbook_xueqiu_orderbook_get` | `get_market_orderbook` | `/xueqiu/orderbook` → **`/market/orderbook`** | 改名 + 改路径 |
| 16 | `trades_xueqiu_trades_get` | `list_market_trades` | `/xueqiu/trades` → **`/market/trades`** | 改名 + 改路径 |
| 17 | `timeline_xueqiu_timeline_get` | `list_stock_discussions` | `/xueqiu/timeline` → **`/market/discussions`** | 改名 + 改路径（**情绪面计划已处理**） |
| 18 | `search_xueqiu_search_get`（A 股 `search`） | `search_stocks` | `/xueqiu/search` → **`/market/search`** | 改名 + 改路径 |
| 19 | `quote_tencent_quote_get`（港股 `hk_quote`） | `get_market_quotes` | 同 #12 | 改名 + 改路径 |
| 20 | `search_xueqiu_search_get`（港股 `hk_search`） | `search_stocks` | 同 #18 | 改名 + 改路径 |
| 21 | `internal_hk_northbound` | — | — | **保持不动**（内部工具） |
| 22 | `internal_hk_index` | — | — | **保持不动** |
| 23 | `klines_market_klines_post`（`commodity_gold` / `klines` / `us_klines` 共 3 条同名） | `get_market_klines` | 未变 | 改名（**3 条一起改**） |
| 24 | `snapshot_market_snapshot_post`（`commodity_silver` / `commodity_platinum` / `snapshot` 共 3 条） | `get_market_snapshot` | 未变 | 改名（**3 条一起改**） |
| 25 | `window_market_window_post`（`window` / `us_window`） | `get_market_window` | 未变 | 改名（**2 条一起改**） |
| 26 | `exchanges_market_exchanges_get` | `list_exchanges` | 未变 | 改名 |
| 27 | `derivatives_history_market_derivatives_history_post` | `get_derivatives_history` | 未变 | 改名 |
| 28 | `hyperliquid_symbols_coinglass_hyperliquid_symbols_get` | `list_hyperliquid_symbols` | 未变 | 改名 |
| 29 | `hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get` | `get_hyperliquid_liquidation_map` | 未变 | 改名 |
| 30 | `hyperliquid_top_position_coinglass_hyperliquid_top_position_get` | `get_hyperliquid_top_position` | 未变 | 改名 |
| 31 | `hyperliquid_user_count_coinglass_hyperliquid_user_count_get` | `get_hyperliquid_user_count` | 未变 | 改名 |
| 32 | `hyperliquid_vaults_coinglass_hyperliquid_vaults_get` | `list_hyperliquid_vaults` | 未变 | 改名 |
| 33 | `liquidation_today_coinglass_liquidation_today_get` | `get_crypto_liquidation_today` | 未变 | 改名 |
| 34 | `funding_rate_coinglass_funding_rate_get` | `list_crypto_funding_rates` | 未变 | 改名 |
| 35 | `internal_us_fundamentals` | — | — | **保持不动** |
| 36 | `internal_us_filings_recent` | — | — | **保持不动** |
| 37 | `news_search` | — | — | **保持不动** |
| 38 | `health_health_get` | `get_service_health` | 未变 | 改名 |
| 39 | `health_market_health_get` | `get_market_health` | 未变 | 改名 |
| 40 | `raw_xueqiu_raw__path__get`（若注册表里有） | `get_xueqiu_raw` | 未变 | 改名（**先 grep 确认是否注册**） |

> **注意 #23/#24/#25**：同一条 operationId 在注册表里被多个 `key`（跨域/大宗商品/美股）复用 —— 改名时**必须全部改**，漏一条那个 key 就永久失败。`BY_NAME` 的 `SHARED_BY_NAME` 机制（`app/gateway/tool_registry.py:655-668`）依赖同名共享，改完要跑 `test_tool_registry*` 确认。

### 1.3 新增工具（3.2.1 有、Mosaic 没有）

**A 股筹码/股东（8 条全新能力，domain=`a_share`）**：

| 新 operationId | 路径 | 建议 `key` | 建议 `category` | purpose 草稿 |
|---|---|---|---|---|
| `get_ashare_capital_flow` | `/ashare/chips/capital` | `capital_flow` | moneyflow | 个股资金流向（筹码口径） |
| `list_ashare_holders` | `/ashare/chips/holders` | `chips_holders` | fundamental | 个股股东/持股明细 |
| `list_ashare_reductions` | `/ashare/chips/reductions` | `chips_reductions` | fundamental | 个股减持记录 |
| `list_ashare_unlocks` | `/ashare/chips/unlocks` | `chips_unlocks` | fundamental | 个股解禁记录 |
| `get_latest_ashare_capital_flow` | `/ashare/chips/latest/capital` | `latest_capital_flow` | moneyflow | 全市场最新资金流（无需 symbol） |
| `get_latest_ashare_holders` | `/ashare/chips/latest/holders` | `latest_chips_holders` | fundamental | 全市场最新股东变动（无需 symbol） |
| `get_latest_ashare_reductions` | `/ashare/chips/latest/reductions` | `latest_chips_reductions` | fundamental | 全市场最新减持（无需 symbol） |
| `get_latest_ashare_unlocks` | `/ashare/chips/latest/unlocks` | `latest_chips_unlocks` | fundamental | 全市场最新解禁（无需 symbol） |

**其它新增**：
- `list_stock_post_comments`（`/market/post-comments`）—— **情绪面计划已注册**，本文不重复。
- `verify_upstream`（`/verify_upstream`）—— **建议不注册**（它是诊断端点，不是市场数据；注册进去只会浪费 planner 预算）。

**⚠️ 这 8 条筹码工具的入参 schema（是否要 symbol、page/count 约束、响应结构）尚未实测** —— 必须先按 §2-B0 探针实测再注册，不要照抄本表。

### 1.4 已消失的端点（7 条）

`/tencent/quote`、`/xueqiu/{abnormal-reasons,longhu,orderbook,search,timeline,trades}` —— **路径本身不存在了**，全部有对应新路径（见 §1.2），**没有"功能被砍"的情况**。

**真正被砍的能力**：`POST /market/snapshot` 对 `exchange=xueqiu` 仍不支持（422 `exchange='xueqiu' 不支持实时行情`），所以「美股/港股实时行情快照」依然无源 —— 这与既有文档的「无实时快照」边界一致，**不要因为 snapshot 改名就以为它能用了**。

### 1.5 反例警示：不要照抄文档

- `allowed_openapi.json`（33 条）是**旧版快照**，与 3.2.1 的 43 条差异巨大。新 schema 在本机：
  `%LOCALAPPDATA%\iiix\plugins\market-gateway\versions\3.2.1\extracted\api\openapi.json`
  （另有 `mcp/tools.openapi.json`，266897 bytes，含 40 个 MCP 工具的 input/output schema）
- **MCP 工具名 = operationId**，Mosaic 的 `tool_name` 必须逐字匹配，否则 `ToolRuntime` 抛 `KeyError: Tool is not allowed by registry: <name>`。
- 上游 release notes 里说的「讨论类型改用独立 feed 参数，统一迁移到 `/market` 路由」**确认为真**（`feed` enum 实测存在，见 `SENTIMENT_PLAN.md` §1.2）。

---

## 2. Phase B：实施

### B0. 探针闸门 —— 扩充 `scripts/verify_us_market.py` 或新建 `scripts/verify_gateway_321.py`

1. 断言 `ToolRuntime` 能 `resolve_tool_by_name` 到**每一个**新 operationId（注册完后跑，这是最便宜的回归）。
2. 逐个真调**旧名** → 记录失败文案（证明迁移必要性，留档）。
3. 逐个真调**新名** → 记录 status + datum 数 + 耗时，产出「旧名失败 / 新名成功」对照表。
4. 8 条筹码工具逐个实测入参与响应（**这是 B1 注册的依据**）：分别用 / 不用 symbol、page/count 边界、响应条数与字段名。
5. `POST /market/snapshot`：`{"symbol":"BTC/USDT"}` 应 200；`{"symbol":"AAPL","exchange":"xueqiu"}` 应 422（确认边界未变）。
6. `/market/quotes`：确认 `symbols` 参数的**分隔符**与批量上限（`symbols=SH600519` 单值已实测 200；批量未测）。

**闸门判定**：全部新名可达 → 继续 B1。个别不可达 → 那个 key 标记降级并在 purpose 里写明，交付报告注明。

### B1. 注册表批量改名（`app/gateway/tool_registry.py`）

1. 按 §1.2 表逐条替换 `tool_name`（**`key` 一律不动**，除情绪面计划里的 `timeline`→`discussions`）。
2. 改了路径的 7 条（#12-#20 减去内部工具）**必须同步 `http_path`**。
3. `SHARED_BY_NAME` 的共享条目（#23/#24/#25）全部改齐。
4. 更新模块 docstring 的工具总数（契约测试 `tests/test_docs_contract.py` 守护）。
5. grep 全仓 `tool_name` 字符串：**除注册表外**，`app/cache.py` 的 TTL 表按 operationId 建键（`app/cache.py:181`、`:186`）**必须一起改**，否则 TTL 静默退回默认值。
6. grep 全仓 `public_*_get` / `*_xueqiu_*_get` / `*_eastmoney_*_get` / `*_coinglass_*_get` 模式，确认无遗漏（含 `WHITELIST_NO_SYMBOL`、测试、提示词）。

### B2. 启用 8 条筹码工具（**取决于 B0 实测**）

若实测通过，新增 `_ASHARE_CHIPS` 列表并在 `ALL_TOOLS` 拼装。分类建议见 §1.3。
- `category` 选 `fundamental` / `moneyflow`：**不要**新造 category（`AnalystName` 是闭集，加新类要动 `app/graph/state.py:96` 与路由）。
- 4 条 `latest_*` 无需 symbol → 加进 `WHITELIST_NO_SYMBOL`（`app/graph/nodes/analysts/base.py:47-68`）。
- 若实测发现某条无数据 / 结构不可解析 → **不要注册**，报告注明。

### B3. 通道健壮性（本计划的核心增值，不只是改名）

**问题**：今天的故障是「默认 mcp 模式下全部工具失败，但没有任何地方报错阻止启动」。必须让它在**启动时**就可见。

实施（**需先确认设计，见 §5 开放问题**）：

1. `app/config.py`：新增
   ```python
   #: 启动时是否对 Gateway 通道做一次自检（mcp 模式：spawn + 握手 + list_tools）
   gateway_startup_selfcheck: bool = True
   #: 自检失败时的行为：warn（只记日志）/ fail（启动即失败）
   gateway_selfcheck_on_failure: str = "warn"   # warn | fail
   ```
2. `app/main.py` 的 lifespan：`market_gateway_mode == "mcp"` 且 `gateway_startup_selfcheck` 为真时做一次 `list_tools()`；失败时按 `gateway_selfcheck_on_failure` 记 **ERROR 级日志 + 在 `/health` 里暴露 `gateway_channel: "mcp_unavailable"`** 或直接抛异常终止。
3. **错误文案必须可操作**（用 §1.1 的原文匹配，不要泛化成「连接失败」）：
   - 检测到 `MCP 已停用` / `Connection closed` → 提示「iiix CLI ≥ 0.8.0 请用 `plugin serve`，并确认 `MCP_ARGS`」
   - 检测到 `登录已失效` → 提示「执行 `iiix login`，并用 `iiix plugin verify market-gateway` 确认」
   - 检测到 `MCP initialize (handshake) timed out` → 提示检查 `RESEARCH_TIMEOUT_SECONDS` 与网络
4. `app/gateway/mcp_client.py`：`connect()` 的 `MCPConnectionError` 消息里**带上 `mcp_command` + `mcp_args` 的实际值**（现在只说 "MCP server startup failed"，运维看不出配的是什么）。
5. `/health` 增加 `gateway_mode` 与 `gateway_channel` 字段（若已有同类字段则复用）。

**硬要求**：自检**不得**让 `http` 模式或纯内部工具链路变慢/失败；自检超时必须独立于 `research_timeout_seconds`（用更短的值，如 10s），否则启动会挂 30s。

### B4. 测试

1. **regression 硬闸门**：新增 `tests/test_gateway_tool_names_321.py` —— 断言注册表里**不存在**任何旧 operationId（把 §1.2 的左列全量硬编码为黑名单），断言新名全部可 `resolve_tool_by_name`。**这条测试就是防今天这种事故复发的**。
2. 自检测试：monkeypatch `list_tools` 抛不同异常 → 断言日志级别、文案关键字、`/health` 字段、`fail` 模式下的启动行为。
3. 缓存 TTL 测试：`_resolve_ttl` 对新 operationId 返回预期 TTL。
4. 保留既有 `test_tool_registry*.py` 语义测试（`BY_NAME` 共享、域过滤、`registry_text`）。

### B5. 文档

| 文件 | 改什么 |
|---|---|
| `README.md` §配置 Market Gateway MCP | **已改**（主 Agent 已完成：`plugin serve` + ≥0.8.0 警告 + verify 自检命令） |
| `README.md` 工具表 | 工具数 44 → 44 + 筹码 8（实测通过才有）+ 情绪面 5（情绪面计划） |
| `docs/architecture.md` §7 | **已改**（主 Agent 已完成） |
| `docs/tools.md` | 全部 operationId 列改名 |
| `.env.example` | **已改**；补 `GATEWAY_STARTUP_SELFCHECK` / `GATEWAY_SELFCHECK_ON_FAILURE` |
| 新增 `docs/gateway_troubleshooting.md` | 把 §1.1 的失败文案表 + 自检命令 + 三步排查写成运维文档 |

---

## 3. Phase C：验证与提交

1. 双模式全量 `pytest` + ruff check/format 全绿。
2. **真链路**：HTTP 模式与 MCP 模式**各真调 ≥5 个不同域的工具**（A 股/港股/Crypto/美股/大宗），记录 status + datum 数 + 耗时。**MCP 模式必须真跑通**（这是本次事故的核心验证项；只跑 HTTP 不算闭环）。
3. 启动自检实测：故意把 `MCP_ARGS` 改回 `mcp serve market-gateway` 跑一次 `python -m app.main`，确认**启动时就看到明确报错**而不是 575 绿 + 线上全 403。
4. 分 Phase commit，中文 + 前缀，**不要 push**。
5. 追加执行记录。

---

## 4. 停止条件

1. 新 operationId 里出现**注册表无从映射**的条目（如上游新增了 Mosaic 不该要的工具）→ 报告，不要自行决定注册。
2. 8 条筹码工具**全部**实测不可用 → 跳过 B2，其余照做，报告注明。
3. 需要改 `App` 的启动流程/端口/守护方式等方案级变更 → 回用户。
4. `SHARED_BY_NAME` 改名后出现**真实**语义冲突（两条不同 key 需要不同 operationId）→ 报告。

---

## 5. 开放问题（需用户/主 Agent 确认）

1. **B3 自检的默认行为**：`warn`（默认，不阻塞启动）还是 `fail`（默认，启动即终止）？主 Agent 倾向 **`warn` 默认 + `/health` 暴露 `mcp_unavailable`**（本地开发不该因为网关挂了起不来），但要在 README 写明。
2. **8 条筹码工具**是否本计划一起做，还是留给下一轮？（涉及新能力，不只是迁移）
3. 是否把 `verify_upstream` 注册为工具？（主 Agent 倾向**不注册**）
4. 情绪面计划与本文的**执行顺序**（同一文件交叉，不可并行）。

---

## 6. 执行记录

（待子 Agent 落地后追加）
