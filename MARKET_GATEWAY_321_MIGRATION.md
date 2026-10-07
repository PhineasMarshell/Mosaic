# Market Gateway 3.2.1 改名迁移 + MCP 通道自检执行计划

> 本文由主 Agent 基于**实测真值**编写（2026-10-06）。
> **`SENTIMENT_PLAN.md` 与本文是两件独立的事**：情绪面接入只改情绪相关条目（6 条），本文负责**其余全部工具的 operationId 迁移**与**通道健壮性**。
> **执行顺序（已定）：先做 `SENTIMENT_PLAN.md`，再做本文。** 两份都改 `app/gateway/tool_registry.py` 与 `WHITELIST_NO_SYMBOL`，**严禁并行**；本文开工前必须确认情绪面那份已 commit，且本文的 40 条改名要**跳过情绪面已改的 6 条**（否则会二次改写）。
> **执行纪律（必须遵守，有既往踩坑）**：
> 1. **必须用 `.venv/Scripts/python.exe`**（Windows 环境，不要用系统 python）。
> 2. 开工前先跑全量基线并记录数字（**双模式各跑一次**：`MARKET_GATEWAY_MODE=mcp` 与 `=http`；基线应为全绿 + 0 skipped；若基线本身有红，**停下报告，不要在红基线上开工**）：
>    ```bash
>    .venv/Scripts/python.exe -m pytest . -q
>    .venv/Scripts/python.exe -m ruff check . --no-cache
>    .venv/Scripts/python.exe -m ruff format --check . --no-cache
>    ```
> 3. 测试**不得依赖真实网络/真实 LLM**：解析函数直测 + monkeypatch 假响应（参考 `tests/test_hk_northbound.py` 只测 `_build_params`/`_parse_*` 纯函数的模式）。
> 4. 注意已知测试坑：`market_cache` 全局污染、monkeypatch 打在模块全局上、污染类测试在单用例内顺序执行；新测试文件的 fixture 隔离要自洽。
> 5. **三步验证全绿才能 commit**；commit message 用中文 + `feat:`/`test:`/`docs:`/`chore:` 前缀（对齐 `git log` 现有风格）；**不要 push**。

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

另外，**排查过程中又发现两个真实缺陷（与改名同源、都在这次事故的复盘里暴露）**，一并纳入本文：

| 节 | 缺陷 | 一句话 |
|---|---|---|
| **B6** | 域过滤在生产链路上没生效 | `app/graph/nodes/supervisor.py:78` 用 `registry_text()`（不传 `domains=`）→ planner 永远看到全部 44 条工具，实测给 AAPL 挑了 binance 的 `snapshot` → 必然 422 |
| **B7** | 证据 `domain` 被硬编码成 `crypto` | `app/research/evidence.py:309` 与 `:364` 写死 `domain="crypto"` → **任何域的 K 线/快照证据都被标成 crypto**（A 股、美股全中） |

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
| **OAuth Token 刷新超时**（工具调用） | `{"code":"gateway_error","message":"刷新 OAuth Token: OAuth Token 刷新失败: Post \"https://logto.x.iiix.dev/oidc/token\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)"}` |
| **OAuth 发现文档超时**（工具调用） | `{"code":"gateway_error","message":"获取 OAuth 发现文档: Get \"https://logto.x.iiix.dev/oidc/.well-known/openid-configuration\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)"}` |

#### 1.1.1 ⚠️ MCP 「能握手、不能调用」的**根因已定位并修复（2026-10-06）**：MCP 子进程丢失代理环境变量

| 阶段 | mcp 模式（`MCP_ARGS="plugin serve market-gateway"`，iiix 0.8.4） | http 模式（API Key） |
|---|---|---|
| spawn + `initialize()` 握手 | ✅ 通过 | — |
| `list_tools()` | ✅ **40 个工具**（~44ms） | — |
| 真实 tool call（修复前） | ❌ `gateway_error` + OAuth 刷新/发现超时（见 §1.1 表） | ✅ 全部 200 |
| 真实 tool call（**注入代理变量后**） | ✅ `get_ashare_sentiment` 返回真实 `series`（`CIGAO/QX` 等） | ✅ |
| `iiix plugin verify market-gateway` | ✅ `status:"passed"`（`req=1128ms`，需同时给 `HTTP_PROXY`**和**`HTTPS_PROXY`+`NO_PROXY`） | — |

**根因链（三段证据）**：

