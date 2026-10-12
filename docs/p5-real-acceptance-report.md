# P5 真实 Gateway、预算与交付验收交接

结论：已经取得真实正常数据链路在 180 秒预算内完成审计的结果，sync 和 SSE 均有 verified；**尚不达到生产上线条件**。真实 abnormal-reasons 仍失败，MCP 业务请求仍有 TLS/上游超时，概览缺少带日期的市场级证据，且部分要求的真实场景尚未观测到。软件回归通过不能替代这些业务验收。

基线为 `main@36f383d2c4b1908f7dfd68a7dba5e49eff845c61`。改动保留在工作区，没有执行 reset、revert、commit、push。保留已有 `memory.db` 和 `memory/evening/`。真实接口写入使用独立 `.tmp/p5-delivery.sqlite`，没有使用用户数据库。

验收主要发生在 2026-10-11（Asia/Shanghai，周日），收尾跨至 10-12。交易日问题明确询问 `2026-10-09 A 股发生了什么？`；周末问题使用真实的“今天 A 股发生了什么？”。**没有把周末执行冒充交易日当天“今天”的现场验收**。交易日当天的 live-today 场景仍需在实际交易时段复验。

## 实现与真实上游诊断

### Gateway

- HTTP/MCP 共用有限重试策略；abnormal-reasons 最多两次，只有可识别瞬态错误允许重试。认证、参数错误和空响应不做无限重试。每次尝试及退避共用绝对 deadline；第一尝试保留第二次机会。该实现选择有限重试，没有声称实现服务端熔断。
- `attempts` 保存每次真实状态（success/partial/error/timeout/empty）、耗时、剩余预算、异常类型、HTTP 状态和 Cloudflare/origin/TLS 标志；`upstream_status` 保留最后真实状态。对外 ToolResult 的 timeout/empty 仍为 error，不能当成功。
- error/timeout/empty 一律 0 datum、无 evidence；删除原来失败工具的 `tool_status` 占位 evidence。稳定 reason_codes 包括 `gateway_timeout`、`deadline_exhausted`、`gateway_upstream_error`、`gateway_upstream_incomplete`、`empty_gateway_response`，可选 abnormal 来源增加 `optional_evidence_unavailable`。
- 为 abnormal 默认请求补有限日期窗口（目标日前 30 天至目标日），保留显式 start/end，避免默认长历史拖累预算；真实尝试短窗口后上游仍失败，不能据此宣称修复 origin。
- Tencent 指数 transport 修复：已知指数 `000300` 编为 `sh000300`，canonical 参数保持原代码；普通股票裸代码不乱加指数前缀。之前真实返回 `items=[]` 的响应现在判 empty error，修复后指数返回 23 datum。
- 从 Tencent row 的 `time=20261009161452` 解析实际观测日期，逐 row 传播标的/时间，支持子级盘口字段；混合日期不盖统一日期。retrieved_at 是 UTC 抓取时间，不能补成市场日期。
- 本地 MCP 可修原因已修复：日志 APPDATA 与插件 LOCALAPPDATA 不再混用。继承真实已安装插件目录后握手/list_tools 发现 40 工具。支持 MCP `is_error`/`isError` 带内错误；并发会话由专用 owner task 管理，AnyIO scope 在同 task 进入和退出。

真实异常响应：HTTP 502，868 字符错误页，`cloudflare=true`、`origin_incomplete=true`；多个真实请求出现相同结果或 timeout。脱敏记录保留 HTTP 状态、长度、分类和逐尝试耗时，没有存原始错误页/凭据。能定位到 Gateway 上游/origin 返回不完整；**缺少 origin 日志和控制面权限，无法进一步证明服务端根因，也未修改远端服务**。

