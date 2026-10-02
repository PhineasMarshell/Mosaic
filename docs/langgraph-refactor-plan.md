# LangGraph Multi-Agent 重构方案

> 状态：P3 已完成，328 测试全绿（2 skipped）
>
> ⚠️ **2026-10-01 复盘修订**：P2 实际半完成（节点化 + Critic 边已做，但 SSE 切图未做，流式/非流式路径已分叉）；kernel 与三 analyst 无条件并行导致双倍 LLM 开销。新增 **P2.5 补平阶段**（见 §6），P4 依赖评论 MCP 就绪状态待确认。详见 §8 复盘记录。

> 前置条件：评论爬取 MCP（雪球 / 富途牛牛 / 同花顺评论区）即将接入

> 核心策略：绞杀者模式（Strangler）——先立图骨架包住现有 MarketDetective，逐节点替换，任何阶段 /api/ask 契约与现有测试保持绿色

---

## 1. 目标架构

```

用户问题

   │

   ▼

┌──────────────┐

│  Supervisor   │  LLM 路由：解析意图 + 选 analyst + 分配工具预算

└──────┬───────┘

       │ Send() 扇出（并行）

       ▼

┌─────────┬─────────┬─────────┬─────────┬─────────┐

│ 技术面   │ 基本面   │ 资金面   │ 舆情情绪 │ 新闻事件 │   各自持有 tool 子集，

│ analyst │ analyst │ analyst │ analyst │ analyst │   只写证据账本，不下结论

└────┬────┴────┬────┴────┬────┴────┬────┴────┬────┘

     └─────────┴─────────┴────┬────┴─────────┘

                    ▼

          ┌──────────────────┐

          │  Evidence Gate    │  纯代码检查（复用现有）

          └────────┬─────────┘

                   ▼

          ┌──────────────────┐

          │    Reasoning     │  汇总证据 → MarketIntelligence

          └────────┬─────────┘

                   ▼

          ┌──────────────────┐

          │     Critic       │  结论 vs 证据审计

          └────────┬─────────┘

             pass  │  fail（≤N 轮）

              ▼    └──────► 回 Reasoning（或回 Supervisor 补一轮研究）

           END

```

关键设计约定：

\1. 证据账本是唯一契约。四个 analyst 只往 state.evidence 追加 Evidence 条目（来源工具、指标、数值、时间戳），不写结论；结论由 Reasoning 统一产出。这直接复用 app/models/evidence.py + app/research/evidence.py 的现有语义。

\2. 节点内保留原生 AsyncOpenAI 调用，不迁 LangChain message 格式。LangGraph 节点只是普通 async 函数，planner.py / evaluator.py / reasoning.py 里的 OpenAI 客户端、llm_json.parse_json_object、围栏剥离逻辑全部原样复用——避免为迁框架重写三套 prompt 解析。

\3. 并行写状态必须用 reducer。四个 analyst 并发追加 evidence / 扣减 tool_budget，这两个字段必须声明 Annotated[list[Evidence], operator.add] / 自定义 merge reducer，否则并行节点互相覆盖（LangGraph 最常见的坑）。

\4. 域（domain）与角色（analyst）正交。四个 analyst 是域无关的角色；Supervisor 根据问题域决定启用哪些 analyst（crypto 问题时基本面/舆情 analyst 无工具可用即跳过）。

## 2. Agent 一览

| Agent | 输入 | 输出（写入 state） | 主要工具（现有 category） | 对应现有代码 |

|---|---|---|---|---|

| Supervisor | question + 对话历史 | intent、route（选中 analyst 列表 + 各自预算） | 无（只看 tool_registry 元数据） | planner.py |

| 技术面 analyst | question + 分配的 ToolCallPlan | evidence 追加 | K 线、盘口、涨停池/题材、市场情绪、异动归因、anomaly detector | market_detective.py 工具循环 |

| 基本面 analyst | 同上 | evidence 追加 | F10：finance / business / concept / shareholders / survey | 同上 |

| 资金面 analyst | 同上 | evidence 追加 | 两融、龙虎榜、detail（解禁/股东户数）、Hyperliquid 持仓、爆仓统计 | 同上 |

| 舆情情绪 analyst | 同上 | evidence 追加 + sentiment_digest | 评论 MCP（新）：雪球/富途/同花顺评论区 | 无（新建） |

| 新闻事件 analyst | 同上 | evidence 追加 | web_search（DDGS，通用兜底，新） + 定向新闻源（金十/东财快讯，新）；宏观日历（新）；公司公告（新） | 无（新建） |

| Reasoning | 全量 evidence + 各 analyst 的 digest | report: MarketIntelligence | 无 | reasoning.py |

| Critic | report + evidence | critique: Critique（pass / fail + 缺口清单） | 无 | evaluator.py 改造 |

非 LLM 节点（图中普通节点）：

| 节点 | 职责 | 对应现有代码 |

|---|---|---|

| Evidence Gate | 代码级证据检查（去重、覆盖率、时新性） | evidence_gate.py（原样） |

| Tool Runtime | 实际执行工具调用：MCP/HTTP、重试、超时、缓存、normalizer | 从 market_detective.py 抽取 |

| 情绪管线（代码部分） | 评论区去重、按标的/时间窗聚合、热度统计 | 新建 |

舆情与新闻的分工：舆情 agent 看大众怎么看（评论区情绪），新闻事件 agent 看发生了什么（新闻/公告/宏观数据）。两者的交叉验证本身就是信号——消息面利好而评论区恐慌，或反过来，都值得 Reasoning 显式指出。

## 3. State Schema（app/graph/state.py，新建）

```python

from typing import Annotated, Literal, Optional

from operator import add

from pydantic import BaseModel

class ResearchState(BaseModel):

    # ── 输入 ──

    question: str

    conversation_id: Optional[str] = None

    domain: Optional[MarketDomain] = None        # 用户显式指定时跳过 Supervisor 判域

    # ── Supervisor 产出 ──

    intent: Optional[ResearchIntent] = None

    route: list[AnalystAssignment] = []          # [{analyst, tool_calls: [ToolCallPlan], budget}]

    # ── 并行写入字段（必须 reducer）──

    evidence: Annotated[list[Evidence], add] = []

    tool_budget_used: Annotated[int, add] = 0    # 全局 max_tool_calls 原子扣减

    # ── 各 analyst 的执行痕迹 ──

    findings: Annotated[list[AnalystFinding], add] = []   # 每个 analyst 一条 digest

    sentiment_digest: Optional[SentimentDigest] = None

    # ── 汇总阶段 ──

    gate: Optional[EvidenceGateResult] = None

    report: Optional[MarketIntelligence] = None

    critique: Optional[Critique] = None

    revision_count: int = 0

    # ── 运行时控制 ──

    errors: Annotated[list[str], add] = []       # 单 analyst 失败不炸整图

```

新增模型（app/models/research.py 扩展）：

```python

AnalystName = Literal["technical", "fundamental", "moneyflow", "sentiment", "news"]

class AnalystAssignment(BaseModel):

    analyst: AnalystName

    tool_calls: list[ToolCallPlan]

    budget: int          # 本轮该 analyst 的工具调用上限

class AnalystFinding(BaseModel):

    analyst: AnalystName

    digest: str           # ≤200 字的中间结论，供 Reasoning 引用

    tools_used: list[str]

    failed: bool = False

class Critique(BaseModel):

    verdict: Literal["pass", "revise", "research_more"]

    missing_points: list[str]        # 证据缺口（research_more 时喂回 Supervisor）

    unsupported_claims: list[str]    # 报告里没有证据支撑的表述（revise 时喂回 Reasoning）

```

## 4. 文件级修改表

### 4.1 新增

| 文件 | 职责 | 备注 |

|---|---|---|

| app/graph/__init__.py | 导出 build_graph() | |

| app/graph/state.py | ResearchState（见上） | 所有节点的唯一契约 |

| app/graph/builder.py | build_graph(settings) → CompiledGraph：注册节点、Send 扇出、Critic 条件边、recursion_limit | 图结构的唯一真相源 |

| app/graph/tool_runtime.py | 从 market_detective.py 抽出的工具执行层：execute_tool_calls(calls, budget, settings) -> list[ToolResult]，含重试/超时/缓存/normalizer/HK 北向兜底 | 本次重构最大的一块纯搬运，逻辑尽量不动 |

