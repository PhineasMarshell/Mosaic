# A 股流式调查修复验收反馈

## 最新复验结论（2026-10-10）

**上一轮的 7 项功能问题全部复验通过。** 当前工作区的修复通过本地功能验收；真实外部 MCP/Gateway 和付费模型仍未连接，因此本结论不包含部署环境的线上链路。

复验结果：

- 原独立探针与正式事故回归一起运行：`30 passed`，其中原独立探针 7 项全部通过。
- 全量 `tests/`：`1020 passed`（20.02 秒，1 条第三方弃用警告）。
- 本次修改的 18 个 Python 文件与新增事故测试：Ruff lint 与 format 均通过。
- `git diff --check`：通过。
- **全仓库 Ruff 未全部通过**：`ruff check app/ tests/` 仍报 9 个错误；对照 `git show HEAD:<file>` 确认这些错误全部存在于基线。`ruff format --check app/ tests/` 仍有 22 个未修改文件需格式化。因此“Ruff 通过”仅适用于本次修改文件，不能作为全仓库 CI 已通过的声明。

复验已确认：股票代码与已知公司名在校验前补参，未知标的返回结构化错误；coverage retry 带 `refresh` 绕过旧缓存并合并重复步骤；本地展示截断保留原始覆盖元信息；HTTP 请求的整体超时生效；合法 claim 文本 alias 正确映射；未验证实体的限定语按句判定；HTTP/MCP 查询日志脱敏；真实 LangGraph 的 route/finalize 阻断原因一致。脱敏事故 fixture 已由正式回归测试读取。

以下为首轮验收历史记录，其中的失败结果和待修复事项已由本轮复验关闭。

## 首轮验收记录

验收日期：2026-10-10。对象：当前工作区相对 `bbb1106` 的未提交改动。

**结论：暂不通过。** 仓库现有 1003 项测试全部通过，但独立补充的 7 个场景均复现了未解决的问题。MCP 的 string/array 编码和 MCP 单次 deadline 修复已通过模拟测试；不能据此认定整个研究流程已修好。

## 验证范围

- 全量：`.\.venv\Scripts\python.exe -m pytest -q tests -p no:cacheprovider --tb=short` → **1003 passed**（19.34 秒，1 条第三方弃用警告）。
- 独立探针：`.\.venv\Scripts\python.exe -m pytest -q .tmp/review_incident_acceptance.py -p no:cacheprovider --tb=short` → **7 failed**。这些用例表达待满足的行为，保留供下一轮修复使用。
- 探针路径：[review_incident_acceptance.py](../.tmp/review_incident_acceptance.py)。它不在默认 `tests/` 收集范围内；实现 agent 应迁入正式测试并补齐相关场景。
- 外部 Gateway/LLM 使用模拟响应；HTTP deadline 用本机回环地址上的真实 HTTP 分块响应验证。没有调用真实 MCP 服务或付费模型，不宣称线上链路通过。
- 本轮未修改业务代码，只增加验收探针与本文。

## 1. [P1] partial 补查仍命中旧缓存，没有真实重拉

定位：`app/graph/gap_loop.py:751`、`app/graph/tool_runtime.py:371`。

新逻辑确实把未满足覆盖的步骤放回计划，但并未把“需要刷新”的语义传到运行时。ToolRuntime 仍缓存 success/partial，并在实际请求前直接返回缓存。独立用例通过真实 analyst 和运行时执行两轮，首轮 Gateway 返回 partial、第二次 Gateway 本应返回完整数据，最终 Gateway 实际调用次数仍是 **1**。第二轮只是再次得到旧 partial。

同时 `append_gap_steps()` 生成“forced 步骤 + 原同签名步骤”，已有新增测试甚至断言 `len(retried) == 2`。执行层随后跳过重复项，计划名额却已被占用；这不是一次额外抓取。

修复要求：把 coverage retry/refresh 策略沿 plan → route → runtime 显式传递；不满足本次 coverage 的缓存不得直接用于这次补查，同签名步骤合并为一次刷新调用。不要关闭所有普通缓存。对本地截断造成的 partial，还需区分原始数据完整性与 prompt 展示截断，否则重拉后仍会被同一截断规则变回 partial。

验收用例：`test_partial_research_retry_fetches_new_data_instead_of_cached_partial`，应看到两次真实 Gateway 调用，第二轮完整结果被消费。

## 2. [P1] 上一轮指出的“先补参再校验”仍未实现

定位：`app/graph/nodes/analysts/base.py:239`（该文件本次没有修改）。

输入“查看600519行情”、planner 输出 `quote(arguments={})` 时，`canonicalize_tool_arguments()` 先报 `missing required argument: symbols`，随后 `continue`，后面的股票代码补参逻辑执行不到。探针记录的实际工具调用数为 **0**，同时节点 `errors=[]`，用户最终可能只得到证据不足。

这不意味着所有不带代码的问题都会失败：市场综述已有默认 `000300` 注入。它意味着“planner 漏参数时系统自行补齐”的承诺仍未达成，甚至问题已经给出代码也会跳过。

修复要求：先按工具契约解析并补齐合法标的，再执行必填校验；真正无法确定标的时保留结构化缺口。公司名称解析不得靠任意默认指数代替，另补“只给公司名”和“市场综述不带代码”的正式回归。

验收用例：`test_quote_parameters_are_filled_from_question_before_validation`，应实际调用 `quote(symbols=["600519"])`。

## 3. [P1] 新增 Gateway 日志绕过查询文本脱敏

定位：`app/gateway/mcp_client.py:237`、`app/gateway/http_client.py:148`。

