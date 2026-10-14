# P16 真实交付验收与收尾

验收日期：2026-10-14，Asia/Shanghai。生产范围按真实场景逐项判断，`production_ready=false`。离线 verified、纯休市说明以及单标的窄行情均不代表全市场报告或全部同花顺技能通过。

## 验收入口与隔离

`scripts/p16_acceptance.py --mode live` 通过 FastAPI 的 `/api/ask` 和 `/api/ask/stream` 入口，使用生产 `get_settings()`、ToolRuntime、Graph、真实同花顺与真实 LLM、Critic、Finalize、响应 builder 和 persistence。没有直接调用 THS adapter 代替入口，也没有替换模型或工具响应。

每次请求都有独立 conversation_id 和 `.tmp/p16-*/memory.db`。仅内存/数据目录指向新临时目录，生产路由、模型、预算与回环配置保持不变。端点 TestClient 不启动定时简报和与研究无关的 Gateway startup selfcheck；真实端点懒初始化生产图。新临时库保留供复核，关闭句柄后才写最终 cleanup_status，不删除用户既有数据库、evening、JSONL 或日志。

模型为 `qwen3.7-flash`；主源 `ths_skill`；路由开启；总预算 1000 秒、completion 配置 2048、max_rewrites=1、max_research_rounds=1。本阶段未增加这些配置。

## 已定位的本地修复

1. **全市场问题被错误缩成单标的。** 首轮今日 sync 的 `run_id=d202e20257aa` 取得 38 条账本证据，包括真实 THS 指数数据，但 Reasoning 从计划内默认 `quote.symbols=[000300]` 推断用户指定该标的，`target_filter` 排除全部 38 条。修改 `app/graph/nodes/reasoning.py`，单标的过滤只取用户问题中的明确证券代码，保留全市场、市场广度和板块上下文。今日问题首轮 sync/SSE 完成后才安排一次复验。
2. **真实证据的 result_partial 丢失。** `app/research/reasoning.py` 的新旧 evidence 展示转换没有复制该字段，旧格式还遗漏 instrument。现直接从真实账本复制，避免 persistence 最后一道校验只有报告时丢失上游限制。回归明确证明：带 result_partial 的证据不能通过伪造 verified 标签落库。窄行情复验不会抹掉上游 partial。
3. **取证与回放。** Finalize 在清空不可信正文前记录完整账本数量、覆盖/完整性/日期分布，以及逐 claim 的严格匹配 reason_codes 和实际引用证据元数据；日志不改变裁决。P16 复用 P14 修复后的直接/包装事件采集器。P15 增加显式 `--mode replay` 与独立 artifact 参数，未知参数报错；P15/P16 replay 对 DNS/socket 连接实行禁止访问。
4. **全市场成文重复完整 evidence，导致实际截断。** 修复过滤后的 sync `78663a674a2d` 在 Reasoning 90 秒阶段限额超时，SSE `2ec61920c73b` 观察到 `finish_reason=length / parse_status=invalid_json`。此时 prompt 约 38 KB，旧契约要求完整 evidence 对象和重叠正文，截断被拒绝，零报告证据、零持久化。新增仅用于非窄行情的 `REFERENCE_REPORT_PROMPT`，模型输出去重 id 选择器与最多六条 claim；证据值及所有 provenance 字段仍由原有解析器从真实账本填充。JSON 解析、schema、引用校验、Critic、Finalize 以及 length 拒绝均保留。针对该新定位原因只复验一次；休市场景亦复验同一过滤修复。没有通过增加回环或总预算掩盖超时。

本轮新增 P16 JSONL 使用 append 写入。采集 datum 数累计实际 executions；账本数取同 run 的 reasoning/critic/finalize ledger 阶段；报告 evidence/claim 数取实际终态响应。sync/SSE 的独立运行不强行逐字段相等；同一交付状态检查审计字段 schema 一致。heartbeat/progress 为非终态，终态按 terminal=true 单独计数。

## 最终功能结论

四场景各完成首轮 sync/SSE，随后仅针对已定位并修复的原因复验，共 16 次真实请求。最终 4 次 degraded、12 次 blocked、0 次 verified、0 次 failed；没有纯结构说明成功对照。`production_ready=false`。

