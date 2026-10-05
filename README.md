# Mosaic — Market Intelligence Agent

> **从 Market Data 到 Market Understanding**

Mosaic 是一个基于 **Market Gateway MCP** 的市场情报 Agent。它不负责交易执行，不以"预测涨跌"为核心，而是通过自主规划研究路径、调用市场数据工具、交叉验证证据、分析市场状态与异常变化，把分散的 A 股、港股、Crypto、大宗商品等数据转化为可解释的市场情报。

---

## Architecture

Mosaic 基于 **LangGraph** 多节点架构，Supervisor 路由 + 多 Analyst 并行采集 + Critic 闭环审计：

```text
User
  ↓
FastAPI (REST + SSE)
  ↓
Orchestrator → LangGraph compiled graph
  ↓
Supervisor (LLM 路由：解析意图 + 选 analyst + 分配工具预算)
  ↓ Send() 扇出（并行）
┌──────────┬──────────┬──────────┬──────────┬──────────┐
│ 技术面    │ 基本面    │ 资金面    │ 新闻事件  │ 舆情情绪  │
│ analyst  │ analyst  │ analyst  │ analyst  │ analyst  │
│ (默认开) │ (默认开) │ (默认开) │ (开关)   │ (开关)   │
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘
     └──────────┴──────────┴────┬─────┴──────────┘
                                ↓
                        Evidence Gate (纯代码质量检查；零证据 → 报告降级 confidence=low，不短路)
                                ↓
                        Reasoning (LLM 汇总证据 → MarketIntelligence)
                                ↓
                        Critic (LLM 结论-证据审计)
                          pass │  revise / research_more（≤ critic_max_revisions 轮）
                           ↓   └──────► 回 Reasoning 重写 / 回 Supervisor 补研究
                          error（仅审计自身失败时内部产生 → 安全终止）
                         END
```

关键设计：
- **证据账本是唯一契约**：各 analyst 只往 `state.evidence` 追加 Evidence 条目，不写结论；结论由 Reasoning 统一产出
- **证据条数硬上限 80**：`build_evidence` 统一截断，超出时按来源保留最新 80 条，并在最后一条 `note` 注明"截断 N 条"
- **Critic 闭环**：证据不足时打回 Reasoning 重写（revise）或回 Supervisor 补充研究（research_more），最多 N 轮；审计自身失败时产出内部 `verdict="error"` 安全终止（不当成 pass，也不伪造 research_more）
- **可选节点**：news / sentiment analyst 由配置开关控制，关闭时行为与三 analyst 基线完全一致

## 支持的市场域

| 市场域 | 状态 | 覆盖能力 |
|--------|------|----------|
| **A 股** | ✅ 完整支持 | 情绪、涨停生态、题材、个股深度、龙虎榜 |
| **Crypto** | ✅ 完整支持 | K线、快照、衍生品(OI/Funding)、清算地图、大户持仓 |
| **港股** | ✅ 基础支持 | 实时行情(腾讯API)、证券搜索(雪球)、**北向资金净流入**(东财直连)、恒生指数 |
| **大宗商品** | 🟡 贵金属就绪 | OKX 永续合约：黄金(XAU)、白银(XAG)、铂金(XPT)，铜/原油待接入 |
| **美股** | 🔴 Placeholder | 预留 domain 和 registry，第三方 API 待接入 |
| **宏观** | 🔴 Placeholder | 预留 domain，CPI/PMI/利率数据待接入 |

## 核心特性

### Research Experience（PRD §18）

Mosaic 让用户看到 **AI 正在研究**，而不是一篇突然生成的文章。通过 SSE 流式推送，前端实时显示研究进度：

```text
Researching...
✓ Checking market performance
✓ Checking sentiment
✓ Checking limit-up activity
⋯ Analyzing evidence chains
→ Generating structured intelligence
```

两种交互模式：
- **`POST /api/ask/stream`** — SSE 流式返回（推荐 Web UI 使用）
- **`POST /api/ask`** — 传统同步返回（CLI / API 客户端使用）

