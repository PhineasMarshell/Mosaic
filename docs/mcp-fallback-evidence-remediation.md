# MCP 备用源与严格证据审计修复交接单

状态：已实现并完成离线回归（2026-10-14）。本文保留为实现交接与审计契约；本次未调用真实 THS/MCP/live 服务。

## 背景与完成定义

2026-10-14 的 live 运行暴露了四个互相叠加的问题：

1. THS 主源 `empty_normalized` 后，MCP 已返回 quote 23 条、limit-up sectors 421 条，但 `ControlledFallbackRouter` 因 datum 没有 `authority`，以及顶层 `count` datum 没有日期，全部退回 `fallback_provenance_insufficient`。
2. 市场级 quote 仍由 `market_plan.py` 注入裸代码 `000300`，而 MCP/腾讯实际需要 `SH000300`；critic 生成的 `SH000001/SH000300/SZ399006` 多指数参数又被市场前缀覆盖。
3. `sentiment` 和 `limit_up_count` 的 THS 结果只有全 A 最新价/涨跌幅时仍被视为 partial 成功，没有补查 MCP；`iwencai_industry` 的 `gateway_business_error` 也没有进入可回退错误集合。
4. 严格 matcher 把新闻的合法信封值 `search_results` 当成不完整，把 `result_partial=true` 连带到每条完整 datum，导致已带日期的窄事实也被拒绝；第二轮还出现同一工具不同无效参数重复计划和 `budget_truncated` 记录与实际 route 不一致。

完成定义：

- 有明确 `source` 的 MCP datum 经过归一化后同时有 `source`、`authority`、`retrieved_at`；行内日期优先由行字段取得，结构性 `count` 不阻塞整批 provenance。
- quote、market breadth、limit-up count、limit-up sectors 的“必需字段缺失”会触发一次受控回退；主源有窄的可用 datum 时，回退失败仍保留该窄 datum 并记录原因，不能伪造完整覆盖。
- 默认市场指数使用带市场前缀的代码；已有 quote 参数（尤其 critic 的多指数参数）不会再被 `market_plan` 覆盖；同一无参数市场工具的 `{date}` 与 `{}` 只执行一次。
- 新闻单篇事实可以使用 `completeness=search_results`，前提是有报道日期、来源/authority 和可审阅的标题/片段；`result_partial` 只限制全市场/穷举式结论，不拖累独立完整 datum 的窄事实。
- 任一标为 `budget_truncated` 的精确 tool+arguments 变体都不会出现在本轮 route；baseline quote 与被丢弃的 critic gap 变体分别记账。
- 不放宽通用 evidence-only fallback 的完整性门槛，不用检索时间填交易日期，不把 transport 名称伪装成数据 authority。

## 实现范围与顺序

按 P0 → P1 顺序实施。每一步都要先补回归，再改实现，最后运行本文件末尾的离线命令。

### P0-A：补齐 MCP provenance，并让 provenance 校验按业务 datum 工作

涉及：`app/gateway/normalizer.py`、`app/gateway/controlled_fallback.py`、必要时 `app/models/market.py`。

1. 在 `normalize_tool_result` 的统一收尾处（设置 `retrieved_at`、`as_of_date`、`completeness` 的位置）补齐来源链：

   - `effective_source = raw.source_used or raw.source`；只在该值是非空字符串时使用。
   - `result.authority = result.authority or effective_source`。
   - 每条 datum：`source = datum.source or effective_source`，`authority = datum.authority or effective_source`。
   - 保持 `retrieved_at` 为本次归一化时间；绝不能从 `retrieved_at` 推 `as_of_date`。
   - source 缺失时继续标记 provenance 不足，不能把 `mcp`、`http_api`、`primary_source` 当作数据 authority。transport 与 provider 必须分开。