| app/graph/nodes/supervisor.py | LLM 路由节点：复用 PLANNER_PROMPT 改写为输出 route；复用 llm_json 解析 | 吸收 planner.py |

| app/graph/nodes/analysts/technical.py | 技术面节点：拿 AnalystAssignment → tool_runtime 执行 → build_evidence → 追加 evidence + finding | P3 才拆出 |

| app/graph/nodes/analysts/fundamental.py | 同上，F10 类工具 | P3 |

| app/graph/nodes/analysts/moneyflow.py | 同上，两融/龙虎榜/筹码类 | P3 |

| app/graph/nodes/analysts/sentiment.py | 舆情节点：调 sentiment.pipeline → 写 sentiment_digest + evidence | P4（评论 MCP 就绪后） |

| app/graph/nodes/analysts/news.py | 新闻事件节点：web_search + 定向新闻源 → evidence；宏观日历/公告归此节点 | P4 |

| app/graph/nodes/analysts/base.py | analyst 通用骨架：预算守卫、异常捕获降级为 failed=True finding | 四个 analyst 共用 |

| app/graph/nodes/reasoning.py | 包装 ReasoningEngine.generate() 为节点；Critic 打回时把 unsupported_claims 注入 prompt 重写 | |

| app/graph/nodes/critic.py | 包装 evaluator 为 Critic 节点 | 吸收 evaluator.py |

| app/graph/nodes/gate.py | 包装 run_evidence_gate 为节点 | 纯搬运 |

| app/sentiment/pipeline.py | 评论区数据管线：代码层（去重、按标的+时间窗聚合、发帖量增速、多空关键词词频、水军/复读过滤）→ LLM 层（对聚合摘要做情绪打分 + 观点归纳，产出 SentimentDigest） | 评论区噪音大，LLM 只看聚合后摘要 |

| app/news/search.py | 通用网络检索封装：DDGS（ddgs 库）+ 结果缓存（TTL 数小时，缓解限流）+ 失败降级（返回空结果 + errors 追加，不炸图） | 网格内唯一非 gateway MCP 的工具通道；后续定向新闻源就绪后可整体迁入 gateway |

| app/agent/prompts_graph.py | Supervisor / Critic / 四个 analyst 的新 prompt（prompts.py 保持不动供旧路径兼容，稳定后再删） | |

| tests/test_graph_state.py | reducer 并行写、Pydantic 校验 | |

| tests/test_graph_routing.py | Supervisor 路由决策（mock LLM） | |

| tests/test_graph_e2e.py | 全图 mock-LLM 端到端：/api/ask 返回结构不变 | |

| tests/test_sentiment_pipeline.py | 清洗聚合逻辑（纯代码，无 LLM） | |

| tests/test_news_search.py | DDGS 封装的缓存命中/失败降级（mock 检索，不打真网） | |

### 4.2 修改

| 文件 | 改动 | 幅度 |

|---|---|---|

| app/agent/orchestrator.py | run() 内部改为 await graph.ainvoke(state)；公开签名（question/domain/conversation_id → ResearchResponse）不变——main.py 与现有测试零改动 | 小 |

| app/models/research.py | 追加 AnalystName / AnalystAssignment / AnalystFinding / Critique；ResearchPlan 保留（Supervisor 过渡期复用其解析） | 小 |

| app/gateway/tool_registry.py | ① ToolMeta.__slots__ 加 category: Literal["technical","fundamental","moneyflow","sentiment","news","shared"]，33 个工具逐一标注；② 新增评论 MCP + 新闻/事件类工具条目（见 §5）；③ 新增 tools_by_category() 辅助函数 | 中 |

| app/agent/evaluator.py | 输出模型扩展出 Critique；evaluate() 增加 report 入参以对比"结论 vs 证据"；原 ResearchDecision 逻辑保留为 research_more 分支 | 中 |

| app/main.py | _stream_research 从固定 5 phase 改为 graph.astream(state, stream_mode="updates")，每个节点完成推一条 progress 事件（事件名 = 节点名）；/api/ask 非流式路径不动 | 中 |

| app/config.py | 新增：critic_max_revisions: int = 2、graph_recursion_limit: int = 25、sentiment_enabled: bool = False（MCP 接入后置 true）、sentiment_max_comments: int = 500、news_enabled: bool = False、news_search_ttl_seconds: int = 21600（6h，缓解 DDGS 限流） | 小 |

| app/scheduler/briefs.py | generate_morning/evening_brief 改为以固定模板 question 调 graph.ainvoke（thread_id=brief 日期，享受 checkpointer）；对外返回 dict 结构不变 | 中 |

| app/memory/storage.py | 增加 get_conversation_history(thread_id)（已有 conversations 表，补读接口给 Supervisor 注入）；业务记忆库与 LangGraph checkpoint 库分离（后者用官方 sqlite saver，独立 db 文件） | 小 |

| app/web/index.html | 进度区从 5 步线性条改为 analyst 卡片（4 张卡 + 汇总卡），SSE 事件驱动状态 | 中 |

| requirements.txt / pyproject.toml | 追加 langgraph>=0.4、langgraph-checkpoint-sqlite>=2.0（不需要 langchain-openai：节点内仍是原生 OpenAI 客户端） | 小 |

| README.md / docs/architecture.md | 重构完成后更新架构说明 | P5 收尾 |

### 4.3 拆解 / 退役

| 文件 | 处置 |

|---|---|

| app/research/market_detective.py（701 行） | 工具执行循环 → graph/tool_runtime.py；HK 兜底 → tool_runtime；investigate() 本体在 P3 后只作为 P0 绞杀者过渡期的整图兜底节点保留，P5 删除 |

| app/agent/planner.py | 逻辑并入 nodes/supervisor.py 后删除（ResearchPlan 模型保留复用） |

| app/agent/evaluator.py | 并入 nodes/critic.py 后删除（保留模型定义在 models/research.py） |

### 4.4 完全不动

app/gateway/{mcp_client,http_client,normalizer,stock_codes}.py、app/cache.py、app/llm_json.py、app/errors.py、app/detector/anomaly.py、app/research/{evidence,hk_northbound}.py、app/models/{market,evidence,response}.py、app/logging_config.py、app/memory/storage.py 既有写入逻辑。

> Gateway 层是这次重构最大的资产：33 个工具的注册/规范化/缓存全部可复用，重构只动"谁来决定调哪些工具、结果如何汇总"这一层。

## 5. 新工具接入（P4）

### 5.1 评论 MCP（舆情 analyst）

在 tool_registry.py 新增 sentiment category 条目（工具名以实际 MCP 为准）：

```python

ToolMeta(

    "xq_comments",      "comments_xueqiu_get",       "雪球个股评论区抓取",   category="sentiment", domain="a_share", ...

),

ToolMeta(

    "futu_comments",    "comments_futu_get",          "富途牛牛个股评论区",   category="sentiment", domain="hk_stock", ...

),

ToolMeta(

    "ths_comments",     "comments_ths_get",           "同花顺个股/板块评论区", category="sentiment", domain="a_share", ...

),

```

情绪管线（sentiment/pipeline.py）分层：

\1. 采集（代码）：按标的拉取 sentiment_max_comments 条评论。

\2. 清洗（代码）：去重（相似度哈希）、过滤复读/纯表情/疑似水军（同一用户高频 + 内容模板化）、按时间窗（当日/3日/7日）聚合。

\3. 统计（代码）：发帖量及增速、多空关键词词频、活跃标的排行。

\4. 打分（LLM，唯一一次模型调用）：输入是第 3 步的聚合摘要（而非原始评论），输出 SentimentDigest：{score: -100..100, stance: 散户观点归纳, extremes: 极端言论摘录, shift: 与前期对比}。

\5. Digest 同时写入 sentiment_digest 和一条 Evidence（metric="sentiment.score"，注明样本量与时间窗），让 Reasoning 像对待其它证据一样对待它。

### 5.2 新闻/事件工具（news analyst）

分两批接入，注册均为 category="news"：

第一批（P4 随图上线）——通用检索兜底：

> **2026-10-01 修正**：DDGS 检索实际已经落地——`app/research/news_search.py`（search_news / extract_news_entries）+ tool_registry 中的 `news_search` INTERNAL 工具（现 category="shared"）。因此 §4.1 中"新建 app/news/search.py"**取消**，P4 改为：category 改 "news" + 补长 TTL 缓存 + news analyst 节点（详见 §9.9-§9.11）。