#### SSE 事件协议

`/api/ask/stream` 返回两种事件：

**`progress` 事件** — 逐节点研究进度：

```json
{"step": "planning", "node": "supervisor", "message": "理解问题并生成研究计划"}
```

| 字段 | 取值 | 说明 |
|------|------|------|
| `step` | `planning` | Supervisor 路由决策中 |
| | `tool_call` | Analyst 节点采集工具数据（technical / fundamental / moneyflow / news / sentiment） |
| | `evaluating` | Evidence Gate 证据质量检查 |
| | `reasoning` | Reasoning 生成结构化情报 |
| | `critic` | Critic 结论-证据审计 |
| | `working` | 心跳保活（长节点执行期间） |
| | `done` | 调查完成 |
| | `error` | 节点执行错误 |
| `node` | `supervisor` / `technical` / `fundamental` / `moneyflow` / `news` / `sentiment` / `gate` / `reasoning` / `critic` / `null` | 当前执行的图节点名；心跳和完成事件为 `null` |
| `message` | string | 人类可读的进度文案 |

**`result` 事件** — 最终结果：

```json
{"code": "ok", "report": {...}, "question": "...", "tool_results": [...]}
```

| `code` | 说明 |
|--------|------|
| `ok` | 调查成功 |
| `timeout` | 超出 `RESEARCH_BUDGET_SECONDS` |
| `upstream` | 上游 LLM 输出无法解析 |
| `internal` | 其它未预期异常 |

### Market Memory（PRD §38-39）

持久化市场记忆，支持跨日历史比较（**目前由晨报/晚报摘要消费**）。全部落在**项目内单个 SQLite 文件** `memory/memory.db`
（四张表：`daily_states` / `anomalies` / `research_records` / `conversations`）：
- `daily_states` — 每日 Market State 快照
- `anomalies` — 持久化的历史异常事件（`record_anomaly`）
- `research_records` — 用户研究历史记录
- `conversations` — 多轮对话历史

> 早期版本写的是 `~/.mosaic/memory/{daily,anomalies,research}/` 三个目录，**P5 已迁到
> 项目内 SQLite**，那三个路径现在不存在。

跨日快照读取器是 `MarketMemory.get_recent_states(days=7)`（取最近 7 天 Market State
摘要），**目前只有晨报/晚报模板消费**（`app/scheduler/briefs.py`）。

> 早期版本承诺"问'今天和昨天有什么不同'时 Reasoning Engine 会自动注入最近 7 天
> Market State"，但那个注入方法（`get_context_for_question`）**从来没有生产调用方**；
> S8 验收时已按裁决删除该方法及其 3 条用例——**交互式提问不做跨日上下文注入**，
> 不要再当成现存能力。若要接进 Reasoning 链路，属新增运行时行为，需单独立任务
> （决定注入点、token 预算与测试）。

### Anomaly Radar（PRD §29-30）

自动检测市场异常并在结果中展示：
- **Crypto**: OI 突变、Funding Rate 极端值、大规模清算事件
- **A 股**: 涨停数量异常扩张/收缩、市场宽度极差、情绪骤降
- 分级：Low / Medium / High / Critical
- 每条异常包含指标、正常范围、可能含义、当前状态

报告里的 `anomalies` 字段**由代码填充**：`build_response_from_state` 用 `detect_anomalies`
现算后覆盖模型输出（模型自己填的也会被覆盖，避免双写），同步与 SSE 两条路径共用这个组装点。
检测失败不静默——报告照常产出，但原因会出现在响应的 `errors` 里。
（与上面 `anomalies` 表不是一回事：表里存的是被持久化的历史异常事件。）

### Daily Briefs（PRD §35-37）

定时简报系统（无需外部依赖，纯 asyncio）：
- **Morning Brief** (09:15): "Good Morning. Here's what matters today."
- **Evening Brief** (15:30): "What actually happened today?"
- 手动触发：`GET /api/brief/morning` / `GET /api/brief/evening`

### 统一内部数据结构

所有 Tool Response 经过 Normalizer 转换为标准格式：