2. 给 `_has_provenance` 增加结构性元数据过滤。建议新增私有 helper（名字可调整）`_is_structural_metadata_datum(result, datum)`：

   - 只忽略位于顶层、且与 `items`/`data`/`rows`/`results` 同级的 `count`、`total`、`row_count`、`code_count`、`page`、`limit` 等容器统计字段。
   - `items[0].count`、`limit_up_count` 的唯一业务字段、以及 payload 只有 `{count: 59, date: ...}` 时，`count` 仍是业务 datum，不能过滤。
   - 过滤后必须仍有至少一个业务 datum；全是元数据仍返回 false。
   - 过滤只影响 provenance 判定，不要从 `normalized`/evidence ledger 删除 datum；审计日志仍应能看到 `count`。

3. `_MARKET_CAPABILITIES` 改为与实际 registry capability 对齐，至少包含：

   `quote`、`kline`、`market_breadth`、`limit_up_count`、`limit_up_pool`、`limit_up_sectors`、`sector_theme`、`abnormal_reasons`。

   对这些 capability 的业务 datum 要求显式 observation date；有 `arguments.date/as_of_date/end/end_date` 时还要逐条匹配请求日期。非市场/检索事件不要套用这条交易日期规则。

4. 日期校验按业务 datum 做：先读 `datum.as_of_date`，再读 `datum.timestamp[:10]`，最后读 `result.as_of_date`。当前 normalizer 已能把 `items[*].time/date` 传播到同一行的兄弟指标；补一个回归覆盖顶层 `count` 无日期、`items[*].time` 有日期的 payload。若 row identity 不能覆盖某种 `results`/`data` 结构，应在 normalizer 中扩展同一逻辑，而不是给整批填今天。

   同时分离结果级 partial 与行级 partial：列表截断、被拒绝 sibling 或上游 envelope 的 `partial=true` 只能设置 `ToolResult.partial/result_partial`，不能把所有已有完整字段的 datum 一律设置成 `datum.partial=True`/`completeness=partial`。行级缺日期、缺标的、缺值才标记该行 partial；这条规则是后面“完整成交额 datum + result_partial=true”可通过窄 claim 的前提。

5. `fallback_allowed` / `ControlledFallbackRouter` 的 provenance 结果必须保持旧不变式：错误主源 + 合格备用源才允许 source switch；authority 缺失、日期缺失、标的错配、请求日期不符都必须拒绝。

### P0-B：按 capability 的必需字段决定是否回退

涉及：`app/gateway/controlled_fallback.py`、`app/gateway/source_routing.py`、`app/gateway/ths_skill_registry.py`、`app/graph/tool_runtime.py`。

1. 把“是否触发 fallback”和“备用结果是否通过 provenance”拆成两个判断。保留 `fallback_allowed(primary, fallback, ...)` 对外兼容，但内部新增等价的 primary trigger helper，返回稳定 reason code。

2. primary trigger 规则：

   - `status=error` 且 reason code 属于 retryable 时回退；把 `gateway_business_error` 加入 retryable（保留 auth/conflict 禁止回退）。
   - `status=success/partial` 只有在 capability 必需字段缺失时回退。已有满足必需字段的窄 datum 不因无关 sibling 缺失而回退。
   - primary 为 partial 且只有无日期 quote/最新价时，quote 触发 `primary_required_fields_missing`；fallback 失败时保留 primary，不能把它升级成完整证据。

3. 建立字段谓词，不依赖原始中文自由文本整句匹配，允许中英文稳定别名：

   - `quote`：每个 requested instrument 至少一个 dated price/last/close 或 change_pct datum；若请求是多指数，不能用另一标的顶替。
   - `market_breadth`（`sentiment`）：同时有 advancing/up_count/上涨家数 与 declining/down_count/下跌家数；只返回最新价/涨跌幅必须触发回退。
   - `limit_up_count`：有数值型涨停家数/limit_up_count/limit-up count；顶层唯一 `count` 在没有 items 容器时按业务字段保留。
   - `limit_up_sectors`：至少有一个带日期的 sector/theme/板块/题材业务行，并有 count/rank/pct 等结构字段；只有空容器或业务错误触发回退。
   - 未列出的 capability 维持旧的 error-only fallback。