1. 本机系统代理开着（注册表 `HKCU:\…\Internet Settings` → `ProxyEnable=1`、`ProxyServer='127.0.0.1:7897'`），但**环境变量里没有** `HTTP_PROXY`/`HTTPS_PROXY`。Python 的 httpx 读系统代理，**iiix（Go）只认环境变量**。
2. `logto.x.iiix.dev` 有 AAAA 记录而本机 **IPv6 完全不通** → Go 直连时先试 IPv6、耗尽超时预算 → `context deadline exceeded`。报文把它包装成「读取 OAuth 发现文档失败」，**看起来像登录问题，其实是网络路径问题**（这是本次排查最大的坑）。
3. `mcp` SDK 的 `stdio_client` 在 `env=None` 时用 `get_default_environment()`，实测**只白名单继承 12 个变量**：
   `APPDATA / HOMEDRIVE / HOMEPATH / LOCALAPPDATA / PATH / PATHEXT / PROCESSOR_ARCHITECTURE / SYSTEMDRIVE / SYSTEMROOT / TEMP / USERNAME / USERPROFILE`
   → **`HTTPS_PROXY` 根本到不了 iiix 子进程**。

**已落地的修复**（`app/gateway/mcp_client.py`）：

```python
PROXY_ENV_KEYS = ("HTTP_PROXY","HTTPS_PROXY","NO_PROXY","ALL_PROXY",
                  "http_proxy","https_proxy","no_proxy","all_proxy")

@staticmethod
def _child_env() -> dict[str, str]:
    env = dict(get_default_environment())
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env

params = StdioServerParameters(command=..., args=args, env=self._child_env())  # 原来是 env=None
```

配套：`MCPConnectionError` 的 4 个分支（startup / handshake / `list_tools`）现在都带 `(command: <mcp_command> <mcp_args>)`，排查时一眼看出起的是哪个二进制、用了什么参数。测试：`tests/test_mcp_handshake.py` 新增 3 个（代理透传 / 未设时不注入 / 报错含 launch 命令）。

**运维侧仍需做的事**（不在代码里）：
```powershell
setx HTTP_PROXY  "http://127.0.0.1:7897"
setx HTTPS_PROXY "http://127.0.0.1:7897"
setx NO_PROXY    "localhost,127.0.0.1"
```
然后**新开终端**执行 `iiix login`（否则 `iiix` 自己新起的进程也拿不到代理）。实测：只给 `HTTPS_PROXY` 时 `plugin verify` 仍 TLS 超时（`req=5178ms`），**必须同时有 `HTTP_PROXY`**；加了 `NO_PROXY=localhost,127.0.0.1` 后从 `req=6543ms` 降到 `1128ms`。

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

**先回答一个容易误解的点**：这 8 条**既不是「Mosaic 已废弃的旧工具」，也不是「上游凭空多出的新工具」**，而是上游 3.2.1 新增的 **A 股筹码/股东数据域**（`/ashare/chips/*`）—— Mosaic 从来没有过，属于**新增能力**，不是改名。
上游 3.2.1 的 MCP 暴露 40 个工具 = 31 个改名后的旧工具 + 8 条筹码 + `list_stock_post_comments`（情绪面计划已注册）+ `verify_upstream`（诊断端点）。

**A 股筹码/股东（8 条，domain 全为 `a_share`）**：

| 新 operationId | 路径 | 建议 `key` | 必填入参 | 语义（上游 description） |
|---|---|---|---|---|
| `get_ashare_capital_flow` | `/ashare/chips/capital` | `capital_flow` | `symbol`, **`as_of`** | 查询 as_of 时点**已采集入库**的最近一次股本状态 |
| `list_ashare_holders` | `/ashare/chips/holders` | `chips_holders` | `symbol`, **`as_of`** | 查询 as_of 时点最新可见的十大股东、流通股东和股东户数 |
| `list_ashare_reductions` | `/ashare/chips/reductions` | `chips_reductions` | `symbol`, **`as_of`** | 查询 as_of 时点采集的减持公告与已发生的股东减持事实 |
| `list_ashare_unlocks` | `/ashare/chips/unlocks` | `chips_unlocks` | `symbol`, **`as_of`** | 查询 as_of 时点已知的历史与未来解禁记录（含 `scheduled`/`effective`） |
| `get_latest_ashare_capital_flow` | `/ashare/chips/latest/capital` | `latest_capital_flow` | `symbol` | 返回查询时点**已经取得**的最新标准股本状态 |
| `get_latest_ashare_holders` | `/ashare/chips/latest/holders` | `latest_chips_holders` | `symbol` | 返回查询时点**已经取得**的最新股东结构 |
| `get_latest_ashare_reductions` | `/ashare/chips/latest/reductions` | `latest_chips_reductions` | `symbol` | 返回查询时点**已经取得**的最新减持事实 |
| `get_latest_ashare_unlocks` | `/ashare/chips/latest/unlocks` | `latest_chips_unlocks` | `symbol` | 返回查询时点**已经取得**的最新解禁记录 |