新增 INFO 日志用 `%s` 原样打印所有工具的 `arguments` 两次。通过 `search_stocks(q=<测试私有查询>)` 的探针，测试查询原文同时出现在 `canonical_arguments` 与 `transport_arguments`。这些日志没有经过 `run_log` 的脱敏逻辑，现有日志 formatter 也不会替其脱敏，回退了原有隐私约束。

修复要求：仅记录参数类型、受限字段和必要的传输形状；使用统一脱敏函数处理 `q/query` 等自由文本。不要为了观察 quote 的数组/字符串编码而输出所有工具的完整参数。MCP 和 HTTP 都要有回归用例。

验收用例：`test_gateway_logs_redact_free_text_query`，测试私有查询不得出现在 INFO 日志中。

## 4. [P2] 合法短别名在 claim 文本中仍被判为伪造引用

定位：`app/research/reasoning.py:74`。

`_resolve_payload_aliases()` 转换了 `claims[].evidence_ids` 和顶层正文，却没有转换 `claims[].claim`。模型返回 `claim="价格见 E001"`、`evidence_ids=["E001"]`，且 E001 明确在当前账本时，后续 `_validate_report_references()` 会把 claim 文本中的 E001 当作不存在的真实 id，产生 `evidence_violations`、降为 low，并强制不能 pass。

修复要求：别名解析与引用校验必须遍历同一组字段，包含 claim 文本；有效 alias 映射后再校验，未知 alias 仍要阻断。审查 `data_caveats` 等可见字段是否也需要同一处理，不要用删除校验解决误报。

验收用例：`test_valid_alias_in_claim_text_is_not_rejected`，合法 E001 不产生 violation，非法 E999 继续产生 violation。

## 5. [P2] 明确标注“未验证”的实体也被无条件阻断

定位：`app/graph/nodes/critic.py:585`。

新增规则只检查 `name in text` 就产出高严重度 invalid_entity，未区分肯定事实与缺口说明。探针文本“行云科技尚未验证，无法确认其行情。”仍触发 issue。Reasoning 若按 remove_or_qualify 要求加上限定语，下轮仍被打回；这与 prompt 中允许写“未验证”的规则冲突，也可能在 data_caveats 中再次触发。

修复要求：按涉及实体的具体句子判定是否断言事实，允许仅说明未知/缺口；同一报告若另有肯定断言仍应被拦截，不能用全篇出现一个“可能”就豁免。

验收用例：`test_entity_explicitly_marked_unverified_is_accepted_as_qualified`；另补明确说其领涨/不存在的反例，确保仍会阻断。

## 6. [P2] HTTP timeout 仍不是请求总 deadline

定位：`app/gateway/http_client.py:154`。

给 HTTPX 设置 `timeout=attempt_timeout` 限制的是连接/单次读取等阶段的等待时间，不是整个响应的墙钟耗时。实际回环 HTTP 服务每 40 毫秒发送一段 JSON，传入 deadline 为 90 毫秒，当前实现约 **230 毫秒后仍返回 success**。只要响应持续有分块到达，工具仍可能长期占用原本留给后续节点的预算。

修复要求：在单次完整 `client.request()` 外增加 `asyncio.timeout()`/`wait_for()` 总耗时边界，保持 HTTPX 的阶段 timeout；处理外层 TimeoutError，继续返回 error ToolResult 并遵守重试预算。

验收用例：`test_http_deadline_caps_whole_response_not_each_chunk`。它使用真实 HTTPX 与本机服务器，没有用忽略 timeout 的 fake request 来制造失败。

## 7. [P2] 在条件边里修改 state 没有持久化真实阻断原因

定位：`app/graph/builder.py:113`、`app/graph/nodes/finalize.py:113`。

`critic_route_decision()` 先把 Pydantic state 转为 dict，再在 `_decide()` 内写 `state["blocked_reason"]`。LangGraph 条件边的局部修改不等于节点 state update。探针使用真实编译的 `StateGraph(ResearchState)`：剩余时间 1 秒、要求至少 60 秒、研究轮次仍为 0，路由正确决定 `time_budget_insufficient`；最终 state 却是 `research_budget_exhausted`，因为 finalize 看不到路由原因，只能执行新增的粗略猜测。

修复要求：由正常节点更新 state 或使用图支持的显式更新机制保存确定性决策；也可让 route/finalize 共用一个读取当前状态和配置的纯决策函数。不要把任意 research_more 终止一律归为轮次耗尽。测试必须覆盖真实图，不只对普通 dict 调函数。

验收用例：`test_real_graph_preserves_time_budget_block_reason`；再覆盖工具调用预算不足、没有报告、没有可执行缺口和实际轮次耗尽，三处 route/finalize/delivery 原因保持一致。

## 其他门禁与测试缺口

- 修改文件的 Ruff lint 未通过：本次新增 `builder.py:121 B010`；`http_client.py F811`、`gap_loop.py I001/F821` 已通过读取 HEAD 确认属于基线问题，不能都归因于本次改动。
- 受检 12 个文件中有 11 个未通过格式检查，包含基线格式问题及新增代码；当前 CI 的 Ruff 门禁仍不能通过。
- 新 fixture `tests/fixtures/incident_ashare_stream_fix.json` 没有被测试引用，不能当作事故回放已完成的证据。
- 新增测试只验证了补查计划长度和直接调用 `resolve_delivery()`，因此没有发现实际缓存与 LangGraph 状态传播问题。修复后至少将以上 7 个失败场景迁入正式测试，再跑全量与 Ruff。

通过条件：以上缺陷修复并有真实执行链路回归，保留现有 invalid-id 阻断和非 verified 不持久化的安全门控；若还没有真实 MCP 服务验收，应明确记录其仍未验证。