4. Router 的流程改为：记录 primary → 计算 trigger；无 trigger 立即返回 primary；有 trigger 才调用 fallback → 用 `_has_provenance` 校验 → 合格则返回 fallback 并写 `fallback_reason=<trigger>`、`source_switch=true`；不合格则保留 primary，并追加 `fallback_provenance_insufficient` 和原 trigger。primary 是 error 时继续保留零 datum 不变式。

5. `list_limit_up_sectors` 的 route capability 必须是 `limit_up_sectors`，不能压缩成当前集合中不存在的 `limit_up`；quote、sentiment、limit-up count 的 operationId 复用不应导致 capability 混淆。

### P0-C：指数代码与市场计划不覆盖 critic 参数

涉及：`app/graph/market_plan.py`、`app/graph/gap_loop.py`、`app/agent/prompts_graph.py`、`app/gateway/stock_codes.py`（若需增加共享 helper）、相关 stage-1/market-plan 测试。

1. 代码拥有的默认市场指数改成带前缀的 canonical 列表：

   `SH000001`、`SH000300`、`SZ399006`。

   可以保留 `A_SHARE_BENCHMARK_SYMBOL` 兼容别名，但新默认值不能再是裸 `000300`。代码前缀 canonicalization 要大小写不敏感，并复用 `stock_codes` 的交易所推导规则；Tencent transport 的现有 `sh000300` 输出保持可用。

2. `apply_market_summary_prefix` 的 quote 策略：

   - 没有 quote step 时注入上述默认列表。
   - 已有 quote step 时先规范 symbols 的交易所前缀；已有多指数列表必须原样保留语义，特别是 critic 的 `SH000001/SH000300/SZ399006`，不得删除后替换成单一默认值。
   - 只有旧式单一裸指数 `000300` 需要转换成 `SH000300`；不要把明确的多标的参数压回单标的。
   - quote step 的最终 arguments 仍走 `canonicalize_tool_arguments`，日志保留 canonical arguments。

3. `append_gap_steps` 中 quote 无参数兜底改用同一默认 canonical 列表；不要在 gap loop 再写一份 `000300` 常量。

4. 更新 Planner prompt 的市场级示例为 `quote(symbols=["SH000001","SH000300","SZ399006"])`，并明确 critic/supervisor 已提供 quote 参数时保留其多标的口径。

5. `apply_as_of_date_to_plan` 与 gap 参数 canonicalization：

   - 对 `sentiment`、`limit_up_count`、`limit_up_sectors`、`telegraph` 等没有 Gateway 日期参数的 key，删除仅用于研究目标的 `{date/as_of_date/trade_date/trading_date}`；`limit_up_pool` 按日期查询，保留不同日期变体并留在 plan/state 审计字段。
   - 在 `coverage_requirements` 计算 variant signature 前做同样 canonicalization，使 `{date: D}` 和 `{}` 视为同一变体；保留较严格的 `min_datum_count` 与 `allow_partial`。
   - 这样 sentiment 不会因一个无效日期变体和一个空变体被执行两次。若 THS 查询需要日期，把日期拼入受控 query 的唯一入口；MCP 参数仍保持 Gateway 合约。

### P0-D：严格审计拆分 datum 完整性、结果集合完整性和新闻检索口径

涉及：`app/agent/evidence_gate.py`、`app/graph/nodes/finalize.py`、`app/graph/nodes/critic.py`，必要时 `app/research/news_search.py` / `app/research/iwencai_search.py`。

1. 在 strict `match_claims_to_evidence` 中区分：

   - `datum_partial`：datum 自身 `partial/status/completeness=partial`，继续拒绝窄事实。
   - `result_partial`：只表示 sibling/集合覆盖不完整；对 dated、complete、正确标的的窄观察允许通过；对“全市场/全部/完整/排名/最强/市场情绪”等穷举式 claim 继续加 `partial_only`。
   - `completeness`：行情 datum 仍只接受 `complete/full`；不得把 `unknown/page` 误放行。