```json
{
  "domain": "a_share" | "crypto" | "hk_stock" | "commodities",
  "instrument": "SH600519" | "BTCUSDT" | null,
  "metric": "...",
  "value": ...,
  "timestamp": "...",
  "source": "tencent" | "coinglass" | ...,
  "status": "success" | "partial" | "error",
  "partial": false
}
```

### 35+ 个工具覆盖 4 大市场

| 类别 | 数量 | 覆盖领域 |
|------|------|----------|
| A 股市场生态 | 4 | 情绪、涨停计数/板块/明细 |
| A 股基本面 | 7 | 概览、业务、概念、财务、股东 |
| A 股微观结构 | 7 | 行情、龙虎榜、异常归因、盘口、成交 |
| Crypto 行情 | 4 | K线、快照、时间窗、交易所信息 |
| Crypto 衍生品 | 1 | 永续合约历史(OI/Funding/多空) |
| CoinGlass/Hyperliquid | 7 | 符号列表、清算地图、持仓榜、地址数、金库、爆仓、费率 |
| 港股北向资金 | 2 | **沪深股通净流入**（东财直连）、恒生指数 |
| 大宗商品贵金属 | 3 | **黄金(XAU)、白银(XAG)、铂金(XPT)** OKX 永续 |
| 健康检查 | 2 | 网关进程、行情模块状态 |

> 注：`klines`、`snapshot`、`window` 为跨域通用工具，被多个市场域复用。

## 快速开始

### 环境要求

Python 3.11+

```bash
cd Mosaic
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

MCP 客户端固定在 `mcp>=2,<3`（`pyproject.toml` 与 `requirements.txt` 同一区间），
装其它版本会在运行时踩到协议不兼容。

### 配置 LLM

```bash
cp .env.example .env    # macOS/Linux
Copy-Item .env.example .env   # Windows PowerShell
```

`.env`：

```env
OPENAI_API_KEY=你的key
OPENAI_BASE_URL=https://你的OpenAI兼容接口/v1
OPENAI_MODEL=你的模型
```

`OPENAI_BASE_URL` 可以是 OpenAI 官方接口，也可以是任何兼容 OpenAI Chat Completions 的统一接口。

### 配置 Market Gateway MCP

默认通过本地 Runtime 启动 MCP：

```bash
iiix mcp serve market-gateway
```

对应 `.env`：

```env
MCP_COMMAND=iiix
MCP_ARGS=mcp serve market-gateway
```

如果不想走 MCP，也可以直接用 HTTP API：

```env
MARKET_GATEWAY_MODE=http
MARKET_GATEWAY_HTTP_URL=https://api.x.iiix.dev/v1/d/market-gateway
MARKET_GATEWAY_API_KEY=你的key
```

> ⚠️ MCP 使用 OAuth，HTTP 使用 API Key，两种方式不要同时设置。

### 启动服务

API + Web UI：

```bash
python -m app.main
```

打开浏览器：

```text
http://127.0.0.1:8000
```

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI |
| GET | `/health` | 健康检查（含调度器状态） |
| POST | `/api/ask` | 同步问答（传统 JSON 响应） |
| POST | `/api/ask/stream` | SSE 流式问答（实时研究进度推送） |
| GET | `/api/brief/morning` | 手动触发晨间简报 |
| GET | `/api/brief/evening` | 手动触发晚间简报 |

CLI 模式：

```bash
python -m app.cli "今天A股为什么这么弱？"
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 安全限制

系统限制防止无限循环调用的配置项：

