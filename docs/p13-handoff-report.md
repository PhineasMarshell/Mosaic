# P13 证据可交付路径与 sync/SSE 对称性交接报告

## 验收范围

P12 之后真实行情与公告仍全部 `blocked`。P13 的目标是：**真实证据已经采集、但无法形成可审计交付**这一问题必须解决，
同时 Critic、Evidence Gate、日期、标的、来源、partial、权威冲突门禁一条都不放宽。

- 入口：`.venv\Scripts\python.exe scripts\p13_focused_acceptance.py`
- 全量追加式日志：[memory/p13-focused-acceptance.jsonl](../memory/p13-focused-acceptance.jsonl)（30 行 / 5 个 probe）
- 最终 probe 的干净切片：[memory/p13-focused-acceptance-final.jsonl](../memory/p13-focused-acceptance-final.jsonl)（5 行）
- P12 场景复跑：[memory/p12-focused-acceptance.jsonl](../memory/p12-focused-acceptance.jsonl)
- 测试结果原始输出：[memory/p13-test-results.log](../memory/p13-test-results.log)
- 生产开关：`production_ready=false`

场景与 P12 一致（每题都跑 sync 与 SSE）：

| scenario | question |
| --- | --- |
| market | 查询贵州茅台 SH600519 在 2026-10-09 的行情 |
| announcement | 查询 SH600519 2026年10月公告 |

## 最终真实结果（probe `p13-focused-18de1f6ffb07fba8`）

| 字段 | market sync | market SSE | announcement sync | announcement SSE |
| --- | --- | --- | --- | --- |
| status / code | degraded | degraded | blocked | blocked |
| final_audit_status | revise_exhausted | research_exhausted | research_exhausted | research_exhausted |
| finish_reason | length | stop | stop | stop |
| parse_status | invalid_json | valid_json_object | valid_json_object | valid_json_object |
| fallback_enabled | true | true | false | false |
| fallback_applied | true | false | false | false |
| report_is_evidence_only_fallback | true | false | false | false |
| candidate / accepted / rejected | 32 / 26 / 6 | 32 / 23 / 9 | 0 / 0 / 0 | 0 / 0 / 0 |
| rejection_reasons | date_mismatch 6, incomplete 6 | date_mismatch 9, incomplete 5 | {} | {} |
| collected_datum_count | 32 | 32 | 0 | 0 |
| ledger_evidence_count | 32 | 32 | 0 | 0 |
| report_evidence_count / claim_count | 3 / 3 | 2 / 1 | 0 / 0 | 0 / 0 |
| heartbeat / terminal_event | 0 / 1 | 14 / 1 | 0 / 1 | 7 / 1 |
| persisted | false | false | false | false |
| cleanup_status | ok | ok | ok | ok |

- **§二.4 现场成立**：market sync 的 Reasoning 以 `finish_reason=length` / `parse_status=invalid_json` 截断，
  但账本里存在同日期、同标的、字段完整的直接事实，于是得到受控 `degraded`，交付物就是 fallback 报告
  （`report_is_evidence_only_fallback=true`），且 `persisted=false`。
- **公告仍然 blocked，且是诚实 blocked**：上游 HTTP 200、响应体约 53 KB，但过滤后**没有任何一行**同时满足
  “真实返回的证券代码 + 真实返回的公告日期落在请求年月内”，于是工具返回 `status=error` / `empty_normalized`，
  终态 `blocked`（`no_valid_datum`、`no_verified_claim`）。没有用请求参数伪造日期或标的。
- 公告 scenario 的 `fallback_reason=missing_requested_date`：问题是“2026年10月”这种年月口径，
  没有可取证的“某一天”，fallback 判定为不适用（`enabled=false`）而不是把它当成日期匹配。

### sync / SSE 对称性