2. 新闻/公告/事件单篇证据走独立规则：

   - 识别 `date_scope=event` 或 `source_tool` 含 news/announcement/telegraph，且 claim 是具体报道/公告事实。
   - 允许 `completeness=search_results`，但必须有 `as_of_date`（或 value 内 `published_at` 可解析为同日）、非空 source/authority、标题或摘要/url/document_id。
   - `result_partial` 对单篇事实不直接阻塞；含“全部、所有、完整消息面、全市场新闻/主线”等穷举语义时仍拒绝，除非未来接入真正 complete 的事件集合。
   - 不修改 `plan_evidence_only_fallback` 对 `search_results` 的通用完整性要求；这是新闻 claim matcher 的窄例外，不是 generic fallback 放宽。

3. `evidence_domain_compatible` 对无唯一证券代码的媒体结果放行“报道 A 股市场/指数盘面”类事件 claim；若媒体 datum 明确带 HK/US/crypto instrument，仍拒绝 A 股 domain。个股新闻的代码/名称匹配继续由后续 instrument matcher 负责。

4. `_degraded_evidence_ok` 与 `finalize` 继续禁止 partial datum 支撑强结论；允许 `search_results` 的单篇新闻事实，并保持 source/authority 检查。`critic._coverage_issues` 的 breadth 计算继续排除 `result_partial` 行，避免窄成交额事实反向证明全市场广度。

### P1：修复研究轮记账与实际 route 一致性

涉及：`app/graph/nodes/supervisor.py`、`app/graph/gap_loop.py`、`app/graph/builder.py`、相关 run-log 测试。

1. gap disposition 用精确的 `(tool_key, canonical_arguments)` signature 记账。`budget_truncated` 只标记没有进入最终 `plan.steps` 的变体；不能仅按 key 把已执行的 baseline quote 标成 dropped。

2. 在 `SupervisorNode._plan` 完成 prefix、date canonicalization、drop-repeat、append-gap、`max_steps` 截断之后，再生成 `gap_key_decisions` 与 `executable_gap_steps`。对每一条 disposition 做断言：

   - decision=kept ⇒ 精确 signature 在最终 plan 中；
   - reason=budget_truncated ⇒ 精确 signature 不在最终 plan 中；
   - 最终 `_build_route(plan)` 的所有 tool calls 都来自最终 plan，不能复用上一轮 route。

3. 保留 `research_budget_exhausted` 的终态语义，但不要因“quote 被标为 truncated”又执行 quote；日志要同时展示 `baseline_executed_signatures` 与 `dropped_gap_signatures`，便于审计。

4. 如果 route 为空或没有 executable gap，继续走 `finalize_after_gap`，不能触发默认 analyst 扇出而暗中再烧一轮工具预算。

## 必须新增或更新的回归

建议直接扩展现有文件，避免新建重复 fixture；每个用例只使用本地 dict/`ToolResult`，不得访问真实网络。

### Gateway / fallback

- `tests/test_p6_ths_skill_adapter.py`：MCP quote raw `{source: "tencent", count: 1, items: [{symbol, time, price/change_pct}]}` 归一化后每个业务 datum 有 authority，顶层 count 不阻塞 `_has_provenance`；limit-up sectors 同样覆盖 `source: "ashare-master"`。
- 同文件：`gateway_business_error`、`status=partial` 且缺 market breadth、`status=partial` 且缺 limit-up count 的 primary 都会调用 fallback；字段齐全的 partial narrow quote 不重复 fallback。
- fallback provenance 失败、日期错、标的错、authority 缺失仍返回 primary/zero datum，并保留 `fallback_provenance_insufficient`。

### 计划与回环

- `tests/test_stage1_quote_contract.py`、`tests/test_market_level_plan.py`：默认 quote 为带前缀三指数；已有 critic 多指数参数经过 prefix 后保持；裸 `000300` 只转为 `SH000300`。
- `tests/test_research_rounds.py` 或 `tests/test_market_level_plan.py`：sentiment 的 `{date}` 与 `{}` canonical 后只产生一个 gap step；quote 的 `budget_truncated` 精确变体不出现在最终 route。
- 保留现有“未在 registry 的 key 不执行”“个股问题不注入市场最低集”“日期不回填为 retrieval time”等回归。

### Evidence gate / finalize

