# Live 报告交付后续修复单

状态：待子 agent 实现。本文针对 2026-10-15 run `4c837f7bf56c` 的真实日志，作为独立执行单；不得用真实 THS/MCP/网络作为开发测试。

## 目标

修复下面两条链路，使“今天 A 股发生了什么”这类全市场问题在主源空结果、MCP 有效返回或 reasoning 超时的情况下仍能得到可审计的结果：

1. `limit_up_sectors` 的问财查询词已由用户修正为 `行业板块 概念板块 涨停数量排名`。保留该修复，并防止以后被默认 label 覆盖。
2. MCP 备用结果不能再只显示 `fallback_provenance_insufficient`，必须明确指出缺少 `source`、`authority`、日期、必需字段、标的匹配中的哪一项；若真实响应具备 provider 和日期，应被采纳。
3. 全市场问题没有单一证券代码，reasoning 超时或输出失败时，不能因为 `missing_target_instrument` 放弃所有降级交付。应只交付符合严格事实门槛的市场级窄事实，不能把它们包装成完整市场总结。
4. reasoning 失败时不要再进入“没有报告可修订”的伪研究回环；最终状态应准确表达 `reasoning_timeout`、可交付事实数量和剩余缺口。

## 已确认的 live 证据

本次 run 的关键事件如下，子 agent 必须用这些行为写回归：

- 初始计划包含 `limit_up_sectors`、`telegraph`、`iwencai_market`。
- `limit_up_sectors`：THS `empty_normalized`；MCP `success`、`datum_count=322`、`evidence_created=true`，但最终结果仍是 0 条，并带 `fallback_provenance_insufficient`。
- `telegraph`：10 条 `search_results` 新闻，其中部分日期是 2026-10-15，但不是完整市场数据。
- `iwencai_market`：140 条行业/板块行；大量 `最新价` 无日期，带日期的涨跌幅/成交额等字段存在；结果级 `partial=true`。
- reasoning：请求 90 秒后超时；解析日志显示 `selected_evidence_count=0`，`ledger_evidence_count=160`。
- evidence fallback：`applied=false`，`fallback_enabled=false`，`fallback_reason=missing_target_instrument`。
- critic：`verdict=research_more`，但 `report_claim_count=0`；路由以 `no_report_to_revise` 结束。
- finalize：`delivery_status=blocked`、`final_audit_status=research_exhausted`，reason codes 至少包含 `reasoning_timeout`、`no_report_to_revise`、`empty_normalized`、`fallback_provenance_insufficient`。

## P0：修复真实 MCP fallback 的可解释性和采纳条件

涉及：`app/gateway/mcp_client.py`、`app/gateway/normalizer.py`、`app/gateway/controlled_fallback.py`、`app/graph/tool_runtime.py`、`app/graph/run_log.py`，必要时 `app/models/market.py`。

### P0.1 保留并验证问财查询词

- `app/gateway/source_routing.py` 中 `limit_up_sectors` 的默认 query 必须保持为：
  `行业板块 概念板块 涨停数量排名`。
- 增加回归：调用 `skill_route_for("list_limit_up_sectors", {})` 时 query 包含上述关键词，不能回到 `涨停板块 涨停题材`。
- 增加响应 fixture：`{"code_count": 0, "datas": []}` 必须归一化为 `error + empty_normalized`；同一工具使用 10 条 `datas` 且 `code_count=2030` 时必须产生业务 datum。
- `get_market_quotes` 的默认 query 也必须包含具体字段和标的；禁止把结构正确但 `datas=[]` 的 query2data 响应当成成功证据。

### P0.2 从 MCP 原始响应完整提取 provider

在 MCP boundary 到 normalizer 的链路中明确区分三类字段：

- `transport`：`mcp`，只说明怎么取到数据；永远不能写入 `authority`。
- `source/source_used/provider`：响应声明的真实数据 provider，例如 `tencent`、`ashare-master`。
- `authority`：由明确 provider 白名单归一出来的可追责来源。

具体要求：

1. `_mcp_result_to_json` 必须保留 structured content 的完整 envelope，不得只取 `datas/items` 而丢掉同级 `source/source_used/provider/metadata`。
2. `normalize_tool_result` 除 root 外，还要在约定的 provider envelope（如 `metadata.provider`、`meta.source`、`result.source_used`）中读取 provider；只读取明确字段，不从自然语言 note 或 transport 推断。
3. 若 MCP tool contract 能确定 provider，必须在 `app/gateway/tool_runtime.py` 的 fallback 调用边界显式传入 `declared_provider`，再交给 normalizer；该值必须来自配置/工具注册表的 provider 映射，不能写死成 `mcp`。
4. 若响应完全没有 provider，继续拒绝 fallback，但必须返回结构化缺口，例如 `fallback_missing_provenance:authority`，不能只返回一个总括性的 `fallback_provenance_insufficient`。
5. source、authority、retrieved_at、as_of_date 的写入仍然分离；绝不能用 retrieved time 补交易日期，也不能把 `同花顺问财` 当成 provider authority。

