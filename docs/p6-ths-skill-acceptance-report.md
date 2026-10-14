# P6 同花顺 Skill 接入与验收报告

基线：`main@a938dd7`。本次只增加适配、路由、测试和验收产物；没有执行 reset、revert、commit 或 push。

## 结论

`production_ready=false`。

真实探针显示，同花顺问财 Skill 的 HTTPS 数据链路可用，至少 finance、announcement、news 等 Skill 返回了可审计的结构化 datum 和 evidence；Skill 失败返回 `status=error`、`normalized=[]`，不会生成 evidence。Skill 成功不代表 MCP 修复：MCP 的 initialize/list_tools/真实 call_tool 都被单独记录，最新 run 中 quote 偶尔成功，但 sentiment、abnormal-reasons 仍出现 `gateway_timeout`/`deadline_exhausted`，因此 MCP 整体不通过。

真实 run_id：`8bbcf7959378`、`1abba8cb753e`、`a2e9780c58ad`、`b5a40d2e9400`、`7c8d63490259`、`ee621141669e`；最新验收 run 为 `ee621141669e`。完整脱敏摘要在 [memory/p6-ths-skill-acceptance.jsonl](/D:/Mosaic/memory/p6-ths-skill-acceptance.jsonl)。

## 十个 Skill 的真实调用形态

十个 Skill 均为远程官方 HTTPS/API，不是本地工具、浏览器自动化或仅提示词能力，也不依赖 iiix/MCP。query2data 类调用 `POST /v1/query2data`；搜索类调用 `POST /v1/comprehensive/search`，通过 `X-Claw-Skill-Id`、`X-Claw-Skill-Version: 1.0.0`、trace header 选择固定官方 Skill。原始 JSON 保留在调用结果的 `raw`（验收 JSONL 不写 raw）。注册表、输入/输出 JSON Schema 和 capability 见 [ths_skill_registry.py](/D:/Mosaic/app/gateway/ths_skill_registry.py)。

| Skill | Skill ID / endpoint | 输入 | 输出 | 能力 | 真实可用性（最新 run） |
|---|---|---|---|---|---|
| finance | `hithink-finance-query` / query2data | query, page, limit, is_cache, expand_index | `datas[]`, columns, code_count | finance | success，有 evidence |
| events | `hithink-event-query` / query2data | 同上 | `datas[]` | events / abnormal-reasons | empty_normalized，error，无 evidence |
| basicinfo | `hithink-basicinfo-query` / query2data | 同上 | `datas[]` | entity_lookup | partial；静态属性日期不冒充行情日期 |
| market | `hithink-market-query` / query2data | 同上 | `datas[]` | quote, kline, market_breadth | partial；本次所问行情字段缺 as_of_date |
| macro | `hithink-macro-query` / query2data | 同上 | `datas[]` | macro | success，有 evidence |
| industry | `hithink-industry-query` / query2data | 同上 | `datas[]` | sector_theme, market_breadth | partial，缺字段日期/分类覆盖 |
| management | `hithink-management-query` / query2data | 同上 | `datas[]` | ownership | partial，分页/日期覆盖受限 |
| announcement | `announcement-search` / comprehensive/search | query, size | `data[]` | news / announcement | success，有 evidence；按相关性，非全量 |
| report | `report-search` / comprehensive/search | query, size | `data[]` | news / report | empty_normalized，error，无 evidence |
| news | `news-search` / comprehensive/search | query, size | `data[]` | news | success，有 evidence；本次无唯一 instrument |

成功 datum 由统一 adapter 保留 `source/source_tool`、`instrument/instrument_name`、`as_of_date/retrieved_at`、`authority`、`completeness/coverage`、`partial` 和原始链路字段。缺日期、标的或来源只会保留为质量缺口，不从 query、检索时间或文本猜测；日期/实体 claim 仍由 Evidence Gate/Critic 阻断或降级。

失败、超时、空响应、业务错误和参数错误均归一为零 datum 的 `status=error`，使用稳定 reason code，例如 `gateway_timeout`、`gateway_auth_error`、`gateway_upstream_error`、`gateway_invalid_response`、`empty_normalized`、`invalid_arguments`。`build_evidence([result]) == []` 是回归契约。

## 能力矩阵与受控路由