> ⚠️ **注意与本文档旧版的说法相反**：`latest_*` 四条**也需要 `symbol`**（实测：不给 symbol → **422**）。原表把它们写成「无需 symbol」是错的，已修正。

**响应结构（8 条统一，实测）**：
```
{ source, symbol, as_of|snapshot_at|temporal_mode, queried_at, coverage_status, coverage[], record|records }
```
- `coverage[]` 每项：`{dataset, status, first_acquired_at, last_attempt_at, last_success_at, rows_seen, rows_normalized, error}`
- `dataset` enum：`capital` / `top_holders` / `top_float_holders` / `holder_count` / `unlocks` / `holder_trades` / `reduction_notices`
- `status` enum：`ok` / `partial` / `no_data` / `source_error` / `not_collected` / `not_observed`
- `coverage_status` enum：`complete` / `partial` / `source_error` / `not_observed` / `not_collected`
- `facts` 字段示例：`total_market_cap` / `circulating_a_market_cap` / `restricted_a_shares` / `close_price` / `rank` / `holder_name` / `holder_type` / `is_mainland_connect` / `share_change_ratio_pct` / `unlock_free_shares` / `event_state(scheduled|effective)` / `average_price` / `announced_at` / `effective_at` / `available_at`（**PIT 语义：数据按「当时可见」而非「事后修订」**）

**❌ 实测结论：这 8 条当前全部无数据，不建议注册**

| 实测 | 结果 |
|---|---|
| 5 个标的（`SH600519` / `SZ000001` / `SZ300750` / `SH601318` / `SH600036`）× 3 个端点 | 全部 **HTTP 200**，但 `coverage_status` 恒为 **`not_collected`**，`coverage[].status` 恒为 `not_collected`、`rows_seen=0`、`rows_normalized=0`，`record=null` / `records=[]` |
| 即 | **端点存在、结构完整、参数校验正常，但上游采集侧尚未对该数据域入库**（本部署/本 key 上） |

**因此 B2 的处置改为**：**本轮不注册这 8 条**，在 `MARKET_GATEWAY_321_MIGRATION.md` 的执行记录与 README 的「已知边界」里如实写明：

> A 股筹码/股东数据域（`/ashare/chips/*`，8 个端点）在 Gateway 3.2.1 已暴露且参数/结构正确，但实测所有标的 `coverage_status=not_collected`（采集侧未入库）。Mosaic 暂不注册，待上游数据可用后再接入。

若将来要接：`latest_*` 四条直接给了当前明细，但**注入时间语义必须靠 `snapshot_at`/`queried_at`**；`as_of` 四条是 PIT 查询（`as_of` 必填 RFC 3339），适合「当时能看到什么」的审计式提问。

**其它新增**：
- `list_stock_post_comments`（`/market/post-comments`）—— **情绪面计划已注册**，本文不重复。
- `verify_upstream`（`/verify_upstream`）—— **建议不注册**（诊断端点，不是市场数据；注册只会浪费 planner 预算）。

**⚠️ 若仍要注册**（例如上游随时可能开始采集）：8 条全部要 `symbol`（走 symbol 守卫），4 条 `as_of` 版还多一个必填时间参数 —— **planner 不会自发产生 `as_of`**，需要 supervisor prompt 明确指示（如「用当前 UTC 时间作为 as_of」），否则注册了也只会 422。这是不建议注册的第二个理由。

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

**方式：全量替换，不留别名、不留旧名。** 用「旧 → 新」一一映射直接改写注册表（不是新增别名、不是双注册）。`key` 一律不动（Supervisor 的 `plan.steps` 与提示词按 `key` 引用；唯一例外是情绪面计划里的 `timeline` → `discussions`）。

理由：旧 operationId 在 3.2.1 上**已经不存在**，留着只会是「必然 403/404 的死条目」，且 MCP 的 `ToolRuntime` 对未注册名直接抛 `KeyError: Tool is not allowed by registry`。做别名等于把死代码留在仓库里。

**实施步骤**：

1. **先用工具生成精确的旧名清单**，不要靠肉眼 grep（`klines_market_klines_post` 之类的名字与缓存键 `market_cache` 极易混淆）：

   ```bash
   .venv/Scripts/python.exe -c "
   from app.gateway.tool_registry import ALL_TOOLS
   for t in ALL_TOOLS:
       print(f'{t.tool_name}\t{t.key}\t{t.domain}\t{t.category}\t{t.http_path}')
   "
   ```

   把输出与 §1.2 的映射表逐条对齐，产出「旧名 → 新名」的 40 行清单。