| 对比字段 | market | announcement |
| --- | --- | --- |
| status | equal | equal |
| code | equal | equal |
| report_present | equal | equal |
| persisted | equal | equal |
| reason_codes | 不等 | equal |
| evidence_ids / claim_refs | 不等 | equal |

公告两条传输七个字段**全部相等**。market 两条不等的原因是 §二.2 自身的前提未满足：sync 与 SSE 各自发起了一次
真实查询，返回的 evidence 集合与 Reasoning 结果本就不同（sync 截断走 fallback，SSE 本轮成文走 degraded 过滤），
因此交付理由不同是**真实差异**而不是传输漂移。传输可控的部分已统一：

- sync 与 SSE 共用 `build_response_from_state` 和新增的 `build_terminal_payload`，`code` 由同一个
  `terminal_code_for` 映射产生；
- SSE 的每个 `result` 事件带 `terminal=true`，每个 `progress`/heartbeat 事件带 `terminal=false`（heartbeat 不可能被读成终态）；
- 探针断言 `terminal_event_count == 1` 且所有 progress 事件既无 `delivery_status` 又显式 `terminal=false`。

“相同输入、相同 evidence 集合、相同 Reasoning 结果 ⇒ 完全一致”由单元测试
`test_sync_and_sse_terminal_payloads_agree_for_identical_state` 用同一份 state 分别经 sync/SSE 构造证明。

### P12 场景复跑（probe `p12-focused-18de20491a1001d8`）

P12 脚本没有 `--mode replay`，因此按要求执行真实复跑（`P12_EXIT=0`），结果追加进
[memory/p12-focused-acceptance.jsonl](../memory/p12-focused-acceptance.jsonl)：

| 字段 | quote sync | quote SSE | announcement sync | announcement SSE |
| --- | --- | --- | --- | --- |
| run_id | c6b2306a85db | ae24c5aca873 | c655d1c46204 | 32495041dcef |
| status / code | degraded | degraded | blocked | blocked |
| final_audit_status | research_exhausted | revise_exhausted | research_exhausted | research_exhausted |
| reason_codes | claims_filtered, degraded_partial_evidence, missing_as_of_date, stale_date, unknown_date | evidence_only_fallback, llm_output_error, missing_as_of_date, unknown_date | empty_normalized, no_valid_datum, no_verified_claim, research_budget_exhausted, status_error | 同 sync |
| collected / ledger | 42 / 42 | 20 / 10 | 0 / 0 | 0 / 0 |
| report_evidence_count / claim_count | 3 / 3 | 3 / 3 | 0 / 0 | 0 / 0 |
| heartbeat / terminal_event | 0 / 1 | 14 / 1 | 0 / 1 | 11 / 1 |
| persisted | false | false | false | false |

关键变化：P12 时期 quote 场景两条传输都是 `blocked`、0 条 claim；复跑后**两条都是受控 `degraded` 且 `persisted=false`**，
即真实行情证据开始能形成可审计交付。quote 两条的 `final_audit_status` 与 `reason_codes` 不同，原因与 P13 probe D 的
market 相同（各自发起独立真实查询：一条本轮成文后经 degraded claim 过滤并标 `claims_filtered` /
`degraded_partial_evidence`，另一条本轮截断走 evidence-only fallback），`delivery_status` 与 `persisted` 一致。
公告两条在 `delivery_status` / `final_audit_status` / `reason_codes` / `persisted` 上完全一致。

## 根因与修复

### §一.1 截断/非法 JSON/空 completion 是否真的进入 evidence-only fallback

是。`app/research/reasoning.py` 在 `finish_reason=length` 时置 `last_parse_status="invalid_json"` 并抛
`LLMOutputError`；空 completion、围栏 JSON、顶层数组同样抛 `LLMOutputError`（`parse_status` 为
`empty_completion` / `invalid_json` / `non_object_json` / `output_error`）。这些异常全部落到
`ReasoningNode` 的 `except LLMOutputError` → `_evidence_fallback_update`。
**P12 之所以 blocked，不是代码路径缺失，而是准入判据把真实行全拒了（见 §三）。**