```python

ToolMeta(

    "web_search",       "web_search_ddgs_get",        "通用网络检索（新闻/事件兜底，跨域）",

    category="news", domain="unknown", priority="medium", ...

),

```

- 实现在 app/news/search.py：DDGS 库直连 + gateway 缓存层同款 TTL 策略（搜索结果缓存数小时）。

- 这是全系统唯一不走 market-gateway 的工具，tool_runtime.py 需为其留本地执行分支（不走 MCP/HTTP client，直接调 Python 函数）。

- 覆盖所有 domain（"BTC ETF 流入" 与 "A股 证监会" 都能搜），这是定向新闻源做不到的。

第二批（后续逐步添加）——定向新闻源 + 结构化数据，全部进 gateway：

| 工具 | 内容 | 价值 | 优先级 |

|---|---|---|---|

| news_flash | 金十/东财财经快讯滚动流 | 中文财经快讯时效与质量远超 DDGS，可规范化为带时间戳的 NormalizedDatum | ★★★ |

| macro_calendar | 宏观日历（CPI/PMI/利率决议/美联储） | 填补 MarketDomain="macro" 空域；解释市场状态的核心证据 | ★★★ |

| announcements | 公司公告（巨潮/东财：减持、回购、业绩预告） | company_research 任务当前只能靠 F10 静态数据 | ★★ |

| sector_flow | 行业板块资金流向（东财） | 补两融/龙虎榜之外的资金轮动视角 | ★★ |

| option_vix / fear_greed | 期权隐波/VIX、crypto 恐贪指数 | 波动率视角，锦上添花 | ★ |

每批的接入模式一致：ToolMeta 注册 → normalizer 规范化 → Supervisor 自动可见（无需改图结构）。这正是 §1 证据账本架构的回报：新增工具只动 registry，不动图。

## 6. 分阶段实施（每阶段验收：现有测试全绿 + /api/ask 契约不变）

| 阶段 | 内容 | 验收 |

|---|---|---|

| P0 骨架 ✅ | 装依赖；state.py + builder.py；单节点图整包调用现有 MarketDetective.investigate()（绞杀者）；orchestrator.run 切到图。Bug 修复：kernel 补写 results 字段供 gate/reasoning 消费；删除 dead code _critic_should_revise | 行为零变化，235+ 测试全绿 |

| P1 执行层抽取 ✅ | tool_runtime.py 从 market_detective 抽出（含测试搬迁）；Supervisor 节点上线（复用 planner prompt + 解析）；图变成 supervisor → market_detective 内核 → reasoning | 路由决策有单测（test_graph_routing.py, 6 tests）；端到端对比 P0 输出一致 |

| P2 Critic 闭环 ⚠️ | gate / reasoning / critic 节点化 ✅；Critic 条件边（revise ≤ critic_max_revisions；research_more 时带 missing_points 回 Supervisor，最多 1 次）✅；**SSE 改 astream(stream_mode="updates") 未做**（main.py `_stream_research` 仍直接调 `market_detective.investigate`） | 节点化 + 打回重写路径有测试；**前端逐节点进度 / 流式享 critic 闭环 未达成 → 移交 P2.5** |

| P2.5 补平（复盘新增，共 7 个任务，详见 §9） | P2.5-0 Critic dict 属性 bug 修复 → P2.5-1 recursion_limit 接线 → P2.5-3 Supervisor 产出 route + analyst 消费 → P2.5-2 kernel 条件兜底 → P2.5-4 reasoning 消费 findings → P2.5-5 Evidence 对齐 → P2.5-6 SSE 切图 + 前端 analyst 卡片 | 每个任务有独立验收标准，见 §9 |

| P3 Analyst 拆分 ✅ | 工具 registry 加 category 标注（39 个工具，by_category 索引）；base.py 骨架（预算守卫、失败降级、finding digest）；拆为 technical/fundamental/moneyflow 三节点，builder 中 add_edge 并行扇出；AnalystName 扩展为 Literal["kernel","technical","fundamental","moneyflow"] | 三节点并行有测试（test_graph_topology.py + test_graph_nodes.py, 14 tests）；单 analyst 超时/失败整图仍出报告 |

| P4 舆情 + 新闻接入（共 5 个任务，详见 §9） | P4-1 config 开关 → P4-2 registry 调整 → P4-3 news_search 补缓存 → P4-4 news analyst 节点 → P4-5 sentiment 管线（阻塞于评论 MCP） | P4-1..4 不依赖 MCP；两开关全关 = P2.5 行为 |

| P5 收尾（共 4 个任务，详见 §9） | P5-1 briefs 走图 → P5-2 删旧路径（market_detective / planner / evaluator / kernel）→ P5-3 文档 → P5-4 全量回归 | 死代码清零；brief 生成路径有 e2e 测试 |

| 持续（P5 后） | 按 §5.2 第二批清单逐步添加定向新闻源/宏观日历/公告等 gateway 工具 | 每次只动 tool_registry + normalizer，图结构不变 |

**任务依赖总图**：

```
P2.5-0 ─┐
P2.5-1 ─┤（独立，可并行）
        ├─► P2.5-3 ─► P2.5-2 ─► P2.5-6
P2.5-4 ─┤（独立）
P2.5-5 ─┘（独立）
P2.5 全部完成 ─► P4-1 ─► P4-2 ─► P4-3 ─► P4-4 ─►（等评论 MCP）─► P4-5
P4-1..4 完成 ─► P5-1 ─► P5-2 ─► P5-3 ─► P5-4
```

## 7. 风险与注意事项

| 风险 | 对策 |

|---|---|

| 并行节点覆盖共享 state | evidence / tool_budget_used / findings / errors 全部声明 reducer；P3 专项测试 |

| token 成本上涨（每 analyst 一份 prompt + 每轮 Supervisor 路由） | analyst prompt 精简（只描述自己 category 的工具）；Supervisor 复用 domain 判断结果缓存；findings digest 限 200 字 |

| thinking 模型空 content（qwen3.7 截断） | 所有节点 LLM 输出一律走 llm_json.parse_json_object，失败抛 LLMOutputError → 502，不静默兜底（沿用 planner 已修复的模式） |

| 总预算超时 | 节点包装 asyncio.wait_for（单节点 ≤ research_timeout_seconds）；图配置 recursion_limit；SSE 心跳逻辑保留 |

| 单 analyst 失败拖垮整图 | base.py 捕获一切异常 → failed=True finding + errors 追加，Reasoning 在报告中标注数据缺口 |

| max_tool_calls 全局预算在并行下被超额 | budget 拆分为 per-analyst 配额（Supervisor 分配时已定），各节点本地守卫，不再全局抢 |

| Windows + MCP 子进程 | 现有 mcp_client 已在 Windows 跑通，tool_runtime 原样搬运不引入新假设 |

| DDGS 限流/不稳定（非官方接口，库历史上有改名与 breaking change） | 搜索结果走长 TTL 缓存；失败降级为空结果 + errors 追加，不炸图；后续定向新闻源（§5.2 第二批）就绪后逐步替代其主力地位，DDGS 退为兜底 |

## 8. 复盘记录（2026-10-01）

对照代码核实进度后的发现与调整。结论：P0 / P1 / P3 落实到位，P2 半完成，P4 未动工。

### 8.1 已核实符合计划

- P0–P3 文件结构齐全：`app/graph/` 下 state / builder / tool_runtime / supervisor / kernel / gate / reasoning / critic / 三 analyst + base 均在
- `orchestrator.run()` 已切到 `graph.ainvoke`，公开签名不变（main.py 与旧测试零改动）
- Critic 条件边（revise ≤ N / research_more 回 supervisor）已在 builder 接好
- config 已有 `critic_max_revisions` / `graph_recursion_limit`

### 8.2 偏离点与处置