2. 按清单**精确字符串替换**（不要用模糊正则；`market` 这个词同时出现在缓存变量名、模块名、路径里）。
3. 改了路径的条目（§1.2 表中标「改名 + 改路径」的 #12–#20）**必须同步 `http_path`**。
4. **共享 operationId 必须一次改齐**（实测精确清单，`ALL_TOOLS`=44 而 `BY_NAME`=37，差额 7 条正式来自这里）：

   | 旧 operationId | 复用它的 key |
   |---|---|
   | `klines_market_klines_post` | `commodity_gold`(commodities)、`klines`(cross)、`us_klines`(us_stock) |
   | `snapshot_market_snapshot_post` | `commodity_silver`(commodities)、`commodity_platinum`(commodities)、`snapshot`(cross) |
   | `quote_tencent_quote_get` | `quote`(a_share)、`hk_quote`(hk_stock) |
   | `search_xueqiu_search_get` | `search`(a_share)、`hk_search`(hk_stock) |
   | `window_market_window_post` | `window`(cross)、`us_window`(us_stock) |

   **改法是改 `tool_name` 常量本身**（这几条在代码里就是同一个字符串），所以只要不写死字面量、而是统一从常量引用，就不会漏；漏一条那个 key 就永久失败。
5. **`app/cache.py` 的 TTL 表按 operationId 建键**（`app/cache.py:181` `"public_sentiment_ashare_master_sentiment_get": SENTIMENT_TTL`、`:186` `"longhu_xueqiu_longhu_get": LONGHU_TTL`）**必须一起改**，否则 TTL 静默退回默认值（测试要覆盖 `_resolve_ttl` 对新名字返回预期值）。
6. `app/graph/nodes/analysts/base.py` 的 `WHITELIST_NO_SYMBOL`（`:47-68`）里所有旧名同步换成新名。**判断一个名字是否该留在白名单，看它是否真的能没有 symbol**（`get_ashare_sentiment` / `get_limit_up_*` / `news_search` / `internal_*` 留；`list_stock_discussions` / `list_stock_post_comments` / `get_market_quotes` 不留）。
7. **测试里的旧名**：实测分布 14 个文件（`tests/test_anomaly_detector.py`、`test_cache_multi_domain.py`、`test_data_integrity.py`、`test_docs_contract.py`、`test_evidence.py`、`test_gateway_reuse.py`、`test_graph_nodes.py`、`test_normalizer_enhanced.py`、`test_normalizer_f10.py`、`test_normalizer_multi_domain.py`、`test_reasoning_parsing.py`、`test_stream_endpoint.py`、`test_tool_registry.py`、`test_tool_registry_multi_domain.py`），同名模式出现 **25 + 11 + 31 + 13 处**量级。**全部要改**。
8. 更新模块 docstring 的工具总数（契约测试 `tests/test_docs_contract.py` 守护）。

**⚠️ 改名后必须核对的隐性依赖**（这些地方通常「按名字」硬编码）：
- `app/gateway/tool_registry.py` 的 `BY_NAME` 规范条目规则（cross 域优先，否则先注册者胜）—— 改名后同名共享关系是否仍成立
- `SHARED_BY_NAME`（`app/gateway/tool_registry.py:655-668`）
- `by_category` / `tools_by_category`（`:677-686`）、`registry_text(domains)`（`:694-715`）
- `app/gateway/normalizer.py` 的 `_DOMAIN_HINTS` 与工具名相关的分支
- supervisor / planner 提示词里若出现具体工具名

### B2. 8 条筹码工具：**本轮不注册**

依据 §1.3 的实测（5 个标的 × 3 个端点全部 `coverage_status=not_collected`，`record=null` / `records=[]`），**本轮不注册这 8 条**，只在文档里记录为已知边界。

若上游数据可用后要接：
- 全部 8 条都要 `symbol`（走 symbol 守卫，**不进** `WHITELIST_NO_SYMBOL`）。
- 4 条 `as_of` 版要 supervisor prompt 明确指示时间参数，否则必 422。
- `category` 建议 `moneyflow`（capital）与 `fundamental`（holders/reductions/unlocks）；**不要新造 category**（`AnalystName` 是闭集，加新类要动 `app/graph/state.py:96` 与路由）。
- **必须先跑 B0 探针确认真有数据**再注册（防今天这种「注册了但必然空手而归」）。

### B3. 通道健壮性（本计划的核心增值，不只是改名）