MCP 最终探针 `a500d5dedd17`：协议初始化/list_tools 成功，真实 verify_upstream/quote 请求超时、0 datum、0 evidence。早期 MCP 带内错误包含 TLS handshake timeout；单独 CLI verify 曾成功（trace `96f481ad5bd7710b5e9a6e4f77e507c1`，client 5952.192 ms / upstream 166.9 ms），只说明该次 verify 正常，不能覆盖 MCP call_tool 失败。

### 缓存、partial 与 refresh

真实 `0c7ab8484560` 在生产同样的 ToolRuntime 截断流程中取得：

| 读取 | cache_hit | refresh_requested | upstream_status | 工作状态/datum | source_partial | retrieved_at（UTC） | 实测墙钟 ms |
|---|---|---|---|---|---|---|---:|
| 首次真实读取 | false | false | success | partial / 201 | false | 2026-10-11T15:54:38.654118+00:00 | 5000.8 |
| 缓存读取 | true | false | success（缓存来源） | partial / 201 | false | 同上 | 234.2 |
| refresh | false | true | success | partial / 201 | false | 2026-10-11T15:54:45.200117+00:00 | 6306.6 |

原始来源 26527 datum，工作对象保留 200 条并附截断说明；每份 evidence 最多 80 条，as_of_date 未提供。这里的 partial 是 **local_truncation**，不能声称观测到了上游自身 partial。缓存命中清空 attempts，只记录真实缓存查找耗时；retrieved_at 保留原来源时间。旧记录中缓存复制了旧 attempts 的现象保留在历史里，不能当作一次新 refresh。

refresh 绕过缓存与同 run dedup，并先删除旧缓存；失败 refresh 后不会恢复旧 partial。后者有明确回归测试，真实上游 partial → 失败 refresh 的完整组合尚未观测到。真实早期 sentiment 超时已记录（例如 `2119d42f58f2`），它发生在修复前，不作为最终版本通过证据。

partial 只允许有限观察。代码门禁继续阻断比较、因果、全市场绝对结论、缺日期和错标的；不会拿 local partial 证明全市场覆盖。

### 预算与提示词

分析员内独立工具最多并发 3 个，按优先级启动，共享 tools phase 最长 45 秒和总 deadline。队列里的工具不获新预算；reasoning/critic/finalize reserve 保留。MCP 连接也受同一运行期限约束。

reasoning 展示上限改为 60 evidence / 每工具 10 datum，去除重复 normalized 正文，只发送工具状态摘要和 evidence。提示要求最多 6 个核心 claims，单股票报价聚焦真实报价和观测日期。SDK 自动重试关闭，由 stage deadline 唯一控制模型耗时。

Critic 的重复 evidence_quality 按工具/原因聚合总数、展示异常样本，**引用条目的完整值及质量保留**。同时修复原有引用值只显示前 500 字符的问题。完整代码账本仍执行真实性、日期、标的、partial 与冲突门禁；没有放宽 Critic parser（基线非法 `action=qualify` 仍失败）。

stage 事件包含 stage、status、exception_type、elapsed_ms、remaining_budget；LLM usage 单独记录。超时/预算不足走明确终态，不恢复成成功、不新增无限回环。

## 同预算前后耗时

下面仅比较 **HTTP、180 秒总预算、同问题**。环境 `.env` 的 1000 秒没有被修改，验收子进程覆盖为 180 秒。真实 LLM 有波动，样本数量不足以推断 p95/p99 或稳定 SLA；前后采样也有上游数据和缓存变化，不能把所有变化归因于代码。

单位 ms。before 的 plan/reasoning/critic 是 LLM telemetry；after 同时列完整 stage 墙钟。before tools 是 plan 事件至 executions 的时间差（含编排），没有把工具 duration 之和冒充并发墙钟。