| 分类 | 功能与实际可用范围 | 限制 |
| --- | --- | --- |
| 真实通过 | 本轮没有达到 verified 并持久化的真实业务场景 | 真实 THS/LLM 请求、合法成文和安全阻断均被观察到，但不等于业务交付通过 |
| 受控降级可用 | SH600519 指定日窄行情可交付带真实引用、保留日期/口径/partial 限制的局部事实，sync/SSE 均 degraded | 始终 persisted=false；不能称为完整行情通过，更不能外推至全部技能 |
| 被证据阻断 | 今日全市场报告、2026-10-11 休市问题、600519 十月公告均 blocked | 全市场广度缺口、板块完整性不足、异日行情、新闻域不匹配、公告无有效匹配不能靠文字补齐 |
| 上游/认证阻塞 | THS 部分返回缺行情日/覆盖，公告源返回内容但有效匹配为零；MCP fallback 缺必要 provenance 被拒 | 本轮未观察到 401、TLS 或 504，不能归因为认证故障。曾观察到真实 LLM Reasoning timeout/length，缩短输出契约后本次复验均 stop |

全部请求均从独立 SQLite 确认 research_records/conversations/daily_states 零写入，历史检索亦为空。因此不能声称真实 verified 的写入及历史检索正向通过；真实成功后检索的自动校验已实现，离线正向门禁及 SQLite 回归通过。memory 是现有 MarketMemory 存储/检索能力，未添加或声称存在第四张 memory 表。

窄行情的四次真实请求（首轮及字段传播复验各 sync/SSE）均 degraded。原始完整账本保留 THS 动态字段中的真实 SH600519、2026-10-09 收盘值；同时上游夹带无日期最新价/涨跌幅、异日或其他字段，响应与账本保留 partial/result_partial。第一次破坏完整交付条件发生在上游数据/adapter provenance 检查，严格匹配为 partial_only 等，模型成文均 stop。存在合格局部事实，受控降级可用；不得落库，也不代表全面行情验收通过。

公告首轮 sync/SSE 均 blocked。实际 announcement_search 为 HTTP 200，约 51 KB 返回，但符合真实标的及公告日期要求的归一化 datum 为零；iwencai_events 同样空。另有 10/20 条新闻账本，并非公告成功或零证据失败源制造的证据。具体阻断包括 invalid_reference、instrument_name_unknown、incomplete_completeness；没有使用请求参数补证券或公告日期，未因空匹配启动额外探针复验。

全市场首轮存在本地上下文过滤缺陷；过滤修复后模型 timeout/length 仍受控拒绝。引用选择器契约复验 sync/SSE 均正常成文 stop、合法 JSON。sync 初次正文约 1667 字符，补证据正文约 1766 字符，最终仍 blocked。SSE 有三条严格匹配合格的当日指数事实，但新闻及板块排名 claim 不合格，且缺完整市场广度，整体仍 blocked。局部指数事实不能自动成为全市场结论，无关工具失败也不改变这些合格事实本身的匹配结果。

休市场景的 plan 明确 requested_date=2026-10-11、planned as_of_date=2026-10-09、market_closed=true。实际响应不能据此回填观测日：复验 sync 的 as_of_date=null，SSE 的 as_of_date=2026-10-14，真实引用也有异日数据，均 blocked。新闻可有 10 月 11 日发布日，但不证明 10 月 9 日行情。没有将纯休市/缺口说明计为行情成功。

实际路由均记录在每条 JSONL 的 actual_routes/attempts：主源 ths_skill；今日/休市还尝试 mcp fallback。fallback 返回若缺标的、日期、来源等必要 provenance，保留 fallback_provenance_insufficient 与失败源 datum_count=0。公告、窄行情没有将直接 adapter 探针冒充公开入口。模型 evidence-only fallback 的候选/接受/拒绝数是另一套诊断，不能与数据源 fallback 混淆；候选接受不代表 fallback 已应用，本轮 fallback_applied 均为 false。

## 真实运行清单

下表的“数量”依次为采集 datum / 完整账本 evidence / 最终报告 evidence / 最终报告 claim。blocked 清空正文前的 claim 审计另见下一节和 JSONL，不能用最终报告的零条误称工具没有返回数据。HB 是 SSE heartbeat 数；所有运行唯一终态计数=1、cleanup_status=ok、acceptance_errors=[]、三表写入=0。所有 final_audit_status=research_exhausted，degraded 是保留合格局部事实后的受控交付状态。

