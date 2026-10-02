# Mosaic 架构文档

## 1. 运行时架构

```text
Browser / CLI
      ↓
FastAPI (REST + SSE)
      ↓
Orchestrator（编译 + 运行 LangGraph）
      ↓
┌─────────────────────────────────────────────────────┐
│                   LangGraph                          │
│                                                      │
│  Supervisor (LLM 路由)                               │
│    │ 解析意图 → 选 analyst → 分配工具预算             │
│    ▼ Send() 扇出（并行）                              │
│  ┌──────────┬──────────┬──────────┬────────┬────────┐ │
│  │ 技术面    │ 基本面    │ 资金面    │ 新闻   │ 舆情   │ │
│  │ analyst  │ analyst  │ analyst  │ analyst│ analyst│ │
│  │ (默认开) │ (默认开) │ (默认开) │ (开关) │ (开关) │ │
│  └────┬─────┴────┬─────┴────┬─────┴───┬────┴───┬────┘ │
│       └──────────┴──────────┴────┬─────┴────────┘      │
│                                  ▼                     │
│                          Evidence Gate                 │
│                          (纯代码质量检查)               │
│                                  ▼                     │
│                          Reasoning (LLM)               │
│                          汇总证据 → MarketIntelligence │
│                                  ▼                     │
│                          Critic (LLM)                  │
│                          结论-证据审计                  │
│                       pass │  revise/research_more     │
│                        ▼   └──► 回 Reasoning / Supervisor│
│                      END                                │
└─────────────────────────────────────────────────────┘
      ↓
Market Gateway (MCP / HTTP)
      ↓
iiix mcp serve market-gateway
```

## 2. 节点说明

| 节点 | 类型 | 职责 |
|------|------|------|
| **Supervisor** | LLM | 解析用户意图（domain/task），从工具注册表选择最少充分的工具，按 category 分组分配给各 analyst，设定 per-analyst 预算 |
| **TechnicalAnalyst** | 工具执行 | 执行 technical 类工具（情绪/涨停/板块/行情/K线），产出 Evidence + finding digest |
| **FundamentalAnalyst** | 工具执行 | 执行 fundamental 类工具（F10/财务/股东/业务），产出 Evidence + finding digest |
| **MoneyflowAnalyst** | 工具执行 | 执行 moneyflow 类工具（龙虎榜/北向资金/资金流），产出 Evidence + finding digest |
| **NewsAnalyst** | 工具执行 | 执行 news 类工具（DDGS 新闻搜索），开关控制，默认关闭 |
| **SentimentAnalyst** | 工具执行 + LLM | 评论爬取 + 清洗 + 聚合 + LLM 打分，开关控制，默认关闭（依赖评论 MCP） |
| **Evidence Gate** | 纯代码 | 检查 ToolResult 状态（success/partial/error），判定证据是否具备基本可用性 |
| **Reasoning** | LLM | 汇总所有 Evidence + findings，生成结构化 MarketIntelligence 报告 |
| **Critic** | LLM | 审计报告结论是否有证据支撑，输出 pass/revise/research_more |

### 关键设计约定

1. **证据账本是唯一契约**：各 analyst 只往 `state.evidence` 追加 Evidence 条目（来源工具、指标、数值、时间戳），不写结论；结论由 Reasoning 统一产出。
2. **节点函数兼容 Pydantic 与 dict 两种 state**：LangGraph v1.x 可能传入 ResearchState 或 dict，所有节点第一行统一 `model_dump()` 归一化。
3. **新增字段走返回字典写入**：禁止直接修改 state 对象，所有写入通过节点返回值 `{"field": value}` 完成。
4. **Critic 闭环**：revise 时回 Reasoning 重写（≤ `critic_max_revisions` 轮）；research_more 时带 missing_points 回 Supervisor 补充研究（最多 1 次）。
5. **可选节点**：news / sentiment analyst 由配置开关控制，关闭时对应 category 的工具归入 technical，行为与三 analyst 基线完全一致。

## 3. 配置项

### 研究预算与超时

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RESEARCH_BUDGET_SECONDS` | 300 | 一次完整调查的总预算 |
| `RESEARCH_TIMEOUT_SECONDS` | 30 | 单次工具调用 / MCP 握手超时 |
| `STREAM_HEARTBEAT_SECONDS` | 15 | SSE 静默期心跳间隔 |
| `LLM_TIMEOUT_SECONDS` | 90 | 单次 LLM 调用超时 |
| `MAX_RESEARCH_STEPS` | 8 | Supervisor 规划的最大工具数 |
| `MAX_TOOL_CALLS` | 12 | 单 analyst 最大工具调用数 |

### 图控制

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CRITIC_MAX_REVISIONS` | 2 | Critic 打回重写最大轮次 |
| `GRAPH_RECURSION_LIMIT` | 25 | LangGraph 递归上限（防无限回环） |