| # | 偏离 | 位置 | 处置 |
|---|---|---|---|
| 1 | P2 的 SSE 未切图：`_stream_research` 仍直接 `market_detective.investigate`，流式走旧单体、非流式走图，**两条路径行为分叉**，前端 analyst 卡片无数据源，流式请求享受不到 Critic 闭环 | main.py `_stream_research` | 移交 P2.5 ① |
| 2 | kernel 与三 analyst **无条件并行**：kernel 整包 `investigate()` 内部又跑一遍 planner LLM + 工具循环，叠加三 analyst 盲扫 → 每请求 ≥4 份规划开销，§7 "token 成本上涨" 对策未落实；与 §4.3 "kernel 仅作兜底" 语义不符 | builder.py 四条 `add_edge` | 移交 P2.5 ②（conditional edge 条件兜底） |
| 3 | Supervisor 的 route 未被 analyst 消费：`_route_raw` 只被 `_extract_stocks` 抽 symbol，analyst 按 category 盲扫全表 + 预算=工具数，LLM 路由决策基本浪费；与 §2 "analyst 输入 = question + 分配的 ToolCallPlan" 不符 | base.py `_execute_tools` | 移交 P2.5 ④（接上 §3 已定义的 `AnalystAssignment`） |
| 4 | `graph_recursion_limit=25` 定义未接线：`ainvoke` 未传 `config`，compile 也未设 | orchestrator / builder | 移交 P2.5 ③ |
| 5 | reasoning 只取 question + results + evidence，**不消费 findings digest**（analyst 写的 digest 成死数据）；evidence 实为裸 `NormalizedDatum`，非 §1 约定的 `Evidence` 条目（缺来源工具/时间戳），gate 覆盖率口径偏松 | reasoning.py / base.py | 移交 P2.5 ⑤⑥ |
| 6 | P4 前置未就绪：`app/sentiment/`、`app/news/` 不存在，config 无 `sentiment_enabled`/`news_enabled`，评论爬取 MCP 就绪状态待确认。**修正**：DDGS 新闻搜索实际已存在（`app/research/news_search.py` + registry 内 `news_search` INTERNAL 工具，category="shared"），P4 无需新建 `app/news/search.py`，改为复用 + 换 category + 补缓存 | — | 见 8.3 |
| 7 | **Critic 节点恒降级（2026-10-01 二轮复盘新发现）**：`critic.py` 开头把 state `model_dump()` 成 dict 后，仍用 `state.domain` / `state.question` 属性访问（dict 无属性）→ AttributeError → except 分支 → **每次都返回降级的 research_more**，审计从未真正执行，还触发满轮次回环 | critic.py L145-155 | P2.5-0 修复 |

### 8.3 P4 前置与推进建议

- **评论爬取 MCP**（雪球/富途/同花顺评论区）：状态待确认。未就绪则 P4 拆两步——先落 config 两开关（默认关）+ registry `sentiment`/`news` category 占位 + `app/news/search.py`（DDGS，不依赖 MCP）；评论管线（`app/sentiment/pipeline.py` + sentiment analyst）等 MCP 就绪再补。
- **P2.5 内部优先级**：SSE 切图 > kernel 条件化 > route 消费 > recursion_limit（一行）。SSE 是对外契约一致性，优先；kernel 条件化同时解决 #2 成本问题；route 消费是 §1 证据账本架构回报兑现的前提。
- 完成 P2.5 后建议重跑全量测试并回填 §6 验收列的"328 全绿"基线。

---

## 9. 详细实施计划（面向执行模型）

> 本章节把 P2.5 / P4 / P5 的每个任务展开为可直接照做的步骤：目标文件、精确改动、代码骨架、验收方式、易踩的坑。
> 执行前请先跑一次 `python -m pytest tests/ -q` 确认基线全绿；每个任务完成后都要再跑一次，红了就停下排查。
> 公共约定：所有节点函数第一行都要兼容 Pydantic 与 dict 两种 state（现有节点已有此模式，照抄即可）；所有新增字段写入都要走已有节点返回字典的方式，不要在节点里直接改 state 对象。

### 9.0 动手前的代码事实（执行模型必读）

执行任何任务前，先确认以下事实与你读到的一致，不一致就停下来报告：

1. `app/graph/state.py`：`ResearchState` 是 Pydantic BaseModel；`results` / `tool_results` / `evidence` / `findings` / `errors` 带 `operator.add` reducer；**没有 `route` 字段**；`AnalystName = Literal["kernel","technical","fundamental","moneyflow"]`。
2. `app/graph/builder.py`：supervisor 之后是 4 条无条件 `add_edge`（kernel / technical / fundamental / moneyflow），四节点各自 `add_edge` 到 gate；`_critic_route` 已实现条件边。
3. `app/graph/nodes/supervisor.py`：`_build_route` 当前返回的是把全部 steps 塞进一个字典的临时结构，写入 key 是 `_route_raw`（**该 key 不在 ResearchState schema 里，LangGraph 会丢弃它**——这是 route 从未被消费的原因之一）。
4. `app/graph/nodes/critic.py`：`__call__` 开头 `state = state.model_dump(...)` 之后，L145-147 用 `state.domain`、L155 用 `state.question`（dict 上属性访问必炸 → 恒走 except → 恒返回降级 research_more）。
5. `app/main.py`：`_stream_research`（L242）仍直接 `_get_orchestrator().market_detective.investigate(...)`；心跳机制是 `asyncio.wait_for(asyncio.shield(task), timeout=min(heartbeat, remaining))` 循环；结束后的落库走 `_save_turn` / `_save_research_and_state` 两个后台 task。
6. `app/research/news_search.py`：DDGS 封装已存在（`search_news` 异步 + `extract_news_entries`）；`app/graph/tool_runtime.py::_execute_internal` 已有 `news_search` 分支；registry 中 `news_search` 是 `http_method="INTERNAL"`、`category="shared"`。
7. `app/research/evidence.py` 已有 `build_evidence(results: list[ToolResult]) -> list[Evidence]`；`app/research/reasoning.py::ReasoningEngine.reason(question, results, evidence, history_context="")` 内部对 evidence 逐条 `.model_dump()`（所以 evidence 里放裸 dict 会炸，必须是 Pydantic 对象）。
8. `app/web/index.html`：SSE 消费在 `runSSE()`；进度条是 `addAILoader` 里写死的 4 步列表 + `updateLoader(index, text)`；`ask()` 里当前是 `runSSE(q, (data) => updateLoader(0, data.message || '调查中…'), ...)`（index 恒为 0）。
9. 测试惯例（照抄现有 fixture 模式）：`tests/test_graph_e2e.py` 用 `monkeypatch.setattr(ToolRuntime, "execute", fake_execute)` mock 工具，用替换 `Node.__init__` 的方式 mock `client.chat.completions.create` 返回固定 JSON。

### 9.1 P2.5-0：修复 Critic 节点 dict 属性访问 bug

**文件**：`app/graph/nodes/critic.py`、`tests/test_graph_nodes.py`

1. 在 `CriticNode.__call__` 中，把所有 dict 化之后的属性访问改为 `.get()`：
   - `state.domain` → `state.get("domain")`
   - `state.question` → `state.get("question", "")`
   - L145-149 的 domain 解析块整体替换为：

   ```python
   intent = state.get("intent")
   domain = "a_share"
   if intent is not None:
       domain = getattr(intent, "domain", None) or state.get("domain") or "a_share"
   elif state.get("domain"):
       domain = state.get("domain")
   ```
   （注意 intent 经 reducer/序列化后可能是 dict：`getattr(dict, "domain", None)` 返回 None 没关系，会落到 `state.get("domain")` 兜底。）
2. 检查同函数内其余 `state.xxx` 用法（如 `state.get("report")` 已是 get 则不动）。
3. **测试**：在 `tests/test_graph_nodes.py` 增加用例：构造一个带最小 `report`（可用 `types.SimpleNamespace` 或真实 `MarketIntelligence`）、`results=[]`、`gate=None` 的 dict state，mock CriticNode 的 client 返回 `{"verdict":"pass","reason":"ok"}`，断言 `critique.verdict == "pass"`（修复前该用例会得到 `research_more`）。

**验收**：新测试通过；`pytest tests/test_graph_nodes.py tests/test_graph_e2e.py -q` 全绿。
**坑**：不要顺手重构 Critic 的 prompt 拼装；本任务只修访问方式。

### 9.2 P2.5-1：接线 recursion_limit

**文件**：`app/agent/orchestrator.py`

1. `orchestrator.py` 的 `run()` 中：

   ```python
   result_state = await graph.ainvoke(
       state,
       config={"recursion_limit": self.settings.graph_recursion_limit},
   )
   ```