| 场景 | 轮次 | transport | run_id | status | 数量 | finish / parse | HB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 窄行情 | 首轮 | sync | da3e219a4439 | degraded | 54/34/2/1 | stop / valid_json_object | 0 |
| 窄行情 | 首轮 | SSE | 8274fc36fb92 | degraded | 12/12/4/3 | stop / valid_json_object | 20 |
| 今日 | 首轮 | sync | d202e20257aa | blocked | 44/38/0/0 | stop / valid_json_object | 0 |
| 今日 | 首轮 | SSE | ed49285cfd07 | blocked | 44/38/0/0 | stop / valid_json_object | 19 |
| 休市 | 首轮 | sync | 814f1243dd77 | blocked | 41/41/0/0 | stop / valid_json_object | 0 |
| 休市 | 首轮 | SSE | 6411066df3f5 | blocked | 52/46/0/0 | stop / valid_json_object | 15 |
| 公告 | 首轮 | sync | 611d3ce8bb7f | blocked | 20/20/0/0 | stop / valid_json_object | 0 |
| 公告 | 首轮 | SSE | 0f5b4a248267 | blocked | 10/10/0/0 | stop / valid_json_object | 16 |
| 窄行情 | provenance 修复 | sync | bcf5b9ffe59b | degraded | 56/56/9/3 | stop / valid_json_object | 0 |
| 窄行情 | provenance 修复 | SSE | 5df1a99b26d2 | degraded | 9/9/2/2 | stop / valid_json_object | 11 |
| 今日 | 过滤修复 | sync | 78663a674a2d | blocked | 38/38/0/0 | 未取得完成结果 / null（timeout） | 0 |
| 今日 | 过滤修复 | SSE | 2ec61920c73b | blocked | 38/38/0/0 | length / invalid_json | 6 |
| 今日 | 引用契约修复 | sync | 2c63bab2a328 | blocked | 57/51/0/0 | stop / valid_json_object | 0 |
| 今日 | 引用契约修复 | SSE | 0133279bcba1 | blocked | 72/66/0/0 | stop / valid_json_object | 19 |
| 休市 | 过滤及引用契约修复 | sync | 72b7eded0510 | blocked | 50/44/0/0 | stop / valid_json_object | 0 |
| 休市 | 过滤及引用契约修复 | SSE | 97bb904c9698 | blocked | 42/36/0/0 | stop / valid_json_object | 15 |

## 逐 claim 审计与剩余根因

每个 run 的 claim_audits 按 claim_index 保留 supported、reason_codes、原 evidence_ids；events 中的 claim_evidence 保留该引用的实际值、标的、日期、来源、authority、retrieved_at、coverage、completeness、partial、result_partial。claim 文本仍按现有日志策略脱敏。下列是最终全市场/休市复验清空前的逐项决策，序号从零开始；首轮、窄行情和公告的相同明细也完整保留于 JSONL。

| run_id / claim_index | 实际引用 evidence_ids | 严格匹配结果 |
| --- | --- | --- |
| 2c63bab2a328 / 0 | technical-bb5dc0a96984c08f, technical-50c3e17c68d16937, technical-eb89fdc1cdc4a01b | partial_only（指数涨跌幅） |
| 2c63bab2a328 / 1 | technical-5752bdb8adab8aab, technical-1d3466d1b5453aaa | partial_only（成交额） |
| 2c63bab2a328 / 2 | fundamental-bb7841469274b362, fundamental-4b4081d48a62d3c3, fundamental-324e09e49ee3a5a0 | partial_only, unknown_date, missing_as_of_date, stale_month, incomplete_completeness（板块） |
| 2c63bab2a328 / 3 | technical-1c39f6a5467b4129 | domain_mismatch（新闻） |
| 2c63bab2a328 / 4 | technical-92ba0873aeac5a71, technical-e459c63c80c65e70 | domain_mismatch（新闻） |
| 0133279bcba1 / 0 | technical-c176f12711218627, technical-dd6acef4f0cd578c | supported=true，reason_codes=[]（创业板当日指数事实） |
| 0133279bcba1 / 1 | technical-b51e5a2b2617d498, technical-f65d889fdecce718 | supported=true，reason_codes=[]（深成指当日指数事实） |
| 0133279bcba1 / 2 | technical-0097d9501351a770, technical-fbb6e42e387cb1d9 | supported=true，reason_codes=[]（上证当日指数事实） |
| 0133279bcba1 / 3 | technical-9197e7fc3bc56b69 | domain_mismatch（新闻） |
| 0133279bcba1 / 4 | technical-b56c7d19841b4d92, technical-32e294390bfd0bd6 | partial_only, incomplete_completeness（板块排名/涨跌） |
| 0133279bcba1 / 5 | technical-664d06090e32466d, technical-1993e725928b5a44 | partial_only, incomplete_completeness（板块排名/涨跌） |
| 72b7eded0510 / 0 | technical-79c151bdee8d9fa3 | domain_mismatch |
| 72b7eded0510 / 1 | technical-128cb9c63c1316a8, technical-ee7ad4133902a484 | domain_mismatch |
| 72b7eded0510 / 2 | technical-128cb9c63c1316a8 | domain_mismatch |
| 72b7eded0510 / 3 | technical-72c733461f33c2f6, technical-a657398bdccb0010 | partial_only |
| 72b7eded0510 / 4 | technical-52e8f990d5203c91, technical-9d30b38074194f4a | partial_only |
| 72b7eded0510 / 5 | technical-8bf3e0d905fb4cf8, technical-4876c853570be7b6 | partial_only |
| 97bb904c9698 / 0 | technical-e38ad5bede691d2b, technical-048decff1632ca64 | domain_mismatch |
| 97bb904c9698 / 1 | technical-db65a5fe5f4ca53f, technical-f86f3d2158932003 | domain_mismatch |
| 97bb904c9698 / 2 | technical-8b81ba4cf381e375 | domain_mismatch |
| 97bb904c9698 / 3 | technical-6564f7f60b306183 | domain_mismatch |
| 97bb904c9698 / 4 | technical-5752bdb8adab8aab, technical-ee3d352c3f465f1a | stale_date, partial_only, unmatched_instrument |

