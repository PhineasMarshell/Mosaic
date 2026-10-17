# Mosaic Agent 应用开发面试问答手册

> 阅读基准：2026-10-16，按当前 `D:\Mosaic` 工作树源码和仓库文档整理。工作树中存在未提交修改，文档里的“当前实现”指本次检查时看到的代码，不代表某个已发布版本。历史线上结果引用其对应日期与验收报告；本次整理没有运行全量测试或真实线上请求。
>
> 使用方法：先熟悉第 1—3 节的项目故事，再按面试岗位选择技术深挖问题。每个回答按“先结论、再机制、最后边界”组织。文中的【个人经历待补】必须替换为你本人真实负责的内容；仓库只能证明系统行为，不能证明个人贡献。

## 目录

- [一、项目介绍与面试开场](#一项目介绍与面试开场)
- [二、架构与一次请求](#二架构与一次请求)
- [三、Agent 编排、工具和数据源](#三agent-编排工具和数据源)
- [四、证据、推理与审计](#四证据推理与审计)
- [五、预算、降级、日志和记忆](#五预算降级日志和记忆)
- [六、API、前端和附加能力](#六api前端和附加能力)
- [七、测试、评估、部署与真实验收](#七测试评估部署与真实验收)
- [八、项目反思、追问与简历表达](#八项目反思追问与简历表达)
- [附录 A：应用代码逐文件索引](#附录-a应用代码逐文件索引)
- [附录 B：测试文件覆盖索引](#附录-b测试文件覆盖索引)
- [附录 C：脚本、文档和配置清单](#附录-c脚本文档和配置清单)
- [附录 D：面试前速查表](#附录-d面试前速查表)

## 一、项目介绍与面试开场

### Q01：请用一句话介绍 Mosaic

**答：** Mosaic 是一个基于 LangGraph 的多市场研究 Agent。它把用户的自然语言问题转成受工具注册表约束的研究计划，调用行情、基本面、资金流、新闻和问财等数据源，再把每条结论绑定到可核查的数据证据，并通过代码审计决定报告能否交付。

**追问要点：** 重点是计划、工具、证据和交付链路，不要把它描述成“LLM 查行情后写摘要”。

### Q02：这个项目解决什么问题

**答：** 市场问题往往需要多个来源和不同口径的数据。直接让模型自由调用工具并总结，容易把过期数据、错误标的、未验证日期或搜索摘要写成确定结论。Mosaic 的重点是让研究过程可追踪，并在证据不够时明确降级或阻断。

**源码：** `app/graph/nodes/supervisor.py`、`app/graph/nodes/reasoning.py`、`app/graph/nodes/critic.py`、`app/graph/nodes/finalize.py`。

### Q03：面试时怎么做 30 秒介绍

**答：** 我做的项目 Mosaic 是一个多市场研究 Agent。用户提交问题后，LangGraph 里的 Supervisor 规划工具并并行调度分析节点，工具层通过 MCP、HTTP Gateway 和同花顺问财等适配器取数。系统把返回值归一化成带来源、日期、标的和完整度的证据，Reasoning 生成带引用的报告，Critic 和最终代码审计再判断结论是否可信。最终状态分为 verified、degraded、blocked、failed，只有符合严格条件的 verified 才会写入研究记忆。

### Q04：请用两分钟介绍一次完整请求

**答：** API 首先校验问题和可选市场域，生成 `run_id`，解析请求日期并创建共享研究状态，同时设定一次请求的绝对截止时间。Supervisor 结合问题、域、工具注册表和对话历史形成计划；图根据计划扇出到 technical、fundamental、moneyflow 等分析节点。分析节点通过 ToolRuntime 校验参数、复用缓存、执行工具并归一化返回结果。Evidence Gate 检查数据是否可用，Reasoning 只基于证据账本生成结构化报告和 claim 引用。Critic 结合模型语义检查与代码规则检查给出 pass、revise 或 research_more。通过或耗尽回环后都会到 FinalizeAuditNode，重新做严格匹配，派生交付状态并决定是否保留报告。同步 API 与 SSE 最终共用响应构造和持久化逻辑。

### Q05：项目主要技术栈是什么

**答：** Python 3.11 及以上，FastAPI 提供 API，LangGraph 编排研究图，Pydantic 定义状态和响应结构，OpenAI 兼容客户端调用模型，MCP SDK 和 httpx 连接工具服务，SQLite 存储通过审计的研究结果，pytest 和 Ruff 做工程检查。网页是单页 HTML、CSS 和原生 JavaScript，没有 React 依赖。

**开源依赖：** 项目复用 LangGraph、FastAPI、Pydantic、MCP Python SDK、httpx、pytest、AkShare 等开源项目；可靠性策略和市场数据语义适配是在本项目中组织的。没有证据表明本项目是某个开源 Agent 产品的 fork。

### Q06：这是一个多 Agent 系统吗

**答：** 它是单个 LangGraph 状态图里的多角色 Agent 工作流。Supervisor 负责计划，多个 analyst 节点负责不同类别的工具采集，Reasoning 负责报告，Critic 负责审查。分析节点共享同一个研究状态，并行执行后通过 reducer 合并。面试时我会说“多角色、多节点 Agent 工作流”，不会暗示它们是独立部署、有各自生命周期的自治 Agent 服务。

### Q07：你的个人贡献是什么

**答：** 这题必须按自己的实际经历回答。建议表述为：“我主要负责【具体模块或阶段】，解决了【有证据的问题】，通过【测试或验收】验证，结果是【可核实指标】。”

**请先补充真实事实：** 你亲自编写或主导的文件、设计决策、排障过程和验收结果。不要把仓库所有能力都说成个人完成。

### Q08：为什么强调证据而不是只追求回答质量

**答：** 因为市场问答里的错误可能来自数据本身而不是语言质量。报告写得流畅，不表示证券代码、日期、来源、单位和覆盖范围正确。系统让 claim 映射到真实数据，再由独立规则审计，这样模型无法仅凭“看起来合理”把不受支持的判断变成可信结论。

### Q09：系统支持哪些市场或研究域

**答：** 当前启用域从注册表派生，包括 A 股、港股、大宗商品、加密资产、美股和宏观。工具表共有 63 个元数据条目、55 个规范 operation 名；有些条目跨域共享或只是兼容占位，因此条目数不等于 63 个独立上游 API，也不等于每个域都已经通过生产验收。

### Q10：项目当前成熟度怎么描述

**答：** 工程上有结构化编排、工具边界、审计、运行日志、离线测试和 Docker/CI；真实数据交付仍要按场景评估。2026-10-14 的 P16 报告里生产就绪标记为 false，16 次真实请求为 4 次 degraded、12 次 blocked、0 次 verified。后续 MCP 语义适配有离线验收，但这不能替代新一轮线上交付验收。

## 二、架构与一次请求

### Q11：整体架构怎么画

**答：** 可以按 API、编排、工具、证据审计、持久化五层讲：

```text
浏览器 / CLI
     │ POST /api/ask 或 /api/ask/stream
     ▼
FastAPI ── Orchestrator ── LangGraph ResearchState
                              │
          Supervisor ── 并行 Analyst 节点
                              │
                 ToolRuntime / Cache / Budget
                    ├─ MCP Client
                    ├─ HTTP Gateway Client
                    ├─ THS Skill adapters
                    └─ 新闻、SEC、AkShare 等内部适配器
                              │
                              ▼
                 NormalizedDatum / Evidence Ledger
                              │
                  Evidence Gate → Reasoning
                              │
                    Critic → Finalize Audit
                      │             │
          revise/research_more      ├─ verified → SQLite
                                    ├─ degraded → 受限报告
                                    └─ blocked/failed → 无报告
```

**关键源码：** `app/main.py`、`app/agent/orchestrator.py`、`app/graph/builder.py`、`app/graph/tool_runtime.py`、`app/research/evidence.py`、`app/graph/nodes/finalize.py`。

### Q12：LangGraph 的节点顺序是什么

**答：** 入口是 Supervisor。它按计划条件扇出到一个或多个 analyst 节点，默认可落到 technical、fundamental、moneyflow；news 和 sentiment 节点由配置开关控制。分析结果进入 Gate、Reasoning、Critic。Critic 可以把控制流送回 Reasoning 做有限重写，或回 Supervisor 补工具证据；其余路径进入 FinalizeAuditNode，再到 END。

**容易答错：** 当前 builder 用条件边返回节点名列表实现 fan-out，没有直接用 LangGraph `Send()`。Critic 的 pass 也经过 FinalizeAuditNode，不是直接结束。

### Q13：同步请求和 SSE 是两套研究流程吗

**答：** 它们最终运行同一张 compiled graph，也共用报告构造、终态编码和持久化函数。同步路径调用 Orchestrator 的运行方法；SSE 路径直接驱动同一图的 `astream`，并用后台任务和队列组织进度与终态事件。因为入口细节不同，异常映射和启动预检仍需单独测试。

### Q14：请求状态里有哪些重要字段

**答：** `ResearchState` 保存问题、域、会话 ID、run ID、计划日期和实际日期、预算截止时间、工具路由、工具结果、证据、finding、Critic 结论、回环计数、终态和诊断信息。这样每个节点读写统一契约，LangGraph 根据字段 reducer 合并并行节点产物。

### Q15：日期语义为什么拆成三个字段

**答：** `requested_date` 是用户提出的日期，`planned_as_of_date` 是规划阶段推导的目标交易日，`as_of_date` 是数据真正证明的观测日期。计划日期不能冒充实际数据日期，抓取时间 `retrieved_at` 也不能冒充行情日期。交易日历只帮助规划，不代表上游真的返回了那天的数据。

### Q16：状态 reducer 如何处理并行和回环

**答：** `results` 按工具名和规范化参数签名去重，同签名用后到结果覆盖；`evidence` 按 ID 去重；`findings` 按 analyst 覆盖；`errors` 仍按列表追加。`route` 等标量字段不使用 reducer，回环时由新值整体覆盖。这样重复研究不会把同一工具结果成倍追加。

**边界：** `len(results)` 不等于底层实际请求次数；同 ID 覆盖也不保证同步删除与其对应的旧账本项，实际审计还要看本轮状态和日志。

### Q17：为什么没有 checkpointer 有什么影响

**答：** 当前 `build_graph` 调用 `compile()` 时没有配置 LangGraph checkpointer，所以没有 LangGraph 级别的跨请求 checkpoint、断点恢复或中断后续跑。项目里的 SQLite 是业务研究记忆库，不能把它说成 LangGraph checkpoint 或 Human-in-the-loop 持久化。

### Q18：run_id 有什么用

**答：** 它把一次用户请求的 plan、工具 route、execution、LLM、Critic、finalize、delivery 和 persistence 事件串起来，也返回给 API/SSE 调用方用于排障。请求内用 ContextVar 或状态传递关联 ID。它不是数据库主键的唯一凭据，持久化另有幂等键。

### Q19：为什么对图调用加超时

**答：** Orchestrator 给整次研究设定单调时钟截止时间，并用 `asyncio.wait_for` 做外层保护。阶段预算还要给后续 Critic 和 finalize 留时间，避免前面的工具调用耗光预算后没有机会输出可解释终态。超时被记录为 failed，而不是伪造空的成功报告。

### Q20：域和工具白名单在哪里确定

**答：** `tool_registry.py` 是工具元数据的集中来源，保存逻辑 key、operation 名、市场域、类别、优先级和是否需要 symbol。Supervisior 生成计划后，代码过滤掉注册表里不存在或域不允许的工具。API 的显式 domain 也根据启用域列表校验。

## 三、Agent 编排、工具和数据源

### Q21：Supervisor 做什么

**答：** Supervisor 调用模型生成结构化 intent 和工具分配，并根据域、用户目标、历史对话、可用工具和代码规则修正计划。它用 JSON object 输出和 Pydantic 校验约束结果，再过滤未注册工具。规划不是 provider-native function calling 的自由工具循环，真正执行工具的是后续 analyst 和 ToolRuntime。

### Q22：分析员节点自己会逐个推理吗

**答：** 当前 technical、fundamental、moneyflow 等常规 analyst 主要承担工具采集和短执行摘要，不是每个角色各自跑一个独立的大模型研究循环。工具计划由 Supervisor 生成，最终自然语言报告主要由 Reasoning 节点生成。Sentiment 节点包含额外的情绪评分逻辑，是例外路径。

### Q23：并行调度如何实现

**答：** Supervisor 条件边按计划返回若干 analyst 节点名，LangGraph 并行调度这些节点，再汇入 Gate。每个 analyst 内部还会按计划调用多个工具并用 `asyncio.gather`，并通过 Semaphore 限制该 analyst 的工具并发。

**边界：** `tool_concurrency=3` 不是全服务全局最多三次调用；多个 analyst 或多个请求可同时各自运行 semaphore。全局服务级并发治理需要单独限流或共享调度器。

### Q24：ToolRuntime 的职责是什么

**答：** 它统一处理参数 canonicalize、调用签名去重、缓存、源选择、预算检查、deadline、异常转换、结果汇总和运行诊断。Analyst 不需要知道每种上游协议的细节。运行失败会变成结构化 ToolResult 和错误信息，让图能够继续进入 Gate 和最终审计。

### Q25：MCP 客户端的调用流程是什么

**答：** MCP 客户端启动本地子进程或连接相应 transport，构造 `ClientSession`，显式执行 initialize，再发现工具、调用工具并在退出时关闭资源。项目还处理 Windows 原生管道兼容、子进程代理环境传递和启动时握手自检。MCP SDK 的 session context manager 不应被误认为已经完成 initialize 握手。

### Q26：MCP 返回成功为什么还可能没有证据

**答：** 协议层的 success 只表示工具调用正常返回，不能证明 payload 具有业务含义。归一化还需要识别具体工具契约、真实数据日期、provider、authority、标的和完整度。对于涨停家数和板块两个特定 MCP 工具，语义适配器会检查非负整数、板块身份、结构字段和日期 lineage；缺证据时返回零业务 datum 或 partial，后续 Gate 不会把它算作有效行情证据。

### Q27：新加的 MCP 语义补标注适配器做了什么

**答：** `mcp_semantics.py` 仅对 `get_limit_up_count` 和 `list_limit_up_sectors` 两个有明确契约的工具做标准指标和日期标注。它深拷贝原始载荷，不改 raw；将结构元数据和业务指标分开，检查 count 别名冲突、整数边界、板块行身份和行级日期。板块工具只有在契约保证同一快照批次时，才从可信 envelope 日期给缺日期的行补 lineage。

**边界：** 这不能推广成“所有 MCP count 都是业务数值”，也不能把 transport 名 `mcp` 当 authority。契约声明这两个无参端点不接受请求日期，planner 日期不能补成观察日期。

### Q28：THS Skill 与 MCP Gateway 是什么关系

**答：** 它们是不同接入路径。同花顺问财适配器调用自己的 HTTPS Skill API；Market Gateway 可以通过 MCP 或 HTTP 传输。工具注册表决定能力和固定路径，路由层在指定能力下选择 primary/fallback source。THS 的 provider/authority 语义来自其自身适配器；MCP 的 authority 需要来源白名单或明确 provider 字段，不能把通道名当来源。

### Q29：数据源 fallback 什么时候发生

**答：** 受控 fallback 只在 primary 出现可重试错误、必需字段缺失或来源信息不足时触发。认证错误会保留为可诊断的配置问题，不盲目切源；权威来源冲突也不会用另一源强行覆盖。fallback 结果还必须满足该 capability 的来源、日期、authority、标的和覆盖要求，失败会写出逐项 provenance reason code。

### Q30：数据源 fallback 和模型 evidence-only fallback 有何不同

**答：** 数据源 fallback 是对工具数据来源做一次受控替换，例如主源失败后尝试备用 Gateway。Evidence-only fallback 则在模型报告生成失败或截断时，尝试仅用现有证据账本构造非常受限的事实报告。两者触发条件、验证逻辑和日志字段不同；evidence-only fallback 即使产生报告也只能降级，不会按 verified 保存。

### Q31：HTTP Gateway 如何避免任意 URL 调用

**答：** HTTP client 通过注册表映射 operation 到固定 method 和 path，并使用 API key header。错误分类区分鉴权、限流、上游失败和超时；请求 URL 日志做脱敏。调用者不能从自然语言中让 Agent 直接访问任意 URL。

### Q32：工具参数如何 canonicalize

**答：** 参数先按工具契约验证和规范化，再生成稳定语义签名用于去重。证券代码会保留或补充交易所前缀，quote 的多个 symbol 会去重并排序；MCP schema 如果说明数组则按数组编码，HTTP 传输按其协议序列化。无法唯一判断的裸代码保持歧义，不擅自猜市场。

### Q33：是否所有市场工具都支持历史日期

**答：** 不支持。工具注册表和 adapter 的参数契约才决定是否能接受日期。涨停数、涨停板块这两个无参 MCP snapshot 明确 `date_from_request=False`，用户询问历史日期时，不能把最新快照冒充目标日期。即使计划器有日期字段，如果 wire adapter 没有把日期发送给上游，也不能认为查到了历史数据。

### Q34：缓存如何设计

**答：** 项目有进程内 TTL/LRU 缓存，按工具参数签名区分结果，并针对变化速度设置 TTL。行情一般短 TTL，快照和 K 线比静态信息短，新闻、问财各类技能也有配置分档。成功或 partial 结果会深拷贝保存，错误和空结果不应伪装成有数据的缓存命中。

**边界：** 缓存不跨多进程共享，也没有分布式 single-flight 保证。刷新路径要绕过相关缓存和去重保护，不能让旧 partial 结果冒充重新查询。

### Q35：重试策略是什么

**答：** `observed_call` 按有限次数执行；默认每工具可增加一次重试，异常最多有小上限。timeout、429、部分 5xx 等瞬态错误可在预算允许时退避重试；鉴权和参数错误不会盲目重试。重试次数受剩余绝对 deadline 约束。

### Q36：ToolResult 为什么比返回一个 list 复杂

**答：** ToolResult 同时保存规范化数据、原始响应、工具名和 operation、status、partial、reason code、attempts、cache、transport、耗时、run ID、Skill 和源切换信息。这样 Gate 和审计能看到数据质量，日志能定位调用故障，面试官也能理解“上游 success”和“业务可用”之间的差别。

### Q37：内部数据源有哪些

**答：** 除 Gateway 外，项目有同花顺问财查询和公告适配器、新闻聚合、SEC EDGAR 基本面和 filings、AkShare 相关数据、港股北向资金等实现。工具注册表存在部分 placeholder 或共享能力，是否真实可用要看对应 adapter 和验收记录，不能只看 registry 里有一个名字。

### Q38：问财数据是如何归一化的

**答：** 固定 Skill 契约定义 query 或 search 输入，adapter 保留上游动态字段、单位、日期和标的字段，再按返回行构建 NormalizedDatum。归一化时排除请求参数和工具状态等结构字段，检查目标字段、实体和用户询问的指标是否匹配。公告搜索额外检查证券代码和发布日期；无匹配结果只能说当前检索没有证据，不能断言公告不存在。

### Q39：新闻证据的边界是什么

**答：** 新闻来源包括 DDGS、Google RSS 和 AkShare 等适配器，聚合时按 URL 或标题去重并按发布时间排序。新闻标题或摘要可支持“某来源这样报道”的单源陈述，但不能自动证明市场价格、因果或全市场共识。多个转载也不等于多个独立来源确认。

### Q40：SEC EDGAR 路径要注意什么

**答：** 请求必须按 SEC 公平访问政策提供 contact User-Agent；系统用 ticker 映射 CIK，再取 companyconcept 和 submissions。财报标签、币种、单位和 filed/end 日期要保留。10-Q 数值可能是年初至今累计值，不一定是单季度值，所以不能不检查期间口径就讲同比增长。

## 四、证据、推理与审计

### Q41：NormalizedDatum 和 Evidence 有什么区别

**答：** NormalizedDatum 是工具输出里的单条规范化业务数据，保留来源、权威性、时间、单位、实体和完整度。Evidence 是从 ToolResult 的有效 datum 中建出的证据账本条目，并带可引用的 ID。ToolResult 是一次调用记录，Evidence 是模型和审计使用的可追溯事实片段。

### Q42：证据账本如何保证不是模型伪造

**答：** Evidence ID 由代码从工具签名、metric、timestamp 和序号生成稳定标识，Reasoning 输出的引用必须映射到账本真实 ID。模型只选择代码提供的 evidence alias 或 ID，解析后代码用账本数据重新 hydration claim 的来源和日期。未知引用会被剥离并记为 invalid reference。

### Q43：为什么有独立 Gate、Critic 和 Finalize

**答：** Gate 在生成报告前检查数据是否满足基础质量和日期/域条件；Critic 检查报告语义和可能的补查缺口；Finalize 再执行独立终态门禁，决定报告是否 verified、degraded 或 blocked。层次分开可以避免“同一个模型写结论又自己判结论可信”。

### Q44：Evidence Gate 检查哪些内容

**答：** 它过滤 error、空值、纯结构元数据、无效日期或不匹配域/标的的 datum，并汇总有效数据量、域、实体和日期状态。Gate 不会因为存在任何一条工具结果就认定问题已回答；market-level 的日期和来源要求也比普通检索信封更严格。

### Q45：Reasoning 如何控制上下文长度

**答：** Reasoning 从完整证据账本中选取相关证据，按全局和每工具限额组织上下文。复杂报告采用引用选择器方式，让模型返回有限条 claim 和 evidence ID，再由代码从账本 hydration 实际值；窄行情路径可使用 compact 事实输出。模型 completion 被截断或 JSON 不合法时，不把部分内容当成功报告。

### Q46：Structured output 能保证 JSON 业务内容正确吗

**答：** 不能。JSON mode 只约束大致格式。代码还要做 JSON 提取、Pydantic schema 校验、日期和证券代码的 code-owned 检查、字段约束、引用 ID 白名单检查和实体一致性验证。即使格式合法，claim 仍需证据匹配。

### Q47：Critic 是完全由 LLM 决定的吗

**答：** 不是。模型可以给结构化 issue 和建议动作，代码再检查引用是否真实、标的和日期是否匹配、数据是否足以支持数字或方向陈述、工具覆盖是否满足缺口要求。非法或空 Critic 输出会 fail closed，不能因为没有报错就默认 pass。

### Q48：Claim 与 Evidence 的匹配有什么规则

**答：** 报告中的事实、推断、单源消息、比较和因果结论分开标记。窄行情结论需要匹配目标证券、指标和日期；单一价格点不能支持原因分析；新闻消息不能替代价格数据；比较或因果 claim 需要覆盖对应的两端数据。strict verified 还要求来源、authority、retrieved_at、as_of_date、完整度和覆盖等 provenance 字段足够。

### Q49：partial 和 result_partial 有何不同

**答：** `datum.partial` 表示这条业务 datum 自身不完整；`result_partial` 表示整个工具返回集合的覆盖受限。一个集合可以整体 partial，但其中一条有明确标的和完整字段的 datum 仍可支持很窄的事实。代码保留两个标记，避免把整页缺口传染给每一行，也不能用一行完整数据推出完整市场结论。

### Q50：为什么 retrieval time 不能当 as-of 日期

**答：** `retrieved_at` 只说明系统什么时候收到数据，`as_of_date` 说明数据描述哪一天。两者可能相差数日或跨越休市。用抓取日补观察日会制造假同日证据，所以无日期的严格市场契约会拒绝或降级，而不是推断。

### Q51：evidence-only fallback 是怎么工作的

**答：** 当模型报告路径超时或输出无法解析时，Reasoning 可以用已有账本挑出符合条件的少量事实，生成受限报告并附清楚 caveat。之后 Critic 走专用严格审计，不启动自由研究回环；Finalize 仍把它限定为 degraded，且不进入 verified 持久化。没有合格事实时就没有报告。

### Q52：Evidence 上限是否始终是 80

**答：** `build_evidence` 会限制单次构建的账本条目数，并按来源轮转保留，但这是单个合并点的约束，不应表述成任意节点并行或全局运行中的所有证据始终不超过 80。日志和 LLM 上下文还有各自不同的截断限制。

### Q53：实体检查如何防止股票代码混淆

**答：** 系统区分证券是否存在、名称和代码是否对应、本次行情是否有证据。静态常见公司表主要帮助从文本抽取名称，不能证明完整上市名单。实体校验优先用带来源的登记快照或成功工具返回数据；请求参数本身、新闻提到一个公司或错误工具调用都不能证明标的身份。

### Q54：Critic 要求更多工具时怎么避免无意义补查

**答：** 缺口带有 tool key 和 coverage requirement，代码按真实执行结果、参数变体、结果状态、partial 和所需覆盖量判断是否满足。工具曾经被调用不代表缺口满足；同一 endpoint 的兄弟 key 也不能互相替代。如果没有可执行缺口、预算不足或已达回环上限，图会走 finalize 并留下阻断原因。

### Q55：推理不确定时为什么不直接给低置信度报告

**答：** low confidence 是报告内部字段，不等于通过交付审计。系统先检查每条可见结论是否有合格证据；如果没有合格 claim，终态会 blocked 并清空正文。只有能通过受限 claim 审计的事实才可能 degraded 展示。

## 五、预算、降级、日志和记忆

### Q56：四种 delivery status 怎么解释

**答：** `verified` 表示报告通过严格证据审计；`degraded` 表示只保留通过匹配的有限结论，并清楚标注局限；`blocked` 表示未达到安全交付条件，不返回报告正文；`failed` 表示运行本身异常或超时。调用方应以 `delivery_status` 判断是否可展示可信结论，不能用 HTTP 200 或 `errors=[]` 替代。

### Q57：HTTP 200 为什么可能是 blocked

**答：** blocked 是审计结论，不一定是服务器异常。同步 API 可以用 HTTP 200 返回结构化 blocked 终态和 `report: null`；参数错误、上游异常等会有不同 HTTP 状态码。客户端必须读 payload 的 delivery status 和 code。

### Q58：degraded 报告如何构造

**答：** Finalize 根据可匹配 claim 过滤不合格内容，用保留的 claim 重建结构化报告，并清掉无法继续成立的因果或强结论，降低置信度并添加 partial/caveat 标记。它是受限报告，不会再交给模型自由润色来补充无依据内容。

### Q59：budget 默认值有哪些

**答：** 常见默认配置包括总研究预算 300 秒、工具阶段 45 秒、单工具 research timeout 30 秒、LLM timeout 90 秒、max_tool_calls 12、max_research_steps 8、单工具额外重试 1 次、工具并发 3、rewrite 和 research_more 各最多 1 轮、recursion limit 25。P16 真实验收把总预算临时设为 1000 秒，所以其耗时结果不能跟默认配置混为一谈。

### Q60：预算如何防止长链路拖死请求

**答：** 一次请求启动时记录 monotonic deadline，Supervisor、工具阶段和研究回环共享这个截止时间。每阶段检查可用时间，给 Reasoning、Critic、Finalize 留下保留预算；单工具超时和总请求超时各有边界。回环不会重新获得一整份新预算。

### Q61：运行日志会记录什么

**答：** `run_log.py` 输出结构化 JSONL 事件，包括 run start、plan、route、executions、LLM 调用、Critic、finalize、delivery 和 persistence。日志对文本字段脱敏和截断，但保留工具状态、reason code、耗时、计数、日期来源等诊断。`metrics.py` 可把事件按 run 汇总为 token、工具时长、错误、降级率和缺口补齐情况。

### Q62：为什么 metrics 没数据时返回 None

**答：** 没有观测样本与真实测量为零是不同含义。指标聚合将缺失数据表示成 `None`，将实测零保留为 `0.0`，避免把“从未运行过”误读成“失败率为零”或“通过率为零”。

### Q63：SQLite 保存什么

**答：** 当前有 daily states、anomalies、research records 和 conversations 等表。通过 verified 且严格校验、没有禁止持久化的传输错误后，研究、对话 turn 和日状态用事务写入；用 run ID 幂等键防止重复写入。degraded、blocked、failed 不作为可信研究记忆保存。

### Q64：业务数据库和图 checkpoint 有什么区别

**答：** SQLite 业务库保存已通过门槛的研究记录、对话和日状态；LangGraph checkpoint 用来恢复图执行状态。当前项目有前者，没有在 graph compile 时配置后者。不能把“有 SQLite”描述为支持 Agent 中断续跑。

### Q65：持久化事务失败如何处理

**答：** 写事务采用有限的锁冲突重试和退避；非锁类错误不无限重试。保存失败会把 `persisted=false` 记录到响应和日志里，即使上游计算报告可信，保存状态也会独立表示。研究结论可信和数据成功落库是两个指标。

### Q66：当前记忆系统会自动反思吗

**答：** 没有向量检索、自动 reflection 或图 checkpoint。对话历史会给 Supervisor 注入有限条最近 turn；简报可读最近日状态。日状态数据生成目前要注意域隔离，代码存在把同一报告 state label 写入多个启用域的风险，适合列为改进项。

### Q67：简报定时任务如何工作

**答：** Scheduler 在单进程 asyncio 生命周期内按中国时区触发早晚简报，设置宽限窗口且不补跑错过的时间。多个 Uvicorn worker 会各自启动 scheduler，当前没有分布式锁，因此多 worker 部署需加单实例调度或外部任务队列。

## 六、API、前端和附加能力

### Q68：API 有哪些入口

**答：** 有主页、health、早间和晚间简报，以及 `/api/ask` 和 `/api/ask/stream`。ask 请求会校验 question、可选 domain 和 conversation ID。run ID 会进入同步结果或 SSE 事件，用来关联后端日志。

### Q69：SSE 是如何工作的

**答：** 服务端启动后台 pump 消费 LangGraph `astream`，将更新和最终 values 放入 asyncio Queue，再序列化成 SSE。updates 用于节点阶段进度，values 用于终态，周期心跳只用于连接保活，唯一 terminal result 才是最终交付状态。断开或超时会取消并回收 pump。

**边界：** 目前不是 token 级流式生成，也不展示模型思维链或每一个底层工具步骤。进度大致是节点级，前端不能把计划状态当作精确的实时工具状态。

### Q70：为什么前端使用 fetch reader 而不是 EventSource

**答：** 提问接口需要 POST JSON body，而浏览器原生 EventSource 主要面向 GET 连接且不支持自定义请求体。页面使用 `fetch` 获取 response body，再通过 reader 逐块读取和拆分 SSE 事件。

### Q71：前端如何处理用户取消和竞态

**答：** 每次请求持有自己的 AbortController、超时器和 loader 状态。发起新问题时取消旧请求；旧请求的 finally 通过 superseded 标记避免把新请求的 UI 状态清掉。渲染前对文本做 HTML escape，降低用户内容被当作 HTML 执行的风险。

### Q72：页面展示的置信度代表模型校准概率吗

**答：** 不是。部分 UI 的进度/置信展示是按 low、medium、high 映射成固定数值，用于呈现过程，不是经统计校准的正确概率。面试中不能把 85% 之类的 UI 值说成准确率或置信概率。

### Q73：前端目前有哪些工程边界

**答：** 页面是单 HTML 原生实现，包含暗色样式和少量进入、加载、hover 动画。它没有完整鉴权、限流或多租户治理。页面还有 emoji，而项目样式约定希望使用图标；退出动画和无缝形变也没有覆盖每个元素。这些是可见的 UI 迭代方向，不影响其 fetch/SSE 基本机制。

### Q74：异常检测是什么类型

**答：** `detector/anomaly.py` 是基于明确规则和历史值缓存的检测器，不是训练出来的异常检测模型。它覆盖加密资产衍生品和部分 A 股广度规则。对于 funding 单位、比较窗口和历史数据完整性，必须结合输入 schema；当前不能宣称是经过校准的统计告警。

### Q75：新闻和 sentiment 功能是否始终启用

**答：** `news_enabled` 和 `sentiment_enabled` 默认关闭独立 analyst 节点，但注册表里某些新闻工具仍可通过其它 analyst 路径执行。Sentiment 评分基于抓取的讨论内容并通过模型或规则生成，分数不代表概率、全市场民意或独立验证的金融信号。

### Q76：health endpoint 代表服务完全就绪吗

**答：** 不代表。它报告进程、配置、Gateway 通道启动自检状态和 scheduler 状态，不会保证此刻所有上游、模型和每个工具都可用，也不进行完整业务请求探测。生产 readiness 应有独立的依赖探测和场景验收。

## 七、测试、评估、部署与真实验收

### Q77：项目测试覆盖了哪些风险

**答：** 测试覆盖图拓扑、reducer、节点、预算和超时、工具参数、MCP 握手、HTTP 分类、normalizer、缓存、来源路由、证据真实性、claim matching、域过滤、SSE、持久化、并发、部署配置和历史事故回归。当前盘点有 111 个测试 Python 文件、3 个 fixture 文件和 1310 个 `test_*` 函数定义；这是源码盘点数量，不等同于这次执行通过数。

### Q78：本次有跑全量 pytest 吗

**答：** 没有。本次工作是源码、配置和历史验收文档审阅，没有执行全量 pytest，也没有发起真实网络请求。仓库文档所记录的测试结果是历史执行记录，不能冒充本次验证。

### Q79：如何理解最新离线 MCP 验收结果

**答：** 2026-10-14 的语义补标注验收报告记录 8 个测试文件合计 211 passed，Ruff、compileall 和 diff check 也通过；全量 pytest 曾记录 1766 passed 和 1 个被判定为既有环境性 flaky 的失败。这些是当时离线 fixture/测试环境的记录，不能说明真实 MCP 线上报告已经 verified。

### Q80：P16 真实验收做了什么

**答：** P16 通过 FastAPI 的同步和 SSE 公开入口，使用真实 THS、MCP/Gateway、LLM、Critic、Finalize 和独立临时 SQLite 库，覆盖窄行情、全市场、休市日和公告问题。它记录每次工具路线、收集数量、claim 审计、终态和数据库写入，避免用离线 mock 代替真实链路。

### Q81：P16 得到了什么结果

**答：** 2026-10-14 的 16 次请求里，4 次窄行情是 degraded，12 次全市场、休市或公告场景是 blocked，0 次 verified，0 次 failed，数据库三表写入为 0，production_ready=false。窄行情能保留少量合格事实；全市场的部分指数事实不能代表完整市场覆盖。公告接口虽返回 HTTP 200，但满足目标证券和发布日期条件的归一化公告 datum 为零。

### Q82：P16 揭示了什么关键问题

**答：** 它发现默认计划里的指数 quote 曾被误当成用户明确标的，导致全市场证据被过滤；有一条 result_partial 信息在报告 hydration 中丢失；长证据回显让模型输出超时或截断；MCP 载荷缺少可信 provider、日期和业务结构时不能被严格采纳。后续代码修了部分本地问题并增加 selector 输出和 MCP 语义适配，但必须重新跑最新线上验收，才能确认交付改善。

### Q83：为什么 200 HTTP response 不等于任务成功

**答：** HTTP 层只是请求协议结果。返回体中的 delivery status 可能是 blocked 或 degraded；即使工具 HTTP 200，也可能因无匹配业务行、日期或标的缺失而生成零 evidence。用户可用结论的准入由最终交付状态和 claim 审计决定。

### Q84：CI 检查什么

**答：** GitHub Actions 在 Ubuntu、Python 3.11 安装开发依赖，运行 `ruff check`、`ruff format --check` 和 pytest。当前 workflow 确实有格式检查；旧的部分阶段文档有过时描述，面试以当前 `.github/workflows/ci.yml` 为准。

### Q85：依赖版本能完全复现吗

**答：** `pyproject.toml` 声明最低版本范围，没有看到锁文件，所以不应说依赖解析完全锁定。Docker 用多阶段构建 wheel 并以非 root 用户运行，但依赖版本和镜像供应链仍可以通过 lock、哈希校验和镜像扫描加强。

### Q86：部署结构是什么

**答：** Dockerfile 使用多阶段 Python slim 镜像构建和运行，配置非 root UID，暴露 FastAPI 服务并提供健康检查；compose 注入环境配置并挂载持久化卷。当前入口没有完整用户认证和 API 限流，生产环境还需由网关或应用补上访问控制、速率限制、密钥轮换和多 worker 行为治理。

### Q87：如何安全回滚 SQLite

**答：** 运行中 SQLite 使用 WAL 时不能只复制主 db 文件当作一致备份。按 runbook 应通过 SQLite backup API 或停服务后成组备份，恢复后检查 integrity 和索引，再切回应用版本。回滚要同时核对 schema 迁移兼容性。

### Q88：Evaluation framework 的局限是什么

**答：** `app/evaluation.py` 有少量场景用例和启发式检查，可验证基本结构、最低工具要求或某些明显不支持的说法。它不是大规模标注评测集，也没有完整的真值、可复现成本测量或校准准确率指标。token/cost 等字段预留不能当作已有全面生产度量。

### Q89：如何区分离线回放和 live acceptance

**答：** 离线回放验证固定 artifact 或 fixture 的代码分支和状态契约；live acceptance 通过真实入口、真实上游和真实配置观察运行结果。报告必须标明 observation origin，不能把 mock fixture 的成功说成在线数据源成功。P16 脚本把 live 与 replay 做了显式模式隔离。

## 八、项目反思、追问与简历表达

### Q90：你最认可的设计取舍是什么

**答：** 我认可把“可交付性”提升为独立状态机。模型输出不是最终事实，数据来源、日期、标的、完整度和覆盖都要通过代码检查；系统能解释 verified、degraded、blocked 和 failed，而不是仅仅返回一段文字。这让失败更诚实，也便于运营侧定位缺口。

### Q91：最明显的架构改进是什么

**答：** 首先为关键用户场景建立按域、日期和工具能力分层的 live acceptance 基线；其次补齐服务级并发控制、鉴权限流、分布式任务调度和跨进程缓存策略；再完善数据 schema/版本契约、数据库域隔离、checkpoint 或异步任务续跑。每项改进都应有独立验收指标，避免只增加代码复杂度。

### Q92：如果 verified 数量长期为零，你先排查什么

**答：** 先用 run_id 串起阶段日志，分清工具是否执行、是否有业务 datum、日期和 authority 从哪来、Gate 为什么拒绝、Reasoning 引用了哪些账本 ID、Critic 和 Finalize 对每条 claim 的 reason code 是什么。再选一个窄场景做真实链路复现，确认从上游到报告的最短可验证路径，而不是先放宽门禁。

### Q93：怎么提升准确率但又不让拒答过多

**答：** 先按 capability 建立高频问题集和线上样本，测工具字段、覆盖、日期和实体匹配的失败分布。对缺日期、来源缺失、工具覆盖不足分别修上游契约或规划；只在有明确语义保证的字段上增加适配规则。用窄事实的 degraded 与完整问题的 blocked 分开统计，再按误报和拒答成本调策略。

### Q94：怎么测试 MCP 语义适配器

**答：** 建 fixture 覆盖合格和不合格 payload：count=0 合法、负数和小数拒绝、同义 count 冲突拒绝、日期来自原始字段或可信 envelope、无日期时不从 request/retrieved time 补日期、板块身份和结构缺失、混合日期、混合 partial 行、原始输入不可变。然后单独验收 fallback provenance 和最终 strict claim gate。

### Q95：面试官问“你们的 Agent 有幻觉吗”怎么答

**答：** 我不会承诺零幻觉。设计目标是限制没有证据的文本进入可信交付：引用白名单、实体和日期检查、Claim-Evidence matching、严格终态和空报告阻断都能降低风险。仍需要持续监控误匹配、数据质量和模型语义判断，并通过真实样本迭代。

### Q96：为什么要把结构元数据从业务 datum 分离

**答：** `count=59` 可能是某个明确 endpoint 的涨停家数，也可能只是列表行数或分页信息。单靠字段名无法可靠推断含义。工具级语义契约明确哪些字段是业务事实，哪些是结构元数据，避免把容器长度、total 或 trace id 误当成市场指标。

### Q97：source、authority、transport 有什么区别

**答：** transport 是数据怎么传输，例如 MCP 或 HTTP；source 是返回值来自哪个服务或产品；authority 是系统认可该事实的来源身份。三者可能相关，但不能互相替代。尤其不能因为“通过 MCP 收到”就说 authority 是 MCP。

### Q98：如何回答“你们用了 LangGraph 的什么优势”

**答：** 这里主要利用显式节点、条件边、共享状态 reducer 和 `ainvoke/astream` 接口，把计划、并行采集、审计和有限回环组织成可观察控制流。项目没有启用 checkpointer，所以我会聚焦它的编排能力，不会声称获得断点恢复等未配置能力。

### Q99：能不能说系统具备生产级安全

**答：** 目前不应这样说。虽然有参数白名单、日志脱敏、非 root 容器和密钥从环境读取等工程措施，但 API 认证、限流、多租户隔离、依赖锁定和多 worker scheduler 等仍需完善，P16 也明确 production_ready=false。更准确的说法是已实现一部分生产可靠性边界，仍需治理和线上验证。

### Q100：怎么写进简历

**答：** 先写真实负责范围和验证结果，不虚构个人主导或性能提升。可使用这个模板：

> 参与开发基于 Python、FastAPI 与 LangGraph 的多市场研究 Agent，负责【本人负责的模块】；实现【具体设计或故障修复】，将工具结果规范化为带日期、来源、标的和完整度的证据，并通过结构化 claim 审计控制 verified/degraded/blocked 交付；使用【真实测试/验收方式】验证【真实结果】，目前【明确限制】仍需迭代。

## 附录 A：应用代码逐文件索引

以下索引覆盖 `app/` 下全部 Python 和 web 文件。本表负责快速定位代码；细节应以当前工作树为准。

### A1. API 与 Agent 核心

| 文件 | 职责 |
|---|---|
| `app/main.py` | FastAPI 生命周期、健康检查、ask/SSE/brief 接口、HTTP 错误与共用终态组装 |
| `app/agent/orchestrator.py` | 建图、初始化研究 state、运行 LangGraph、绝对 deadline 和错误终态 |
| `app/agent/state.py` | 兼容 AgentState 类型 |
| `app/agent/persistence.py` | verified 交付的事务持久化和幂等保护 |
| `app/agent/evidence_gate.py` | evidence 可用性、域/日期/标的和 claim 匹配规则 |
| `app/agent/prompts.py` | 通用 Agent prompt 模板 |
| `app/agent/prompts_graph.py` | LangGraph 节点规划与审计 prompt |
| `app/config.py` | Pydantic Settings、超时/预算/开关/TTL 和默认配置 |
| `app/cache.py` | 进程内 TTL/LRU 缓存和参数签名 |
| `app/errors.py` | 领域异常类型 |
| `app/evaluation.py` | 小型场景评估与启发式检查框架 |
| `app/llm_json.py` | JSON 提取、对象解析和 LLM 输出错误归一 |
| `app/logging_config.py` | logging formatter、初始化和阶段计时工具 |
| `app/net_env.py` | 代理环境及 NO_PROXY 规范化 |
| `app/cli.py` | 命令行入口和 Markdown 报告渲染 |

### A2. 图编排与节点

| 文件 | 职责 |
|---|---|
| `app/graph/builder.py` | LangGraph 节点、fan-out、Critic 条件边和终态边 |
| `app/graph/state.py` | ResearchState 与列表/字典 reducer |
| `app/graph/budget.py` | 阶段超时、deadline 和下游保留预算 |
| `app/graph/gap_loop.py` | Critic 缺口、工具覆盖要求和补查步数分类 |
| `app/graph/market_plan.py` | A 股市场级最低计划和指数计划规则 |
| `app/graph/iwencai_plan.py` | 问财技能触发词、计划约束和强制工具匹配 |
| `app/graph/metrics.py` | 从结构化事件计算单 run 与多 run 指标 |
| `app/graph/run_log.py` | JSONL 事件、脱敏、plan/route/critic/finalize/delivery 日志 |
| `app/graph/tool_runtime.py` | 工具参数、缓存、源路由、并发、预算、重试和结果聚合 |
| `app/graph/nodes/supervisor.py` | 解析意图、规划工具、分派分析角色和补查计划 |
| `app/graph/nodes/gate.py` | 生成报告前的证据质量检查 |
| `app/graph/nodes/reasoning.py` | 证据选择、LLM 结构化报告、引用解析和 evidence-only fallback |
| `app/graph/nodes/critic.py` | 模型审查、代码级 issue、缺口覆盖和 verdict 路由数据 |
| `app/graph/nodes/finalize.py` | 最终严格审计、受限 degraded 报告和 blocked/failed 清理 |
| `app/graph/nodes/analysts/base.py` | 分析员共用工具选择、校验、执行和 finding 构造 |
| `app/graph/nodes/analysts/technical.py` | 技术类工具采集角色 |
| `app/graph/nodes/analysts/fundamental.py` | 基本面工具采集角色 |
| `app/graph/nodes/analysts/moneyflow.py` | 资金流工具采集角色 |
| `app/graph/nodes/analysts/news.py` | 新闻工具采集角色 |
| `app/graph/nodes/analysts/sentiment.py` | 讨论数据采集和情绪评分角色 |

### A3. Gateway 与工具适配

| 文件 | 职责 |
|---|---|
| `app/gateway/tool_registry.py` | 63 条 ToolMeta 注册及 operation/domain/参数能力索引 |
| `app/gateway/arguments.py` | 参数 canonicalize、transport 参数映射、签名和日志脱敏 |
| `app/gateway/mcp_client.py` | MCP session 生命周期、握手、工具发现/调用和结果解析 |
| `app/gateway/http_client.py` | 固定 registry 路由的 HTTP Gateway client 和错误分类 |
| `app/gateway/normalizer.py` | 上游响应归一化、日期/实体/结构元数据和 partial 处理 |
| `app/gateway/policy.py` | 有限重试、异常分类和调用预算策略 |
| `app/gateway/source_routing.py` | 工具源选择、THS primary/fallback 配置和 capability route |
| `app/gateway/controlled_fallback.py` | fallback 触发条件、provenance 审计及双源诊断 |
| `app/gateway/ths_skill_registry.py` | 同花顺问财 Skill 元数据和调用契约注册 |
| `app/gateway/ths_skill_adapter.py` | Skill 返回结果、来源、日期和错误统一适配 |
| `app/gateway/iwencai_contracts.py` | 固定问财 query/search 工具 key 集合 |
| `app/gateway/mcp_semantics.py` | 涨停家数/板块 MCP 工具的专用语义及日期 lineage 适配 |
| `app/gateway/selfcheck.py` | Gateway/MCP 启动通道检查与状态提示 |
| `app/gateway/stock_codes.py` | 本地证券代码/名称辅助数据 |

### A4. 数据模型、研究、记忆、调度和 web

| 文件 | 职责 |
|---|---|
| `app/models/market.py` | NormalizedDatum、ToolResult 及市场数据状态模型 |
| `app/models/evidence.py` | evidence datum 和来源字段结构 |
| `app/models/research.py` | 域、intent、report、claim 和审计模型 |
| `app/models/response.py` | API ResearchResponse、终态 payload 和 status/code 统一映射 |
| `app/research/evidence.py` | ToolResult 到真实 evidence ledger 的构建和稳定 ID |
| `app/research/reasoning.py` | 报告解析、引用/claim 校验和 evidence hydration 辅助函数 |
| `app/research/trading_calendar.py` | A 股交易日及 requested/planned 日期语义 |
| `app/research/entity_registry.py` | 证券实体快照、代码规范化和身份核验 |
| `app/research/entity_check.py` | 报告文本与结构化证据的实体一致性检查 |
| `app/research/iwencai.py` | 同花顺问财公告 Skill 协议适配 |
| `app/research/iwencai_query.py` | 问财 query2data 指标/行/日期/单位解析 |
| `app/research/iwencai_search.py` | 问财搜索/研报类返回适配 |
| `app/research/news_search.py` | 新闻搜索和 DDGS 辅助封装 |
| `app/research/news_sources/types.py` | 新闻统一数据结构 |
| `app/research/news_sources/ddgs_source.py` | DDGS 新闻来源 |
| `app/research/news_sources/google_rss.py` | Google RSS 读取和解析 |
| `app/research/news_sources/akshare_sources.py` | AkShare 新闻/电报来源 |
| `app/research/news_sources/aggregate.py` | 多新闻源并发聚合、去重和排序 |
| `app/research/sec_edgar.py` | SEC EDGAR 公司概念数据和 filings 查询 |
| `app/research/hk_northbound.py` | 港股/北向资金内部来源适配 |
| `app/detector/anomaly.py` | 基于规则的行情/衍生品异常检测 |
| `app/memory/storage.py` | SQLite 连接、schema、对话、日状态和研究 CRUD |
| `app/scheduler/briefs.py` | 早晚简报生成和进程内定时调度 |
| `app/web/index.html` | 单页原生前端和 fetch/SSE 展示 |

> 包目录中的 `__init__.py` 文件负责 Python 包识别或导出；没有单独业务逻辑的初始化文件不逐行重复说明。


### A5. 包初始化文件

- `app/__init__.py`：Python 包初始化文件。
- `app/agent/__init__.py`：Python 包初始化文件。
- `app/detector/__init__.py`：Python 包初始化文件。
- `app/gateway/__init__.py`：Python 包初始化文件。
- `app/graph/__init__.py`：Python 包初始化文件。
- `app/graph/nodes/__init__.py`：Python 包初始化文件。
- `app/graph/nodes/analysts/__init__.py`：Python 包初始化文件。
- `app/memory/__init__.py`：Python 包初始化文件。
- `app/models/__init__.py`：Python 包初始化文件。
- `app/research/__init__.py`：Python 包初始化文件。
- `app/research/news_sources/__init__.py`：Python 包初始化文件。
- `app/scheduler/__init__.py`：Python 包初始化文件。
- `app/web/__init__.py`：Python 包初始化文件。

## 附录 B：测试文件覆盖索引

当前盘点包含 **111 个 tests Python 文件、3 个 fixture 文件、1310 个 `test_*` 函数定义**。数量来自 AST 文件盘点，不是本次执行的通过数。表中列出每一个测试 Python 文件、函数数量和最多两个代表名称；详细断言以代码为准。

| 文件 | 测试函数数 | 覆盖方向 | 用例示例 |
|---|---:|---|---|
| `tests/conftest.py` | 0 | API、前端、部署、评估、CLI、日志和项目约定 | 未发现以 test_ 开头的顶层测试定义 |
| `tests/test_anomaly_detector.py` | 30 | 新闻、情绪、异常检测和多市场适配 | `test_numeric_extraction`, `test_dict_and_list_return_none` |
| `tests/test_ask_endpoint.py` | 13 | API、前端、部署、评估、CLI、日志和项目约定 | `test_empty_or_non_string_question_is_400`, `test_unsupported_domain_is_400` |
| `tests/test_ask_run_id_correlation.py` | 5 | API、前端、部署、评估、CLI、日志和项目约定 | `test_sync_ask_correlates_all_seven_events_with_one_run_id`, `test_sync_ask_exposes_correlated_run_id_to_the_client` |
| `tests/test_audit_issue_contract.py` | 48 | 证据、claim、实体、交易日期和审计 | `test_evidence_gaps_route_to_research_more`, `test_overstated_wording_is_revise_not_research_more` |
| `tests/test_audit_run_log.py` | 14 | 证据、claim、实体、交易日期和审计 | `test_blocked_fixture_reaches_blocked_without_errors`, `test_blocked_fixture_first_round_already_requests_research` |
| `tests/test_b6_domain_filter.py` | 9 | API、前端、部署、评估、CLI、日志和项目约定 | `test_registry_text_us_stock_exact_six`, `test_registry_text_a_share_excludes_us_and_snapshot` |
| `tests/test_brief_scheduler.py` | 5 | SQLite、对话记忆、简报和调度 | `test_trigger_sequence_and_once_per_day`, `test_seconds_until_uses_fixed_offset` |
| `tests/test_briefs.py` | 4 | SQLite、对话记忆、简报和调度 | `test_morning_brief_uses_graph`, `test_evening_brief_uses_graph` |
| `tests/test_budget_deadline.py` | 4 | 图编排、状态 reducer、预算、并发和研究回环 | `test_deadline_derived_from_investigation_budget_deadline`, `test_expired_investigation_deadline_skips_gateway` |
| `tests/test_cache_capacity.py` | 6 | 工具协议、参数、数据源、缓存和归一化 | `test_ttl_zero_disables_caching`, `test_ttl_none_uses_default_not_zero` |
| `tests/test_cache_multi_domain.py` | 11 | 工具协议、参数、数据源、缓存和归一化 | `test_quote_ttl_short`, `test_sentiment_ttl_medium` |
| `tests/test_candle_summary.py` | 3 | API、前端、部署、评估、CLI、日志和项目约定 | `test_price_last_is_last_close_and_non_candle_kept`, `test_high_low_used_for_range` |
| `tests/test_cli.py` | 5 | API、前端、部署、评估、CLI、日志和项目约定 | `test_none_report_prints_real_error`, `test_none_report_without_errors_still_exits` |
| `tests/test_controlled_degraded.py` | 15 | API、前端、部署、评估、CLI、日志和项目约定 | `test_evidence_only_fallback_requires_explicit_complete_provenance`, `test_mixed_claims_are_filtered_into_degraded_report` |
| `tests/test_conventions.py` | 2 | API、前端、部署、评估、CLI、日志和项目约定 | `test_every_test_module_declares_its_mock_scope`, `test_node_call_replacement_requires_explicit_waiver` |
| `tests/test_conversation.py` | 11 | SQLite、对话记忆、简报和调度 | `test_save_single_turn`, `test_append_multiple_turns` |
| `tests/test_critic_gap_keys.py` | 3 | 证据、claim、实体、交易日期和审计 | `test_critic_prompt_lists_supplementary_tools`, `test_critic_drops_hallucinated_and_executed_gap_keys` |
| `tests/test_critic_verdict.py` | 6 | 证据、claim、实体、交易日期和审计 | `test_route_decision`, `test_revise_exhausted_revisions_goes_to_finalize_audit` |
| `tests/test_data_integrity.py` | 27 | API、前端、部署、评估、CLI、日志和项目约定 | `test_raw_none_without_error_is_not_success`, `test_explicit_error_still_wins` |
| `tests/test_delivery_status.py` | 11 | API、前端、部署、评估、CLI、日志和项目约定 | `test_exhausted_revise_runs_end_as_blocked`, `test_exhausted_research_more_runs_end_as_blocked` |
| `tests/test_deployment_config.py` | 10 | API、前端、部署、评估、CLI、日志和项目约定 | `test_dockerfile_runs_unprivileged_and_declares_port`, `test_dockerfile_has_healthcheck_hitting_health_endpoint` |
| `tests/test_docs_contract.py` | 8 | API、前端、部署、评估、CLI、日志和项目约定 | `test_prompt_evidence_id_example_matches_real_generated_format`, `test_registry_docstring_tool_count_matches_actual` |
| `tests/test_entity_registry.py` | 18 | 证据、claim、实体、交易日期和审计 | `test_successful_limit_up_pool_verifies_guanghe_technology`, `test_successful_quote_code_name_pair_verifies_shenghong` |
| `tests/test_evaluation.py` | 5 | API、前端、部署、评估、CLI、日志和项目约定 | `test_valid_report_counts_tools_from_report`, `test_none_report_records_real_error` |
| `tests/test_evidence.py` | 5 | 证据、claim、实体、交易日期和审计 | `test_evidence_keeps_tool_and_metric`, `test_candle_summary_domain_not_crypto_for_us_klines` |
| `tests/test_evidence_authenticity.py` | 27 | 证据、claim、实体、交易日期和审计 | `test_error_empty_and_request_only_are_not_valid_evidence`, `test_code_only_quote_has_unknown_date_and_cannot_prove_today_limit_up` |
| `tests/test_evidence_cap.py` | 2 | 证据、claim、实体、交易日期和审计 | `test_evidence_capped_and_newest_kept`, `test_evidence_under_cap_unchanged` |
| `tests/test_evidence_gate.py` | 9 | 证据、claim、实体、交易日期和审计 | `test_has_evidence_true_with_success`, `test_has_evidence_false_all_errors` |
| `tests/test_evidence_id.py` | 4 | 证据、claim、实体、交易日期和审计 | `test_different_prefixes_produce_disjoint_ids`, `test_default_prefix_is_evidence` |
| `tests/test_gateway_reuse.py` | 8 | 工具协议、参数、数据源、缓存和归一化 | `test_analyst_run_creates_single_gateway`, `test_gateway_closed_on_tool_exception` |
| `tests/test_gateway_selfcheck.py` | 8 | 工具协议、参数、数据源、缓存和归一化 | `test_actionable_hint_matches_verbatim_failures`, `test_selfcheck_pass_marks_channel_ok` |
| `tests/test_gateway_tool_names_321.py` | 11 | 工具协议、参数、数据源、缓存和归一化 | `test_registry_has_no_legacy_operation_id`, `test_registry_has_no_legacy_name_in_shared_index` |
| `tests/test_graph_e2e.py` | 4 | 图编排、状态 reducer、预算、并发和研究回环 | `test_full_flow_produces_report`, `test_market_level_question_plans_and_executes_market_tools` |
| `tests/test_graph_nodes.py` | 21 | 图编排、状态 reducer、预算、并发和研究回环 | `test_by_category_returns_tools`, `test_by_category_tools_have_required_attributes` |
| `tests/test_graph_routing.py` | 8 | 图编排、状态 reducer、预算、并发和研究回环 | `test_supervisor_returns_intent_on_success`, `test_supervisor_handles_llm_error_gracefully` |
| `tests/test_graph_state_reducers.py` | 2 | 图编排、状态 reducer、预算、并发和研究回环 | `test_same_signature_loop_back_overwrites`, `test_parallel_analysts_still_merge` |
| `tests/test_graph_topology.py` | 21 | 图编排、状态 reducer、预算、并发和研究回环 | `test_build_graph_compiles`, `test_all_p3_nodes_present` |
| `tests/test_hk_northbound.py` | 10 | 新闻、情绪、异常检测和多市场适配 | `test_returns_required_keys`, `test_custom_days` |
| `tests/test_http_client.py` | 1 | 工具协议、参数、数据源、缓存和归一化 | `test_http_mode_requires_api_key` |
| `tests/test_http_client_budget.py` | 4 | 工具协议、参数、数据源、缓存和归一化 | `test_retry_stops_when_deadline_exhausted`, `test_already_expired_deadline_makes_no_request` |
| `tests/test_incident_ashare_stream_fix.py` | 17 | 阶段、事故和端到端验收契约 | `test_incident_mcp_quote_transport_schema_preserves_canonical`, `test_incident_mcp_timeout_is_capped_by_deadline` |
| `tests/test_iwencai.py` | 16 | 工具协议、参数、数据源、缓存和归一化 | `test_announcement_query_filters_wrong_code_and_month`, `test_announcement_normalizes_exchange_suffix_code` |
| `tests/test_iwencai_batch2.py` | 16 | 工具协议、参数、数据源、缓存和归一化 | `test_fixed_contract_runtime_cache_and_refresh`, `test_report_paragraph_identity_rating_and_price_isolation` |
| `tests/test_iwencai_batch3.py` | 13 | 工具协议、参数、数据源、缓存和归一化 | `test_actual_runtime_protocol_refresh_cache_and_ttl`, `test_industry_index_identity_missing_fields_and_per_metric_date_scope` |
| `tests/test_iwencai_index_selector.py` | 17 | 工具协议、参数、数据源、缓存和归一化 | `test_runtime_fixed_official_protocol_actual_refresh_and_cache`, `test_index_point_claim_numeric_date_identity_and_forged_reference_checks` |
| `tests/test_iwencai_query.py` | 21 | 工具协议、参数、数据源、缓存和归一化 | `test_official_contract_per_skill`, `test_finance_metric_unit_period_and_zero_preserved` |
| `tests/test_iwencai_sector_business_research.py` | 13 | 工具协议、参数、数据源、缓存和归一化 | `test_official_protocol_runtime_cache_actual_refresh_and_provenance`, `test_sector_identity_classification_dates_limit_and_partial_remain_explicit` |
| `tests/test_live_report_delivery_followup.py` | 20 | API、前端、部署、评估、CLI、日志和项目约定 | `test_limit_up_sector_query_keeps_user_corrected_terms`, `test_query2data_empty_datas_is_error_without_evidence` |
| `tests/test_logging_namespace.py` | 3 | API、前端、部署、评估、CLI、日志和项目约定 | `test_app_namespace_info_emitted`, `test_mosaic_namespace_info_emitted` |
| `tests/test_market_level_plan.py` | 20 | 工具协议、参数、数据源、缓存和归一化 | `test_market_level_question_is_recognised_by_three_conditions`, `test_market_question_injects_minimum_evidence_set_before_llm_steps` |
| `tests/test_market_memory.py` | 24 | SQLite、对话记忆、简报和调度 | `test_save_and_retrieve`, `test_missing_date_returns_none` |
| `tests/test_mcp_fallback_remediation.py` | 41 | 工具协议、参数、数据源、缓存和归一化 | `test_mcp_quote_datums_get_source_authority_and_retrieved_at`, `test_fallback_provenance_requires_both_source_and_authority` |
| `tests/test_mcp_handshake.py` | 5 | 工具协议、参数、数据源、缓存和归一化 | `test_connect_handshakes_and_lists_tools`, `test_connect_failure_cleans_up_stdio` |
| `tests/test_mcp_semantic_annotation.py` | 25 | 工具协议、参数、数据源、缓存和归一化 | `test_7_1_top_level_count_normalizes_to_single_limit_up_count`, `test_7_1_equal_count_aliases_still_produce_one_business_datum` |
| `tests/test_memory_thread_safety.py` | 4 | SQLite、对话记忆、简报和调度 | `test_save_turn_from_multiple_threads_persists`, `test_same_thread_connection_is_reused` |
| `tests/test_metrics.py` | 11 | API、前端、部署、评估、CLI、日志和项目约定 | `test_parse_lines_skips_broken_and_non_object_lines`, `test_group_by_run_keeps_order_and_marks_missing_run_id` |
| `tests/test_models_multi_domain.py` | 13 | API、前端、部署、评估、CLI、日志和项目约定 | `test_default_domains_contains_expected`, `test_market_domain_type_accepted_values` |
| `tests/test_net_env.py` | 15 | API、前端、部署、评估、CLI、日志和项目约定 | `test_unwraps_bracketed_ipv6`, `test_unwraps_full_ipv6` |
| `tests/test_news_no_symbol.py` | 2 | 新闻、情绪、异常检测和多市场适配 | `test_news_search_executed_without_code`, `test_missing_symbol_skip_logs_warning` |
| `tests/test_news_pipeline.py` | 56 | 新闻、情绪、异常检测和多市场适配 | `test_critic_news_rules_attached`, `test_planner_prompt_has_news_tool_guidance` |
| `tests/test_news_search.py` | 10 | 新闻、情绪、异常检测和多市场适配 | `test_ddgs_available_returns_bool`, `test_ddgs_import_succeeds` |
| `tests/test_normalizer.py` | 3 | 工具协议、参数、数据源、缓存和归一化 | `test_metadata_only_partial_is_error`, `test_partial_with_data_is_preserved` |
| `tests/test_normalizer_empty.py` | 7 | 工具协议、参数、数据源、缓存和归一化 | `test_empty_or_scalar_payload_is_error`, `test_none_payload_is_error` |
| `tests/test_normalizer_enhanced.py` | 15 | 工具协议、参数、数据源、缓存和归一化 | `test_error_path`, `test_flat_dict_extraction` |
| `tests/test_normalizer_f10.py` | 35 | 工具协议、参数、数据源、缓存和归一化 | `test_quote_tool_domain_is_ashare`, `test_search_tool_domain_is_ashare` |
| `tests/test_normalizer_multi_domain.py` | 23 | 工具协议、参数、数据源、缓存和归一化 | `test_ashare_sentiment`, `test_ashare_limit_up` |
| `tests/test_orchestrator.py` | 4 | 图编排、状态 reducer、预算、并发和研究回环 | `test_orchestrator_includes_critique_and_errors`, `test_orchestrator_marks_pass_run_as_verified` |
| `tests/test_p0d_acceptance.py` | 14 | API、前端、部署、评估、CLI、日志和项目约定 | `test_supervisor_research_more_refreshes_partial_empty_and_insufficient`, `test_every_analyst_request_stays_inside_absolute_deadline` |
| `tests/test_p13_evidence_delivery.py` | 26 | 证据、claim、实体、交易日期和审计 | `test_fallback_rejection_vocabulary_is_fixed_and_every_candidate_is_explained`, `test_fallback_decision_and_report_can_never_disagree` |
| `tests/test_p14_verified_path.py` | 8 | API、前端、部署、评估、CLI、日志和项目约定 | `test_complete_graph_state_reaches_verified_and_persists_all_targets`, `test_real_graph_nodes_with_deterministic_complete_tool_result` |
| `tests/test_p15_verified_boundary.py` | 18 | API、前端、部署、评估、CLI、日志和项目约定 | `test_unreferenced_positive_claim_is_blocked`, `test_mixed_claims_are_filtered_and_never_persist` |
| `tests/test_p16_acceptance.py` | 11 | API、前端、部署、评估、CLI、日志和项目约定 | `test_finalize_records_actual_claim_rejection_and_original_metadata`, `test_report_hydration_cannot_drop_upstream_partial_before_persistence` |
| `tests/test_p1e_critic_finalize.py` | 14 | 证据、claim、实体、交易日期和审计 | `test_closed_day_data_gap_is_verified_without_invented_market_claim`, `test_real_quote_or_limit_up_evidence_can_pass` |
| `tests/test_p2_real_acceptance.py` | 3 | API、前端、部署、评估、CLI、日志和项目约定 | `test_live_replay_scenarios_are_correlated_and_redacted`, `test_live_replay_preserves_blocking_and_transport_failures` |
| `tests/test_p3_real_acceptance.py` | 4 | API、前端、部署、评估、CLI、日志和项目约定 | `test_p3_plugin_verify_spawn_and_list_tools_are_real_passes`, `test_p3_failed_calls_never_create_evidence` |
| `tests/test_p4_budget_contract.py` | 3 | 图编排、状态 reducer、预算、并发和研究回环 | `test_cloudflare_error_envelope_is_not_evidence`, `test_stage_budget_reserves_downstream_terminal_time` |
| `tests/test_p4_real_acceptance.py` | 3 | API、前端、部署、评估、CLI、日志和项目约定 | `test_p4_artifact_is_redacted_and_has_real_mcp_smoke`, `test_p4_failed_gateway_results_are_zero_datum_and_never_evidence` |
| `tests/test_p5_stability.py` | 8 | API、前端、部署、评估、CLI、日志和项目约定 | `test_http_200_incomplete_retries_then_preserves_real_empty`, `test_mcp_inband_transient_retries_but_auth_does_not` |
| `tests/test_p6_ths_skill_adapter.py` | 6 | API、前端、部署、评估、CLI、日志和项目约定 | `test_registry_retains_ten_baseline_skills_and_no_mcp_dependency`, `test_adapter_converts_exceptions_to_zero_datum_error` |
| `tests/test_p7_source_routing.py` | 8 | 工具协议、参数、数据源、缓存和归一化 | `test_ths_success_does_not_probe_mcp`, `test_ths_error_uses_mcp_and_keeps_provenance` |
| `tests/test_p8_source_routing.py` | 4 | 工具协议、参数、数据源、缓存和归一化 | `test_default_primary_source_is_enabled_by_real_credential`, `test_runtime_calls_ths_first_and_mcp_only_after_failure` |
| `tests/test_p9_e2e_acceptance.py` | 12 | API、前端、部署、评估、CLI、日志和项目约定 | `test_response_aggregates_auth_and_source_switch_diagnostics`, `test_response_aggregates_source_switch_from_tool_result` |
| `tests/test_packaging_config.py` | 4 | API、前端、部署、评估、CLI、日志和项目约定 | `test_packages_find_directive_is_declared`, `test_include_covers_every_top_level_package` |
| `tests/test_persistence.py` | 5 | SQLite、对话记忆、简报和调度 | `test_unverified_result_never_reaches_memory`, `test_persistence_uses_report_fields` |
| `tests/test_reasoning_parsing.py` | 30 | 证据、claim、实体、交易日期和审计 | `test_truncated_json_completion_fails_closed_with_diagnostics`, `test_compact_target_context_excludes_other_dates_and_instruments` |
| `tests/test_reasoning_traceability.py` | 29 | 证据、claim、实体、交易日期和审计 | `test_valid_reference_is_kept`, `test_forged_reference_is_stripped_and_report_degraded` |
| `tests/test_research_rounds.py` | 11 | 图编排、状态 reducer、预算、并发和研究回环 | `test_default_caps_are_one_round_per_path`, `test_legacy_critic_max_revisions_can_only_tighten` |
| `tests/test_sec_edgar.py` | 35 | API、前端、部署、评估、CLI、日志和项目约定 | `test_contains_contact`, `test_ua_template_never_hardcodes_email` |
| `tests/test_sentiment_analyst.py` | 10 | 新闻、情绪、异常检测和多市场适配 | `test_sentiment_node_skips_when_disabled`, `test_sentiment_node_registered_when_enabled` |
| `tests/test_stage1_quote_contract.py` | 10 | 工具协议、参数、数据源、缓存和归一化 | `test_market_plan_quote_is_symbols_array_and_prompt_uses_canonical_contract`, `test_legacy_symbol_is_converted_only_at_gateway_argument_boundary` |
| `tests/test_stage2_evidence_context.py` | 13 | 证据、claim、实体、交易日期和审计 | `test_stable_ids_survive_new_results_and_reducer_loop`, `test_duplicate_legacy_id_without_source_is_ambiguous` |
| `tests/test_stage3_research_loop.py` | 5 | API、前端、部署、评估、CLI、日志和项目约定 | `test_all_discarded_gap_keys_keep_structured_reasons`, `test_research_more_budget_checks_time_and_tool_calls` |
| `tests/test_stage4_integration.py` | 6 | API、前端、部署、评估、CLI、日志和项目约定 | `test_api_sse_model_and_delivery_log_share_terminal_fields`, `test_persistence_gate_matrix_uses_controlled_memory` |
| `tests/test_stage6_stability.py` | 3 | API、前端、部署、评估、CLI、日志和项目约定 | `test_sse_cancellation_logs_terminal_fields`, `test_persistence_partial_write_rolls_back` |
| `tests/test_stage7_concurrency.py` | 6 | 图编排、状态 reducer、预算、并发和研究回环 | `test_concurrent_verified_persistence_serializes_same_conversation`, `test_concurrent_verified_persistence_across_conversations` |
| `tests/test_stage8_release.py` | 17 | API、前端、部署、评估、CLI、日志和项目约定 | `test_legacy_schema_history_migrates_and_reopens_idempotently`, `test_empty_persistence_keys_are_normalized_and_never_deduplicate` |
| `tests/test_stage9_release.py` | 5 | API、前端、部署、评估、CLI、日志和项目约定 | `test_service_process_health_loads_release_settings`, `test_legacy_backup_migration_failure_restore_and_restart` |
| `tests/test_stream_endpoint.py` | 7 | API、前端、部署、评估、CLI、日志和项目约定 | `test_stream_emits_per_node_progress_then_result`, `test_stream_result_includes_critic_errors` |
| `tests/test_supervisor_gap_loop.py` | 14 | 图编排、状态 reducer、预算、并发和研究回环 | `test_executed_index_only_counts_data`, `test_legacy_executed_keys_path_is_gone` |
| `tests/test_tool_registry.py` | 4 | 工具协议、参数、数据源、缓存和归一化 | `test_registry_resolves_core_tool`, `test_registry_text_contains_core_tools` |
| `tests/test_tool_registry_multi_domain.py` | 27 | 工具协议、参数、数据源、缓存和归一化 | `test_total_tool_count`, `test_all_have_keys` |
| `tests/test_tool_runtime_dedup.py` | 3 | 工具协议、参数、数据源、缓存和归一化 | `test_same_signature_called_three_times_hits_gateway_once`, `test_failed_call_signature_not_registered_allows_retry` |
| `tests/test_tool_runtime_ttl.py` | 2 | 工具协议、参数、数据源、缓存和归一化 | `test_resolve_ttl_uses_settings_for_news_search`, `test_news_search_cache_ttl_survives_execute` |
| `tests/test_trading_calendar_edge_cases.py` | 11 | 证据、claim、实体、交易日期和审计 | `test_today_takes_precedence_over_historical_comparison_date`, `test_explicit_day_with_that_day_stays_historical` |
| `tests/test_trading_date_evidence_edges.py` | 7 | 证据、claim、实体、交易日期和审计 | `test_gate_promotes_only_valid_market_datum_dates`, `test_gate_preserves_stale_observation_but_not_unknown_date` |
| `tests/test_trading_day_semantics.py` | 14 | 证据、claim、实体、交易日期和审计 | `test_weekend_today_keeps_requested_date_and_uses_previous_trading_day`, `test_known_holiday_today_is_closed` |
| `tests/test_truncate.py` | 3 | 工具协议、参数、数据源、缓存和归一化 | `test_truncate_keeps_newest_and_reports_partial`, `test_truncate_under_cap_unchanged` |
| `tests/test_us_stock_domain.py` | 12 | 新闻、情绪、异常检测和多市场适配 | `test_us_stock_domain_has_entries`, `test_us_stock_keys_not_duplicated` |
| `tests/test_web_frontend_contract.py` | 8 | API、前端、部署、评估、CLI、日志和项目约定 | `test_no_global_error_timeout_variable`, `test_error_timeout_is_request_local` |
| `tests/verified_report_fixture.py` | 0 | API、前端、部署、评估、CLI、日志和项目约定 | 未发现以 test_ 开头的顶层测试定义 |

### B1. 固定 fixture 和测试数据

- `tests/fixtures/audit_regression_blocked.json`：198 行固定数据。
- `tests/fixtures/audit_regression_pass.json`：47 行固定数据。
- `tests/fixtures/incident_ashare_stream_fix.json`：12 行固定数据。

> 名称含 real、live 或 acceptance 的用例，不代表每次测试都会请求真实上游；要检查脚本/fixture 和执行模式。历史测试通过记录不是本次验证结果。

## 附录 C：脚本、文档和配置清单

### C1. scripts 入口索引

脚本主要分两类：带显式 `live/probe` 模式的真实验收入口，以及 `replay/verify` 的离线回归和数据适配探测。运行 live 模式前要确认外部请求、费用、临时数据库和 artifact 写入范围。

| 文件 | 用途 |
|---|---|
| `scripts/p2_real_acceptance.py` | P2 Gateway/LLM artifact replay；live 另行显式操作 |
| `scripts/p3_real_acceptance.py` | P3 iiix/MCP 真实调用结果的离线 replay |
| `scripts/p4_real_acceptance.py` | P4 Gateway/LLM/SSE 验收入口 |
| `scripts/p5_real_acceptance.py` | P5 probe、partial-refresh、research、sync/SSE 和 replay |
| `scripts/p6_ths_skill_acceptance.py` | THS Skill probe 与离线契约 replay |
| `scripts/p8_acceptance.py` | P8 来源路由验收 |
| `scripts/p9_e2e_acceptance.py` | P9 端到端 probe/replay |
| `scripts/p10_e2e_acceptance.py` | P10 e2e probe/replay |
| `scripts/p10_focused_acceptance.py` | P10 聚焦 sync/SSE 场景 |
| `scripts/p12_focused_acceptance.py` | P12 聚焦验收 |
| `scripts/p13_focused_acceptance.py` | P13 报告交付与 evidence-only fallback 聚焦验收 |
| `scripts/p14_acceptance.py` | P14 真实入口验收 |
| `scripts/p15_acceptance.py` | P15 离线 Finalize/持久化边界 replay |
| `scripts/p16_acceptance.py` | P16 live 与严格离线 replay；显式 live 才访问真实链路 |
| `scripts/replay_iwencai_probes.py` | 问财探针 artifact replay |
| `scripts/verify_commodities.py` | 大宗商品工具适配/验证入口 |
| `scripts/verify_gateway_321.py` | Gateway 3.2.1 工具名称/协议验证 |
| `scripts/verify_iwencai.py` | 问财适配验证入口 |
| `scripts/verify_news.py` | 新闻来源验证入口 |
| `scripts/verify_sec_edgar.py` | SEC EDGAR 适配验证入口 |
| `scripts/verify_us_market.py` | 美股市场适配验证入口 |

### C2. docs 历史与设计文档索引

仓库中 36 个 Markdown 文档包含 README 之外的架构说明、计划、事故、交接报告和验收记录。旧报告描述的是其日期的实现状态。涉及 Send、图 pass 直出、工具覆盖、日期和 verified 结果时，要以当前源码和最近的验收报告交叉确认。

| 文档 | 主题 |
|---|---|
| `docs/agent-interview-guide.md` | 早期面试笔记，可能未覆盖最新实现 |
| `docs/agent-interview-guide-full.md` | 本手册：基于 2026-10-16 工作树的完整面试问答和代码索引 |
| `docs/architecture.md` | 架构概览和状态流 |
| `docs/audit-evidence-authenticity-remediation.md` | 证据真实性修复思路和验证 |
| `docs/audit-reliability-remediation-plan.md` | 审计可靠性问题清单与计划 |
| `docs/evaluation.md` | 评估方法概览 |
| `docs/gateway_troubleshooting.md` | Gateway 登录/版本/连通排障 |
| `docs/incident-2026-10-10-acceptance-review.md` | A 股交付审查事故记录 |
| `docs/incident-2026-10-10-ashare-stream-fix.md` | A 股 SSE/工具修复事故记录 |
| `docs/iwencai-index-selector-report.md` | 指数和选股问财报告 |
| `docs/iwencai-sector-business-research-report.md` | 板块筛选和经营研究报告 |
| `docs/iwencai-tools.md` | 问财工具能力说明 |
| `docs/live-report-delivery-followup.md` | live 交付跟进及修复背景；部分执行单文字可能落后于当前代码 |
| `docs/mcp-fallback-evidence-remediation.md` | MCP fallback 与证据门禁交接 |
| `docs/mcp-semantic-annotation-acceptance-report.md` | MCP 语义补标注离线验收结果 |
| `docs/mcp-semantic-annotation-execution-plan.md` | 两个 MCP 快照工具语义契约实施计划 |
| `docs/p0d-implementation-handoff.md` | P0-D 阶段实现和自测记录 |
| `docs/p2-real-acceptance-report.md` | P2 线上验收历史记录 |
| `docs/p3-real-acceptance-report.md` | P3 MCP/iiix 验收历史记录 |
| `docs/p4-real-acceptance-report.md` | P4 Gateway/LLM/SSE 交接 |
| `docs/p5-real-acceptance-report.md` | P5 Gateway、预算、缓存和交付历史验收 |
| `docs/p6-ths-skill-acceptance-report.md` | THS Skill 历史实测 |
| `docs/p7-handoff-report.md`、`docs/p8-handoff-report.md`、`docs/p9-handoff-report.md` | 后续阶段交接记录 |
| `docs/p10-handoff-report.md` | P10 端到端验收记录 |
| `docs/p10-main-agent-followup.md`、`docs/p10-main-agent-review.md` | P10 复核及补修记录 |
| `docs/p13-handoff-report.md` | evidence-only fallback、claim 和 sync/SSE 交付修复记录 |
| `docs/p14-handoff-report.md` | P14 交接和 THS 观察 |
| `docs/p15-handoff-report.md`、`docs/p15-main-agent-review.md` | P15 终态/持久化边界与复核 |
| `docs/p16-acceptance-prompt.md` | P16 场景和验收要求 |
| `docs/p16-handoff-report.md` | P16 真实交付数据、claim 审计和未通过结论 |
| `docs/release-runbook.md` | 发布、SQLite 备份和回滚步骤 |
| `docs/sentiment-analyst-plan.md` | Sentiment analyst 设计和历史实施说明，含部分过时计划文字 |
| `docs/tools.md` | 工具注册表说明 |

### C3. 根目录构建、部署和资源

| 文件 | 职责 |
|---|---|
| `README.md` | 项目介绍、运行方式与历史说明；部分架构陈述应对照当前代码 |
| `pyproject.toml` | 构建、依赖范围、pytest、Ruff 和 package-data 配置 |
| `requirements.txt` | pip 依赖安装列表；不提供完整版本锁 |
| `Dockerfile` | 多阶段镜像、运行用户、端口和 health check |
| `docker-compose.yml` | 服务、环境文件、volume 和端口配置 |
| `.dockerignore` | Docker build context 排除规则 |
| `.gitignore` | Git 忽略规则 |
| `.env.example` | 环境变量名称和示例占位，不等于生产密钥 |
| `allowed_openapi.json` | Market Gateway allowlist schema，首行有 Markdown 围栏，文件不是裸 JSON |
| `.github/workflows/ci.yml` | Python 3.11 CI：安装、Ruff lint、Ruff format check、pytest |

## 附录 D：面试前速查表

| 主题 | 准确说法 | 避免说法 |
|---|---|---|
| 图并行 | 条件边返回多个 analyst 节点 | “当前用了 Send API 扇出” |
| 图终态 | Critic pass 仍进入 FinalizeAuditNode | “pass 直接 END” |
| 日期 | requested、planned、observed/as_of 分开 | “计划日期就是数据日期” |
| MCP | transport 与 provider/authority 分开 | “MCP 就是数据权威来源” |
| 工具 success | 协议成功还要过语义、provenance 和 evidence gate | “HTTP/MCP 200 就是证据成功” |
| partial | datum partial 与 result coverage partial 分开 | “只要 result partial 就所有行都不能用” |
| 状态 | delivery_status 决定可交付性 | “errors 为空就是 verified” |
| Memory | SQLite 业务记忆，图未配 checkpoint | “支持 LangGraph 断点恢复” |
| 测试 | 文档保存历史测试结果；本次未运行 | “本次全量测试 1766 通过” |
| P16 | 4 degraded、12 blocked、0 verified，production_ready=false | “P16 验收全部通过” |
| MCP 新适配 | 两个工具的离线语义契约和 fixture 验收 | “所有 MCP 工具都能可信交付” |
| 个人贡献 | 只描述本人实际负责范围 | “把整个仓库能力都说成自己开发” |

### 面试时可以反问

1. 这个岗位更看重 Agent 编排、数据接入、评估治理还是生产服务工程？
2. 团队如何定义线上 Agent 的成功：任务完成率、证据正确率、拒答率、成本还是人工接管率？
3. 目前生产环境如何追踪一次任务从模型计划、工具调用到用户交付的全链路？

### C4. 本次代码和文档盘点范围

自动索引共盘点 268 个自有项目文件：app 86 个、tests 114 个（含测试源码和 fixture）、scripts 21 个、docs 37 个、CI 配置 1 个，以及 README、依赖/构建/部署、忽略规则、环境示例和 Gateway schema 等根目录文件 9 个。运行缓存、虚拟环境、构建目录和运行数据库属于生成或运行数据，不作为应用模块逐项介绍；真实 `.env` 密钥内容不进入面试文档。仓库中部分历史 pytest 临时目录权限受限，因此本清单不声称逐条审阅那些临时产物。