```text
MAX_TOOL_CALLS=12
MAX_RESEARCH_STEPS=8
MAX_RETRY_PER_TOOL=1
RESEARCH_TIMEOUT_SECONDS=30      # 单次工具调用 / MCP 握手超时
RESEARCH_BUDGET_SECONDS=300      # 一次完整调查的总预算
STREAM_HEARTBEAT_SECONDS=15      # SSE 静默期心跳间隔
LLM_TIMEOUT_SECONDS=90           # 单次 LLM 调用超时
CRITIC_MAX_REVISIONS=2           # Critic 打回重写最大轮次
GRAPH_RECURSION_LIMIT=25         # LangGraph 递归上限（防无限回环）
SENTIMENT_ENABLED=false          # 舆情分析员总开关（评论 MCP 就绪后启用）
SENTIMENT_MAX_COMMENTS=500       # 单次拉取评论上限
NEWS_ENABLED=false               # 新闻分析员总开关
NEWS_SEARCH_TTL_SECONDS=21600    # DDGS 搜索结果缓存时长（秒）
```

都是 `.env` 中的配置项，不是硬编码。

### 两层超时的关系（重要）

`RESEARCH_TIMEOUT_SECONDS` 管的是**单个工具调用**，`RESEARCH_BUDGET_SECONDS`
管的是**一整次调查**（planner LLM → N 个工具 → evidence gate → reasoning LLM）。

后者必须显著大于前者，否则 HTTP 层会在研究跑完之前把它掐断。历史上这两个值
耦合成了 `RESEARCH_TIMEOUT_SECONDS + 30 = 60s`，而一次真实调查动辄需要几分钟
—— 于是几乎每个问题都必然超时，前端再把同样的活从头重跑一遍。

超时后的 HTTP 语义：

| 场景 | 状态码 |
|------|--------|
| `question` 为空 / `domain` 非法 | 400 |
| 上游模型返回无法解析的内容（`LLMOutputError`） | 502 |
| 调查超出 `RESEARCH_BUDGET_SECONDS` | 504 |
| 其它未预期异常 | 500 |

SSE 端点不会用状态码表达失败（响应头早已发出），而是在 `result` 事件里带
`error` + `code`（`timeout` / `upstream` / `internal`），前端据此显示错误气泡
和重试按钮，**不会自动重跑**。

## 缓存策略

| 数据类型 | TTL | 说明 |
|----------|-----|------|
| 实时行情 | 10s | 价格变化快 |
| 情绪/涨停 | 60s | 盘中快照 |
| Crypto K线 | 60s | 7×24 市场 |
| Crypto 快照 | 15s | 数字资产 |
| 衍生品/OI | 120s | 变化相对较慢 |
| 资金费率 | 180s | 8h 结算周期 |
| 交易所列表 | 3600s | 几乎不变 |

## 项目结构

