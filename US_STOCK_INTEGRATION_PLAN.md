# 美股域接入执行计划（交给子 Agent 直接落地）

> 本文由主 Agent 基于仓库真值（2026-10-06，main @ 64cbdc6，工作树干净）编写。
> 所有 file:line 均已人工核实，执行时**不必重新调研**；如实际代码与本文引用不符，以代码为准并在交付报告中说明差异。

---

## 0. 目标与范围

**目标**：让 `us_stock` 域从 Placeholder 变为可用 —— 用户问"苹果/NVIDIA/美股今天怎么样"时，Agent 能规划美股工具、拿到真实行情、产出经 Evidence Gate 和 Critic 审计的市场情报。

**本计划做**：
- Phase A：美股行情接入（Gateway 雪球通道，带决策闸门）
- Phase B：文档真值同步 + 测试 + 提交

**本计划不做**（明确排除，勿顺手实现）：
- 舆情 Analyst 节点（等评论 MCP）
- 宏观域、铜/原油
- 引入 yfinance/finnhub 等第三方 Python 依赖
- AIHOT 资讯工具（许可条款未确认，见 §6）

---

## 1. 关键真值事实（已核实，直接引用）

### 1.1 域机制 —— 大部分接线已存在，只缺工具

| 事实 | 位置 |
|---|---|
| `MarketDomain` Literal 已含 `"us_stock"` | `app/models/research.py:19`（值列表 23 行） |
| `DEFAULT_DOMAINS` 已含 `"us_stock"` | `app/models/research.py:30`（35 行） |
| normalizer 域推断关键词已含 `us_stock` 组（yahoo/finnhub/nasdaq/nyse 等） | `app/gateway/normalizer.py:76` |
| Supervisor prompt 已会路由美股（"苹果/Tesla/NVIDIA/美股..." → `domain=us_stock`） | `app/agent/prompts_graph.py:27, 55` |
| 简报标题已覆盖 us_stock（"今日美股市场情报"） | `app/agent/prompts.py:67` |
| Critic 域规则 `_DOMAIN_RULES` **只有** `a_share`、`crypto`，无 us_stock | `app/graph/nodes/critic.py:43-55` |
| `registry_text(domains=[...])` 域过滤为空时只附 health 工具（T24 行为，勿改语义） | `app/gateway/tool_registry.py:650-672`，注释在 661 行 |

**结论**：README"扩展新市场域 4 步"中的第 1 步（models）、第 3 步（normalizer）已完成；实际要做的只有第 2 步（tool_registry）和第 4 步（critic 规则）。

### 1.2 Gateway 美股能力 —— 行情可能零新依赖

- `allowed_openapi.json` 中 `/market/klines`、`/market/snapshot`、`/market/window` 三个 POST 端点的 `symbol` 参数描述写明：**"雪球另支持港股 03690、美股 SPCX"**（三个 schema 的 `properties.symbol.description`，KlinesRequest / SnapshotRequest / WindowRequest）。即 `exchange=xueqiu` 时 symbol 用**裸美股代码**（如 `AAPL`）。
- `/tencent/quote` 仅支持 A/京股，**不含美股**（其 operation 描述与 symbol 描述均确认）。
- `/xueqiu/longhu` 仅 A 股；`/xueqiu/*` 其余端点 symbol 无文档说明，**默认视为不支持美股，不要注册**。
- Gateway 无任何宏观 / 铜 / 原油端点（已 grep 确认零命中）。

### 1.3 工具注册表结构

- `ToolMeta` 字段（`app/gateway/tool_registry.py:25-57`）：`key`（逻辑键，Supervisor 的 plan.steps 用它引用）、`tool_name`（Gateway operationId）、`purpose`（给 LLM 的描述，参数要求写进括号）、`domain`、`priority`（high/medium/low）、`http_method`、`http_path`、`category`（technical/fundamental/moneyflow/news/shared/sentiment）。
- `ALL_TOOLS` 拼装在 `app/gateway/tool_registry.py:587-601`，**`_US_STOCK_PLACEHOLDERS` 已在求和式里**（596 行）——只需替换列表内容，不用改拼装。
- 当前 `_US_STOCK_PLACEHOLDERS` 定义了两次：`tool_registry.py:378`（`= []`，**死代码，被 521 行覆盖**）和 `tool_registry.py:521`（注释占位）。**删除 378 行的重复定义**。
- 模板参考：`_COMMODITIES_PLACEHOLDERS`（338-369 行）——同一条 Gateway 端点按 symbol 差异注册多个 ToolMeta 的写法。
- 跨域通用工具 `klines`/`snapshot`/`window`（domain=`"cross"`，约 533-557 行）与美股将注册的条目共用同一批 operationId，注意 **key 不能冲突**。
- docstring 里的工具总数必须与 `len(ALL_TOOLS)` 一致，有契约测试守护：`tests/test_docs_contract.py::test_registry_docstring_tool_count_matches_actual`。**新增条目后必须同步更新 tool_registry.py 模块 docstring 的计数**。
- README 第 170 行写"35+ 个工具覆盖 4 大市场"，注册表注释（661 行）写"40 个工具"——计数变化后一并核对这些数字。