**问题**：今天的故障是「默认 mcp 模式下全部工具失败，但没有任何地方报错阻止启动」。必须让它在**启动时**就可见。

**设计已定（用户裁定）**：自检失败默认 **`warn`** —— 不阻塞启动，但必须**大声**：ERROR 级日志 + `/health` 暴露 `gateway_channel: "mcp_unavailable"`。理由：本地开发不该因为网关/登录挂了起不来，但「静默失败」绝不可接受（这正是今天 575 绿而线上全挂的成因）。

实施：

1. `app/config.py`：新增
   ```python
   #: 启动时是否对 Gateway 通道做一次自检（mcp 模式：spawn + 握手 + list_tools）
   gateway_startup_selfcheck: bool = True
   #: 自检失败时的行为：warn（默认，只记 ERROR 日志 + /health 标记）/ fail（启动即终止）
   gateway_selfcheck_on_failure: str = "warn"
   ```
2. `app/main.py` 的 lifespan：`market_gateway_mode == "mcp"` 且 `gateway_startup_selfcheck` 为真时做一次 `list_tools()`；失败时按 `gateway_selfcheck_on_failure` 处理（`warn` → ERROR 日志 + `/health` 标记；`fail` → 抛异常终止启动）。
3. **错误文案必须可操作**（用 §1.1 的原文匹配，不要泛化成「连接失败」）：
   - 检测到 `MCP 已停用` / `Connection closed` → 「iiix CLI ≥ 0.8.0 请用 `plugin serve`，并确认 `MCP_ARGS`」
   - 检测到 `登录已失效` → 「执行 `iiix login`，再用 `iiix plugin verify market-gateway` 确认」
   - 检测到 `handshake timed out` → 「检查 `RESEARCH_TIMEOUT_SECONDS` 与网络」
4. `app/gateway/mcp_client.py`：`connect()` 的 `MCPConnectionError` 消息里**带上 `mcp_command` + `mcp_args` 的实际值**（现在只说 "MCP server startup failed"，运维看不出配的是什么），并把 CLI 的就绪自检命令写进提示。
5. `/health` 增加 `gateway_mode` 与 `gateway_channel` 字段（若已有同类字段则复用）。

**硬要求**：自检**不得**让 `http` 模式或纯内部工具链路变慢/失败；自检超时必须独立于 `research_timeout_seconds`（建议 10s），否则启动会挂 30s。

### B4. 测试

1. **regression 硬闸门**：新增 `tests/test_gateway_tool_names_321.py` —— 断言注册表里**不存在**任何旧 operationId（把 §1.2 的左列全量硬编码为黑名单），断言新名全部可 `resolve_tool_by_name`。**这条测试就是防今天这种事故复发的**。
2. 自检测试：monkeypatch `list_tools` 抛不同异常 → 断言日志级别、文案关键字、`/health` 字段、`fail` 模式下的启动行为。
3. 缓存 TTL 测试：`_resolve_ttl` 对新 operationId 返回预期 TTL。
4. 保留既有 `test_tool_registry*.py` 语义测试（`BY_NAME` 共享、域过滤、`registry_text`）。

### B5. 文档

| 文件 | 改什么 |
|---|---|
| `README.md` §配置 Market Gateway MCP | **已改**（主 Agent 已完成：`plugin serve` + ≥0.8.0 警告 + verify 自检命令） |
| `README.md` 工具表 | 工具总数 **不变（44）** —— 本轮只改名不增删；8 条筹码实测无数据、不注册（见 §1.3 / B2/5） |
| `docs/architecture.md` §7 | **已改**（主 Agent 已完成） |
| `docs/tools.md` | 全部 operationId 列改名 |
| `.env.example` | **已改**；补 `GATEWAY_STARTUP_SELFCHECK` / `GATEWAY_SELFCHECK_ON_FAILURE` |
| 新增 `docs/gateway_troubleshooting.md` | 把 §1.1 的失败文案表 + 自检命令 + 三步排查写成运维文档 |

---

### B6. 域过滤在生产链路上没生效（P1）——**改名之外发现的真实缺陷**

**症状（实测，2026-10-06）**：让 Orchestrator 以 `domain="us_stock"` 跑「苹果（AAPL）最近的技术面走势和最新基本面怎么样？」

- `report.used_tools = ['us_fundamentals', 'klines_market_klines_post', 'snapshot_market_snapshot_post']`
- 其中 `snapshot_market_snapshot_post` 的结果是 **error / 0 datums**：
  `Invalid parameters (422): {"ok":false,"error":"symbol='AAPL' 在 binance 不存在。请用 ccxt 统一写法(现货 BTC/USDT，永续 BTC/USDT:USDT)…"}`