- `tests/test_evidence_gate.py`：`completeness=search_results` 的带日期、source、title/url 的单篇新闻在 strict event claim 下 supported；同一证据用于“全市场完整消息面”仍拒绝。
- `tests/test_evidence_authenticity.py`：`result_partial=True + datum.completeness=complete + datum.partial=False` 的窄成交额/价格事实通过；同一 datum 用于全市场/排名/完整结论仍为 `partial_only`；`datum.partial=True` 仍拒绝。
- 媒体无 instrument 的 A 股盘面报道不再 `domain_mismatch`；明确 HK/US 媒体证据仍拒绝 A 股 claim。
- 现有 `tests/test_p13_evidence_delivery.py` 中 generic evidence-only fallback 对 `search_results` 继续拒绝，证明新闻例外没有扩散到通用降级路径。

## 离线验收命令

在 `D:\Mosaic` 执行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_p6_ths_skill_adapter.py tests/test_stage1_quote_contract.py tests/test_market_level_plan.py tests/test_evidence_gate.py tests/test_evidence_authenticity.py tests/test_p13_evidence_delivery.py tests/test_research_rounds.py -q
.venv\Scripts\python.exe -m pytest tests/test_p15_verified_boundary.py tests/test_p16_acceptance.py tests/test_controlled_degraded.py -q
.venv\Scripts\python.exe -m ruff check app tests
.venv\Scripts\python.exe -m compileall -q app
git diff --check
```

若仓库中某个测试文件尚未包含本交接单指定用例，子 agent 必须先补用例再执行命令；不得以跳过测试代替验收。真实 live acceptance 另行授权后再跑，且使用独立临时 memory/artifact，不覆盖既有 `memory/` 和 `.tmp/` 记录。

## 交付物与审查清单

子 agent 完成后提交以下内容给主 agent：

1. 变更文件列表及每个文件对应的 P0/P1 项；
2. 新增/修改测试名称和离线命令输出摘要；
3. 一条包含 quote、limit-up sectors、sentiment、limit-up count、news、result_partial 的端到端离线 fixture 结果，至少列出 `fallback_reason`、`source_switch`、`datum_count`、`as_of_date`、`authority`、`completeness`、`partial`、`result_partial`；
4. 明确说明没有把 retrieval time 当 observation date、没有把 MCP transport 当 authority、没有放宽 generic evidence-only fallback；
5. 若真实接口仍缺字段，只报告保留的窄事实和阻塞原因，不要把 status=partial 改成 success 或手工补日期。

## 最终审查补修与离线交接（2026-10-14）

本章节补充前文的历史审计记录，纠正文件开头“已完成”的过早结论。接手基线为主 agent 的 **5 failed、1650 passed**；四个已复现漏洞均已修复，最终完整离线 pytest 为 **1709 passed、0 failed**。本次没有开启新的编号阶段，`production_ready=false` 保持不变。

### 复用范围与最小修复

先检索仓库已有实现和现有开源依赖（Pydantic、pytest、HTTPX、MCP SDK、LangGraph），沿用当前架构，没有下载项目、安装新依赖或查询外部服务。复用 `normalize_date`、既有字段别名与数值检查、`semantic_signature`、`coverage_records/_variant_satisfied`、真实 matcher、Finalize、hydration 和 persistence。未增加生产工具预算、研究轮数或回环次数。

| 实现漏洞 | 根因与修复 | 实际变更文件 |
| --- | --- | --- |
| 单篇新闻支持全市场穷举 | 新闻完整性例外只检查 evidence，未检查 claim 范围。`_single_news_evidence_allowed` 现必须接收实际 claim，要求日期、来源、authority、retrieval 和可审阅内容；全市场、全部、所有、完整、排名和因果拒绝与 `result_partial` 无关。Finalize 的 degraded 保留检查复用同一例外，避免严格审计拒绝后仍保留越界新闻。generic evidence-only fallback 未放宽 | `app/agent/evidence_gate.py`、`app/graph/nodes/finalize.py` |
| 行自身不完整丢失 | `_make_datum` 只保留本地截断，忽略上游行的 `partial/status/completeness`。递归抽取现从业务行继承不完整语义，并将 completeness 视为元数据；结果容器的 partial 不污染独立完整行，明确 partial 的行及其嵌套价格不能升级为完整 | `app/gateway/normalizer.py` |
| 板块 fallback 字段不足 | 原字段组只检查整个结果内是否出现板块相关列。现按同一业务行检查非空板块/题材身份和有限数值 count/rank/pct 等结构字段；不拼接不同业务行，不用信封 count，不接受空值、布尔值、NaN 或文本伪指标。原 source/authority/日期检查继续执行，fallback 拒绝保留 primary 窄事实及诊断 | `app/gateway/controlled_fallback.py` |
| 多变体截断漏记 | 原 `recorded` 按 key+reason 去重，第二个被截断 query 被吞掉；多 kept 变体也可能没有签名。现逐精确 key+canonical arguments/signature 生成 kept/dropped 记录，与最终计划对照；已满足的 baseline 记为 already_satisfied，未执行变体逐条 budget_truncated；executable variants 只保留最终计划中的签名 | `app/graph/nodes/supervisor.py` |

### 五个旧失败的契约核对

1. **P16 hydration 两个 legacy 分支：契约调整，不是放宽行完整性。** `result_partial=true` 表示结果集合有限；独立完整的指定日单标的事实可以 verified 并持久化。保留并扩展原测试，两个 selector 格式分别验证所有 provenance、instrument、partial/result_partial、status、coverage、completeness 均从真实账本复制；datum.partial 仍拒绝，全市场完整行情仍因 result_partial 拒绝，拒绝场景三表零写入。没有删除旧负向覆盖。
2. **P5 腾讯编码：真正的 transport 回归。** 前文把 canonical 指数改成 `SH000300` 后，transport helper 错把 uppercase canonical 直接发出。结合 P5 已有真实记录及 MCP string/array、HTTP query 边界回归，现 canonical/route/cache/audit 仍为 `SH000300`，Tencent wire 为 `sh000300`；显式交易所代码在 wire 层小写。裸 `000001` 继续保持裸代码，不推断为上证指数。原 P5 负向断言原样保留，另补 canonical→transport 对照。变更：`app/gateway/arguments.py`、`tests/test_p5_stability.py`；现有 HTTP/MCP 测试的 wire 预期在 `tests/test_stage1_quote_contract.py`、`tests/test_incident_ashare_stream_fix.py` 中按实际边界更新，canonical 断言仍保留。
3. **两个交易日测试：研究参数与 transport 分离。** 仓库 `allowed_openapi.json` 中 `/ashare-master/limit-up/pool`、count、sectors、sentiment 均没有参数声明，而 `skill_route_for` 会把 `limit_up_pool.date` 写进 THS query。因此不同 pool 研究日期保留在 plan、签名、audit 和 `ToolResult.arguments`，THS query 保留日期；MCP/HTTP 当前端点发送 `{}`。新增共享 `gateway_transport_arguments` 仅在两个 client 的 wire 边界清参数。无返回日期的结果仍无日期，不能由请求或 retrieval 时间补齐；当前日期结果不能满足异日 fallback。变更：`app/gateway/arguments.py`、`app/gateway/http_client.py`、`app/gateway/mcp_client.py`、`tests/test_trading_calendar_edge_cases.py`。旧测试按这个双边界契约扩展，不是只改期望让测试变绿。

### 关键回归与隔离修正

`tests/test_mcp_fallback_remediation.py` 增加或扩展：

- 三个 news query 变体，预算 1/2 的正反对照：每个 kept 签名在最终 route，每个 budget_truncated 签名不在 route，数量分别为两个/一个；另测已执行 query-a baseline 不被标成丢弃。生产预算没有改变。
- 单篇 `domain=media/source_tool=internal_iwencai_news_search/metric=news_1` fixture，`partial=false`，分别令 `result_partial=false/true`；全市场完整消息面、全部、所有、完整消息面、排名、因果逐项走严格 matcher、Critic=pass 的真实 Finalize 和伪造 verified 的 persistence，永远不能 verified/落库；有限带日期公告正向链路通过。
- envelope.partial=true 搭配独立完整行的正向链路；行显式 partial、status=partial、completeness=partial/incomplete/truncated，分别包含直接和嵌套价格的负向链路。缺日期/标的的 sibling 不从请求参数或 retrieval 时间补齐，partial 容器不污染完整 sibling。
- 板块仅有名称、空值、文本 rank、NaN pct、布尔 count、两个无关行字段拼接等反例，以及 count=0、rank=1、数值 pct 正例；source、authority、日期缺失/异日继续拒绝。Router 回退拒绝保留主源可用事实与 `missing_required_fields:limit_up_sectors/fallback_provenance_insufficient`。
- 拒绝场景额外以 degraded、blocked、failed 标签调用 persistence，均 `persisted=false` 且 research_records/conversations/daily_states 零写入。

`tests/test_p16_acceptance.py` 保留 hydration 元数据断言，扩展两种 selector×三种 scope；`tests/test_trading_calendar_edge_cases.py` 扩展无日期端点清理及两日期×HTTP/MCP 实际发送参数检查。

全量运行还发现两个既有测试隔离问题，已修正并保留原契约检查：

- `tests/test_deployment_config.py` 默认路径测试原来真实打开用户 `memory/memory.db`。现先核对真实 `project_root()`，再把该依赖替换为独立临时项目根，真实构造/SQLite/默认路径规则继续执行。
- `tests/test_stage9_release.py` 原来启动 Uvicorn listener 并真实连接 loopback `/health`。现仍在独立 Python 子进程加载真实发布配置、运行真实 lifespan 和 `/health`，通过 ASGI TestClient 执行，子进程禁止 DNS/服务连接。保留 health、scheduler 和 release settings 断言；本离线测试不再声称覆盖 Uvicorn 监听网络。这是验证隔离调整，没有修改生产服务实现。

本次实际追加/修改文件共 17 个：上述 8 个 app 文件、8 个测试文件及本交接文档。接手时已存在的其它工作树修改保持原样；没有修改 `app/research/reasoning.py` 的真实账本 hydration，亦没有删除 P15 日期/标的、无引用/伪造引用、source/authority、冲突、价格域和覆盖门禁。

### 实际离线验证结果

新记录目录：`.tmp/mcp-remediation-review-20261014-223331-13b97f3b/`。日志使用独立文件名，数据库均在新 UUID 临时目录；SQLite 复现传入明确 `offline-remediation-*` 或 `p16-partial-*` conversation_id。没有向用户库写入。

| 检查 | 实际结果与记录 |
| --- | --- |
| 接手聚焦基线 | 5 failed、77 passed，与主 agent 的五个失败一致 |
| 先补回归、未修实现 | MCP/P16 集合 25 failed、73 passed；覆盖四项漏洞。wire/日期边界补回归后 8 failed、58 passed，随后修复实现 |
| 修复后聚焦 | 327 passed、1 条第三方 warning，`focused-02.log` |
| 隔离问题初次全量 | 2 failed、1707 passed，默认历史库访问和服务连接均被保护器拒绝；保留 `full-pytest-01.log` |
| 隔离专项 | 15 passed，`isolation-regressions.log` |
| 最终完整 pytest | **1709 passed、0 failed、1 条 Starlette/AnyIO BlockingPortal 弃用 warning**，`full-pytest-final.log`；保护器记录 200 次独立 SQLite 连接、0 次受保护写入，5 次既有可选 registry DNS 尝试均在连接前阻止 |
| Ruff | `app scripts tests` 全部通过，`ruff-final.log` |
| compileall | `app scripts tests` 全部通过，`compileall-final.log` |
| git diff --check | 通过；只有既有 LF/CRLF 转换提示，`diff-check-final.log` |
| P9 `--mode replay` | 26 行、12 场景通过，历史仍为 not_observed，`p9-replay.log` |
| P10 `--mode replay` | 25 行、12 场景通过，仅验证历史记录，`p10-replay.log` |
| P15 `--mode replay --artifact <全新路径>` | 7 场景通过，新 artifact 为本目录 `p15-new/acceptance.jsonl`，`p15-replay.log`；不会覆盖历史 P15 artifact |
| P16 `--mode replay` | 16 条保存观察通过，live_market_verified=0、pure_structure_rows=0、production_ready=false，`p16-replay.log`；没有重新请求真实服务 |
| 独立 SQLite 只读复核 | 本次快照后创建的 217 个边界复现库中，182 个拒绝库三表为 0、35 个离线正向对照三表为 1；来自本次多次聚焦/全量运行，不是 217 个生产场景，详见 `independent-boundary-sqlite.json` |

明确离线入口均通过本目录 `offline_run.py` 执行。它为每次运行设置新的 `MOSAIC_MEMORY_DB/MOSAIC_DATA_DIR`，关闭真实启动自检，禁止 DNS/服务连接、真实 MCP 启动器及受保护历史数据库/文件写入；Windows asyncio 内部 socketpair 仅用于本地事件循环。P15/P16 自带禁止网络的 replay 保护继续生效。P12/P13/P14 没有 replay 模式，本次没有运行这些 acceptance 脚本或向它们传入 replay 参数，只运行其离线回归。

```powershell
# 每次用唯一日志名，下面省略 Tee-Object 日志重定向
.venv\Scripts\python.exe .tmp/mcp-remediation-review-20261014-223331-13b97f3b/offline_run.py pytest -q -p no:cacheprovider --tb=short
.venv\Scripts\python.exe -m ruff check app scripts tests
.venv\Scripts\python.exe -m compileall -q app scripts tests
git diff --check
.venv\Scripts\python.exe .tmp/mcp-remediation-review-20261014-223331-13b97f3b/offline_run.py scripts/p9_e2e_acceptance.py --mode replay
.venv\Scripts\python.exe .tmp/mcp-remediation-review-20261014-223331-13b97f3b/offline_run.py scripts/p10_e2e_acceptance.py --mode replay
# 再次复现 P15 应选择另一个全新 artifact 路径，或明确 append
.venv\Scripts\python.exe .tmp/mcp-remediation-review-20261014-223331-13b97f3b/offline_run.py scripts/p15_acceptance.py --mode replay --artifact .tmp/mcp-remediation-review-20261014-223331-13b97f3b/p15-new/acceptance.jsonl
.venv\Scripts\python.exe .tmp/mcp-remediation-review-20261014-223331-13b97f3b/offline_run.py scripts/p16_acceptance.py --mode replay
```

### 保留性、联网事实与具体交接结论

复核快照覆盖 9770 个可读历史数据库、evening、JSON、JSONL、日志等文件。无文件删除；9769 个字节内容一致。历史 `.tmp/iiix-appdata/iiix/logs/iiix.log` 从 194941 字节追加到 203000 字节，原始 194941 字节的 SHA-256 前缀一致，没有覆盖或截断。15 个既有不可读目录明确记录，不能声称审计其内部，详见 `protected-before.json` 和 `protected-after-final.json`。

必须如实说明隔离发现：早期全量中，6 个旧 API fixture 的真实 lifespan 自检曾启动本地 iiix，均因本地 update-coordinator.lock 权限错误退出，未进入 initialize/list_tools/call_tool。该既有日志追加保留，不作真实成功记录；随后在验证入口增加启动器拒绝并关闭真实自检，最终全量没有启动真实 MCP。所有 registry DNS 尝试在 DNS/连接前被阻止；本轮没有真实 THS、MCP、LLM 业务调用，没有真实外部联网或生产通过证据。

没有执行 commit、push、reset、revert；没有删除或覆盖历史 artifact、evening、数据库、JSONL 或日志。离线 fixture 的 verified 与 SQLite 正向对照只证明本地门禁实现；P9/P10/P16 replay 只复核历史记录，不能算本轮真实通过。

**交接结论：四项审查漏洞和五个原全量失败均已处理，离线实现验收通过，可以交给主 agent 复核当前工作树；生产验收仍未完成，`production_ready=false`。** 后续若另行授权真实验收，仍需在独立临时库和 artifact 中检查上游实际日期、标的、source/authority、结构指标与全市场覆盖；本次不自动启动 live、发布或新的编号阶段。

