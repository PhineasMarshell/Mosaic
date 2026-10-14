# P10 补修独立复核（2026-10-13）

基线：`main@a938dd7`。结论：局部证据门禁已复核并补齐，真实端到端交付仍未通过；`production_ready=false`，本轮不提交或推送。工作树原有修改和运行时文件均保留。

## 复核结果

- 公告 `media/reference` 的市场兼容性现在进入共用 claim→evidence matcher。Critic 的 verified 路径与 Finalize 的 degraded 路径使用同一规则：带可识别 A 股证券代码的事件 claim 可通过；港股代码和混合跨市场引用会阻断。新闻/公告仍不能证明价格变化。
- Evidence Gate 现在分开记录 datum 的 `partial` 与结果集的 `result_partial`，完整、明确日期的单条 datum 不再进入 `partial_only`。Critic 的全市场 breadth 检查仍拒绝仅由 partial 结果集支撑的总量结论。
- 失败工具的零 datum 约束在 replay 中按工具执行；一个可选来源失败不删除另一成功来源的受控 degraded claim。聚焦本地正反向测试覆盖以上行为。
- 独立全量检查：1471 passed（1 条 Starlette/AnyIO 弃用警告）；Ruff、compileall、`git diff --check`、P10/P9 replay 通过。P10 replay 复核的是已有的 24 条真实请求记录，不代表补修后新增的 verified 观察。

## 真实交付仍未通过

`memory/p10-focused-acceptance.jsonl` 的公告和行情各 sync/SSE，共 4 次 observed、累计 174 条工具 datum，均为 blocked、`persisted=false`、交付 evidence=0。累计 datum 包含重试和双 transport，不是独立事实数。

- 公告两次均有 20 条成功结果，但 Reasoning 约 108/111 秒后 `reasoning_timeout`，未产出报告，故无 claim 可供 Critic 或 degraded 筛选。这不是公告域误判的证据。
- 行情两次有请求日期的 K 线等非空数据，约 422/296 秒后 `research_budget_exhausted`。Critic 文字显示模型把可用的 2026-10-09 数据说成缺口、混入 2026-10-13 数据，且扩展到无证据的市场情绪或完整性描述。不得据此放宽日期、标的或覆盖校验。
- 聚焦记录在临时目录清理 wrapper 加入前产生；JSONL 已完成，但 probe 进程当时尾部非零退出。wrapper 的真实 Windows 外部运行尚未复验。
- 当前脱敏终态缺少逐工具 `tool_key/operation_id`、逐 datum 日期和账本证据数；不能凭最终 `evidence_count=0` 判断采集或账本为空，也不能据此定位具体成功 Skill 的日期覆盖。

## 下一轮最小任务

1. 只围绕一条明确日期、标的、来源和公告标题的事件事实，以及一条 2026-10-09 的 600519 行情事实做真实运行。先在隔离环境保留脱敏的工具身份、datum 日期/coverage、账本 evidence 数、Reasoning 输入大小/耗时/异常类型和 Critic issue；不要记录凭据或原始私有响应。
2. 定位公告 Reasoning 超时发生在模型请求、解析还是上下文组装；用实际调用耗时和输入规模证明根因。让超时保持 blocked/failed 且 `persisted=false`，不靠提高总预算或回环次数掩盖问题。
3. 对行情核对 Reasoning 实际看到的 K 线字段和日期，防止把非请求日 quote 写入请求日事实；单标的问题不自动扩写全市场情绪、资金流向或“数据完整”断言。只删除或限定无证据语句，保留有据的窄 claim；Critic 的伪造引用、错误日期、错误标的和 partial 强结论门禁保持不变。
4. 在本地和真实探针中分别验证至少一条带 evidence 的 verified 或 degraded 终态，再扩展到 sync/SSE 和其余场景。未观察到时如实交付阻塞证据，维持 `production_ready=false`。

主 agent 本轮补修文件：`app/agent/evidence_gate.py`、`app/graph/nodes/critic.py`、`app/graph/nodes/finalize.py`、`tests/test_evidence_authenticity.py`、`tests/test_stage2_evidence_context.py`、`tests/test_p1e_critic_finalize.py`。未重跑外部 Gateway/MCP/LLM 请求，未 reset/revert/commit/push。
