# P0-D 实施与自测交接

- 日期：2026-10-11（Asia/Shanghai）
- 基础：`main@513d57e4446b5f467ae57b04723bceba751c3711`
- 状态：实施与主 agent 独立本地验收完成；不是线上验收结论。
- 依据：`docs/audit-evidence-authenticity-remediation.md` 的 P0-D、§6.3、§6.4、§7。
- 保留进入任务时的全部未提交修改；初始 tracked diff 保存于 `memory/p0d-initial-worktree.patch`。未 reset、revert、commit 或 push。
- 没有修改回环配置上限，没有实施 P1-E 或产生 degraded 交付。

## 1. 五条链路的结果

| 链路 | 实施和验证 |
| --- | --- |
| MCP symbols 边界 | 继续使用工作区已有 canonical `list[str]`，只在 MCP transport 按发现的 `inputSchema`/`input_schema` 编成 string 或 array；对象和字典形式的工具 schema 都有模拟覆盖。HTTP 仍按其逗号字符串契约编码。fixture 两次 quote 参数均实际经过 MCP client 双 schema 回放。 |
| 整次 deadline | HTTP 完整响应、MCP 调用和重试受截止时间限制；补修 Analyst 后续均分预算未扣除已耗时间的问题，任何下发 deadline 都不超过研究绝对 deadline。ToolRuntime 使用同一任务内的 `asyncio.timeout`，把按需建连、握手及内部工具纳入预算；已耗尽时不再建立连接，取消建连时清理会话。HTTP 连续分块响应和 fixture 两个超时工具均有本地验证。 |
| 真正 refresh | 保留已有 `refresh` 从 Supervisor → route → Analyst → ToolRuntime 的传递和缓存绕过。补修未声明 required_coverage 时 partial/空结果被去重的问题，避免 partial quote 原标的被丢失后替换成默认指数。status=success 但 partial=true 也不能满足禁止 partial 的要求。三种不足结果在同一用例内先写入缓存，再经真实 Supervisor/Analyst 发出第二次上游请求。 |
| Evidence alias | Reasoning 的证据、修订上下文和完整提示词，以及 Critic 的完整提示词只展示合法短 alias。Critic 返回的结构化文本先映射回真实 ID，再校验和保存。中文紧邻 alias 可解析；未知 alias 保留 high/invalid_reference，不能 pass。Critic 非法 JSON/非法 issue 的 error 门禁保留。 |
| 终态原因与日志 | route 的确定性原因由 finalize 共用；新增 ResearchResponse.blocked_reason，贯通同步和 SSE 的响应及 delivery 日志。无审计结论进入安全失败终态。rationale、claim、查询参数、错误文本等脱敏；合法标的/日期参数保留回放能力；HTTPX INFO URL 也脱敏。代码级 issue 增加 reason_codes，模型不能自由提供日志原因码。阻断运行保留 run_id、issue kind/severity/action、reason_codes、blocked_reason 和 persisted=false。 |

## 2. 本次增量修改文件

进入任务前已有的其他修改仍保留；下表仅列本次进一步编辑或新增的源码/回归文件。

| 文件 | 本次增量 |
| --- | --- |
| `app/gateway/arguments.py` | 默认脱敏任意字符串参数；另提供可回放的合法标的/日期日志参数。 |
| `app/gateway/http_client.py` | HTTPX 请求 URL 脱敏过滤器，覆盖其 INFO 日志中的查询字符串。 |
| `app/gateway/mcp_client.py` | 被 deadline 取消的连接清理，清理错误只记录异常类型。 |
| `app/graph/builder.py` | 缺少 Critique 时走 finalize；未知 verdict 的普通日志不记录原文。 |
| `app/graph/gap_loop.py` | partial 标志一致性；未给具体 coverage 的回环去重仍检查实际覆盖。 |
| `app/graph/nodes/analysts/base.py` | 后续工具的绝对 deadline 上限；异常日志脱敏。 |
| `app/graph/nodes/critic.py` | 模型返回 alias 映射、未知 alias 门禁、代码原因码、解析失败审计事件、异常日志脱敏。 |
| `app/graph/nodes/finalize.py` | 未知 verdict 原因码和 finalize 异常原因码；异常日志脱敏。 |
| `app/graph/nodes/reasoning.py` | 非预期异常日志仅记录类型。 |
| `app/graph/run_log.py` | 自由文本/参数默认脱敏，结构化原因与 issue 原因码保留，delivery 追加 issue 摘要。 |
| `app/graph/tool_runtime.py` | 总 deadline 包含建连和内部工具，取消清理，内部上游异常日志脱敏。 |
| `app/main.py` | 同步/SSE 响应和 delivery 日志传递同一 blocked_reason。 |
| `app/models/response.py` | 新增并组装 blocked_reason。 |
| `app/research/reasoning.py` | 完整提示词/修订上下文的 alias 展示；中文相邻引用解析；无效引用日志不带原文。 |
| `tests/test_budget_deadline.py` | 更新已耗尽预算的断言：运行时直接 error，不发网关请求。 |
| `tests/test_graph_topology.py` | 更新缺少 Critique 的安全终态断言；pass 仍正常结束。 |
| `tests/test_supervisor_gap_loop.py` | 重复调用测试改用真实 normalizer 的非空成功结果，避免把空 success 当成充分覆盖。 |
| `tests/test_p0d_acceptance.py`（新增） | 25 条 P0-D 回归：跨轮缓存刷新、连接超时、绝对 deadline、双向 alias、未知引用、外部无据事实、终态日志、同步/SSE、fixture 双 schema/双超时工具回放。 |