### P0.3 对 fallback 失败给出逐项诊断

新增纯函数，例如 `fallback_provenance_failures(primary, fallback, capability, arguments) -> list[str]`，至少返回以下稳定码：

- `fallback_missing_source`
- `fallback_missing_authority`
- `fallback_missing_retrieved_at`
- `fallback_missing_date`
- `fallback_date_mismatch`
- `fallback_instrument_mismatch`
- `fallback_missing_required_fields`
- `fallback_structural_metadata_only`
- `fallback_partial_datum`

要求：

- `fallback_satisfies_provenance` 继续返回 bool，保持现有调用方兼容。
- Router 在拒绝 fallback 时把逐项码写入 `ToolResult.reason_codes` 和 run log 的 fallback attempt；保留 `fallback_provenance_insufficient` 作为汇总码。
- 日志必须记录 primary/fallback 的 `authority` 集合、日期集合、业务 datum 数量、结构元数据数量、缺失字段组；不得记录完整原始 payload 或密钥。
- `limit_up_sectors` 的顶层 `count/code_count` 只作为结构元数据，不得阻塞 rows 内真实板块行；`rows[*]` 必须按同一行检查板块身份、涨停家数/排名/涨幅和日期。

### P0.4 真实 MCP fixture 回归

新增或扩展 `tests/test_mcp_fallback_remediation.py`：

1. root `source_used=ashare-master`、`rows` 带日期：fallback 被采纳，`source_switch=true`，`datum_count>0`，每条业务 datum 有 `authority` 和 `retrieved_at`。
2. provider 位于 `metadata.provider` 或 `result.source_used`：同样被采纳。
3. 只有 `transport=mcp`、没有 provider：拒绝，并出现 `fallback_missing_authority`。
4. provider 有但 rows 无日期：拒绝，并出现 `fallback_missing_date`。
5. 只有顶层 `count/code_count`：拒绝，并出现 `fallback_structural_metadata_only`。
6. MCP 返回 `datas=[]/code_count=0`：结果保持零业务 datum，不能生成 evidence。

## P1：全市场 reasoning 超时的受控降级

涉及：`app/graph/nodes/reasoning.py`、`app/research/reasoning.py`、`app/graph/builder.py`、`app/graph/nodes/finalize.py`、`app/agent/evidence_gate.py`、`tests/test_p13_evidence_delivery.py` 及新增回归文件。

### P1.1 为全市场问题定义 market-level evidence-only fallback

当前 `plan_evidence_only_fallback` 要求 `target_instrument`，这只适合单标的事实。新增独立模式，不要放宽原有 generic fallback：

- 触发条件：`domain=a_share`、`intent.task in {market_summary, market_diagnosis}`、没有用户点名的唯一证券代码。
- 允许的 evidence：`quote`、`market_breadth/sentiment`、`limit_up_count`、`limit_up_sectors` 的单条完整事实，以及具体日期、来源、标题的单篇新闻。
- 每条必须满足：`status=success`、`datum.partial=false`、`completeness=complete|full`、`source`、`authority`、`retrieved_at`、真实 `as_of_date`；市场级事实的日期必须等于 `planned_as_of_date` 或实际确认的交易日。
- `result_partial=true` 时，只允许交付独立完整 datum；禁止交付“全市场完整、全部、排名、最强、市场情绪已确定”等穷举结论。
- `iwencai_market` 的行业候选行不自动等价于 `market_breadth`；没有上涨家数/下跌家数就不能写“市场涨跌家数”。
- `limit_up_sectors` 的行是结构事实，不得自动写成“最强板块”或完整热点排名，除非覆盖完整性满足该结论。

建议新增 `plan_market_evidence_fallback` / `build_market_evidence_only_fallback`，与单标的 `plan_evidence_only_fallback` 分开，避免改变 P13 通用路径。

### P1.2 超时路径必须真的调用降级构建器

在 `reasoning.py` 的 `TimeoutError` 和 `LLMOutputError` 路径：

1. 先根据 intent 选择 single-instrument 或 market-level fallback。
2. market-level fallback 不因 `target_instrument=None` 直接返回 `missing_target_instrument`。
3. 有合格 rows 时返回固定标题的事实报告，设置：
   - `delivery_status=degraded`
   - `persisted=false`
   - `reason_codes` 包含真实的 `reasoning_timeout` 或 `llm_output_error` 与 `evidence_only_fallback`
   - `partial=true`，并在 caveat 中声明“仅交付可验证事实，不代表完整市场综述”。
4. 无合格 rows 时仍返回 `report=None`，但 diagnostics 要列出每一条候选的拒绝码，不得伪造空报告。

### P1.3 不要把 reasoning 失败转成可补查的 research_more