### 可选分析员

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `NEWS_ENABLED` | false | 新闻分析员总开关 |
| `NEWS_SEARCH_TTL_SECONDS` | 21600 | DDGS 搜索结果缓存时长（秒，6h） |
| `SENTIMENT_ENABLED` | false | 舆情分析员总开关（评论 MCP 就绪后启用） |
| `SENTIMENT_MAX_COMMENTS` | 500 | 单次拉取评论上限 |

## 4. SSE 事件协议

`POST /api/ask/stream` 返回 SSE 事件流，包含两种事件类型。

### progress 事件

```json
{"step": "planning", "node": "supervisor", "message": "理解问题并生成研究计划"}
```

| 字段 | 取值 | 说明 |
|------|------|------|
| `step` | `planning` | Supervisor 路由决策中 |
| | `tool_call` | Analyst 节点采集工具数据 |
| | `evaluating` | Evidence Gate 证据质量检查 |
| | `reasoning` | Reasoning 生成结构化情报 |
| | `critic` | Critic 结论-证据审计 |
| | `working` | 心跳保活（长节点执行期间） |
| | `done` | 调查完成 |
| | `error` | 节点执行错误 |
| `node` | `supervisor` / `technical` / `fundamental` / `moneyflow` / `news` / `sentiment` / `gate` / `reasoning` / `critic` / `null` | 当前执行的图节点名；心跳和完成事件为 `null` |
| `message` | string | 人类可读的进度文案 |

节点名 → (step, 默认文案) 映射（`_NODE_PROGRESS`）：

| node | step | 默认文案 |
|------|------|----------|
| supervisor | planning | 理解问题并生成研究计划 |
| technical | tool_call | 技术面分析员采集中 |
| fundamental | tool_call | 基本面分析员采集中 |
| moneyflow | tool_call | 资金面分析员采集中 |
| news | tool_call | 新闻事件分析员采集中 |
| sentiment | tool_call | 舆情分析员采集中 |
| gate | evaluating | 证据质量检查 |
| reasoning | reasoning | 正在生成结构化市场情报… |
| critic | critic | 结论-证据审计 |

### result 事件

```json
{"code": "ok", "report": {...}, "question": "...", "tool_results": [...]}
```

| `code` | 说明 |
|--------|------|
| `ok` | 调查成功 |
| `timeout` | 超出 `RESEARCH_BUDGET_SECONDS` |
| `upstream` | 上游 LLM 输出无法解析 |
| `internal` | 其它未预期异常 |

## 5. 数据流

```text
用户问题
  → Supervisor: LLM 生成 ResearchPlan（intent + steps）
  → _build_route: steps 按工具 category 分组为 AnalystAssignment
  → 各 Analyst: 执行分配的 tool_calls → ToolResult 列表
  → build_evidence: ToolResult → Evidence 列表
  → Evidence Gate: 代码级质量检查 → gate 结果（pass/fail）
  → Reasoning: LLM 汇总 evidence + findings → MarketIntelligence report
  → Critic: LLM 审计 report vs evidence → Critique
    → pass: END
    → revise: 回 Reasoning 重写（≤ N 轮）
    → research_more: 回 Supervisor 补充研究（最多 1 次）
  → 落库：research 记录 + daily_state 快照
```

## 6. 存储

| 数据 | 位置 | 格式 |
|------|------|------|
| Market Memory | `memory/memory.db` | SQLite（daily_states / research / conversations） |
| 晨间简报 | `memory/morning/YYYY-MM-DD.json` | JSON |
| 晚间简报 | `memory/evening/YYYY-MM-DD.json` | JSON |
| 工具缓存 | 内存（TTL） | `market_cache` |

## 7. MCP 与 HTTP API

默认走 MCP：

```bash
iiix login
iiix mcp install market-gateway
iiix mcp serve market-gateway
```

Mosaic 不重新实现 Market Gateway，也不把 API Key 写进代码。

HTTP API 可以作为备选数据通道（`MARKET_GATEWAY_MODE=http`）。

内部工具（`http_method="INTERNAL"`）绕过 Gateway 直接调用本地函数：
- `internal_hk_northbound` — 港股通北向资金（东财直连）
- `internal_hk_index` — 恒生指数快照（东财直连）
- `news_search` — DDGS 新闻搜索