### §三 行情证据为何被全部拒绝（真实根因）

`app/research/iwencai_query.py` 在 5 个 datum 分支把 datum 级 `completeness` 硬编码成 `"page"`。
这个 token 描述的是**响应信封**（分页的一页），不是 datum 自身字段是否完整；而 evidence-only fallback 要求
`completeness ∈ {complete, full}`，于是 100% 的真实行情行被判 `unknown_completeness` 拒绝，
即使它们同时具备：

- 真实标的（`股票代码 600519.SH` → `SH600519`）
- 真实日期（从上游列名 `收盘价[20261009]` 解析出的 `2026-10-09`，不是请求参数回填）
- `source`/`authority`=同花顺问财、`retrieved_at` 非空、`coverage=instrument`、`status=success`、`partial=false`

修复：新增 `_datum_completeness(*, as_of_date, value, instrument, instrument_scope, clipped, scope)`，
只有“确实带自己的日期/标的/取值且未被裁剪”才返回 `complete`，否则 `partial`；5 个 datum 分支全部改用它。
结果就是最终 probe 里行情从 `blocked` 变成受控 `degraded`。
`partial` 语义、result 级信封 `completeness` 以及 Evidence Gate 判据均未改动。

### §四 公告证据为何被拒绝

`app/research/iwencai.py::normalize_announcements` 做了三处收紧：

1. 取不到可验证证券代码的行**不再**以 `instrument=None` 放行，直接计入 rejected；
2. 有年月口径时，没有真实公告日期的行不能过关（日期绝不从检索时间回填）；
3. datum `completeness` = `complete` 仅当“有真实公告日期且恰好一个真实标的”。

代码格式 `600519` / `600519.SH` / `SH600519` 均可识别并归一到同一标的（`_same_instrument` 按 6 位代码比较）。
修复后真实公告请求得到的是诚实 `blocked`：返回体里没有一行能同时满足真实标的与真实年月。

### §一.2/§一.3 fallback 结构化诊断

新增 `FallbackCandidate` / `FallbackDecision` / `plan_evidence_only_fallback`：

- 固定拒绝词表 `FALLBACK_REJECTION_REASONS`：`status_not_success`、`partial`、`incomplete`、
  `unknown_completeness`、`unknown_coverage`、`date_mismatch`、`symbol_mismatch`、`missing_source`、
  `missing_retrieved_at`、`unsupported_fact_type`、`authority_conflict`；
- 每个候选都留档：`candidate_rejections`（逐候选完整拒绝原因映射）、`rejected_candidates`（截断的人读视图）、
  `candidate_evidence_ids`（accepted / rejected）、`candidate_count` / `accepted_count` / `rejected_count` /
  `rejection_reasons`；
- 判定在**每一次** Reasoning 调用后都记录（`phase=output_error|timeout|not_required`），
  因此“本轮没降级”和“根本没判定过”在 JSONL 上可区分；
- 诊断同时落在 `ResearchResponse.fallback_diagnostics`（完整、不截断）与一条有界的
  `evidence_fallback` run_log 事件上。

准入判据严格按 §一.4：`status=success`、`partial=false`、`completeness ∈ {complete, full}`、
`coverage` 明确且非 `unknown`、`source` 非空、`retrieved_at` 非空、`requested_date` 与 evidence 日期一致、
`requested_target` 与 evidence 标的同码、事实类型允许直接陈述、无权威冲突。
交付物只含直接事实（`claim_type=fact`），不含推断、排名、全市场结论或“新闻导致价格变化”类结论。

### §一.6 不靠加回环/加 token 掩盖问题

`effective_max_rewrites`、`effective_max_research_rounds`、`llm_max_completion_tokens`（仍是 2048）与
`graph_recursion_limit` 都没有改动；fallback 只读取已有账本，不发起任何新的研究或模型调用。