2. 不改 config.py（`graph_recursion_limit: int = 25` 已存在）。

**验收**：现有测试全绿。预算核算：最坏回环 = (supervisor + 4 执行 + gate + reasoning + critic) × (1 + critic_max_revisions) ≈ 18 < 25，无需调参。

### 9.3 P2.5-3：Supervisor 产出结构化 route（先于 P2.5-2 做）

**文件**：`app/models/research.py`、`app/graph/state.py`、`app/graph/nodes/supervisor.py`、`app/graph/nodes/analysts/base.py`、`tests/test_graph_routing.py`

**第 1 步 — 模型**（`app/models/research.py` 末尾追加）：

```python
class AnalystAssignment(BaseModel):
    """Supervisor 给某个 analyst 的工具调用分配。"""
    analyst: str                                # technical / fundamental / moneyflow / news / sentiment
    tool_calls: list[ToolCallPlan] = Field(default_factory=list)
    budget: int = 0                             # 本 analyst 本轮工具调用上限
```

**第 2 步 — state**（`app/graph/state.py`）：

- `AnalystName` 扩展为 `Literal["kernel","technical","fundamental","moneyflow","news","sentiment"]`（P4 提前占位）。
- `ResearchState` 增加字段（放在 "Supervisor 产出" 区块，**不加 reducer**——research_more 回环时 supervisor 需要整体覆盖它）：

```python
#: Supervisor 产出的按 analyst 分组的工具分配（P2.5-3）
route: list = []   # list[AnalystAssignment]
```

**第 3 步 — supervisor 分组**（`app/graph/nodes/supervisor.py`）：

- 删除 `_route_raw` 相关代码。`__call__` 返回改为：

```python
return {
    "intent": plan.intent,
    "route": self._build_route(plan),
}
```

- `_build_route` 重写为按工具 category 分组：

```python
@staticmethod
def _build_route(plan: ResearchPlan) -> list:
    """将 ResearchPlan.steps 按工具 category 分组为 AnalystAssignment 列表。"""
    from app.gateway.tool_registry import resolve_tool
    groups: dict[str, list] = {}
    for step in plan.steps:
        try:
            meta = resolve_tool(step.tool_key)
            cat = meta.category
        except Exception:
            cat = "technical"          # 无法解析的工具键兜底给技术面
        if cat not in ("technical", "fundamental", "moneyflow"):
            cat = "technical"          # shared/news 类暂归技术面（P4 后 news 独立）
        groups.setdefault(cat, []).append(step)
    return [
        {"analyst": cat, "tool_calls": [s.model_dump() for s in steps], "budget": len(steps)}
        for cat, steps in groups.items()
    ]
```

（route 元素用 dict 而非 Pydantic 对象，避免 LangGraph 序列化后下游 `isinstance` 判断分叉；analyst 节点按 dict 消费。）

**第 4 步 — analyst 消费 route**（`app/graph/nodes/analysts/base.py`）：

- `_execute_tools` 重写为"只执行分配给我的 tool_calls"，删除按 `by_category` 全表盲扫的逻辑（`budget = len(meta_list)` 那套整体删掉）：

```python
async def _execute_tools(self, state: dict, called_signatures: set[str]) -> list[ToolResult]:
    from app.gateway.tool_registry import resolve_tool
    route = state.get("route") or []
    mine = next((a for a in route if a.get("analyst") == self.category), None)
    if mine is None or not mine.get("tool_calls"):
        logger.info("%s: no assignment from supervisor, skip", self.category)
        return []

    stocks = self._extract_stocks(state)
    results: list[ToolResult] = []
    budget = int(mine.get("budget") or len(mine["tool_calls"]))

    for tc in mine["tool_calls"]:
        if budget <= 0:
            break
        budget -= 1
        tool_key = tc.get("tool_key", "")
        try:
            meta = resolve_tool(tool_key)
        except Exception:
            logger.warning("%s: unknown tool_key %s, skip", self.category, tool_key)
            continue
        arguments = dict(tc.get("arguments") or {})
        # symbol 守卫：非白名单工具且 planner 没给 symbol → 用问题里抽到的代码补，仍无则跳过
        if meta.tool_name not in self.WHITELIST_NO_SYMBOL and not arguments.get("symbol"):
            if stocks:
                arguments["symbol"] = ";".join(stocks)
            else:
                logger.debug("%s skipping %s (no symbol)", self.category, tool_key)
                continue
        results.append(await self._runtime.execute(meta.tool_name, arguments, called_signatures))
    return results
```

- `_extract_stocks` 中读取 `_route_raw` 的部分改为读取 `state.get("route")`：遍历每个 assignment 的 `tool_calls`，取 `arguments.symbol`（逻辑与原来一致，只是外层结构变了）。
- `_build_arguments` 在 route 模式下不再被调用，可保留但不再是主路径（不要删，P4 news/sentiment 节点可能覆写它）。

**第 5 步 — 测试**（`tests/test_graph_routing.py` 追加）：

- mock supervisor client 返回带 3 个 steps 的 plan JSON（tool_key 分别属于 technical / fundamental / moneyflow 三个 category——从 `app/gateway/tool_registry.py` 里挑真实的 key），调用 `SupervisorNode(...)(state)`，断言返回的 `route` 是 3 个 assignment、分组正确、各组 `budget == len(tool_calls)`。
- mock supervisor 返回空 steps → `route == []`。

**验收**：`pytest tests/test_graph_routing.py tests/test_graph_nodes.py -q` 全绿；`test_graph_e2e.py` 仍绿（其 mock plan 的 steps 为空 → route 空 → 行为不变）。
**坑**：此时 kernel 仍是无条件并行，route 生效但开销没降——这是预期，下一个任务解决。**不要在本任务动 builder。**

### 9.4 P2.5-2：kernel 改为条件兜底

**文件**：`app/graph/builder.py`、`tests/test_graph_topology.py`

1. 删除 `builder.add_edge("supervisor", "kernel")` / `technical` / `fundamental` / `moneyflow` 四条。
2. 增加条件扇出（返回节点名列表即并行扇出）：

```python
def _supervisor_fanout(state):
    if hasattr(state, "model_dump"):
        state = state.model_dump(exclude_none=False)
    route = state.get("route") or []
    assigned = {a.get("analyst") for a in route if isinstance(a, dict)}
    names = [n for n in ("technical", "fundamental", "moneyflow") if n in assigned]
    if not names:
        return ["kernel"]      # supervisor 没给出计划 → 整包兜底（旧行为）
    return names

builder.add_conditional_edges(
    "supervisor",
    _supervisor_fanout,
    {
        "kernel": "kernel",
        "technical": "technical",
        "fundamental": "fundamental",
        "moneyflow": "moneyflow",
    },
)
```

3. 四条执行节点 → gate 的边保持不变（LangGraph 会等实际执行过的分支都到 gate 才继续）。
4. **测试**（`tests/test_graph_topology.py` 追加，用 e2e 文件里的 mock 模式）：
   - mock plan steps 非空（至少 1 个 technical 工具）→ 执行后 `kernel` 节点的 `MarketDetective.investigate` 未被调用（可给 `KernelNode.__call__` 打点记录），`technical` 被执行；
   - mock plan steps 为空 → 只有 `kernel` 执行。

**验收**：上述 2 个拓扑测试通过；全量测试绿。
**坑**：`research_more` 回环会再次进入 supervisor → 重新扇出，这是期望行为（带着 missing_points 重新规划）。

### 9.5 P2.5-4：Reasoning 消费 findings digest

**文件**：`app/research/reasoning.py`、`app/graph/nodes/reasoning.py`、`tests/test_reasoning_parsing.py`（或新文件）

1. `ReasoningEngine.reason` 增加可选参数：

```python
async def reason(self, question, results, evidence, history_context: str = "",
                 findings: list | None = None) -> MarketIntelligence:
```

2. prompt 拼装前构造 findings 段（放在 `history_context` 之前拼接）：

