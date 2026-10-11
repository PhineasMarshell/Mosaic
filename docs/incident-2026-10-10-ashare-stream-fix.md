# A 股市场综述流式调查故障修复单

## 目的

本文把 2026-10-10 这次 `/api/ask/stream` 运行失败整理成可直接执行的修复任务。交给实现 agent 时，按“根因 → 修改点 → 回归测试 → 验收”执行。修复目标是让“今天 A 股发生了什么？”在 Gateway 参数、补证据回环和证据引用都正常时完成一次可审计交付；证据仍不足时必须清晰阻断，不能为了通过而放宽审计或持久化未验证报告。

## 现场结果与影响

运行 `run_id=fb1abaf863bb` 的主要现象：

- 首轮和补查轮的 `get_market_quotes` 都被 Gateway 拒绝：`symbols: Invalid type. Expected: string, given: array`。计划层记录的是 `{"symbols": ["000300"]}` 以及 `{"symbols": ["SH000001", "SZ399001", "SZ399006"]}`。
- `get_ashare_sentiment` 两次各超时 30 秒，`get_limit_up_count` 也发生一次重试并耗时约 39 秒。一个工具失败会消耗大段整次研究预算。
- `list_limit_up_sectors` 首轮返回 `partial`、201 条数据，但 Critic 要求完整覆盖时，回环仍把它当成“已拿到数据的重复步骤”丢弃；第二轮没有真正执行该缺口。
- Reasoning/Critic 收到的是哈希型 evidence id（例如 `technical-58e74920441933cd`），模型却生成 `technical-001`、`technical-004`、`technical-005`、`technical-006` 等不存在的引用。代码正确地把这些引用判为无效，但报告因此产生多条高严重度 `unsupported_claim`。
- 报告还出现没有证据支持的外部事实或实体结论，例如 ASML 韩国替换零部件涨价、财联社 C50 信贷判断、上海普陀算力补贴，以及未经证据确认的板块/个股表述。Critic 只能将其标为 `unsupported_claim`/`invalid_entity`。
- 研究预算耗尽后以 `final_audit_status=research_exhausted`、`delivery_status=blocked` 结束，未持久化。这一终态符合安全门控，但日志中的 `executable_gap_steps=[]`、`blocked_reason` 为空，无法解释“缺口为什么没有执行”。

## 根因与代码定位

### 1. MCP 与 HTTP 的 `symbols` 传输契约不一致（P0）

`app/gateway/arguments.py` 将 Mosaic 内部参数规范化为 `symbols: list[str]`。`app/gateway/http_client.py` 在 HTTP 查询边界把列表序列化成逗号字符串，因此 HTTP 测试通过；`app/gateway/mcp_client.py` 则把规范化后的列表原样传给 `session.call_tool()`。当前 MCP/Gateway schema 实际要求字符串，所以每一次 quote 调用都在数据获取前失败。

相关位置：

- `app/gateway/arguments.py::canonicalize_tool_arguments`
- `app/gateway/http_client.py::MarketGatewayHttpClient.call`
- `app/gateway/mcp_client.py::MarketGatewayClient.call`
- `app/gateway/tool_registry.py`、`allowed_openapi.json` 中 `get_market_quotes` 的 schema
- 现有 `tests/test_stage1_quote_contract.py` 只覆盖 canonical 形状和 HTTP 编码，没有 MCP 编码回归

实施要求：

1. 保留内部 canonical 形状为 `list[str]`，避免 planner、缓存、语义签名和证据追踪继续分裂。
2. 在 Gateway transport boundary 统一编码；MCP 调用前根据已发现的 `inputSchema`（或明确的当前 Gateway 契约）把 `symbols` 编成 `"SH000001,SZ399001"`。不要把字符串形状泄漏回 planner/state。
3. 如果未来 MCP schema 改为数组，编码器应按 schema 类型选择数组或字符串，而不是写死在业务节点。
4. 参数错误返回 `ToolResult(status="error", normalized=[], tool_key="quote", arguments=<canonical list>)`，不得生成成功证据。

验收：

- MCP fake session 收到字符串时调用成功；收到数组 schema 时仍能按 schema 发送数组。
- quote 的 HTTP/MCP 两条通道都保留相同的 canonical `arguments`，日志中可看到传输编码前后的边界。
- 对空列表、混用 `symbol`/`symbols`、类型错误分别有测试。

### 2. 工具超时没有严格受整次研究 deadline 约束（P0）

`app/config.py` 默认 `research_timeout_seconds=30`、`max_retry_per_tool=1`。`mcp_client.py` 每次 `wait_for()` 固定等待 30 秒，未将剩余 deadline 取最小值；重试睡眠也可能继续消耗回环预算。结果是两个超时尝试约 60 秒，后续 reasoning/critic 被迫在更少预算下运行。HTTP client 也需要使用同一 deadline 语义。

实施要求：

