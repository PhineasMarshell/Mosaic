# LangGraph Multi-Agent 重构方案

> 状态：P3 已完成，314 测试全绿

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

| P1 执行层抽取  | tool_runtime.py 从 market_detective 抽出（含测试搬迁）；Supervisor 节点上线（复用 planner prompt + 解析）；图变成 supervisor → market_detective 内核 → reasoning | 路由决策有单测（test_graph_routing.py, 6 tests）；端到端对比 P0 输出一致 |

| P2 Critic 闭环  | gate / reasoning / critic 节点化；Critic 条件边（revise ≤ critic_max_revisions；research_more 时带 missing_points 回 Supervisor，最多 1 次）；SSE 改 astream(stream_mode="updates") 按节点推事件 | 前端能看到逐节点进度；打回重写路径有测试 |

| P3 Analyst 拆分  | 工具 registry 加 category 标注（39 个工具，by_category 索引）；base.py 骨架（预算守卫、失败降级、finding digest）；拆为 technical/fundamental/moneyflow 三节点，builder 中 add_edge 并行扇出；AnalystName 扩展为 Literal["kernel","technical","fundamental","moneyflow"] | 三节点并行有测试（test_graph_topology.py + test_graph_nodes.py, 14 tests）；单 analyst 超时/失败整图仍出报告 |

| P4 舆情 + 新闻接入 | 评论 MCP 工具注册 + sentiment/pipeline.py 清洗统计 LLM 打分 + sentiment analyst 节点；news/search.py（DDGS）+ news analyst 节点 + tool_runtime 本地执行分支；两开关独立：sentiment_enabled / news_enabled，默认关 | 清洗层纯代码单测；两开关全关 = P3 行为 |

| P5 收尾 | briefs 走图 + checkpointer（thread_id 绑 conversation_id）；删 market_detective.py / planner.py / evaluator.py 旧路径与 prompts.py 旧 prompt；更新 README / docs | 死代码清零；brief 生成路径有 e2e 测试 |

| 持续（P5 后） | 按 §5.2 第二批清单逐步添加定向新闻源/宏观日历/公告等 gateway 工具 | 每次只动 tool_registry + normalizer，图结构不变 |

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