- 即 **planner 给美股问题挑了 binance 的 crypto 快照工具**，必然失败，还往报告里塞了一条错误 caveat。

**根因（一行）**：`app/graph/nodes/supervisor.py:78` 是

```python
registry = registry_text()          # ← 没有传 domains=
```

→ **planner 永远看到全部 44 条工具，与用户指定的 `domain` 完全无关**；显式域只在 `supervisor.py:112-114` 事后覆盖 `plan.intent.domain`（那已经晚了，路由与工具选择已经定完）。

**反证（这套 API 本身是对的）**：`registry_text(domains=["us_stock"])` 实测**只**返回

```
- us_klines: klines_market_klines_post [us_stock] — 美股历史 K 线（雪球通道，必填: symbol=裸代码如 AAPL, exchange=xueqiu, interval=1d, start/end=ISO 日期范围如 2026-09-20/2026-10-05） [high]
- us_window: window_market_window_post [us_stock] — 美股复盘时间窗聚合（雪球通道，必填: symbol=裸代码如 AAPL, exchange=xueqiu, interval=1d, anchor=锚定日期如 2026-10-02） [medium]
- us_fundamentals: internal_us_fundamentals [us_stock] — 美股基本面（SEC EDGAR XBRL，必填: symbol=美股裸代码如 AAPL…） [high]
- us_filings_recent: internal_us_filings_recent [us_stock] — 美股近期 SEC 申报（…） [medium]
- health: health_health_get [unknown] …  /  - market_health: health_market_health_get [unknown] …
```

`tools_by_domain("us_stock")` 同样 4 条 —— **过滤能力是有的，只是没人用**（`tests/test_us_stock_domain.py:92-111` 与 `tests/test_tool_registry_multi_domain.py:164` 都覆盖了它）。

**修法（分三步，第 2 步是承重的）**：

1. **接入域过滤**：`supervisor.py` 的 `_plan` 在 `state["domain"]` 显式存在时用 `registry_text(domains=[explicit_domain, "cross"])`；不存在时保持 `registry_text()` 不变（planner 要自己判域，此时不能预先过滤）。
   ⚠️ **`"cross"` 是必须带的**：`snapshot` / `klines` / `window` 三条共享 operationId 的注册域就是 `cross`，而 **crypto 侧的实时 ticker 与 K 线只有这几个 key 提供**（crypto 域自己的 9 条是 coinglass/hyperliquid 衍生品数据）。所以**不能只传 `[domain]`**，否则 crypto 域会失去行情能力。
2. **给 `cross` 工具的 `purpose` 文本加上边界声明**（**这条才是真正防错的**）—— 光有域过滤挡不住 `snapshot`，因为它确实是 `cross` 域、确实在渲染文本里。必须让 planner 从文本就知道它不适用：
   - `snapshot`（`key=snapshot`）purpose 末尾加：「**仅支持 crypto 交易所**（binance/okx/bybit/aster/hyperliquid）；**不支持 A股 / 港股 / 美股个股**（实测 `exchange=xueqiu` → 422）。美股行情用 `us_klines`/`us_window`。」
   - `klines`（`key=klines`）与 `window`（`key=window`）purpose 补：「**多源**：crypto（`exchange=binance` 等）与大宗商品；**美股请用 `us_klines`/`us_window`**（`exchange=xueqiu` + 裸代码）。」
   - 这与美股行情接入时那次「purpose 未写明 start/end 必填 → LLM 漏参 422」是**同一类修法**（已验证有效；当时的计划文档已归档删除，结论在 `README.md` 美股域一节与 `tests/test_us_stock_domain.py`）。
3. **加一道零成本守卫**：`_plan` 产出后校验每个 step 的 `key` 是否出现在**本次渲染出来的**注册表文本里；不在则**不执行**该 step 并把 `tool_call_filtered: <key>` 记进 `errors`（防 LLM 幻觉出旧名或串域）。**不新增 LLM 调用**。

**测试（B4 内新增）**：
- `registry_text(domains=["us_stock"])` 含 4 条 `us_*`、**不含** `snapshot`/`discussions`/`longhu`（已有覆盖，补 `snapshot` 这条断言）。
- `registry_text(domains=["a_share"])` 不含 `us_*`。
- `registry_text(domains=["crypto"])` **含** `snapshot`/`klines`（守住「必须带 cross」这条约束，防止后来者「优化」掉它）。
- `snapshot` 的 purpose 含「不支持」与「A股」字样（文本契约，防被改回去）。
- 守卫单测：伪造一个不在渲染文本里的 `key` → 被过滤 + `errors` 出现 `tool_call_filtered`。