1. 每次 attempt 计算 `remaining = deadline - monotonic()`，实际 timeout 为 `min(research_timeout_seconds, remaining)`；剩余时间不足时直接返回 error ToolResult。
2. 退避时间必须 `min(backoff, remaining)`，不能在最后几秒启动一个注定超时的重试。
3. 保持 timeout 的 `status=error`、`partial=false`、`normalized=[]`；在 `note/error` 中标明 attempt、预算和工具名。
4. 对确定性参数错误/工具级 `isError` 不重试；对超时是否重试由剩余预算决定，至少保证不会超过总 deadline。
5. 日志同时记录 `attempt_timeout_ms`、`remaining_budget_ms`、`retry_skipped_reason`，便于区分上游慢和本地预算不足。

验收：

- deadline 只剩 2 秒时，fake MCP 不得等待 30 秒。
- 超时重试总耗时不能超过传入 deadline。
- `sentiment`/`limit_up_count` 的两次超时不会把整次 SSE 运行拖过总预算。

### 3. research_more 回环把“调用过”误当成“缺口已满足”（P0）

`app/graph/nodes/supervisor.py::_plan` 先调用 `drop_repeat_steps()`，而 `app/graph/gap_loop.py::is_repeat_step()` 使用 `executed_index()`。后者把 `success` 和 `partial` 都视为已执行，未读取 Critic 的 `required_coverage`、最小 datum 数量或 `allow_partial=false`。因此 `limit_up_sectors` 的 partial 结果被删除。

此外，`append_gap_steps()` 在没有参数变体时遇到同 key 已存在于 `steps` 会直接 `continue`。当同一 key 已有一个低覆盖度步骤时，它无法插入替代/重试步骤，导致日志出现 `gap_key_decisions=kept` 但 `forced_gap=[]`、`executable_gap_steps=[]`。

实施要求：

1. 把“是否重复”与“是否满足缺口”彻底分开：只有同工具、同参数、且满足本条 issue 的覆盖要求时才可删除；`partial`、空 normalized、低于 `min_datum_count`、不满足时间/标的参数时必须保留重试机会。
2. `drop_repeat_steps()` 接收 coverage requirements，或在调用前把满足性判定结果传入；不得只依据 `status in {success, partial}`。
3. `append_gap_steps()` 对 `decision.kept` 的未满足 key 即使计划中已有同 key，也要生成一条可执行替代步骤：优先使用 `required_coverage.arguments`；没有参数时复用 canonical 参数但允许重试，并在 step purpose 标明“覆盖不足重拉”。
4. 计划截断后重新计算 `planned_gap`；凡是 Critic 判定 `kept` 但没有进入 route 的 key，必须进入 `gap_key_decisions` 的 `budget_truncated`，并设置明确 `blocked_reason`。
5. `executable_gap_steps` 必须记录最终真正进入 route 的步骤，而不是只记录 append 前的候选。

验收：

- `partial + allow_partial=false` 的 `limit_up_sectors` 会重新执行。
- `success + datum_count < min_datum_count` 会重新执行；满足覆盖后才被标记 `already_satisfied`。
- 已执行但参数不同（例如 `000300` 与 `SH000001/SZ399001/SZ399006`）不能错误合并。
- 回归日志不再出现 `gap_key_decisions=kept` 与 `executable_gap_steps=[]` 的无解释组合。

### 4. Evidence id 对模型不友好，导致引用伪造（P1）

`app/research/evidence.py::_stabilize_result_ids()` 为跨轮稳定性生成哈希 id。`app/graph/nodes/reasoning.py::_select_evidence()` 和 `app/graph/nodes/critic.py::_format_evidence_for_review()` 会截断上下文，但没有给模型一个小而稳定的引用别名表。模型看到长哈希后自行生成 `technical-001` 等传统编号，`app/research/reasoning.py` 的 `_validate_claims()`/`_validate_report_references()` 只能事后删除并降级。

实施要求：

1. 不要改写持久化 evidence id；增加每次 Reasoning 调用的短别名映射，例如 `E001 -> technical-58e74920441933cd`，并在 prompt 中明确“只能使用表中 id/别名，不能自行编号”。
2. 解析 LLM 输出前将别名解析回真实 id；未知别名继续按高严重度无效引用处理。
3. 正文中的别名、claims 的 `evidence_ids`、报告 `evidence` 条目使用同一映射；映射完成后再执行现有 valid-id 校验。
4. Critic 的引用优先展示逻辑使用解析后的真实 id；截断说明中列出允许引用的 id 集合，不能让模型根据“technical-001”模式猜编号。
5. 保留现有的 invalid-id 安全策略：未知引用必须被剥离并触发 `unsupported_claim`，不能因为 alias 解析失败而 pass。

验收：

- 哈希 evidence id 的报告使用 `E001` 能被正确映射并通过引用校验。
- 模型返回 `technical-001`、`E999` 或随机字符串时，结果仍是可解释的 `unsupported_claim`，不会生成成功证据。
- 跨 research_more 回环后，同一证据仍映射到同一个真实 id。

### 5. Reasoning/critic 对“只允许证据内事实”的约束不够强（P1）

