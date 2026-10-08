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
iiix plugin serve market-gateway
```

## 2. 节点说明

| 节点 | 类型 | 职责 |
|------|------|------|
| **Supervisor** | LLM | 解析用户意图（domain/task），从工具注册表选择最少充分的工具，按 category 分组分配给各 analyst，设定 per-analyst 预算 |
| **TechnicalAnalyst** | 工具执行 | 执行 technical 类工具（情绪/涨停/板块/行情/K线），产出 Evidence + finding digest |
| **FundamentalAnalyst** | 工具执行 | 执行 fundamental 类工具（F10/财务/股东/业务），产出 Evidence + finding digest |
| **MoneyflowAnalyst** | 工具执行 | 执行 moneyflow 类工具（龙虎榜/北向资金/资金流），产出 Evidence + finding digest |
| **NewsAnalyst** | 工具执行 | 执行 news 类工具（多源新闻聚合：东财个股新闻 + 财联社电报 + Google 资讯 + DDGS，跨源去重、来源标注），开关控制，默认关闭 |
| **SentimentAnalyst** | 工具执行 + LLM | 评论爬取 + 清洗 + 聚合 + LLM 打分，开关控制，默认关闭（依赖评论 MCP） |
| **Evidence Gate** | 纯代码 | 检查 ToolResult 状态（success/partial/error），判定证据是否具备基本可用性；`has_evidence=False` 时 Reasoning **降级不短路**：报告照常产出，但强制 confidence=low + data_caveats + errors（T15/D2） |
| **Reasoning** | LLM | 汇总所有 Evidence + findings，生成结构化 MarketIntelligence 报告 |
| **Critic** | LLM | 审计报告结论是否有证据支撑，输出 pass/revise/research_more；**另有节点内部产生的 `error`**（T11）：模型审计自身失败（LLM 超时 / 输出无法解析）或返回无法识别的 verdict 时，节点产出 `verdict="error"` + `errors`，路由层据此**安全终止**，既不当成 pass，也不伪造 research_more 再烧一到两轮完整工具 + LLM。`error` 不允许模型自己返回；research_more 时输出 `missing_points`（自然语言缺口）与 `missing_tool_keys`（只从 prompt 里「可补充的工具」小节挑、落库前再过滤幻觉 key 与已拿到数据的 key），供回环轮补齐 |

### 关键设计约定

1. **证据账本是唯一契约**：各 analyst 只往 `state.evidence` 追加 Evidence 条目（来源工具、指标、数值、时间戳），不写结论；结论由 Reasoning 统一产出。
2. **节点函数兼容 Pydantic 与 dict 两种 state**：LangGraph v1.x 可能传入 ResearchState 或 dict，所有节点第一行统一 `model_dump()` 归一化。
3. **新增字段走返回字典写入**：禁止直接修改 state 对象，所有写入通过节点返回值 `{"field": value}` 完成。
4. **Critic 闭环**：revise 时回 Reasoning 重写（≤ `critic_max_revisions` 轮）；research_more 时回 Supervisor 补充研究（≤ `critic_max_revisions` 次，默认 2）。回环轮 Supervisor 会读到 Critic 的 `missing_points` / `missing_tool_keys` 与已执行工具清单（渲染进 planner prompt 的「补充研究轮上下文」），并在代码层丢弃「已拿到数据且参数被覆盖」的重复步骤、把缺口工具强制补进 plan 头部（≤ `gap_max_steps` 个）。审计自身失败 → 内部 `verdict="error"` 安全终止（见上表 T11）。
5. **可选节点**：news / sentiment analyst 由配置开关控制，关闭时对应 category 的工具归入 technical，行为与三 analyst 基线完全一致。
6. **证据条数硬上限 80**（T6）：`build_evidence` 最后一步统一截断，超出时按来源保留最新 80 条，并在保留的最后一条 `note` 里写明"截断 N 条"；报告因此不会因证据过载而膨胀。
7. **报告里的 `anomalies` 字段由代码填**（D3）：`build_response_from_state` 用 `detect_anomalies` 计算后覆盖模型输出（模型填了也会被覆盖，避免双写）；检测失败不静默——报告照常产出但往 `errors` 追加原因。它与存储层 `anomalies` 表（`record_anomaly` 持久化的历史异常事件）是两回事。

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
| `CRITIC_MAX_REVISIONS` | 2 | Critic 打回重写最大轮次（同时是 `research_more` 回环轮次上限） |
| `GAP_MAX_STEPS` | 3 | research_more 回环轮最多代码级补齐几个 Critic 点名的缺口工具 |
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
  → Evidence Gate: 代码级质量检查 → gate 结果（pass/fail；has_evidence=False 时 Reasoning 降级为 confidence=low，不短路）
  → Reasoning: LLM 汇总 evidence + findings → MarketIntelligence report
  → Critic: LLM 审计 report vs evidence → Critique
    → pass: END
    → revise: 回 Reasoning 重写（≤ N 轮）
    → research_more: 回 Supervisor 补充研究（≤ `critic_max_revisions` 次，默认 2；回环轮读到 missing_points / missing_tool_keys 与已执行工具清单，代码层补齐缺口工具、丢弃重复步骤）
    → error（仅节点内部产生，T11）: 审计自身失败 / verdict 无法识别 → 写 errors 并安全终止
  → anomalies: 代码用 detect_anomalies 覆盖 report["anomalies"]（D3，不依赖模型输出）
  → 落库：research 记录 + daily_state 快照
```

## 6. 存储

| 数据 | 位置 | 格式 |
|------|------|------|
| Market Memory | `memory/memory.db` | SQLite，四张表：`daily_states` / `anomalies` / `research_records` / `conversations` |
| 晨间简报 | `memory/morning/YYYY-MM-DD.json` | JSON |
| 晚间简报 | `memory/evening/YYYY-MM-DD.json` | JSON |
| 工具缓存 | 内存（TTL + LRU，上限 512 条） | `market_cache` |

> 早期版本把记忆写进 `~/.mosaic/memory/{daily,anomalies,research}/` 三个目录，
> **P5 已迁到项目内单个 SQLite 文件**；代码与本文档均以 `memory/memory.db` 为准。
> 注意区分两个 anomalies：报告响应里的 `anomalies` 字段是 `detect_anomalies` 每次现算的
> （D3，代码填充），`anomalies` 表才是被 `record_anomaly` 持久化的历史异常事件。

## 7. MCP 与 HTTP API

默认走 MCP：

```bash
iiix login
iiix plugin install market-gateway
iiix plugin serve market-gateway
```

> ⚠️ iiix CLI ≥ 0.8.0：`iiix mcp` 子命令在 0.8.0 已被删除，改用
> `iiix plugin login | list | status | verify | serve`。旧语法会得到
> `iiix: MCP 已停用: 服务器目录已移除或当前账号无权使用`。
> 自检命令：`iiix plugin verify market-gateway`（`status: passed` 表示登录态与上游都正常）。

Mosaic 不重新实现 Market Gateway，也不把 API Key 写进代码。

MCP 客户端依赖 `mcp>=2,<3`（D1，已与 `pyproject.toml` / `requirements.txt` 对齐）；
`pip install -e ".[dev]"` 会装上该区间内的版本。

HTTP API 可以作为备选数据通道（`MARKET_GATEWAY_MODE=http`）。

内部工具（`http_method="INTERNAL"`）绕过 Gateway 直接调用本地函数：
- `internal_hk_northbound` — 港股通北向资金（东财直连）
- `internal_hk_index` — 恒生指数快照（东财直连）
- `news_search` — DDGS 新闻搜索