```text
Mosaic/
├── README.md                  # 本文档
├── pyproject.toml             # 项目配置与依赖
├── .env.example               # 环境变量模板
├── allowed_openapi.json       # Market Gateway OpenAPI 规范快照
│
├── app/
│   ├── main.py                # FastAPI 入口 + REST API + SSE Stream + Briefs 调度
│   ├── cli.py                 # CLI 客户端
│   ├── config.py              # Settings (Pydantic Settings)
│   ├── cache.py               # TTL 内存缓存
│   ├── errors.py              # 自定义异常
│   ├── llm_json.py            # LLM JSON 解析工具
│   ├── evaluation.py          # 评估工具函数
│   ├── logging_config.py      # 日志配置
│   │
│   ├── agent/
│   │   ├── orchestrator.py    # 总控调度（LangGraph 编译 + 运行）
│   │   ├── prompts.py         # System / Reasoning Prompt
│   │   ├── prompts_graph.py   # Supervisor Planner Prompt
│   │   ├── evidence_gate.py   # 代码级证据门控
│   │   └── state.py           # Agent 运行时状态
│   │
│   ├── graph/                 # LangGraph 多节点架构
│   │   ├── state.py           # ResearchState (Pydantic) + AnalystName
│   │   ├── builder.py         # 图构建（节点注册 + 条件边）
│   │   ├── tool_runtime.py    # 通用工具运行时（MCP/HTTP/内部工具 + 缓存）
│   │   └── nodes/
│   │       ├── supervisor.py  # LLM 路由节点（意图解析 + analyst 分配 + 预算）
│   │       ├── gate.py        # 证据门控节点
│   │       ├── reasoning.py   # 推理节点
│   │       ├── critic.py      # 审计节点（结论-证据一致性）
│   │       └── analysts/
│   │           ├── base.py          # Analyst 通用骨架（预算守卫/异常降级）
│   │           ├── technical.py     # 技术面分析员
│   │           ├── fundamental.py   # 基本面分析员
│   │           ├── moneyflow.py     # 资金面分析员
│   │           └── news.py          # 新闻事件分析员（配置开关）
│   │
│   ├── gateway/
│   │   ├── mcp_client.py      # MCP 协议客户端
│   │   ├── http_client.py     # HTTP REST 客户端
│   │   ├── tool_registry.py   # 多域工具注册表（by_category 索引）
│   │   ├── normalizer.py      # 跨域数据规范化层
│   │   └── stock_codes.py     # 股票代码映射表
│   │
│   ├── research/
│   │   ├── reasoning.py       # 推理引擎（注入 Market Memory 上下文）
│   │   ├── evidence.py        # 证据构建
│   │   ├── news_search.py     # DDGS 新闻搜索
│   │   └── hk_northbound.py   # 港股通北向资金（东财直连，不走 Gateway）
│   │
│   ├── models/
│   │   ├── market.py          # ToolResult, NormalizedDatum, Status
│   │   ├── evidence.py        # Evidence, Claim
│   │   ├── research.py        # ResearchIntent, ResearchPlan, Critique 等
│   │   └── response.py        # MarketIntelligence + ResearchResponse
│   │
│   ├── web/
│   │   └── index.html         # 产品级 Web UI
│   │
│   ├── detector/              # Anomaly Detection
│   │   └── anomaly.py         # 规则引擎 + 阈值匹配
│   │
│   ├── memory/                # Market Memory (SQLite)
│   │   └── storage.py         # SQLite 持久化存储
│   │
│   └── scheduler/             # Daily Briefs
│       └── briefs.py          # asyncio 后台调度 + 简报生成
│
├── tests/                     # 335+ 个测试用例
│   ├── test_graph_e2e.py      # 图端到端
│   ├── test_graph_routing.py  # Supervisor 路由
│   ├── test_graph_nodes.py    # 节点单元
│   ├── test_graph_topology.py # 图拓扑 + 开关
│   ├── test_stream_endpoint.py # SSE 流式端点
│   ├── test_ask_endpoint.py   # 同步端点 + 错误分类
│   ├── test_briefs.py         # 定时简报
│   ├── test_news_search.py    # 新闻搜索缓存
│   ├── test_data_integrity.py # 域数据隔离
│   ├── test_tool_registry.py
│   ├── test_tool_registry_multi_domain.py
│   ├── test_normalizer.py
│   ├── test_normalizer_enhanced.py
│   ├── test_normalizer_multi_domain.py
│   ├── test_normalizer_f10.py
│   ├── test_evidence.py
│   ├── test_evidence_gate.py
│   ├── test_http_client.py
│   ├── test_cache_multi_domain.py
│   ├── test_models_multi_domain.py
│   ├── test_anomaly_detector.py
│   ├── test_hk_northbound.py
│   ├── test_market_memory.py
│   ├── test_conversation.py
│   └── test_reasoning_parsing.py
│
├── memory/                    # 运行时数据（SQLite + 简报 JSON）
│   ├── memory.db              # Market Memory 数据库
│   ├── morning/               # 晨间简报（按日期命名 JSON）
│   └── evening/               # 晚间简报（按日期命名 JSON）
│
├── scripts/
│   └── verify_commodities.py  # OKX/Binance/Bybit 商品合约可用性验证
│
└── docs/
    ├── architecture.md        # 架构设计文档
    ├── tools.md
    └── evaluation.md
```

## 扩展新市场域

新增市场只需 4 步，不需要重写 Agent：

1. **models/research.py**: 在 `MarketDomain` Literal 中添加值，加入 `DEFAULT_DOMAINS`
2. **tool_registry.py**: 添加新的 `ToolMeta` 条目（或占位符列表）
3. **normalizer.py**: 可选 — 在 `_DOMAIN_HINTS` 补充域名推断关键词
4. **critic.py** / **prompts.py**: 补充该域的评估规则和 prompt 描述

