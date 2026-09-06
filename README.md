# Mosaic — Market Intelligence Agent

> **从 Market Data 到 Market Understanding**

Mosaic 是一个基于 **Market Gateway MCP** 的市场情报 Agent。它不负责交易执行，不以"预测涨跌"为核心，而是通过自主规划研究路径、调用市场数据工具、交叉验证证据、分析市场状态与异常变化，把分散的 A 股、港股、Crypto、大宗商品等数据转化为可解释的市场情报。

---

## Architecture

```text
User
  ↓
Planner (LLM 自动判断目标市场域 + 生成研究计划)
  ↓
Tool Executor → Market Gateway MCP / HTTP API
  ↓
Normalizer → Cross-Domain Normalized Data
  ↓
Evidence Engine + Evidence Gate (代码级质量检查)
  ↓
Evaluator (Domain-Aware 评估证据充分性)
  ↓
Reasoning Engine → Structured Intelligence
  ↓
UI / CLI / API
```

## 支持的市场域

| 市场域 | 状态 | 覆盖能力 |
|--------|------|----------|
| **A 股** | ✅ 完整支持 | 情绪、涨停生态、题材、个股深度、龙虎榜 |
| **Crypto** | ✅ 完整支持 | K线、快照、衍生品(OI/Funding)、清算地图、大户持仓 |
| **港股** | 🟡 基础支持 | 实时行情(腾讯API)、证券搜索(雪球)，南向资金待接入 |
| **大宗商品** | 🟡 占位 | 通用快照/K线接口暂代，独立品种数据源待接入 |
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

### Market Memory（PRD §38-39）

持久化市场记忆，支持跨日历史比较：
- `~/.mosaic/memory/daily/` — 每日 Market State 快照
- `~/.mosaic/memory/anomalies/` — 异常事件记录
- `~/.mosaic/memory/research/` — 用户研究历史记录

用户问"今天和昨天有什么不同？"时，Reasoning Engine 会自动注入最近 7 天的 Market State 上下文。

### Anomaly Radar（PRD §29-30）

自动检测市场异常并在结果中展示：
- **Crypto**: OI 突变、Funding Rate 极端值、大规模清算事件
- **A 股**: 涨停数量异常扩张/收缩、市场宽度极差、情绪骤降
- 分级：Low / Medium / High / Critical
- 每条异常包含指标、正常范围、可能含义、当前状态

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

### 32 个工具覆盖 4 大市场

| 类别 | 数量 | 覆盖领域 |
|------|------|----------|
| A 股市场生态 | 4 | 情绪、涨停计数/板块/明细 |
| A 股基本面 | 7 | 概览、业务、概念、财务、股东 |
| A 股微观结构 | 7 | 行情、龙虎榜、异常归因、盘口、成交 |
| Crypto 行情 | 4 | K线、快照、时间窗、交易所信息 |
| Crypto 衍生品 | 1 | 永续合约历史(OI/Funding/多空) |
| CoinGlass/Hyperliquid | 7 | 符号列表、清算地图、持仓榜、地址数、金库、爆仓、费率 |
| 健康检查 | 2 | 网关进程、行情模块状态 |

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
RESEARCH_TIMEOUT_SECONDS=30
LLM_TIMEOUT_SECONDS=90
```

都是 `.env` 中的配置项，不是硬编码。

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
│   ├── main.py                # FastAPI 入口 + REST API + SSE Stream + Briefs
│   ├── cli.py                 # CLI 客户端（含 what_changed / risks 渲染）
│   ├── config.py              # Settings (Pydantic Settings)
│   ├── cache.py               # TTL 内存缓存
│   ├── logging_config.py      # 日志配置
│   │
│   ├── agent/
│   │   ├── orchestrator.py    # 总控调度
│   │   ├── planner.py         # 研究计划生成 (LLM)
│   │   ├── prompts.py         # System / Planner / Reasoning Prompt（含 Memory 上下文）
│   │   ├── evaluator.py       # 证据充分性评估 (LLM, Domain-Aware)
│   │   ├── evidence_gate.py   # 代码级证据门控
│   │   └── state.py           # Agent 运行时状态
│   │
│   ├── gateway/
│   │   ├── mcp_client.py      # MCP 协议客户端
│   │   ├── http_client.py     # HTTP REST 客户端
│   │   ├── tool_registry.py   # 多域工具注册表 (32 tools)
│   │   └── normalizer.py      # 跨域数据规范化层
│   │
│   ├── research/
│   │   ├── market_detective.py # 主入口：规划→执行→评估→推理+异常检测
│   │   ├── evidence.py        # 证据构建
│   │   └── reasoning.py       # 推理引擎（注入 Market Memory 上下文）
│   │
│   ├── models/
│   │   ├── __init__.py        # 统一导出 (含 MarketDomain)
│   │   ├── market.py          # ToolResult, NormalizedDatum, Status
│   │   ├── evidence.py        # Evidence, Claim
│   │   ├── research.py        # ResearchIntent, ResearchPlan (多域)
│   │   └── response.py        # MarketIntelligence + EvidenceItem, ResearchResponse
│   │
│   ├── web/
│   │   └── index.html         # 产品级 Web UI (PRD §14-15 设计)
│   │
│   ├── detector/              # Anomaly Detection (PRD §29-30)
│   │   ├── __init__.py
│   │   └── anomaly.py         # 规则引擎 + 阈值匹配
│   │
│   ├── memory/                # Market Memory (PRD §38-39)
│   │   ├── __init__.py
│   │   └── storage.py         # JSON 文件持久化存储
│   │
│   └── scheduler/             # Daily Briefs (PRD §35-37)
│       ├── __init__.py
│       └── briefs.py          # asyncio 后台调度 + 简报生成
│
├── tests/                     # ~153 个测试用例
│   ├── test_planner.py
│   ├── test_tool_registry.py
│   ├── test_normalizer.py
│   ├── test_normalizer_enhanced.py
│   ├── test_evidence.py
│   ├── test_evidence_gate.py
│   ├── test_http_client.py
│   ├── test_market_detective.py
│   ├── test_evaluator.py
│   ├── test_models_multi_domain.py
│   ├── test_tool_registry_multi_domain.py
│   ├── test_normalizer_multi_domain.py
│   ├── test_cache_multi_domain.py
│   ├── test_anomaly_detector.py ← 新增（44 tests）
│   └── test_market_memory.py  ← 新增（12 tests）
│
└── docs/
    ├── architecture.md
    ├── tools.md
    └── evaluation.md
```

## 扩展新市场域

新增市场只需 4 步，不需要重写 Agent：

1. **models/research.py**: 在 `MarketDomain` Literal 中添加值，加入 `DEFAULT_DOMAINS`
2. **tool_registry.py**: 添加新的 `ToolMeta` 条目（或占位符列表）
3. **normalizer.py**: 可选 — 在 `_DOMAIN_HINTS` 补充域名推断关键词
4. **evaluator.py** / **prompts.py**: 补充该域的评估规则和 prompt 描述

当前已有 `hk_stock`、`commodities`、`us_stock`、`macro` 四个域就绪，其中：
- `hk_stock` 可通过 `quote_tencent_quote_get`(HK 代码) + `search_xueqiu_search_get` 直接获得行情
- `commodities` 可复用 `market/snapshot` 和 `market/klines` 通用接口暂代
- `us_stock` 和 `macro` 需要后续接入专用第三方数据源

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
