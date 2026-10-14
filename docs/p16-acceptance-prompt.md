# P16：真实交付验收与收尾

请在 `D:\Mosaic` 完成 P16。保留所有既有工作树修改、数据库、evening、JSONL 和日志；禁止 reset、revert、commit、push。先阅读 `docs/p15-main-agent-review.md` 和已有验收脚本。

P15 已经通过独立本地验收：无引用正向 claim 被阻断，混合报告可安全降级，persistence 有最后一道校验。主 agent 补齐了 P14 probe 的 cleanup 最终状态写入；全量测试 1595 passed。真实生产链路仍未通过。

## 目标

验证真实同花顺主源和真实 LLM 能否完成用户问题并正确交付。覆盖最初的“今天 A 股发生了什么”，以及单标的窄行情。根据实际可用范围收尾，不预设必须新开 P17，也不把离线 verified 当作真实通过。

## 真实场景

以下场景各运行 sync 和 SSE，使用生产 `get_settings()`、ToolRuntime、实际 Graph、Critic、Finalize、响应和 persistence。日志必须记录实际路由，不能通过直接调用 THS adapter 代替整个入口。

1. 窄行情：`SH600519 在 2026-10-09 的收盘价是多少`。只要求一个直接事实。完整证据与合法模型报告应有机会 verified；截断后的 evidence-only fallback 仍只能 degraded。
2. 原始用户问题：`今天 A 股发生了什么`。按运行时 Asia/Shanghai 日期及真实交易日处理。验证指数、市场广度、板块等实际证据覆盖；局部事实不能变成全市场结论。
3. 休市问题：`2026-10-11 A 股发生了什么`。区分请求日和最近交易日；单纯休市/缺口结构说明不能被计为行情成功对照。
4. 公告：`600519 2026年10月公告`。只使用真实返回的标的与公告日期；缺失或无匹配继续 blocked，不得从请求参数回填。

所有场景使用独立临时 SQLite 和明确 conversation_id。不要向用户既有数据库写入验收数据。verified 后从真实临时库确认 research_records、conversations、daily_states 的写入，并验证历史检索可以读取可信结果；memory 是现有存储/检索能力，不虚构第四张表。degraded、blocked、failed 必须零写入。

## 失败时的最小修复

先指出第一次破坏交付条件的位置，再做有证据的最小修复：

- `finish_reason=length`：检查单一事实请求为何仍输出冗长 JSON、重复证据或无关正文。可缩短模型输出契约、让代码从真实账本填充证据展示；保留 claim 引用、严格 schema 校验和输出截断拒绝。不得提高回环次数或总预算来掩盖根因。
- 日期/标的字段丢失：仅修复真实上游字段的传播与格式归一，不使用请求参数伪造观测元数据。
- Critic/Finalize 拒绝：逐 claim 列出实际 reason_codes 和引用证据，区分合格事实、无据推断、无关工具失败和全市场覆盖缺口。保留 P15 既有 verified/degraded 策略。
- 公告空匹配、401、TLS、504、上游 timeout：保留结构化失败与零证据语义。没有修复本地代码的证据时，明确列为上游/认证阻塞，不能反复跑探针当修复。

每个场景先跑一次；若已定位并修复具体原因，再复验该场景。不要为追求一次偶然成功无限重试。

## 审计与传输要求

- 保留 P15 所有正负向门禁，无引用/伪造引用正向 claim 永远不能 verified 或落库。
- 保留日期、标的、来源、authority、retrieved_at、coverage、completeness、partial、result_partial 和权威冲突语义。
- 新闻/公告不能证明价格变化；局部或单标的证据不能证明全市场、排名或因果。
- 独立成功来源可按现有规则支持 degraded；degraded 始终 persisted=false。
- sync/SSE 相同状态必须有一致审计载荷；独立真实运行可因数据与模型结果不同而不同，不伪造逐字段相等。
- SSE heartbeat/progress 非终态，terminal=true 的终态恰好一个。

## 取证与验证

新增 `scripts/p16_acceptance.py`、`memory/p16-acceptance.jsonl`、`docs/p16-handoff-report.md`。脚本必须显式区分 live/replay 参数，不识别参数时报错，replay 绝不联网。

JSONL 按 run_id 记录场景、transport、实际主源/fallback、status/code、final_audit_status、reason_codes、persisted、采集 datum 数、完整账本 evidence 数、报告 evidence 数、claim 数、finish_reason、parse_status、fallback 候选/接受/拒绝原因、heartbeat 数、唯一终态计数、最终 cleanup_status、实际数据库写入计数、是否为纯结构说明。明确区分真实网络观察与离线 fixture。

复用已修复的日志采集器，数量必须来自同 run 的对应阶段。清理告警不丢取证、不吞业务错误；新写入记录不得为 pending。保留历史 artifact，不覆盖旧记录，不记录凭据。

执行适当的聚焦回归、完整 pytest、Ruff、compileall、diff check，以及具有明确离线入口的 P9/P10/P15/P16 replay。P12/P13/P14 若没有 replay 模式，只检查已有 artifact 和回归测试，禁止误传参数触发 live。

## 收尾结论

报告按功能列出：真实通过、受控降级可用、被证据阻断、上游/认证阻塞。窄行情通过不能代表全部技能或全市场报告已通过。

保留 `production_ready=false`，除非声明的生产范围确实满足真实验收标准；一次纯休市说明 verified 不算行情成功。即使仍有上游阻塞，也给出明确可用范围、剩余根因和可复现命令。本阶段形成具体验收结论，不自动扩展成无止境的新编号阶段。