当前已有 `hk_stock`、`commodities`、`us_stock`、`macro` 四个域就绪，其中：
- `hk_stock` 可通过 `quote`(腾讯HK代码) + `search`(雪球) 直接获得行情，**北向资金由 `app/research/hk_northbound.py` 直连东财 KLineJSAPI 自动注入**
- `commodities` 已接入 OKX 永续合约贵金属品种（黄金 XAU / 白银 XAG / 铂金 XPT），铜/原油待接入
- `us_stock` 和 `macro` 需要后续接入专用第三方数据源

### Internal Tools

Agent 支持通过注册表中设置 `http_method="INTERNAL"` 的工具来绕过 Market Gateway 直接调用内部函数。当前内置了两个内部工具：
- `internal_hk_northbound` — 港股通北向资金净流入数据（直连东方财富）
- `internal_hk_index` — 恒生指数 & 恒生科技指数快照（直连东方财富）

这为不经过统一网关的外部数据源提供了干净的集成方式，当未来这些接口迁移到 Gateway 后只需更新 tool_name 即可无缝切换。

## 设计理念

Mosaic 的核心流程：

```
Market Data → Observe → Understand → Investigate → Validate → Reason → Explain → Monitor
```

传统工具解决："数据是多少？"  
Mosaic 解决：
- "市场现在发生了什么？"
- "为什么会这样？"
- "这个判断有什么证据？哪些地方可能不成立？接下来应该关注什么？"

## 非目标

第一阶段明确不做：
1. 自动交易 / 下单 / 托管资金
2. 无证据的个股/币种预测
3. 用单一指标直接生成"买入/卖出"结论
4. 把第三方市场数据包装成确定性事实
5. 让 LLM 直接猜测缺失行情数据

Mosaic 的定位是：**Market Research / Market Intelligence / Decision Support**
不是 Trading Execution / Financial Advisor。

## License

Internal project — see [Mosaic产品设计文档](../Mosaic产品设计文档.md) for full specification.

## 变更记录

### Latest

- **架构升级**：LangGraph 多节点架构全面落地（P2.5 + P3 + P4 + P5），Supervisor 路由 + 三 Analyst 并行采集 + Critic 闭环审计；旧路径（market_detective / planner / evaluator / kernel）已全部删除
- **新闻分析员**：新增 NewsAnalystNode（配置开关 `NEWS_ENABLED`），DDGS 搜索结果走 6h 长 TTL 缓存
- **定时简报走图**：Morning/Evening Brief 改为调用完整 LangGraph 调查（含 LLM），失败时回退 memory 模板；简报 JSON 存 `memory/morning/` 和 `memory/evening/`
- **存储迁移**：Market Memory 从 `~/.mosaic/memory.db` 迁入项目目录 `memory/memory.db`
- **SSE 协议**：progress 事件新增 `node` 字段，逐节点推送研究进度（supervisor/technical/fundamental/moneyflow/gate/reasoning/critic）
- **Bug 修复**：SSE 流式路径补全 research/daily state 持久化（与同步 `/api/ask` 一致）
- **Bug 修复**：CLI `render_report` 兼容新 `EvidenceItem` 格式（`id/source_tool/metric/value/note`）
- **Bug 修复**：`daily_state` dict key 从值误用改为正确的 `"market_state": value`
- **港股北向资金接入**：新增 `app/research/hk_northbound.py` 直连东财 KLineJSAPI，自动注入沪深股通/沪股通/深股通净流入 + 恒生指数数据
- **内部工具机制**：支持 `http_method="INTERNAL"` 工具绕过 Gateway 直接调用本地函数
- **大宗商品贵金属**：OKX 永续合约接入 XAG(白银)、XPT(铂金)，共三个品种
- **新增测试**：HK 北向资金解析、对话历史管理等 10+ 用例
- **Bug 修复**：F10 数据解析和域名标注 bug（`test_normalizer_f10.py`）