| 场景/版本 | run_id | 总耗时 | plan LLM / stage | tools 墙钟 | reasoning LLM / stage | critic LLM / stage | finalize | 终态 |
|---|---|---:|---:|---:|---:|---:|---|---|
| 交易日 before | 7b5c95ef4e8c | 117815.4 | 22163.6 / 未观测 | 10261（事件差） | 41897.2 / 未观测 | 37195.9 / 未观测 | 有事件；阶段耗时未观测 | blocked |
| 交易日 after 最终 | 286e0d1fd26d | 155374.7 | 20662.9 / 20247.6 | 6884.2 | 72133.5 / 77791.0 | 49922.9 / 49922.5 | 5.0 | blocked |
| 周末 before | e44841b95b9b | 109524.1 | 7636.3 / 未观测 | 9400（事件差） | 60861.9 / 未观测 | 25671.9 / 未观测 | 有事件；阶段耗时未观测 | failed |
| 周末 after 最终 | 91b0291b10a2 | 103026.7 | 9504.2 / 9068.9 | 5078.1 | 44734.6 / 50800.2 | 37094.2 / 37093.4 | 5.0 | blocked |

周末 after 另有一次有限补研 plan，429.1 ms timeout；保留 77 秒 downstream reserve 后不能完成重规划，最终 blocked，`plan_timeout/time_budget_insufficient/domain_mismatch`，persisted=false。交易日最终保留约 24.626 秒，日期缺口和无依据解释未修复，`invalid_reference/time_budget_insufficient` 阻断。

| prompt tokens | reasoning before → after | critic before → after |
|---|---:|---:|
| 交易日 | 32051 → 11755 | 44733 → 27615 |
| 周末 | 28154 → 11769 | 42838 → 26332 |

最新交易日总耗时比基线增加 37559.3 ms，周末减少 6497.4 ms；输入缩小没有带来稳定的端到端收益。中间交易日 `8023f99c5048` 为 96047.3 ms blocked，但最终样本没有保持这个耗时，因此不以该中间值宣称持续优化成功。

早期 MCP 基线 `4b470a37213e` 134125.4 ms（180 秒），`f92ba297ff84` 248415.4 ms（1000 秒）含本地/传输故障，另行保留，不与 HTTP 180 秒样本混作同条件对比。

## 正常链路及真实 sync/SSE

| 场景 | run_id | 总耗时 ms | Critic/路由/finalize | delivery | persisted |
|---|---|---:|---|---|---|
| 正常报价 sync | a0da64b19fd6 | 84067.4 | pass → pass → verified | verified | true（独立验收 DB） |
| 最终代码正常报价 sync | 7aa3cbbb6d67 | 88541.4 | pass → pass → verified；完整 finalize 计时和交付诊断 | verified | true（独立验收 DB） |
| 正常报价真实 SSE | e31417f0a9df | 140091.8 | 一轮有限补研后 pass → verified | verified；唯一 result | true（独立验收 DB） |
| 报价有限降级 | 43071b228865 | 145746.5 | revise；有限 rewrite 后过滤未验证 claim | degraded | false |
| 周末真实 SSE | 5afc5eac7065 | 117083.4 | research_more → 无足够补研预算 → blocked | blocked；唯一 result | false |
| 5 秒预算真实 SSE | a143e24e214a | 663.1 | plan 预检 StageBudgetExceeded → finalize error | failed；唯一 result | false |

正常 sync 43 datum / 41 evidence，source=tencent，instrument=688981，as_of_date=2026-10-09，Critic pass，无 unresolved issues。该 run 返回 2 个 fact claims。阶段墙钟：plan 17361.6、tools 579.9、reasoning 36703.4、critic 28836.6 ms；对应 LLM duration 17715.9 / 29624.3 / 28837.4 ms。reasoning stage 包含前置准备，不能只用 LLM duration 当整个 stage。

最终代码复验 `7aa3cbbb6d67` 仍为 verified：plan 23649.0、tools 831.5、reasoning 37025.4、critic 26352.9、finalize 1.7 ms；客户端总 88541.4 ms，响应图耗时 88480.8 ms，剩余预算 91.519 秒。finalize、delivery、响应同 run_id；交付日志已保留 stage=finalize、elapsed_ms、remaining_budget，2 个 fact claims，persisted=true。LLM prompt tokens 为 2668 / 9013 / 6494。