```python
parts: list[str] = []
if findings:
    lines = ["ANALYST SUMMARIES（各分析员中间结论，仅供交叉参考）:"]
    for f in findings:
        analyst = f.get("analyst") if isinstance(f, dict) else getattr(f, "analyst", "?")
        digest = f.get("digest") if isinstance(f, dict) else getattr(f, "digest", "")
        failed = f.get("failed") if isinstance(f, dict) else getattr(f, "failed", False)
        if failed:
            lines.append(f"  - {analyst}: [失败] {digest}")
        elif digest:
            lines.append(f"  - {analyst}: {digest}")
    parts.append("\n".join(lines))
if history_context:
    parts.append(history_context)
combined_context = "\n\n".join(parts)
```

   然后把 `REASONING_PROMPT.format(..., history_context=combined_context)` 传入。
3. `ReasoningNode.__call__` 调用处补 `findings=state.get("findings", [])`。
4. **测试**：mock client，调用 `reason(..., findings=[{"analyst":"technical","digest":"执行了 3 个工具, 3 成功","failed":False}])`，从 mock 捕获的 prompt 断言包含 `"ANALYST SUMMARIES"` 与该 digest 文本。

**验收**：新测试通过；`test_reasoning_parsing.py` 原有用例不回归（不传 findings 时行为与现在完全一致）。
**坑**：不要改 `REASONING_PROMPT` 模板本身（`{history_context}` 占位符已存在）。

### 9.6 P2.5-5：Evidence 条目对齐 §1 约定

**文件**：`app/graph/nodes/analysts/base.py`、`app/graph/nodes/kernel.py`、`tests/test_graph_nodes.py`

背景：当前 analyst 写入 `evidence` 的是 `result.normalized`（NormalizedDatum），缺 §1 约定的来源工具/时间戳语义；且 `ReasoningEngine.reason` 对 evidence 逐条 `.model_dump()`，混入裸 dict 会炸。`app/research/evidence.py::build_evidence(results)` 已能把 `list[ToolResult]` 提炼为 `list[Evidence]`，直接复用。

1. `base.py __call__` 中，把 `evidence_items.extend(result.normalized)` 的循环逻辑改为：先收集全部 `results`，最后统一

```python
from app.research.evidence import build_evidence
evidence_items = build_evidence(results)
```

   （在 truncate 之后、return 之前；`results` 就是现有那个列表。）
2. `kernel.py` 的返回字典增加 `"evidence": build_evidence(tool_results)`（import 同上），让兜底路径也有同格式证据。
3. **测试**：用 fake runtime 返回带 `normalized` 的 ToolResult，跑 `TechnicalAnalystNode`，断言输出 `evidence` 非空且每个元素是 `Evidence` 实例（`isinstance(e, Evidence)`）、`source_tool` 等于工具名。

**验收**：新测试通过；`test_graph_e2e.py` 绿。
**坑**：`reasoning.py` 节点里的证据截断用 `_get_evidence_key(item)` 分组——先读一遍该函数确认对 Evidence 对象可用（它按 tool 属性分组，Evidence 有 `source_tool` 而无 `tool`，若 key 函数只认 `.tool` 需要加 `getattr(item, "source_tool", ...)` 兜底——**读代码确认后再决定改不改**，改就一并加测试）。

### 9.7 P2.5-6：SSE 切图 + 前端 analyst 卡片

**文件**：`app/main.py`、`app/web/index.html`、`tests/test_stream_endpoint.py`（新建）

**第 1 步 — 后端 `_stream_research` 重写**（`app/main.py`，整体替换现函数；保留 `_get_step_message` 但扩充）：

```python
#: 节点名 → (progress step, 默认文案)
_NODE_PROGRESS = {
    "supervisor":   ("planning",    "理解问题并生成研究计划"),
    "kernel":       ("tool_call",   "整包调查执行中…"),
    "technical":    ("tool_call",   "技术面分析员采集中"),
    "fundamental":  ("tool_call",   "基本面分析员采集中"),
    "moneyflow":    ("tool_call",   "资金面分析员采集中"),
    "news":         ("tool_call",   "新闻事件分析员采集中"),
    "sentiment":    ("tool_call",   "舆情分析员采集中"),
    "gate":         ("evaluating",  "证据质量检查"),
    "reasoning":    ("reasoning",   "正在生成结构化市场情报…"),
    "critic":       ("critic",      "结论-证据审计"),
}
```

核心结构（异步队列 + 心跳，逻辑对齐现有 shield/wait_for 模式）：

```python
async def _stream_research(question, domain, conversation_id=None):
    settings = _get_settings()
    budget = settings.research_budget_seconds
    heartbeat = max(1, settings.stream_heartbeat_seconds)

    def json_event(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    orchestrator = _get_orchestrator()
    graph = orchestrator._ensure_graph()
    initial_state = {"question": question, "domain": domain, "conversation_id": conversation_id}

    queue: asyncio.Queue = asyncio.Queue()

    async def _pump():
        try:
            async for mode, payload in graph.astream(
                initial_state,
                stream_mode=["updates", "values"],
                config={"recursion_limit": settings.graph_recursion_limit},
            ):
                await queue.put((mode, payload))
            await queue.put(("__done__", None))
        except Exception as exc:
            await queue.put(("__error__", exc))

    task = asyncio.create_task(_pump())
    final_state: dict = {}
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + budget

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                task.cancel()
                raise UpstreamTimeoutError(f"Research exceeded the {budget}s budget")
            try:
                mode, payload = await asyncio.wait_for(queue.get(), timeout=min(heartbeat, remaining))
            except (asyncio.TimeoutError, TimeoutError):
                elapsed = int(loop.time() - started_at)
                yield json_event("progress", {"step": "working", "node": None,
                                              "message": f"仍在调查中…（{elapsed}s / 预算 {budget}s）"})
                continue

            if mode == "__done__":
                break
            if mode == "__error__":
                raise payload
            if mode == "values":
                final_state = payload           # 全量 state，留最后一次
            elif mode == "updates":
                for node_name in payload.keys():
                    step, message = _NODE_PROGRESS.get(node_name, (node_name, node_name))
                    yield json_event("progress", {"step": step, "node": node_name, "message": message})

        # ── 组装结果（与 orchestrator.run 对齐）──
        from app.models.response import ResearchResponse
        result = ResearchResponse(
            question=question,
            report=final_state.get("report"),
            tool_results=list(final_state.get("results", [])),
            cache_stats=dict(final_state.get("cache_stats", {})),
            conversation_id=conversation_id,
        )
        yield json_event("progress", {"step": "done", "node": None, "message": "调查完成"})

        save_result = result.model_dump()
        if conversation_id:
            asyncio.create_task(_save_turn(conversation_id, question, save_result))
        asyncio.create_task(_save_research_and_state(question, save_result))
        yield json_event("result", save_result)

    except (UpstreamTimeoutError, asyncio.TimeoutError, TimeoutError):
        ...  # 与现有超时分支一致：progress error + result {error, code:"timeout"}
    except LLMOutputError as exc:
        ...  # 与现有一致：code "upstream"
    except Exception as exc:
        ...  # 与现有一致：code "internal"
    finally:
        if not task.done():
            task.cancel()
```

要点：
- `astream(stream_mode=["updates","values"])` 产出 `(mode, payload)` 二元组——这是 LangGraph 官方多模式签名，**不要**用单 mode 再拼第二次调用。
- `values` 模式每次给全量 state，最后一次就是终态；`updates` 用来做逐节点进度。
- 删除 `detective = _get_orchestrator().market_detective` 及旧 investigate 调用。
- `Orchestrator.__init__` 里的 `self.market_detective` 属性本任务**保留**（P5 才删），但 `_stream_research` 不再引用它。
- `research_more` 回环时 supervisor 会第二次出现 → 前端再收一条 planning 进度，属正常。

**第 2 步 — 前端 `app/web/index.html`**：

1. `addAILoader`：把写死的 4 步列表替换为分析员卡片结构：

```javascript
const analysts = [
  {node: 'technical',   label: '📈 技术面'},
  {node: 'fundamental', label: '🏛 基本面'},
  {node: 'moneyflow',   label: '💰 资金面'},
  {node: 'news',        label: '📰 新闻舆情'},
];
// 渲染：4 张 .analyst-card（data-node=...，初始 pending）+ 1 张 #summaryCard（汇总：计划 → 证据检查 → 推理 → 审计）
```

   配套 CSS：`.analyst-card` 三态 `pending`（灰）/ `running`（高亮+脉动）/ `done`（✓），复用现有 `.research-step` 的配色变量即可，样式保持紧凑（一行 4 卡 + 下方汇总行）。