统一层为 [ths_skill_adapter.py](/D:/Mosaic/app/gateway/ths_skill_adapter.py) 和 [controlled_fallback.py](/D:/Mosaic/app/gateway/controlled_fallback.py)。ToolRuntime 的 Gateway/MCP error、timeout 和 unavailable 路径现在实际调用 ControlledFallbackRouter；连接初始化异常也进入同一受控路径。ToolRuntime 不再为十个 Skill 分散实现分支；Evidence Gate 没有增加 Skill 特例。

| 能力 | HTTP Gateway | MCP | 同花顺 Skill | fallback 条件 |
|---|---|---|---|---|
| quote | `get_market_quotes` | 同 operation | market query | primary 明确 timeout/error，Skill 有日期、标的、source、authority、retrieved_at |
| kline | `get_market_klines` | 同 operation | market query | 同上；partial 不能支持强比较 |
| market breadth / sentiment | `get_ashare_sentiment` | 同 operation | market query | 不把文本或无日期结果当行情 |
| limit-up | `limit_up_count/pool/sectors` | 同 operation | market/industry query（实际覆盖需 query 证据） | 不把 Skill 成功等同 limit-up MCP 修复 |
| sector/theme | `limit_up_sectors` | 同 operation | industry query | 分类体系和日期必须明确 |
| news | news/search tools | MCP 若有 | news/announcement/report search | 只支持有媒体、发布日期、链接/文档链的 news claim |
| abnormal-reasons | `get_stock_abnormal_reasons` | 同 operation | events query | empty/upstream/timeout 分开记录，未观测继续 `not_observed` |

只有 primary 是明确 timeout/error，且 fallback 具备不弱于 primary 的 provenance 时才切换。authority conflict、conflicting evidence、日期/标的不完整都会保留 primary 失败；切换结果写入同一 `run_id` 的 `primary_source`、`fallback_source`、`fallback_reason`、`source_switch`。Metric 记录 success/error/timeout、p50/p95、日期完整率、标的匹配率、partial 比例和 evidence 可用率。

## 真实对照结果

最新 run `a2e9780c58ad` 的 quote/sentiment/abnormal-reasons 均分别走 HTTP、MCP、同花顺 Skill。MCP rows 记录 `mcp_initialize=true`、`mcp_list_tools=true`、`mcp_real_call=true` 后再记录业务结果；这三个阶段全部成功才算 MCP 通过。本次 MCP 仅出现局部 quote success，sentiment 和 abnormal-reasons timeout，因此结论是：**同花顺 Skill 数据链路可用；MCP TLS/上游问题仍未修复**。

HTTP abnormal-reasons 返回 success；MCP 返回 timeout/deadline；同花顺 Skill 本次返回 empty_normalized。empty/upstream-error 场景没有在该 run 真实观测到，JSONL 明确标为 `observed=false`、`not_observed`，没有把 timeout 或文本响应冒充这些场景。

交易日 probe 保留 requested date、market_closed、as_of_date。周末 probe 固定使用明确日期 `2026-10-11`，并断言 `requested_date=2026-10-11`、`market_closed=true`；不会把周末新闻当成周末行情。最近交易日数据必须有自己的 `as_of_date`。code-only quote 可以验证代码行情，名称仍需结构化映射，未验证实体不会被判为虚假。

partial refresh 现在通过 ToolRuntime 连续执行 normal/normal/refresh：第二次命中共享缓存，第三次真实执行且 `refresh_requested=true`、`cache_hit=false`。失败 refresh 不从旧缓存恢复。partial datum 只保留已验证 claim，不能支撑强比较、因果或全市场绝对结论。

## 验收与后续修复

测试覆盖统一 adapter、十项 registry、错误零 evidence、provenance 缺口、fallback、权威冲突、日期/实体和 refresh；验收脚本可重复执行：

```text
python scripts/p6_ths_skill_acceptance.py --mode probe
python scripts/p6_ths_skill_acceptance.py --mode replay
```

验收脚本是 tool probe，不伪造研究链路结论：Evidence Gate 字段单独命名为 `evidence_gate`，`critic_verdict/finalize/delivery` 固定为 `not_invoked_tool_probe`；只有真实 Orchestrator 运行才可填写 Critic/Finalize/Delivery 终态。本次已通过完整 `pytest`（1332 passed）、P6 新增 adapter/fallback 回归、`compileall`、Ruff 和 `git diff --check`。最小后续修复是：补齐 market/industry/management 的字段日期覆盖、为 events/report 提供可观测的有效查询样例、修复 MCP 子进程/TLS/上游 timeout，并重新观测 abnormal-reasons 的 empty/upstream-error；在这些条件满足前保持 `production_ready=false`。