正常 SSE 最后一次 result 到达 140087.9 ms，客户端读取结束 140091.8 ms。报价 evidence 支撑最终 1 个 fact claim；额外 sentiment 工作 partial 没有替代报价事实依据。两轮 stage 墙钟合计：plan 18599.5、tools 3628.9、reasoning 68337.1、critic 49373.2 ms。该历史 pass finalize 有事件但没有单独阶段计时；最新代码已给 pass response build 增加 finalize 计时，不能把历史未计时填成 0。

degraded run 仅保留 4 条已验证 fact claims，未验证市场解释被移除，persisted=false。引用真实性/日期/标的/强 partial 结论和权威冲突仍受完整门禁，不能因存在部分真实数据放行伪造引用。

5 秒 SSE 是 **启动前预算不足**，不是“真实模型已调用并超时”。周末 after 的 429.1 ms plan timeout 是真实补研模型调用超时。二者分别记录，不能混为同一种观测。

sync/SSE 共用响应构造器，公开同一图 run_id 与 final_audit_status、delivery_status、stage、exception_type、reason_codes、elapsed_ms、remaining_budget、persisted。SSE heartbeat 只有 progress；replay 检查每条真实 SSE 只有一个 result，且在该 run 总预算内，并与 delivery 日志同 run_id、同终态。blocked/failed 清空报告，degraded/blocked/failed 均不持久化。

## 必需场景覆盖

| 场景 | 真实观测 | datum/evidence 与 provenance | 审计/交付 | 仍需补验 |
|---|---|---|---|---|
| 交易日概览 | 286e0d1fd26d | quote=23/21；sentiment=201/80；limit_up_count=2/1；sectors=201/80；telegraph=13/13；digest=10/10 | 3 个候选 claims，Critic research_more，final blocked，false | 实际交易日当天 today；市场来源日期缺口 |
| 周末概览 | 91b0291b10a2 | quote 同为 10-09；sentiment/sectors partial，市场级日期未知 | 3 个候选 claims，research_more，补研 timeout，blocked，false | 日期完整的复盘与新闻因果证据 |
| 静态映射外 688981 | c79222c0ba1a | success，43/41，tencent，688981，10-09，retrieved_at=15:43:20.006594Z；402.6 ms | 直接工具探针，无 Critic/route/finalize，不持久化；完整审计另见正常 sync/SSE | 已观测 |
| code-only quote 600519 | c79222c0ba1a | success，43/41，tencent，600519，10-09，retrieved_at=15:43:20.335142Z；328.3 ms | 直接工具探针，未调用审计阶段，false | 已观测 |
| abnormal success 目标 | c79222c0ba1a | 实际 timeout，两次，25012.9 ms，0/0，无 provenance evidence | tool error，false | **not_observed**，不能判 success 通过 |
| abnormal timeout | c79222c0ba1a | 实际 timeout，12.3 ms（50 ms deadline），0/0 | tool error，false | 已观测安全失败 |
| abnormal empty 目标 | c79222c0ba1a | 实际 HTTP502/origin incomplete，两次，23729.9 ms，0/0 | tool error，false | **not_observed**；空响应逻辑仅有 transport double 覆盖 |
| sentiment partial → refresh | 0c7ab8484560 | working partial 201/80；来源 success 26527 datum；cache/refresh/retrieval 时间区分明确 | 直接工具探针，无结论交付，false | 上游自身 partial、失败 refresh 组合仍未观测 |
| Gateway deadline | df2f0ae4c50b | 20 ms deadline，实际 timeout 1.2 ms，0/0，无 evidence | tool error，false | 已观测安全失败 |
| MCP call_tool | a500d5dedd17 | list_tools=40；真实 verify/quote 请求 error/timeout，0/0 | 不伪装成正常工具成功，false | 上游 TLS/业务调用成功仍阻塞 |
| 真实 SSE | e31417f0a9df 等 | JSONL 逐 progress/result 到达时间及同 run 事件 | verified / blocked / failed，均唯一 result | 实际网络断连、慢持久化争用等未观测，未标通过 |