2. 用 `updateProgress(data)` 替换 `updateLoader`（保留函数名 `updateLoader` 作为包装以防遗漏引用也可以，但 `ask()` 里必须改为传 `updateProgress`）：

```javascript
function updateProgress(data) {
  const row = document.getElementById('ai-loading');
  if (!row) return;
  const txt = row.querySelector('.loading-text');
  if (txt && data.message) txt.textContent = data.message;
  const node = data.node;
  if (data.step === 'planning') {
    // 规划完成 → 所有分析员卡片进入 running
    row.querySelectorAll('.analyst-card').forEach(c => c.className = 'analyst-card running');
  } else if (node && row.querySelector(`.analyst-card[data-node="${node}"]`)) {
    const card = row.querySelector(`.analyst-card[data-node="${node}"]`);
    card.classList.remove('running'); card.classList.add('done');
  } else if (data.step === 'evaluating' || data.step === 'reasoning' || data.step === 'critic') {
    const sc = document.getElementById('summaryCard');
    if (sc) sc.textContent = data.message;   // 汇总卡显示当前阶段
    row.querySelectorAll('.analyst-card.running').forEach(c => { c.className = 'analyst-card done'; });
  }
  scrollToBottom(true);
}
```

   （`kernel` 节点没有对应卡片：收到 `node:"kernel"` 时只更新文案即可，上面 else-if 分支自然兜住。）
3. `ask()` 中：`runSSE(q, (data) => updateLoader(0, ...), ...)` 改为 `runSSE(q, updateProgress, ...)`。

**第 3 步 — 测试**（`tests/test_stream_endpoint.py`）：

- 构造 fake compiled graph：一个对象，`astream(input, stream_mode, config)` 是 async generator，依次 yield `("updates", {"supervisor": {...}})`、`("values", {...})`、…、覆盖 supervisor/technical/gate/reasoning/critic 节点，最后一个 `("values", final_state)`（final_state 含一个最小可用的 `report` dict——注意 `ResearchResponse` 需要 `report` 能通过 model_dump，用真实 `MarketIntelligence.model_validate(min_json)` 构造）。
- `monkeypatch` 掉 `Orchestrator._ensure_graph` 返回 fake。
- 用 `httpx.AsyncClient`（参考 `tests/test_ask_endpoint.py` 的 client 构造方式）POST `/api/ask/stream`，读取 SSE 文本，断言：出现 `event: progress` 且依次包含 `"node": "supervisor"`、`"node": "technical"`、…、最后 `event: result` 且 JSON 含 `report`。
- 追加一个超时用例：fake astream 里 `await asyncio.sleep(10)`，把 `research_budget_seconds` monkeypatch 成 1（`Settings` 实例替换 `_get_settings` 缓存），断言收到 `code: "timeout"` 的 result 事件。

**验收**：新测试通过；全量测试绿；手动验证（可选）：`python -m app.main` 起服务，浏览器提问一次，观察卡片状态流转与逐节点文案。
**坑**：
- `astream` 传初始 state 用 **dict** 即可（LangGraph 会按 schema 校验），不要传 ResearchState 实例。
- 不要在 generator 里 `await queue.get()` 不带 timeout——心跳全靠这个 timeout 发出去。
- `json.dumps` 必须带 `default=str`，state 里可能有 datetime/枚举。

### 9.8 P4-1：config 开关

**文件**：`app/config.py`

在 "LangGraph 图配置" 区块下追加：

```python
# ── P4：舆情 / 新闻 ────────────────────
#: 舆情分析员总开关（评论 MCP 就绪后置 true）
sentiment_enabled: bool = False
#: 单次拉取评论上限
sentiment_max_comments: int = 500
#: 新闻分析员总开关
news_enabled: bool = False
#: DDGS 搜索结果缓存时长（秒），缓解限流
news_search_ttl_seconds: int = 21600
```

**验收**：`pytest tests/ -q` 绿（Settings 新增字段有默认值，不破坏任何现有用例）。

### 9.9 P4-2：registry 调整

**文件**：`app/gateway/tool_registry.py`、`tests/test_tool_registry.py`

1. 把 `news_search` 的 `ToolMeta` 的 `category="shared"` 改为 `category="news"`。
2. 在 sentiment 区块位置（`ALL_TOOLS` 拼装列表内）加入**注释掉的**模板，注明"评论 MCP 就绪后取消注释并按实际工具名校准"：

```python
# ── P4-5：评论爬取 MCP 就绪后启用（工具名以实际 MCP 为准）──
# ToolMeta("xq_comments", "comments_xueqiu_get", "雪球个股评论区抓取",
#          category="sentiment", domain="a_share", ...),
# ToolMeta("futu_comments", "comments_futu_get", "富途牛牛个股评论区",
#          category="sentiment", domain="hk_stock", ...),
# ToolMeta("ths_comments", "comments_ths_get", "同花顺个股/板块评论区",
#          category="sentiment", domain="a_share", ...),
```

3. **测试**：断言 `by_category["news"]` 恰含 `news_search` 一条、`"sentiment" not in by_category`（MCP 未接入）。

**坑**：`registry_text()` 会把所有工具喂给 Supervisor prompt——news_search 改为 news category 不影响它出现在 prompt 里（它本来就在），行为无变化。

### 9.10 P4-3：news_search 结果缓存

**文件**：`app/graph/tool_runtime.py`、`tests/test_news_search.py`

1. `_execute_internal` 的 `news_search` 分支开头加缓存检查、成功返回前写缓存（与 `execute()` 里同款逻辑）：

```python
if tool_name == "news_search":
    cache_key = _make_cache_key(tool_name, arguments)
    cached = market_cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for news_search")
        return cached
    try:
        ...（现有搜索逻辑不动）...
        if status == STATUS_SUCCESS:
            market_cache.set(cache_key, result, ttl=self.settings.news_search_ttl_seconds)
        return result
```

   注意 TTL 用 `self.settings.news_search_ttl_seconds`，**不要**走 `_resolve_ttl`（那张表没有 news_search）。
2. **测试**（`tests/test_news_search.py` 追加，mock `search_news` 不打真网）：同一参数连续两次 `ToolRuntime.execute("news_search", {...})`，断言底层 `_search_news` 只被调 1 次；不同参数调 2 次。

### 9.11 P4-4：news analyst 节点

**文件**：`app/graph/nodes/analysts/news.py`（新建）、`app/graph/nodes/analysts/__init__.py`、`app/graph/builder.py`、`tests/test_graph_nodes.py` 或 `tests/test_graph_topology.py`

1. `news.py`——route 消费逻辑已在 base 里，节点本体极薄：

```python
"""新闻事件分析员 — 执行 Supervisor 分配的 news 类工具（DDGS 检索等）。"""

from app.graph.nodes.analysts.base import MarketAnalystNode


class NewsAnalystNode(MarketAnalystNode):
    category = "news"
```

   在 `analysts/__init__.py` 导出。
2. `builder.py`：
   - 节点注册改为条件：`if settings.news_enabled: builder.add_node("news", NewsAnalystNode(settings))`。
   - `_supervisor_fanout` 的候选元组改为动态：`candidates = ("technical","fundamental","moneyflow") + (("news",) if settings.news_enabled else ()) + (("sentiment",) if settings.sentiment_enabled else ())`；同时 `_build_route` 的归类也要同步（supervisor.py 里 `cat not in (...)` 的白名单：`news_enabled` 时允许 "news" 独立成组，否则维持归入 technical——**把白名单做成从 settings 派生的模块级函数**，避免两处写死）。
   - `news_enabled` 时加 `builder.add_edge("news", "gate")` 与条件边映射表里的 `"news": "news"`。
   - 若未来 sentiment_enabled，同型处理（本任务可留 hook 不实现）。
3. **测试**：
   - `news_enabled=False`（默认 Settings）→ `build_graph` 的图不含 "news" 节点（可用 `graph.get_graph().nodes` 断言）；
   - `news_enabled=True` + mock plan 含 news_search step → NewsAnalystNode 被执行、fake runtime 收到 `news_search` 调用；
   - 两开关全关的整图 e2e 输出与 P2.5 基线一致（复用 test_graph_e2e 的 fixture 跑一遍）。

**验收**：上述测试绿；`news_enabled=False` 时行为与 P2.5 完全一致（这是 P4 的总验收门槛）。