**不在本计划范围**：为 `cross` 共享条目拆成按域独立的 key（如新增 `crypto_klines`）—— 那是注册表结构变更，影响 `BY_NAME`/`SHARED_BY_NAME`/缓存键，**要单独评估**。

---

### B7. 证据 `domain` 被硬编码成 `crypto`（P3）——**影响全部域，不只是美股**

**症状（实测）**：`domain="us_stock"` 那条 e2e 报告里，苹果 K 线的证据是

```json
{"id": "technical-001", "source_tool": "klines_market_klines_post", "domain": "crypto",
 "metric": "candle_summary", "value": {"count": 33, "price_min": 213.92, "price_max": 237.49, "price_last": 230.1, ...}}
```

**根因（两处硬编码，不是 `infer_domain_from_tool` 的错）**：`app/research/evidence.py`

| 行 | 代码 | 影响 |
|---|---|---|
| `app/research/evidence.py:309` | K 线摘要分支写死 `domain="crypto"` | **任何域的 K 线证据都被标成 crypto**（A 股、美股全中） |
| `app/research/evidence.py:364` | 快照摘要兜底分支写死 `domain="crypto"` | 同理 |

对比：非 K 线分支（`app/research/evidence.py:342`）用的是 `result.normalized[0].domain`，而 `infer_domain_from_tool("klines_market_klines_post")` **正确**返回 `"cross"`（`tests/test_normalizer_multi_domain.py:29` 有断言）—— 说明**问题就在这两个写死的字面量**。

**修法**：
1. `evidence.py:309` / `:364` 的 `domain="crypto"` 改为按工具推断（同 `:342` 的取法；拿不到时用 `"unknown"`）。
2. **共享 operationId 的域归属是次要问题**：`klines_market_klines_post` 被 `commodity_gold`/`klines`/`us_klines` 三 key 共用，仅凭 tool_name 只能得到 `"cross"`，拿不到 `us_stock`。若要让美股 K 线证据显示 `us_stock`，需要在 `ToolResult` 上带上**已解析的 key / 域**（`ToolRuntime` 里 `resolve_tool_by_name` 的结果本来就拿到 `ToolMeta`，把它透传到 `ToolResult` 即可）。**实现顺序**：先做第 1 步（真 bug、影响全部域、改动小）；第 2 步若牵连 `ToolResult` 结构，**停下问用户**（属方案级变更）。

**测试（B4 内新增）**：
- 用假的 klines 结果（`candle_summary` 路径）喂 `build_evidence` → 断言 `evidence[0].domain != "crypto"`（对 a_share/us_stock 的 K 线都不该是 crypto）。
- 快照摘要兜底分支同理。
- 回归：不得让 `domain` 变成 `None`/空串（`Evidence.domain` 的既有断言要保持通过）。

---

### B8. 附录：留档（**不执行**，别顺手做）

**AIHOT 资讯工具**：AIHOT（aihot.news）提供匿名 REST API（`/api/v1`，OpenAPI 定义在 `https://aihot.news/openapi-v1.json`，Agent 说明在 `/api/v1/agent`）与远程 MCP（`https://aihot.news/api/mcp`，Streamable HTTP，8 个工具，单次 ≤30 条）。**许可仅限个人/公益非商业，商用需书面授权** —— **在用户确认许可适用之前，本项不执行**。届时推荐路径：仿 `news_search` 注册 `internal_ai_news`（`http_method="INTERNAL"`），在 `_execute_internal` 增加分发分支，新增 `app/research/ai_news.py`；**不走 MCP**（现有 `mcp_client` 为单 Gateway 设计，为资讯源扩展多服务器支持不成比例）。
> 本条原先写在已完成的 `US_STOCK_INTEGRATION_PLAN.md` §5，该文件已按用户要求删除（内容可从 git 历史取回），这里保留唯一还活着的待办。

---

## 3. Phase C：验证与提交

1. 双模式全量 `pytest` + ruff check/format 全绿。
2. **真链路**：HTTP 模式真调 ≥5 个不同域的工具（A 股/港股/Crypto/美股/大宗），记录 status + datum 数 + 耗时。
   **MCP 模式**：验证「spawn + `initialize()` 握手 + `list_tools()` 40 个工具 + 启动自检」全部通过；**真实 tool call 的闭环以 §1.1.1 的 OAuth 阻塞解除为先决条件** —— 若实施时仍未解除，允许阶段性交付，**但执行记录必须如实写明「MCP 真调未验证（OAuth 刷新超时）」**，不许写成「MCP 已闭环」。