### 1.4 调用链（理解即可，一般不用改）

- Supervisor：`registry_text()` 全量给 planner（`app/graph/nodes/supervisor.py:78`），planner 按 `key` 选工具，`_build_route` 按 `category` 分组派发给 Analyst（`supervisor.py:122-131`）。
- 工具执行：`app/graph/tool_runtime.py:248` —— `meta.http_method == "INTERNAL"` 走 `_execute_internal`（320 行，按 operationId 硬编码分发），否则走 Gateway（mode 分支在 `_gateway_class()` 305-313 行：`mcp`/`http`）。
- 缓存：ToolRuntime 层有 TTL 缓存（见 `tests/test_tool_runtime_ttl.py`），**工具层无需自建缓存**。
- INTERNAL 工具注册写法参考 `hk_northbound_daily`（tool_registry.py:318 附近）：`http_method="INTERNAL", http_path=""`，tool_name 即分发用的 operationId。

### 1.5 测试与环境纪律（必须遵守，有既往踩坑）

1. **必须用 `.venv/Scripts/python.exe`**（Windows 环境，不要用系统 python）。
2. 全量基线先跑一遍并记录数字（双模式：mcp 与 http 各跑一次；基线应为全绿 + 0 skipped，若基线本身有红，停下报告，不要在红基线上开工）：
   ```bash
   .venv/Scripts/python.exe -m pytest . -q
   .venv/Scripts/python.exe -m ruff check . --no-cache
   .venv/Scripts/python.exe -m ruff format --check . --no-cache
   ```
3. 测试不得依赖真实网络/真实 LLM：解析函数直测 + monkeypatch 假响应（参考 `tests/test_hk_northbound.py` 只测 `_build_params`/`_parse_*` 纯函数的模式）。
4. 注意已知测试坑：market_cache 全局污染、monkeypatch 打在模块全局上、污染类测试在单用例内顺序执行（详见 memory 惯例；新测试文件里fixture 隔离要自洽）。
5. **三步验证全绿才能 commit**；commit message 用中文、`feat:`/`test:`/`docs:` 前缀（对齐 git log 现有风格）。

---

## 2. Phase A：美股行情接入

### A0. 决策闸门 —— 先验证 Gateway 雪球美股通道（必须最先做）

§1.2 的依据只是 OpenAPI 文档声明，**必须实测**。写一次性探针脚本 `scripts/verify_us_market.py`（风格仿 `scripts/verify_commodities.py`：asyncio + httpx 直调 Gateway HTTP API，打日志）：

1. 从 `.env` 读 `MARKET_GATEWAY_HTTP_URL` 与 `MARKET_GATEWAY_API_KEY`（`X-API-Key` header）。
2. 依次请求 `POST /market/snapshot`，body：`{"symbol": "<SYM>", "exchange": "xueqiu", ...}`，对 `AAPL`、`NVDA`、`TSLA`、`SPCX` 各测一次（SPCX 是文档原例，用于核对格式）。如 422 报参数缺失，按 `/market/snapshot` 在 allowed_openapi.json 里 requestBody schema 的 required 字段补齐后重试，并把最终可用的最小 body 记进脚本注释。
3. 对成功的 symbol 再测 `POST /market/klines`（`period` 用 `1d`，其余参数同样以 schema required 为准）和 `POST /market/window`。
4. 输出一张 symbol × 端点 的可用性表。

**闸门判定**：
- **通过**（≥2 个主流 symbol 在 snapshot+klines 上返回真实价格数据）→ 按 §2-A1 注册。
- **部分通过**（仅个别 symbol 或仅 snapshot 可用）→ 只注册验证通过的端点组合，purpose 里写清限制，交付报告里注明。
- **失败**（全部 4xx/5xx 或返回非行情数据）→ **停止 Phase A**，删除探针脚本或不提交，直接跳到 §3 收尾并报告：文档声称的美股支持实际不可用，需要 Gateway 侧修复或换数据源。**不得擅自改走 yfinance 等第三方**（那是方案级变更，需回到用户决策）。