严格匹配通过的指数事实、无据/不合域 claim、独立工具失败和市场覆盖缺口分别记录，未修改 P15 verified/degraded 策略。窄行情仍需上游返回完整且口径明确的指定日结果；全市场仍需同交易日指数、实际市场广度及可核验板块覆盖；公告仍需真实匹配的标的及公告日期。本轮没有证据支持继续改解析器、伪造日期/标的或重跑认证探针，故在以上范围形成验收结论，不自动扩展新编号阶段。

## 验证与保留性

- 聚焦输出契约与既有门禁回归：220 passed；P16 专项 24 passed，包含无引用/伪造引用拒绝、result_partial 最后持久化防线、真实账本 hydration、length 拒绝、跨 run 数量隔离、休市说明排除、离线网络禁止、cleanup 警告与业务异常保留。
- 最终完整 pytest：1619 passed，1 条 Starlette/AnyIO 第三方弃用 warning，见 `memory/p16-pytest-complete.log`。完整测试使用独立 `.tmp/p16-final-verification/memory.db`。
- Ruff、compileall、git diff --check 通过；全仓 Ruff 扫描会提示既有不可读历史目录，限定 `app scripts tests` 检查通过。
- P9/P10 显式 replay 通过，只核对其历史 artifact；P9 历史为 not_observed，不算本轮真实通过。P15 显式 replay 的 7 个离线场景通过，新 artifact 为 `.tmp/p16-p15-replay-final/acceptance.jsonl`，不覆盖历史 P15 记录。
- 最终 P16 replay 在禁用 DNS/socket 连接下核对 16 条 real_network 观察：live_market_verified=0、pure_structure_rows=0，同状态 sync/SSE 审计字段 schema 一致，非 verified 零写入，唯一终态和最终 cleanup 检查通过。replay 没有重新请求服务，也没有把离线校验改标为真实通过，见 `memory/p16-replay-complete.log`。
- 起始快照保护 8372 个既有数据库、evening、JSONL、日志等文件；最终核对无缺失、无覆盖。真实 MCP 追加了既有 `.tmp/iiix-appdata/iiix/logs/iiix.log`，原 137253 字节前缀 SHA-256 校验完整。8 个起始不可读目录在快照中显式记录，未声称检查其内部。新临时库和历史 artifact 均保留。
- 本阶段真实验收期间未执行 reset、revert、commit 或 push；后续 Git 发布依用户单独授权。未记录凭据。新增记录无 pending cleanup；本地修复没有抹掉来源、日期、coverage、authority、partial/result_partial 或权威冲突约束。

## 复现命令

```powershell
cd D:\Mosaic
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe scripts/p16_acceptance.py --mode live
.venv\Scripts\python.exe scripts/p16_acceptance.py --mode live --scenario quote --label provenance-recheck
.venv\Scripts\python.exe scripts/p16_acceptance.py --mode live --scenario today --label market-filter-recheck
.venv\Scripts\python.exe scripts/p16_acceptance.py --mode live --scenario today --scenario closed_day --label reference-contract-recheck
```

这些 live 命令会请求真实服务并创建新的独立临时库；复现并非无限重试建议。

明确离线入口：

```powershell
.venv\Scripts\python.exe scripts/p9_e2e_acceptance.py --mode replay
.venv\Scripts\python.exe scripts/p10_e2e_acceptance.py --mode replay
.venv\Scripts\python.exe scripts/p15_acceptance.py --mode replay --artifact .tmp/p16-p15-replay/acceptance.jsonl
.venv\Scripts\python.exe scripts/p16_acceptance.py --mode replay
```

P12/P13/P14 没有 replay 模式，本阶段只读已有 artifact 并运行相关回归测试，未向这些脚本传入 replay 参数。历史核对记录在 `memory/p16-historical-inspection.jsonl`。