3. 启动自检实测：故意把 `MCP_ARGS` 改回 `mcp serve market-gateway` 跑一次 `python -m app.main`，确认**启动时就看到明确报错**而不是 575 绿 + 线上全 403（这一步不需要 OAuth，已验证会得到 `iiix 已停用…`）。
4. **B6/B7 的回归验证（必须做，不许只跑单测）**：HTTP 模式下用 `domain="us_stock"` 真跑一次「苹果（AAPL）最近的技术面走势和最新基本面怎么样？」，然后把 `report.used_tools` 与 `report.evidence` 打出来，确认两件事：
   - `used_tools` 里**不再出现** `snapshot_market_snapshot_post`（B6）；
   - K 线那条证据的 `domain` **不再是 `crypto`**（B7）。
   基线参照（修复前实测）：`used_tools=['us_fundamentals','klines_market_klines_post','snapshot_market_snapshot_post']`、`technical-001.domain='crypto'`、耗时 314.1s、`errors=[]`。
5. 分 Phase commit，中文 + 前缀，**不要 push**。
6. 追加执行记录（含 B6/B7 的前后对照）。

---

## 4. 停止条件

1. 新 operationId 里出现**注册表无从映射**的条目（如上游新增了 Mosaic 不该要的工具）→ 报告，不要自行决定注册。
2. 8 条筹码工具**全部**实测不可用 → 跳过 B2，其余照做，报告注明。
3. 需要改 `App` 的启动流程/端口/守护方式等方案级变更 → 回用户。
4. `SHARED_BY_NAME` 改名后出现**真实**语义冲突（两条不同 key 需要不同 operationId）→ 报告。

---

## 5. 开放问题

1. ~~B3 自检的默认行为~~ → **已定：`warn`**（用户裁定，见 B3）。
2. ~~8 条筹码工具是否本轮做~~ → **已定：本轮不注册**（实测全部 `not_collected`，见 §1.3 / B2）。
3. ~~是否把 `verify_upstream` 注册为工具~~ → **已定：不注册**（诊断端点）。
4. **两计划的执行顺序**：`SENTIMENT_PLAN.md`（情绪面）与本文都改 `app/gateway/tool_registry.py` 与 `WHITELIST_NO_SYMBOL`，**不可并行**。建议**先情绪面、再本文**（情绪面的 6 条也包含在本文的 40 条改名范围内，先做情绪面等于本文的 6/40 提前落地，两边不会互相踩）。若实施者认为先做本文更顺（一次性改完 40 条，情绪面只剩新增聚合工具），也可 —— **但必须串行，且第二份开工前先 rebase/确认第一份已 commit**。
5. 工具总数会变：**44 → 44**（本轮只改名、不增删，8 条筹码不注册）。情绪面计划完成后是 **44 + 5 = 49**（6 条情绪面条目中 `discussions` 是从 `timeline` 改名而来，净增 5 条：`post_comments`、`sentiment_index`、`internal_sentiment_index`、`internal_stock_discussions`、`internal_post_comments`）。**README/docs 的数字必须按最终状态写，不要写中间态。**
6. **MCP 真调阻塞（§1.1.1）**：MCP 能握手/`list_tools`，但真实工具调用被 iiix CLI 侧 OAuth 刷新超时挡住。**HTTP 模式可正常执行**。需要实施者/用户确认：是等该环境问题解除后再闭环 MCP，还是本轮按「HTTP e2e + MCP 握手自检」阶段性交付。
7. **B6 步 3（域守卫）的范围**：守卫只拦「`key` 不在本次渲染文本里」（防幻觉/串域），**不拦「在文本里但不适用于该 symbol」**（如 `snapshot` 对 AAPL）—— 后者靠 B6 步 2 的 purpose 边界声明 + critic 纠偏。若要求更强的机械保证（如按 symbol 前缀校验工具），属方案级变更，**先问用户**。
8. **B7 步 2（共享 operationId 的域归属）是否本轮做**：需要把 `resolve_tool_by_name` 得到的 `ToolMeta` 透传到 `ToolResult`，会动 `ToolResult` 结构（有既有测试依赖），**属方案级变更，先问用户**；B7 步 1（硬编码 `crypto` 改按工具推断）是本轮必做。
9. **B6 是否需要拆 `cross` 共享条目**（如新增 `crypto_klines`/`crypto_snapshot`）→ **本轮不做**，已在 B6 末尾注明理由（影响 `BY_NAME`/`SHARED_BY_NAME`/缓存键）。

---

## 6. 执行记录

（待子 Agent 落地后追加）
