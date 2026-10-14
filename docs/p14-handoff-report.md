# P14 交接报告

## 结论

P14 已把完整可信证据到 `verified` 的路径接通，并保留所有既有工作区文件、`memory.db`、`memory/evening`、JSONL 和日志。没有执行 reset、revert、commit 或 push。生产开关继续为 `production_ready=false`。

完整路径现在要求：`status=success`、非 `partial` / `result_partial`、`completeness=complete|full`、明确 `coverage`、`source`、`authority`、`retrieved_at`、真实 `as_of_date`、请求标的匹配、无权威冲突。同步路径和 SSE 路径共用 response builder；SSE 终态仍只有一个 `terminal=true` 事件。只有 `final_audit_status=pass`、`delivery_status=verified`、`code=ok` 且 Critic `pass` 才允许持久化。

## 实现与回归

- `app/agent/evidence_gate.py` 增加严格 provenance 模式，拒绝 unknown completeness/coverage、缺 source/authority/retrieved_at、partial/result_partial、错误日期或标的。
- `app/graph/nodes/finalize.py` 在 verified 边界执行严格 claim→evidence 校验，记录具体 reason codes；异常工具、权威冲突、forge/unknown evidence reference 不得进入 verified。
- `app/models/response.py` 的统一终态 builder 复用同一严格边界，避免 sync/SSE 漂移。
- inference/comparison/causation 等强推断若只有单一价格证据，会记录 `inference_scope_exceeded` 并过滤。
- `tests/test_p14_verified_path.py` 覆盖真实 graph 节点（确定性 supervisor/reasoning/critic + 完整工具 datum）、SQLite research_records/conversation/daily_state/memory 持久化、sync/SSE 字段一致、单终态事件、严格负向门禁和公告月份语义。Critic `pass` 现在也实际经过 `FinalizeAuditNode`，由同一严格 provenance 审查决定 `verified`。

## 真实 THS 观察

入口：`.venv\\Scripts\\python.exe scripts/p14_acceptance.py`。产物：[memory/p14-acceptance.jsonl](../memory/p14-acceptance.jsonl)。

- `SH600519`, `2026-10-09` 行情 sync/SSE：均为 `degraded`，`persisted=false`。真实返回包含目标日收盘/成交量等行，但同一响应还包含缺真实日期的字段和 `2026-10-14` 行；SSE 还观察到 `unknown_date`、`stale_date`、`status_error`，因此没有 verified。
- 行情 sync fallback：`candidate_count=46`、`accepted_count=35`、`rejected_count=11`；SSE：`17/10/7`。拒绝原因来自真实日期不匹配、缺日期/不完整和独立 quote 失败，未泛化为“critic 过严”。
- `600519 2026年10月公告` sync/SSE：均为 `blocked`，`persisted=false`。上游 HTTP 200 但约 52 KB 返回无法归一化成同时具备真实证券代码和公告日期的行，故未把检索月份或请求标的补入 evidence。
- 行情与公告 SSE 均观察到 `terminal_event_count=1`。本次 probe cleanup 为 `warning`，仅表示 Windows 临时目录清理告警，不影响审计结论。

## 验证命令

验证记录：完整 pytest `1527 passed`；P14、P13、P1E、交付和持久化回归均通过（P14 相关定向集合 `31 passed`）。Ruff、compileall、git diff --check 已通过（diff check 仅报告既有 LF/CRLF 提示）。P9/P10 replay 已执行并保持 `production_ready=false`；P12/P13 已保存 JSONL 与对应回归测试已核对，脚本没有独立 replay 模式，误传 `--mode replay` 会启动 live probe；本轮误启动的 P12 live probe 已中止，未将其写成通过。P14 JSONL 场景行字段完整，`verified_observed=0`、`production_ready=0`。真实网络 probe 受上游返回质量限制，没有观察到真实 `verified/persisted=true`，因此报告没有声称通过该场景。