原有 `tests/test_incident_ashare_stream_fix.py` 和 `tests/fixtures/incident_ashare_stream_fix.json` 原样保留并执行。`app/agent/prompts.py`、market_plan、Supervisor、models/research、news_search 等进入任务时已有修改未被撤销。

## 3. 验证结果与复查命令

- 完整 pytest：**1127 passed，0 failed**；1 条 Starlette/AnyIO 的既有 DeprecationWarning。
- focused 事故/新增 P0-D/结构化日志：63 passed。
- `compileall -q app tests scripts`：通过。
- `git diff --check`：通过；Git 的 LF/CRLF 提示不属于 diff whitespace 错误。
- 相关 Ruff：本次新增/修改没有引入新问题；相关文件保留 3 项基线问题，见下表。全量 `app tests` 共 9 项，已逐文件用 `git show 513d57e:<file>` 经 Ruff stdin 验证全部存在于 main 基线。
- 没有提高 max_rewrites/max_research_rounds，也没有降低 Critic、证据 ID、来源/标的/日期和 partial 的门禁。
- P0-A/B/C 回归随完整 pytest 通过：真实表外股票不因静态名单缺失而被判虚假；题材不因公司后缀规则成为公司；错误请求 instrument 不成为存在性证据；周末语义保留休市/最近交易日；无据事实仍不能 pass。

```powershell
.venv/Scripts/python.exe -m pytest -q tests -p no:cacheprovider --tb=short
.venv/Scripts/python.exe -m compileall -q app tests scripts
$p0dChangedFiles = @(git diff --name-only -- '*.py')
& .venv/Scripts/ruff.exe check $p0dChangedFiles tests/test_incident_ashare_stream_fix.py tests/test_p0d_acceptance.py
.venv/Scripts/ruff.exe check app tests
git diff --check
```

本地执行 pytest 使用了仓库内 `--basetemp=memory/p0d-complete`，避免机器上既有临时目录枚举权限问题；不影响被测生产逻辑。

### Ruff 基线（main@513d57e）

| 基线文件/行 | 规则 | 是否在本次相关文件集合中 |
| --- | --- | --- |
| `app/main.py:79` | I001 | 是 |
| `app/main.py:305`（本次内容位移至 310） | F841 | 是 |
| `tests/test_graph_topology.py:11` | F401 | 是 |
| `tests/test_sentiment_analyst.py:10` | F401 | 否 |
| `tests/test_sentiment_analyst.py:15` | F401 | 否 |
| `tests/test_stage1_quote_contract.py:3` | I001 | 否 |
| `tests/test_stage3_research_loop.py:11` | F401 | 否 |
| `tests/test_stage4_integration.py:8` | I001 | 否 |
| `tests/test_stage8_release.py:9` | I001 | 否 |

这些问题没有混报成 Ruff 全绿，也没有为本任务改动无关基线文件。

## 4. 回放与检查产物

- `memory/p0d-pytest-complete.log`：最终完整 pytest 结果。
- `memory/p0d-replay.jsonl`：脱敏结构化回放事件，来自真实节点、route、finalize 和 API delivery 的本地模拟运行。包含 pass、research_more（可继续补查）、revise、blocked 和 error。独立验收场景使用 fixture run_id 前缀及场景后缀，以免不同模拟运行被混为一条。
- `memory/p0d-replay.log`：用于提取结构化事件的测试日志。
- `memory/p0d-ruff-main-baseline.json`：main 基线的 9 项 Ruff 证据。
- `memory/p0d-ruff-related.json`：本次相关文件 Ruff 结果。
- `memory/p0d-ruff-initial.json`：进入实施后的初始全量 Ruff 结果。
- `memory/p0d_check_baseline.py`：只读 Git 基线 lint 对比与脱敏 JSONL 提取辅助脚本。
- `memory/p0d-initial-worktree.patch`：进入任务时未提交 tracked diff 的备份。

## 5. 尚未解决的外部验收缺口

**只能声明本地模拟自测通过，线上链路未验证。** 本轮没有连接真实外部 Gateway/MCP 服务或付费/真实 LLM，也没有验证真实证券主数据、线上 schema、鉴权、供应商取消行为、实际数据完整性或模型审计质量。HTTP 分块回归使用本机 TCP 服务；MCP 与 LLM 使用本地响应替身。这些不构成真实服务已恢复的证据。

非 pass 仍保持 blocked/failed、清空报告正文且不进入 verified 持久化。真实事故的数据缺口能否通过补查补齐，须由主 agent 后续在真实服务上验证；当前没有用降低审计标准或 degraded 绕开它。

主 agent 复核期间另修复了 Finalize 异常分支回显原始异常文本的问题：响应现在只返回结构化 `audit_error` 和通用错误，回归覆盖在 `tests/test_p0d_acceptance.py`。

主 agent 已按原方案 §7 独立验收，核对上述日志与测试结果。仓库方案状态为 P0-D 本地验收完成；线上链路仍待真实环境验证。