完整工具级 source、instrument、as_of_date、retrieved_at、partial、原始条数、reason_codes、attempts 和耗时在 JSONL；候选 claims、Critic、route、finalize、delivery 用同 run_id 关联。直接工具探针的审计/交付阶段明确 `not_invoked_tool_probe`，不会虚造 pass。blocked/failed 的最终 claims 为空，候选 claims 仍在对应 Critic 事件里供审查。

## 回归、脱敏与复现

主记录：`memory/p5-real-acceptance.jsonl`。去除 raw/normalized 大 payload，问题、claim 文本、异常正文及未解决问题脱敏；保留机器诊断、来源/标的/日期、引用和各阶段事件。基线工具返回曾经是原始大结构，现已转成历史摘要；未记录的 baseline evidence_count 明确 null/not_recorded，修复前失败工具的 evidence_count=1 保留，标 phase=before，**没有改写成修复后的 0**。

验收驱动：`scripts/p5_real_acceptance.py`，包括 probe、partial-refresh、research、sync、sse、replay；sync/SSE 新运行从服务器 /health 读取实际预算。历史服务预算由已记录的服务环境补注明，5 秒场景独立保留。网络使用真实已配置凭据与代理，仓库内没有保存凭据。

```powershell
# 只读离线脱敏 replay，无需 LLM/网关
.venv/Scripts/python.exe scripts/p5_real_acceptance.py --mode replay

# 真实执行前需配置 Gateway/LLM 环境；research 总预算显式覆盖
$env:MARKET_GATEWAY_MODE='http'
$env:RESEARCH_BUDGET_SECONDS='180'
.venv/Scripts/python.exe scripts/p5_real_acceptance.py --mode research --scenario trading_day
.venv/Scripts/python.exe scripts/p5_real_acceptance.py --mode research --scenario weekend_today
.venv/Scripts/python.exe scripts/p5_real_acceptance.py --mode partial-refresh
```

全量 pytest：**1173 passed**，两个既有环境警告（Starlette AnyIO API 弃用、pytest cache 写入受限）；没有跳过失败测试。Ruff 全仓库扫描与 `app tests scripts` 均通过，前者对已有不可访问运行数据目录有警告，代码目录扫描没有漏扫；compileall（app/tests/scripts）通过；git diff --check 通过。对应日志为 `memory/p5-pytest-final.log`、`memory/p5-ruff.log`、`memory/p5-ruff-code.log`、`memory/p5-compileall.log`、`memory/p5-diff-check.log`、`memory/p5-replay.log`。

真实性/错日期/错标的/partial 强结论/权威冲突及 degraded 过滤继续由原有严格测试覆盖（如 `test_evidence_authenticity.py`、`test_controlled_degraded.py`、`test_reasoning_traceability.py`、`test_p1e_critic_finalize.py`）。P5 新增有限重试/认证不重试/空响应/row 日期/指数编码/refresh 不复活旧 partial/缓存诊断/工具并发 reserve/Critic 完整引用回归；这些 transport doubles 明确只是补充，不能替代真实 JSONL。

上线前仍需：修复 Gateway origin incomplete 和 MCP TLS 链路，取得真实 abnormal success/empty、上游 sentiment partial/失败 refresh 组合；让市场级聚合数据提供可核验观测日期，补齐概览的因果证据；在真实交易日当天重复 today、在部署网络重复 SSE，增加重复负载与持久化争用验收。当前不得宣布 P5 的全部业务验收已通过或直接上线。