### §二 一并修掉的一个真实审计漏洞（P13 才被激活）

`ReasoningNode` 正常成文分支过去**无条件**返回 `{"report": report, ...}`，于是“先 fallback、后成文”的场景里：
模型的新报告覆盖了 fallback 报告，但那次失败留下的 `delivery_status="degraded"`、
`degraded_reason_codes=["evidence_only_fallback","llm_output_error"]` 仍留在 state 上。
`finalize` 看到这个陈旧标记后会把**普通报告**当成 fallback 报告直接放行，跳过
`build_degraded_report` 的 claim 级过滤，并给出不真实的交付理由。P12 时期这条分支是死代码（fallback 永远返回 None），
P13 修好 completeness 传播后被激活。

修复（`app/graph/nodes/reasoning.py` 正常成文分支）：

```python
out["degraded_reason_codes"] = []
out["reason_codes"] = [str(code) for code in (state.get("reason_codes") or [])
                       if str(code) not in _STALE_FALLBACK_CODES]
if state.get("delivery_status") == "degraded":
    out["delivery_status"] = None
```

即：一次正常成文取代 fallback 产物时，必须清掉那次失败留下的终态标记与降级码（只清标记，不改路由、不加回环、不动预算）。

顺带修正 `finalize` 的交付理由：`degraded_codes` 改为只列**真实发生过**的原因码，
超时截断的 run 不再被写成 `llm_output_error`。

**现场证据**：最终 probe 的 market SSE 正是“截断被后续成文取代”的情形，修复后的交付物是模型报告经
`build_degraded_report` 过滤的结果（`degraded_partial_evidence`、`claims_filtered`，3 条 claim 只剩 1 条，
`report_is_evidence_only_fallback=false`）；修复前同样的路径会被误标为 fallback 交付并**原样放行**。

## 测试

新增 [tests/test_p13_evidence_delivery.py](../tests/test_p13_evidence_delivery.py)（26 个用例，全部本地构造、不联网、不写 memory.db）：

- §一：拒绝词表固定且每个候选都有解释；判定与报告不可能不一致；无显式日期/标的时 `enabled=false`；
  权威冲突被拒；信封 token（`page`/`search_results`/`unknown`/空）不被当作 `complete`；正常成文也留准入判定。
- 截断路径：`length` + 合格证据 ⇒ `degraded`；非法 JSON + 合格证据 ⇒ `degraded`；`length` + 不完整证据 ⇒ `blocked`；
  日期不匹配 / 标的不匹配 ⇒ `blocked`；缺 `source`/`retrieved_at`、`unknown` completeness/coverage ⇒ `blocked`。
- 陈旧标记：fallback 之后再成文必须清标记、保留无关旧码，并且带伪造引用的正文重新被审计拦下（`invalid_reference`）；
  超时 fallback 的理由码不得写成 `llm_output_error`。
- §二：同一 state 下 sync/SSE 终态一致；SSE 唯一终态且 progress/heartbeat 永不终态；`terminal_code_for` 全覆盖。
- §三：THS datum completeness 描述自身字段；无自带日期的行不算 complete；真实单标的同日 evidence 能走到受控 degraded；
  全市场行不能顶替单只股票。
- §四：`600519`/`600519.SH`/`SH600519` 均可识别；错误月份/错误标的/无返回日期 ⇒ 拒绝；
  真实匹配公告可交付（经 degraded 过滤保留其 claim）；公告证据不能支持价格涨跌（`news_mention_not_price`）。
- fallback 交付不落库（`persisted=false`）。

## 验证记录

全部通过：

- `.venv\Scripts\python.exe -m pytest -q` → **1510 passed**，1 warning（starlette `BlockingPortal` 弃用告警），30.25s，exit 0
  （本次运行把 `MOSAIC_MEMORY_DB` 指向临时文件，因此不写真实库；原始输出见
  [memory/p13-test-results.log](../memory/p13-test-results.log)）