### A1. 注册美股工具条目

改 `app/gateway/tool_registry.py`：

1. **删除 378 行的死代码重复定义** `_US_STOCK_PLACEHOLDERS = []`。
2. 用真实条目替换 521 行的占位列表。模板（按 A0 实测结果调整 symbol 例子与注释）：

```python
_US_STOCK_PLACEHOLDERS = [
    # ✅ A0 实测（YYYY-MM-DD）：exchange=xueqiu 支持美股裸代码（AAPL/NVDA/TSLA…）
    # symbol 格式：裸代码，如 AAPL；港股式前缀/后缀均不需要
    ToolMeta(
        "us_snapshot",
        "snapshot_market_snapshot_post",
        "美股实时行情快照（雪球通道，需 symbol=裸代码如 AAPL，exchange=xueqiu）",
        domain="us_stock",
        priority="high",
        http_method="POST",
        http_path="/market/snapshot",
        category="technical",
    ),
    ToolMeta(
        "us_klines",
        "klines_market_klines_post",
        "美股历史 K 线（雪球通道，需 symbol=裸代码如 AAPL，exchange=xueqiu，period=1d）",
        domain="us_stock",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
        category="technical",
    ),
    ToolMeta(
        "us_window",
        "window_market_window_post",
        "美股复盘时间窗聚合（雪球通道，需 symbol=裸代码如 AAPL，exchange=xueqiu）",
        domain="us_stock",
        priority="medium",
        http_method="POST",
        http_path="/market/window",
        category="technical",
    ),
]
```

3. 模块 docstring 的工具总数 +3（或按实际数量），保持与 `len(ALL_TOOLS)` 一致。
4. **不注册** fundamental 类美股工具：Gateway 无美股基本面端点，`category="fundamental"` 留空是诚实状态（美股研究以 technical 路线为主，报告 limitation 即可）。

### A2. Critic 域规则

`app/graph/nodes/critic.py:43-55` 的 `_DOMAIN_RULES` 增加 `"us_stock"` 条目，风格对齐现有两条（断言与数据绑定，禁止无数据断言）。建议内容（可微调）：

```python
"us_stock": (
    "[美股审查规则]\n"
    "- 关于美股个股涨跌的结论必须有 us_snapshot / us_klines 数据支撑\n"
    "- 不能在没有 K 线数据的情况下声称 '趋势突破/破位'\n"
    "- 美股无基本面工具接入时，报告不得声称财务数据支撑"
),
```

### A3. 测试（新增 `tests/test_us_stock_domain.py`）

参考 `tests/test_tool_registry.py` 与 `tests/test_tool_registry_multi_domain.py` 的写法，至少覆盖：

1. 注册表含 `us_stock` 域 ≥3 个条目，key 无重复、operationId 与 http_path 匹配 allowed 端点。
2. `registry_text(domains=["us_stock"])` 返回的工具列表**包含** us_* 条目与 health 工具（T24 行为回归：不为空、不含其他域工具）。
3. `resolve_tool("us_snapshot")` 返回的 meta 各字段正确。
4. `_DOMAIN_RULES["us_stock"]` 存在且非空（critic 接线）。
5. `MarketDomain`/`DEFAULT_DOMAINS` 含 us_stock（防回归断言，成本极低）。
6. 模块 docstring 计数 == `len(ALL_TOOLS)`（已有契约测试会覆盖，勿重复造轮子，跑通即可）。

**不要**写需要真实 Gateway 的集成测试进 pytest（网络依赖违反测试纪律）；A0 的探针脚本是一次性验证工具，不进测试套件。

### A4. 文档真值同步（与代码同一 commit 或紧随的 docs commit）

- `README.md`：§支持的市场域（46 行起）美股行从"需后续接入专用第三方数据源"改为实际能力（雪球通道行情，无基本面）；§扩展新市场域（491 行起）里 `us_stock` 的状态描述同步；170 行"35+ 个工具"数字核对更新。
- `docs/tools.md`：如有工具清单/计数，同步。
- `US_STOCK_INTEGRATION_PLAN.md`（本文件）：完成后在文末追加"执行记录"小节（日期、A0 实测结果表、基线数字、commit 列表），然后此文件可留在仓库作为档案。

---

## 3. Phase B：验证与提交

