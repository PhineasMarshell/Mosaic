# P15 交接报告

P15 已修复 Critic `pass` 时无引用正向 claim 绕过 Finalize 并持久化的漏洞。完整 pytest 为 **1592 passed**；新增 P15 回归为 65 条。工作区已有修改在其当前内容上增量修改，既有 `memory.db`、`memory/evening`、JSONL 和日志均保留，没有执行 reset、revert、commit、push。全部验收使用确定性数据和临时 SQLite；没有本轮真实网络 verified 验收，`production_ready=false`。

## 审计边界

- `app/agent/evidence_gate.py` 的严格模式逐条检查 claim，包括缺失/空引用和所有被引用 datum。空引用正向 claim 返回 `missing_reference`；类型标签 `structure`、`other`、`inference` 或相邻限制语不能免除校验。
- 无证据例外只接受完整的代码可识别休市/数据缺口模板。休市与请求日期必须符合代码元数据；“未验证”“暂无可核验数据”和最近交易日行情未核实等不得附带价格、行情方向、公告或实体正向事实。
- 日期、公告月份与请求标的校验移入共享 matcher，Finalizer 收集每一行的 `reason_codes`，不再通过 `if row.get("evidence_ids")` 跳过整行。严格模式保留每条引用的 source、authority、retrieved_at、coverage、completeness、partial、result_partial、日期、标的、域及 datum 有效性失败；另一条合格引用不能掩盖这些问题。
- 缺省 `completeness=unknown` 也拒绝 verified。真实状态账本显式为空时，不再拿模型报告内的 evidence 替代空账本；受控 degraded 使用同一账本选择规则。
- verified 同时要求 Critic `pass`、report 存在、至少一个可交付 claim、逐 claim 严格校验通过、没有权威实体冲突/无效引用及独立工具失败。空 claims 阻断为 `no_verified_claim`；没有 report 为 failed/`no_report`。
- 混合报告的无据 claim 会阻断 verified；能够隔离时，从合格 claim 重建 degraded 正文并删除无据内容。`missing_reference` 与 `verified_provenance_failed` 仍保留。forged/unknown ID 使用项目既有代码 `invalid_reference`，不可通过删除单行掩盖。

## 响应与持久化

`app/models/response.py` 将严格审计原因带到最终 response，修复“报告被清空，但原因未透出”的问题；sync/SSE 使用相同 builder，新增回归确认空引用报告都得到 blocked、`report=null`、`persisted=false`，SSE 仍只有一个终态事件。

`app/agent/persistence.py` 在任何写入和默认 memory 初始化前复用 `_strict_verified_issues`。即使直接构造 `delivery_status=verified`、`final_audit_status=pass`、Critic `pass` 的响应，缺引用 claim 仍返回 False、`persisted=false`，在响应及日志记录 `missing_reference` / `unverified_claim`，不写 research_records、conversations、daily_states 或可复用 memory。

旧测试中依赖“空 claims + pass 即 verified”的成功输入已更新。网络空数据的图接线测试使用明确的数据缺口结构 claim；存储、事务回滚、并发和重试测试使用完整引用的行情 fixture，继续真实进入写入/失败分支。P1E 的公告成功 fixture 补齐 `completeness=complete`；没有放宽严格门禁来兼容旧 fixture。

## P14 统计修复

`scripts/p14_acceptance.py` 将三个指标保持独立：

| 指标 | 来源 |
| --- | --- |
| collected_datum_count | 同 run_id 的 executions datum_count；无 executions 时保留响应中的统计 |
| ledger_evidence_count | 同 run_id 的 reasoning_context / critic_context 中真实 ledger_evidence_count 的最大值；没有账本日志时为 null |
| report_evidence_count | 最终 report.evidence 长度；blocked 报告为 0 |

日志收集器兼容 run_log 直接事件 JSON 和包装在 `msg` 中的 JSON，避免实际事件被全部忽略。回归测试确认原账本 46 条、报告只保留 1 条时两个数字分别为 46/1；blocked 清空报告仍保留账本 46；不串入其他 run_id。

## 复现前后对照

保留的 P14 Finalize/persistence 代码快照来自 `.tmp/p15-baseline-1791965026500629600`，只读加载后在两份独立临时 SQLite 中执行最小复现。没有替换或回退工作区代码。

| 同一输入：贵州茅台收盘价为1488元，fact，evidence_ids=[]，Critic pass | P14 快照 | P15 |
| --- | --- | --- |
| delivery_status | verified | blocked |
| persisted | true | false |
| 新增 research_records / conversations / daily_states | 1 / 1 / 1 | 0 / 0 / 0 |
| 审计原因 | 空 | missing_reference、no_verified_claim、verified_provenance_failed |

对照记录：[memory/p15-before-after.log](../memory/p15-before-after.log)。复现脚本在 `.tmp/p15-before-after.py`，仅写临时库。

## 验证与产物

- 全量 pytest：**1592 passed**，日志 [memory/p15-pytest-full-final.log](../memory/p15-pytest-full-final.log)。唯一 warning 为依赖库 anyio BlockingPortal 弃用提示。
- P15 新增回归覆盖空/缺失/None 引用、无据正向行情/公告/实体事实、所有 claim 类型、混合过滤、休市限制语、伪造引用、空真实账本、完整/错误引用并存、默认 unknown completeness、响应原因、实际 SQLite 持久化防线及 sync/SSE 一致性。
- 修改文件 Ruff、compileall 和 git diff --check 均通过；diff 检查仅有工作区既有换行风格提示。
- 离线回放：[scripts/p15_acceptance.py](../scripts/p15_acceptance.py)，命令 `.venv\Scripts\python.exe scripts/p15_acceptance.py`。以追加方式写 [memory/p15-acceptance.jsonl](../memory/p15-acceptance.jsonl)，共 7 个场景：空引用 blocked、混合 degraded、休市结构事实 verified、完整行情 verified、伪造/缺引用 blocked、独立工具失败 degraded、直接 verified 持久化拒绝。所有数据库位于 `.tmp/p15-acceptance-*`，仅用于确定性回放。
- P14/P13/P1E/受控 degraded 初次定向集合 **131 passed**；旧兼容性输入调整后的全量结果如上。初次失败和中间日志均保留，未将其描述为通过。
- [memory/p15-preservation-check.json](../memory/p15-preservation-check.json) 核对任务开始时的 39 个受保护文件，**39/39 哈希一致、0 删除、0 内容改变**。包括既有根目录/实际 memory 数据库、memory/evening 文件、memory JSONL/日志与 tmp 中已有 JSONL/日志。P15 产物均为新增文件。

本轮没有真实行情/公告 live probe，因此离线 fixture 的 verified 与临时持久化成功不代表上游真实数据质量或生产验收通过。