本次报告包含 ASML、C50 信贷、普陀补贴等外部事实，以及没有当前证据的板块/个股和因果推断。现有代码能阻断一部分无效 entity、无效 evidence id 和强因果，但不能保证模型不把背景知识写成当天事实。`partial` 新闻也不能支持“市场主线/全面反转”这类强结论。

实施要求：

1. 强化 `app/agent/prompts.py` 的 Reasoning prompt：当前事实必须有真实 evidence id；单源必须标注单源；缺日期/来源只能写“无法确认”；不得用模型记忆补齐当天行情。
2. 强化 `app/graph/nodes/critic.py` 的审计规则：无当前证据的外部事实、未校验实体、跨数据源因果、由 partial/截断数据推出全市场结论，都生成结构化 issue。需要数据的 issue 必须 `action=research_more`，可删改解决的才是 `remove_or_qualify`。
3. 对 `partial`/无时间戳/仅单源的 evidence，向模型显式传递限制；不能仅因为 datum 数量大就视为完整覆盖。
4. 不要通过降低 Critic 严格度来减少 `research_exhausted`；安全门控仍应阻断不可信报告。

验收：

- 没有对应 evidence 的 ASML、普陀政策、C50 判断会被删改或进入 `unsupported_claim`，不能 pass。
- “可能/有待确认”的合格限定语可以在现有证据下通过；绝对化因果/全面涨跌表述不能通过。
- `partial` 板块数据不能支持“最密集/主线/全市场反转”等比较或因果结论。

### 6. 阻断终态缺少可解释的 blocked reason（P1）

`app/graph/builder.py::critic_route_decision()` 的 route 日志会记录 `research_budget_exhausted`，但 `blocked_reason` 只在 Supervisor 的 `no_executable_gap_steps` 分支写入 state。进入 `finalize_audit` 时 state 可能没有 `blocked_reason`，所以 finalize/delivery 日志出现空值，用户无法知道是预算耗尽、缺口不可执行还是预算截断。

实施要求：

1. 所有 `finalize_audit` 路径都写入结构化 `blocked_reason`：`research_budget_exhausted`、`rewrite_budget_exhausted`、`no_executable_gap_steps`、`time_budget_insufficient`、`tool_call_budget_insufficient` 等有限枚举。
2. `finalize.py::resolve_delivery()`、`ResearchResponse.delivery_reason`、SSE `result` 和 `run_log` 使用同一 reason，不能只在日志局部拼接。
3. `delivery_status=blocked` 继续清空 report、不持久化、不发送“调查完成”；该行为是安全要求，不要修改成“有 report 就交付”。

验收：

- 本次场景的 finalize/delivery 日志包含非空 `blocked_reason`，并指出 `quote`/`limit_up_sectors` 的未满足要求。
- 同步 API 与 SSE 返回相同的 `final_audit_status`、`delivery_status`、`delivery_reason`、`unresolved_issues`。
- blocked/failed 结果不会写入 conversation、daily state 或 research memory。

## 建议实现顺序

1. 先修 MCP transport 编码和 deadline 约束，运行 quote/MCP/timeout 单测。
2. 再修 gap loop 的 coverage-aware 去重、重试步骤和终态字段，运行 supervisor/gap/research-round 单测。
3. 加 evidence alias 映射和 prompt/critic 约束，运行 reasoning traceability、evidence context、critic 单测。
4. 最后补 SSE/同步终态一致性和一次脱敏端到端回归。

## 必须新增或修改的回归测试

- `tests/test_stage1_quote_contract.py`：MCP transport 的 string/array schema 编码、canonical 参数保留、工具级参数错误。
- `tests/test_http_client_budget.py` 与新增 MCP budget 测试：attempt timeout、deadline 截断、重试退避不超预算。
- `tests/test_supervisor_gap_loop.py` / `tests/test_critic_gap_keys.py`：partial/低 datum 不算 satisfied；同 key 可生成 coverage retry；`forced_gap` 与 `executable_gap_steps` 一致。
- `tests/test_reasoning_traceability.py` / `tests/test_stage2_evidence_context.py`：哈希真实 id、短 alias、未知 alias、跨轮稳定映射。
- `tests/test_delivery_status.py` / `tests/test_stage3_research_loop.py` / `tests/test_stream_endpoint.py`：所有 blocked 原因非空，sync/SSE 终态一致且不持久化。
- 增加一份脱敏 fixture，复现本次 `run_id` 的关键形态：quote 参数错误、sentiment/limit_up_count timeout、limit_up_sectors partial、伪造 evidence id、最终 blocked。

## 完成标准

实现完成必须同时满足：

- quote 在真实 MCP 通道不再因数组/字符串契约失败；
- 工具调用和重试严格服从整次研究 deadline；
- Critic 点名的未满足 coverage 能进入实际 route，且日志可解释被截断/阻断的原因；
- 模型只能引用真实 evidence 或合法 alias，伪造引用仍被阻断；
- 无证据的外部事实、实体和因果不会通过审计；
- `pass` 才能 verified/persisted，所有非 pass 终态都带明确原因并阻止持久化。