- `critic` 只在存在 `state.report` 时允许 `research_more` 进入补查；reasoning 失败且没有 report 时，必须产生内部终态 `reasoning_failed_no_report` 或等价 error verdict。
- `app/graph/builder.py` 对 `research_more + report=None` 不再使用含糊的 `no_report_to_revise` 作为唯一解释；保留兼容码，但追加真实上游码 `reasoning_timeout`/`llm_output_error`。
- 不要因为空 report 再消耗一轮 research budget；日志必须显示 `research_round_count` 未被虚增。
- 如果 market-level degraded report 已构建，则不得再进入 critic 的 free-form revise/research_more；Finalize 只做 claim-level strict audit，失败时返回 blocked 并列出原因。

### P1.4 降低全市场 reasoning 的上下文和超时风险

本次 160 条 evidence 仍使 reasoning 90 秒超时。保留完整 ledger，但给模型的 market-summary prompt 使用确定性摘要：

- 每个 market tool 最多保留字段白名单和少量代表行；不要把 140 条行业行全部放入模型上下文。
- `telegraph` 最多保留日期匹配的 3-5 条单篇新闻摘要；其余留在 ledger，不送入首轮 prompt。
- quote/sentiment/limit-up-count 优先级高于行业候选；若这些核心 evidence 缺失，模型必须输出 caveat，而不是等待更多上下文。
- `reasoning_context` 日志要记录 `ledger_evidence_count`、`selected_evidence_count`、每个 tool 的裁剪数量和裁剪原因。
- 不通过提高 `llm_timeout_seconds` 或 `llm_max_completion_tokens` 来掩盖问题；优先缩短 prompt 和使用 bounded output。

## P2：市场级最低计划与工具字段一致性

涉及：`app/graph/market_plan.py`、`app/gateway/source_routing.py`、`app/graph/nodes/supervisor.py`、`app/graph/gap_loop.py`。

- 全市场问题的最低计划必须包含带前缀三指数 quote、sentiment/market breadth、limit-up count、limit-up sectors、telegraph。
- `iwencai_market` 只作为额外行业/板块候选，不得替代最低计划中的 sentiment 或 quote。
- 已经执行过的 baseline quote 不能在 gap 轮被标记为 `budget_truncated`；精确 canonical signature 必须区分保留/丢弃变体。
- `limit_up_sectors` 默认 query 修复只影响 THS primary query，不改变 MCP fallback 的参数契约。
- 对无日期 transport 参数的工具，计划可以保留审计日期，但传输层必须剥除无效 `date`；`limit_up_pool` 等真正按日期查询的工具保留日期变体。

## 验收测试

子 agent 完成后必须运行并报告：

```powershell
$env:RUFF_CACHE_DIR="$env:TEMP\mosaic-ruff-cache"
$env:PYTHONPYCACHEPREFIX="$env:TEMP\mosaic-pycache"

.venv\Scripts\python.exe -m pytest `
  tests/test_mcp_fallback_remediation.py `
  tests/test_p6_ths_skill_adapter.py `
  tests/test_p7_source_routing.py `
  tests/test_p8_source_routing.py `
  tests/test_evidence_gate.py `
  tests/test_evidence_authenticity.py `
  tests/test_p13_evidence_delivery.py `
  tests/test_p14_verified_path.py `
  tests/test_p15_verified_boundary.py `
  tests/test_p16_acceptance.py `
  tests/test_supervisor_gap_loop.py `
  tests/test_stage3_research_loop.py `
  -q

.venv\Scripts\ruff.exe check app tests
.venv\Scripts\python.exe -m compileall -q app
git diff --check
```

必须补充至少 10 个新的离线断言：

1. 问财 `limit_up_sectors` 新 query 不回退。
2. `datas=[]` 仍是空结果，不生成 evidence。
3. MCP root provider 能通过 provenance。
4. MCP nested provider 能通过 provenance。
5. MCP 无 provider 的拒绝原因可解释。
6. 顶层 count 不阻塞真实板块行。
7. market-level reasoning timeout 能生成事实降级报告。
8. market-level fallback 不要求 target instrument。
9. `iwencai_market` 行业数据不能冒充涨跌家数。
10. reasoning 无 report 时不触发 research_more 补查。

## 子 agent 交付格式

完成后只需返回以下内容：

1. 修改文件和对应 P0/P1/P2 项。
2. 新增/修改测试名称。
3. 离线命令的通过数量和警告；不得声称已完成 live acceptance。
4. 一条端到端 fixture 的字段摘要：`fallback_reason`、`fallback_provenance_failures`、`source_switch`、`datum_count`、`as_of_date`、`authority`、`completeness`、`partial`、`result_partial`、`delivery_status`。
5. 明确说明没有把 `mcp` transport 当 authority、没有用 retrieved time 补交易日期、没有放宽 generic evidence-only fallback。

## 约束

- 不调用真实网络、THS、MCP、live acceptance。
- 不执行 commit、push、reset、revert。
- 不改动 `memory/`、`.tmp/`、日志或历史 artifact。
- 不删除或覆盖现有 dirty worktree 修改；手工编辑使用 `apply_patch`。
- 不通过放宽严格审计、把 partial 改成 success、或者把行业候选强行命名为全市场结论来“修复”交付。