- `.venv\Scripts\python.exe -m ruff check app tests scripts` → All checks passed
- `.venv\Scripts\python.exe -m compileall -q app tests scripts` → exit 0
- `git diff --check` → exit 0（仅 LF→CRLF 提示）
- `.venv\Scripts\python.exe scripts\p9_e2e_acceptance.py --mode replay` → exit 0，12 场景全部 `not_observed`
  （沿用既有 artifact 状态，非 P13 回归）
- `.venv\Scripts\python.exe scripts\p10_e2e_acceptance.py --mode replay` → exit 0，`not_observed=[]`
- `.venv\Scripts\python.exe scripts\p13_focused_acceptance.py` → `P13_EXIT=0`，`cleanup_status=ok`，
  4 个传输全部写入 JSONL，每行都带 `run_id`、status/code、`reason_codes`、`finish_reason`、`parse_status`、
  `candidate_count`、`accepted_count`、`rejection_reasons`、`collected_datum_count`、`ledger_evidence_count`、
  `report_evidence_count`、`claim_count`、`heartbeat_count`、`terminal_event_count`、`persisted`、`cleanup_status`
- `.venv\Scripts\python.exe scripts\p12_focused_acceptance.py`（P12 无 `--mode replay`，按要求执行真实复跑）→ `P12_EXIT=0`，
  `probe_run_id=p12-focused-18de20491a1001d8`，quote sync/SSE 均为受控 `degraded`（`persisted=false`），
  announcement sync/SSE 均为 `blocked`，SSE `terminal_event_count=1`

说明：`ruff format --check` 显示 84 个文件会被重排，说明**本仓库不以 ruff format 为准**，只强制 `ruff check`。

## 保留与安全

- 未执行 `git reset` / `git revert` / `git commit` / `git push`；`HEAD` 仍是 `a938dd7`（`fix: stabilize production research delivery`）。
- 工作区既有修改全部保留（43 个已跟踪文件变更，2531 insertions / 326 deletions，另有 P6–P13 期间新增的未跟踪文件）。
- `memory/memory.db` 未被删除、重置或覆盖（只读校验：`anomalies` 0、`conversations` 356、`daily_states` 16、`research_records` 1472；
  约 11.41 MB）。计数比 P13 开始时略增（348 → 356、1456 → 1472），这是仓库既有测试中有一部分走默认 DB 路径、
  由应用自身 `persist_research` 正常写入所致（P13/P12 探针脚本都把 `MOSAIC_MEMORY_DB` 指向临时目录，不写真实库）；
  P13 本身没有执行任何删除或重建。`memory/evening/`、既有 JSONL 与运行日志均保留。
- `memory/p13-focused-acceptance.jsonl` 与 `memory/p12-focused-acceptance.jsonl` 为追加式：本轮只新增行，未修改历史行。
- 脱敏已校验：P13 artifact 中不含提问原文、标的名、completion 正文或凭据字段（仅保留计数与原因码）。
- `memory/p13-focused-acceptance-final.jsonl` 是从最终 probe 过滤出的 5 行干净切片（4 个场景行 + `run_complete`）。

## 未改变与未声称

- `production_ready` 保持 **false**。本轮没有任何场景达到真实完整标准：真实行情只能到受控 `degraded`
  （`persisted=false`），真实公告因返回行无法同时满足真实标的与年月口径而保持 `blocked`。
- 没有观察到完整的认证级证据集合，也没有观察到任何 `verified` 交付；`persisted=true` 的次数为 0。
- 行情 **sync/SSE 两条真实行不是逐字段相同**：它们各自发起真实查询，evidence 集合与 Reasoning 结果不同，
  §二.2 的前提不成立；相同前提下的对称性由单元测试证明。
- 公告路线即便上游返回了行（HTTP 200、约 53 KB），也仍不可交付 —— 这是审计结论，不是采集失败。