### 9.12 P4-5：舆情管线（阻塞项——评论 MCP 就绪后才开工）

**前置**：评论爬取 MCP（雪球/富途/同花顺评论区）已接入 market-gateway 且 §9.9 的三条 ToolMeta 已取消注释。未满足前**不要**开始本任务。

**文件**：`app/sentiment/pipeline.py`（新建）、`app/sentiment/__init__.py`、`app/models/research.py`、`app/graph/state.py`、`app/graph/nodes/analysts/sentiment.py`（新建）、`app/graph/builder.py`、`tests/test_sentiment_pipeline.py`

1. **模型**（`app/models/research.py`）：

```python
class SentimentDigest(BaseModel):
    score: int = 0            # -100..100
    stance: str = ""          # 散户观点归纳
    extremes: list[str] = Field(default_factory=list)   # 极端言论摘录
    shift: str = ""           # 与前期对比
    sample_size: int = 0
    window: str = "1d"        # 1d / 3d / 7d
```

2. **state**：`sentiment_digest: Optional[object] = None`（不带 reducer，单节点写）。
3. **管线** `app/sentiment/pipeline.py`，五个纯函数分层（§5.1）：
   - `fetch_comments(tool_runtime, metas, symbol, max_comments) -> list[dict]`：调 sentiment 类工具；
   - `clean_comments(comments) -> list[dict]`：去重（内容归一化后哈希）、过滤纯表情/复读（同一内容 ≥3 次）/疑似水军（同一用户 ≥5 条且内容相似度 >0.8）；
   - `aggregate(comments, windows=("1d","3d","7d")) -> dict`：按时间窗统计发帖量、增速、多空关键词词频（内置一份小型中文多空词表，放模块常量）；
   - `score_with_llm(client, model, agg_summary) -> SentimentDigest`：唯一一次 LLM 调用，输入聚合摘要、输出 JSON → `parse_json_object` → `SentimentDigest.model_validate`；
   - `run_sentiment_pipeline(...) -> SentimentDigest`：串起上面四步。
   清洗/聚合层**不得**依赖 LLM 与网络（测试要求）。
4. **节点** `sentiment.py`：覆写 `MarketAnalystNode.__call__`（不完全走 base 的工具遍历）：跑管线 → 返回 `{"sentiment_digest": digest, "evidence": [Evidence(metric="sentiment.score", value=digest.score, source_tool="sentiment_pipeline", note=f"样本量 {digest.sample_size}, 窗口 {digest.window}")], "findings": [一条 digest], ...}`。
5. **builder**：`sentiment_enabled` 时注册节点 + 扇出候选 + `add_edge("sentiment","gate")`（与 P4-4 同型）。
6. **测试**：`tests/test_sentiment_pipeline.py` 纯代码层——构造含复读/水军/表情的假评论列表，断言 clean/aggregate 的过滤与统计数字；LLM 层用 mock client 断言 digest 解析。

### 9.13 P5-1：briefs 走图

**文件**：`app/scheduler/briefs.py`、`tests/test_briefs.py`（若无则新建）

1. `generate_morning_brief` / `generate_evening_brief` 改为调图：

```python
async def generate_morning_brief(settings=None, memory=None) -> dict[str, Any]:
    from app.agent.orchestrator import Orchestrator
    settings = settings or _get_settings()
    memory = memory or _get_memory()
    question = "请做一份今日开盘前的市场状态检查：当前市场状态、强势方向、今日需要重点跟踪的变量。"
    try:
        result = await asyncio.wait_for(
            Orchestrator(settings).run(question),
            timeout=settings.research_budget_seconds,
        )
        report = result.report
        items = [
            f"市场当前状态: {report.state_label}",
            f"发生了什么: {report.what_happened[:120]}",
        ]
        if report.strong_areas:
            items.append(f"强势方向: {', '.join(report.strong_areas[:3])}")
        if report.risks:
            items.append(f"风险: {report.risks[0][:80]}")
    except Exception as exc:
        logger.warning("Graph-based brief failed, fallback to memory template: %s", exc)
        items = _legacy_items_from_memory(memory)   # 把现在的模板逻辑抽成这个私有函数
    return {"title": "Good Morning. Here's what matters today.",
            "items": items[:5], "generated_at": datetime.now(UTC).isoformat(), "type": "morning"}
```

   晚报同型（question 换成"今天实际发生了什么：状态变化、验证/证伪了什么、主题是否轮换"）。**保留 memory 兜底路径**：图失败时简报功能不中断。
2. checkpointer 暂不引入（收益低、多一个 sqlite 依赖）；`/api/ask` 的 conversation_id 机制已覆盖多轮需求。若后续要加：`build_graph(settings, checkpointer=None)` 加可选参数 + `langgraph-checkpoint-sqlite` 独立 db 文件，作为 P5 后的独立小任务。
3. **测试**：mock `Orchestrator.run` 返回最小 ResearchResponse，断言 brief dict 结构不变（title/items/generated_at/type）；再 mock run 抛异常，断言走 memory 兜底且结构仍不变。

**注意**：此改动让定时简报从"纯模板零成本"变成"每天两次完整调查（含 LLM）"——上线前确认定时任务时段网关与模型配额无压力。

### 9.14 P5-2：删除旧路径（绞杀者收尾）

**前置**：P2.5 + P4-1..4 全部完成且全量测试绿。**逐文件删，每删一组跑一次测试。**

| 步骤 | 删除/修改 | 说明 |
|---|---|---|
| 1 | 删 `app/graph/nodes/kernel.py`；builder 移除 kernel 分支；`_supervisor_fanout` 的 route 空兜底改为"三个默认 analyst 全上"（`return ["technical","fundamental","moneyflow"]`） | kernel 的历史使命结束 |
| 2 | 删 `app/research/market_detective.py` | 先全局搜 `market_detective` / `MarketDetective` 的引用点：`orchestrator.py`（删 `self.market_detective` 与对应 import）、`tests/test_market_detective.py`（删） |
| 3 | 删 `app/agent/planner.py`、`tests/test_planner.py` | supervisor 已吸收；确认无其他引用（`set_enabled_domains` 在 supervisor 里有自己的一份，保留） |
| 4 | 删 `app/agent/evaluator.py`、`tests/test_evaluator.py` | critic 已吸收；`ResearchDecision` 若无其他引用一并删 |
| 5 | `app/agent/prompts.py`：保留 `SYSTEM_PROMMENT` 与 `REASONING_PROMPT`（research/reasoning.py 在用），删 `PLANNER_PROMPT`——先把 `PLANNER_PROMPT` 移到新文件 `app/agent/prompts_graph.py` 并改 supervisor 的 import，再删 | 分两步避免一次性断引用 |
| 6 | `orchestrator.py` 头部注释与 `__init__` 清理（删 P0 过渡注释、删 MarketDetective 相关） | |
| 7 | `tests/test_ask_endpoint.py` / `test_graph_e2e.py` 中若有 mock `MarketDetective` 的地方改为 mock 图/Orchestrator | 先搜再改 |
| 8 | `app/graph/state.py`：`AnalystName` 移除 `"kernel"` | 确认无引用后 |

**验收**：`grep -ri "market_detective\|MarketDetective\|planner\.Planner\|EvidenceEvaluator" app/ tests/` 零命中；全量测试绿；`/api/ask` 与 `/api/ask/stream` 手动各跑一次。

### 9.15 P5-3：文档

**文件**：`README.md`、`docs/architecture.md`（不存在则新建）

- 架构图更新为 §1 目标拓扑（无 kernel 版）；
- 记录新增配置项（critic_max_revisions / graph_recursion_limit / sentiment_* / news_*）；
- SSE 事件协议：`progress` 事件新增 `node` 字段、step 取值表（_NODE_PROGRESS）；
- 更新本文档顶部状态与 §6 验收记录。

### 9.16 P5-4：全量回归与收尾

1. `python -m pytest tests/ -q` 全绿；
2. 手动回归清单：`/api/ask` 普通问题（含股票代码）一次、`/api/ask/stream` 一次、`/api/brief/morning` 一次、`news_enabled` 开关切换各验证一次；
3. 更新 `docs/langgraph-refactor-plan.md` 顶部状态为 "P5 已完成"，§6 各行勾选；
4. 提交信息沿用现有风格（参考 `git log`：`p2.5 ...` / `p4 ...` / `p5 ...`）。
