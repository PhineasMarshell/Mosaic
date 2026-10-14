# P10 主 agent 独立验收与补修要求

日期：2026-10-13。基线仍为 `main@a938dd7`。

结论：本地检查通过，P10 的真实交付目标未通过。暂不提交或推送，也不以增加阶段编号代替修复。主 agent 本轮只增加本验收文档，未修改业务代码、配置或运行时数据库，未重新执行全部 24 次外部请求。

## 已复核的验证与取证

- 独立完整 pytest：1461 passed，1 条第三方弃用警告。
- Ruff、compileall、`git diff --check`：通过。
- P10 replay：选择 `6c75656326e2`，12 场景、24 条 observed、全部 blocked。
- 旧 P9 replay：结构校验通过，仍为历史 not_observed 记录。
- P10 已有 JSONL 中，22/24 次请求含非空 normalized，共计 877 条 datum、32 次 success 工具结果、18 次 partial 工具结果。此为请求记录累计数，包含重试和 sync/SSE，不代表独立事实数量或已验证 claim 数量。
- 24 次请求耗时中位数约 260 秒，范围约 123–378 秒；run_start 记录总预算为 1000 秒，不能把本轮结果解释为默认预算下的性能通过。

## 阻塞一：A 股公告被来源分类误判为市场域冲突

位置：`app/research/iwencai.py:137`、`app/research/iwencai_search.py:84`、`app/graph/nodes/finalize.py:261`。

公告和新闻 datum 使用 `domain=media`。Finalize 的 degraded 路径把任何非 `a_share` 的 evidence 一律判为 `domain_mismatch`，还会检查未被保留 claim 引用的证据。来源分类与目标市场域在此混用了。

实际公告场景 `3e8d5dde9522` 和 `8895b1cba63b` 均有 20 条 success datum，终态原因包含 `domain_mismatch`、`research_budget_exhausted`。

主 agent 最小复现：具备来源、日期、600519 标的和真实引用的公告 claim 能通过 `match_claims_to_evidence()`，同一 claim 在 `build_degraded_report()` 被拒绝为 `domain_mismatch`。这证明该检查能够误阻断合格公告；并不表示原始 20 条公告已经全部通过日期、标的等检查。

补修要求：区分来源类型与目标市场域，依据被引用证据的证券、市场及 claim 语义判断兼容性。不得把所有 `media/reference` 数据直接改写为 A 股，也不得取消真实跨市场、跨标的的检查。

## 阻塞二：失败工具的零证据规则扩大到了整份报告

位置：`scripts/p9_e2e_acceptance.py:300`。

当前 replay 只要响应汇总 reason_codes 含认证、timeout、upstream 等错误，就要求整份交付报告的 evidence_count 为零。

主 agent 最小复现：一条来源、日期、600519 标的和涨幅数值均匹配的有效 claim，独立校验为 `claims_valid=true`；另一个工具出现认证错误时，已标记 `persisted=false` 的 degraded 仍因 `transport failure must contain zero evidence` 被 replay 拒绝。

主 agent 纠正此前提示词：零 datum/零 evidence 应约束失败工具及其输出，不能删除独立成功来源的有效证据。最终报告可以仅保留经验证的 claim，并披露其他来源失败。未引用失败证据的有效结论，不应被可选工具错误整体否决。全局伪造引用、权威冲突及审计执行错误等既有硬门禁继续保留。

补修要求：逐工具检查失败结果的 normalized/evidence 为空；逐 claim 检查实际引用；degraded 继续 `persisted=false`。增加“成功证据 + 可选来源 401/timeout”的正向验收反例。

## 阻塞三：部分覆盖污染了独立有效 datum

位置：`app/research/evidence.py:388`、`:389`、`:425`；`app/agent/evidence_gate.py:527`；`app/graph/nodes/finalize.py:149`。

当前 build_evidence 把 result 的 partial 标志传播到全部 datum。strict replay 和 degraded 筛选再拒绝所有 partial evidence，导致完整、明确标注日期的个别指标也无法被保留。

主 agent 最小复现：同一响应含“600519 在 2026-10-09 的涨跌幅 2%”和另一条缺日期的最新价。前一 datum 自身为 success、partial=false、日期明确，构建账本后却成为 partial。即使 claim 明确限定为部分样本，严格校验仍报 `partial_only`，Finalize 报 `no_verified_claim`。

补修要求：分别记录单条 datum 的可验证性与结果集的覆盖限制。完整单条指标可以支持对应的限定事实；分页、缺字段、筛选条件未验证等限制继续阻止全市场统计、排名、全部条件满足、强比较和因果结论。不得将真正不完整的 datum 洗成 complete。

## 修正取证口径

`scripts/p9_e2e_acceptance.py:103` 的 evidence_count 来自 `report.evidence`。blocked/failed 的正文会被清空，因此这里为零不能证明采集或账本为零。

下一次取证需区分 collected datum、账本 evidence、交付 evidence、已验证 claim 数量；保留脱敏 operation_id/tool_key、日期、coverage、门禁拒绝原因及 Critic/Finalize 原因。当前 P10 工具摘要没有工具身份，不能从最终汇总直接定位每个 Skill 的权限或数据缺口。

同一批记录存在成功的新闻/公告来源和 `ths_invalid_credentials` 的行情来源。因此应按 Skill 排查鉴权、Skill ID、接口授权和请求协议，不能仅凭部分 Skill 的 401 推断整个 API key 无效。排查不得打印凭据或把密钥写入文档、日志。

## 交给实现 agent 的补修任务

在当前工作树补修 P10，保留全部已有修改、数据库和运行时文件；禁止 reset、revert、commit、push。

1. 修复上述三项冲突，优先完成公告域兼容、逐工具失败隔离和逐 datum 可验证性。保留错误日期、错误标的、新闻证明价格、伪造引用、权威冲突等负向门禁。
2. 回归必须包含合格 A 股公告可交付、真实跨市场公告拒绝、有效 claim 不被无关来源错误污染、完整历史字段不被另一缺日期字段污染，以及 partial 不支持全市场强结论。
3. 区分交付 evidence 与采集/账本证据计数；验收不能因 blocked 后报告被清空就声称未取得数据。
4. 按 Skill 定位行情 401。已知认证失败不要同轮反复重试；不以增加总预算或研究回环次数作为修复。
5. 先做两个小范围真实验收：一条明确日期/标的的公告或新闻事实；一条明确日期的行情事实。至少观察到一个符合预期的 verified 或有已验证 claim 的 degraded，再扩展 sync/SSE 及其余场景。认证确实不可用时保留证据和阻塞原因，不伪造成功。
6. 完整 pytest、Ruff、compileall、diff check、replay 通过后交接主 agent。真实交付未达到要求时保持 `production_ready=false`。