1. 双模式全量测试（§1.5-2 的命令）+ ruff check + ruff format --check，全绿。
2. 手动冒烟（如环境具备）：`MARKET_GATEWAY_MODE=http` 下起服务，`POST /api/ask` 问 "NVIDIA 最近走势如何"，确认 Supervisor 能选中 us_* 工具、Evidence 非空、报告产出。不具备条件（无 key）则跳过并在报告中注明。
3. 提交拆分建议：
   - `feat: 美股域接入 —— 注册 us_snapshot/us_klines/us_window（雪球通道）+ critic 美股审查规则`
   - `test: 美股域注册表与域过滤测试`
   - `docs: README 美股能力真值同步`
   - （探针脚本如保留，单独 `chore:` 提交）
4. 最终交付报告需包含：A0 实测可用性表、基线前后测试数字对比（双模式）、所有改动文件清单、遗留限制（如"美股无基本面数据源"）。

---

## 4. 停止条件（出现即停，写报告交回主 Agent）

1. §A0 闸门失败（Gateway 美股通道实测不可用）。
2. 基线测试本身有红（开工前就红，不属于本任务修复范围）。
3. 实际代码与 §1 真值冲突且无法局部解决（比如 tool_registry 结构已变）。
4. 任何需要新增第三方依赖、修改 `_execute_internal` 分发、或改 LangGraph 拓扑的冲动 —— 这些都是方案级变更，超出本计划授权。

---

## 5. 附录：AIHOT 资讯工具（暂不执行，留档）

AIHOT（aihot.news）提供匿名 REST API（`/api/v1`，OpenAPI 定义在 `https://aihot.news/openapi-v1.json`，Agent 说明在 `/api/v1/agent`）与远程 MCP（`https://aihot.news/api/mcp`，Streamable HTTP，8 个工具，单次 ≤30 条）。**许可仅限个人/公益非商业，商用需书面授权** —— 在用户确认许可适用之前，本项不执行。届时推荐路径：仿 `news_search` 注册 `internal_ai_news`（`http_method="INTERNAL"`），在 `_execute_internal` 增加分发分支，新增 `app/research/ai_news.py`；**不走 MCP**（现有 mcp_client 为单 Gateway 设计，为资讯源扩展多服务器支持不成比例）。

---

## 6. 执行记录（2026-10-06，main @ 64cbdc6）

### A0 决策闸门实测（scripts/verify_us_market.py，exchange=xueqiu）

| symbol | /market/snapshot | /market/klines | /market/window |
|---|---|---|---|
| AAPL | ❌ 422 | ✅ 真实 OHLCV | ✅ 真实 OHLCV |
| NVDA | ❌ 422 | ✅ 真实 OHLCV | ✅ 真实 OHLCV |
| TSLA | ❌ 422 | ✅ 真实 OHLCV | ✅ 真实 OHLCV |
| SPCX | ❌ 422 | ✅ 真实 OHLCV | ✅ 真实 OHLCV |

- snapshot 的失败原因是服务端明确 422：`exchange='xueqiu' 不支持实时快照`——与 allowed_openapi.json 中该端点 `exchange` 描述（只列 binance|okx|bybit|aster|hyperliquid，不含 xueqiu）一致，属文档真值而非偶发故障。
- **闸门判定：部分通过** → 只注册验证通过的 `us_klines` + `us_window`，不注册 `us_snapshot`（计划模板中的第三条未落地，系实测限制）。
- 实测可用的最小 body：klines 需 `symbol/exchange=xueqiu/interval/start/end`；window 需 `symbol/exchange=xueqiu/interval/anchor`（详见探针脚本 docstring）。

### 基线与收尾测试数字（双模式全量 pytest）

| 阶段 | mcp 模式 | http 模式 | ruff check | ruff format --check |
|---|---|---|---|---|
| 开工基线 | 527 passed | 527 passed | 通过 | 108 files formatted |
| 收尾 | 540 passed | 540 passed | 通过 | 110 files formatted |

新增 13 个测试（tests/test_us_stock_domain.py），0 skipped，双模式全绿。

### 冒烟验证（MARKET_GATEWAY_MODE=http，`POST /api/ask` "NVIDIA 最近走势如何"）

- 首轮暴露真问题：planner 只看 purpose 文本，`us_klines` 未在括号中写明 start/end 必填，LLM 漏参导致 422；已按 §1.3 补全 purpose 的必填参数后复测，planner 传齐 `symbol/exchange=xueqiu/interval=1d/start/end`，klines 返回 NVDA 真实行情，报告正常产出（Evidence 30 条）。

### 与计划的偏差

1. 注册条目 2 个（非模板中的 3 个）：`/market/snapshot` 不支持雪球通道，`us_snapshot` 未注册，critic 规则相应改为引用 `us_klines / us_window`。
2. 冒烟发现 purpose 缺必填参数说明导致 LLM 漏参，已补全（属 A1 授权范围）。
3. 其余按计划执行，未扩 scope。
