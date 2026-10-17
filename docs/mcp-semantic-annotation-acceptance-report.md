# MCP 语义补标注 —— 离线验收报告

对应执行单：`docs/mcp-semantic-annotation-execution-plan.md`
执行目录：`D:\Mosaic`（Windows + PowerShell + `.venv`）
验收方式：**仅离线 fixture**（无 live MCP / THS / 问财调用、未写 `memory/`、未执行 commit/push/reset/revert）。

---

## 1. 结论

- `get_limit_up_count`、`list_limit_up_sectors` 两个 MCP 工具在 MCP 已返回业务结果时，由新增语义适配器 `app/gateway/mcp_semantics.py` 按工具契约补标准 metric、观察日期、来源/authority 标记与逐行日期 lineage，**有契约依据的同一快照批次日期（envelope）可传播到全批**，从而通过 provenance 与 strict evidence audit。
- 日期无法证明（行与信封都没有日期、非法日期、混合快照日期）、缺 provider/authority、缺板块身份/结构、任一业务行 partial 时，**继续拒绝**，零 datum，不把 `retrieved_at`/系统日期当交易日。
- 验收命令全绿：8 个验收文件 **210 passed**；`ruff check app tests` **All checks passed**；`compileall -q app` **exit 0**；`git diff --check` **exit 0**（仅有既有的 LF→CRLF 提示）。
- 全量回归（`pytest -q`）：**1766 passed**；唯一失败为 `test_p9_e2e_acceptance.py::test_sync_blocked_response_has_same_terminal_code_as_sse`，经证实是**与本次改动无关的既有环境性 flaky 用例**（见 §6）。

---

## 2. 修改文件清单

### 新增
- `app/gateway/mcp_semantics.py` —— 纯函数语义适配器：`annotate_mcp_payload` / `semantic_annotation_diagnostics`；深拷贝输入、不改 `raw`；count 非负整数校验与单条 `limit_up_count` 归一、sectors 逐行身份/结构/日期解析 + envelope 批次 lineage。
- `tests/test_mcp_semantic_annotation.py` —— 覆盖 §7.1–§7.23 全部 23 条断言（参数化后 38 个用例）。

### 修改（应用代码）
- `app/models/market.py` —— `NormalizedDatum` 增加 `semantic_annotation / date_annotation / annotation_contract_id / original_field_path`；`ToolResult` 增加 `date_annotation / business_datum_count / structural_metadata_count / semantic_diagnostics / fallback_provenance_failures / evidence_created`。
- `app/gateway/tool_registry.py` —— `_MCP_OPERATION_ALIASES`、`_MCP_SEMANTIC_CONTRACTS`（契约 id `mcp.get_limit_up_count.v1` / `mcp.list_limit_up_sectors.v1`，均 `date_from_request=False`，sectors `batch_date=True`）、`canonical_mcp_operation()`、`get_semantic_contract()`；补全板块身份字段（plate/plate_name/行业板块/概念板块/题材板块等）。
- `app/gateway/normalizer.py` —— 有契约走适配器分支（跳过通用 flatten 与 row_identity 传播）；空业务判 `empty_normalized` 并挂 sidecar；最终 ToolResult 挂日期/计数/诊断/evidence；支持 `requested_as_of_date`（仅透传，不进 wire）。
- `app/gateway/mcp_client.py` —— `McpClient.call` 增加 `requested_as_of_date` 透传。
- `app/graph/tool_runtime.py` —— 新增 `_research_as_of_date(arguments)`，`_call_gateway` 用 `inspect.signature` 探测并透传 `requested_as_of_date`（不改变 wire args/去重签名）。
- `app/gateway/controlled_fallback.py` —— 拆出 `_raw_rows_own_dates`，`_rows_have_own_dates` 优先读受信 `row_date_lineage`；新增 `_sector_field_gaps`（`fallback_missing_sector_identity` / `fallback_missing_sector_structure`，保留汇总码 `fallback_missing_required_fields`）；行批能力任一业务行 partial 即整批拒绝；拒绝时把逐项码写入 `fallback_result.fallback_provenance_failures`；对齐 fallback 门禁与契约的板块身份/结构字段集（含 `plate`/`plate_name`）。
- `app/graph/run_log.py` —— 执行行增加 `business_datum_count / structural_metadata_count / result_partial / evidence_created / fallback_provenance_failures / date_annotation / date_annotations / authority_set / semantic_annotation_counts`（固定 schema 分支保留全部键）。
- `app/agent/evidence_gate.py` —— `is_usable_datum` 排除 structural；`run_evidence_gate` 结构元数据不计入 invalid；语义契约结果（`result.date_annotation` 存在且 market scope）下无日期/partial 业务 datum 追加硬码 `missing_as_of_date` / `partial_datum`。
- `app/research/evidence.py` —— `build_evidence` 排除 structural，并对语义契约结果跳过 partial/无 as_of_date 的业务 datum。

### 修改（测试）
- `tests/test_mcp_fallback_remediation.py` —— count metric 断言（期望唯一 `limit_up_count`、value=59、`original_field_path="count"`、`date_annotation=observed`）。
- `tests/test_live_report_delivery_followup.py` —— 旧"信封日拒绝"测试改写为"信封日经可信 lineage 采纳" + "真正无日期拒绝"。
- `tests/test_incident_ashare_stream_fix.py` —— 两处 retry/refresh 测试的最小合成载荷补观察日期与板块结构字段（其本意是重试/刷新，与日期溯源无关；新契约要求快照可证明观察日）。

---

## 3. 测试清单与通过数

### 3.1 验收命令（§8）结果

| 文件 | 结果 |
|---|---|
| tests/test_mcp_semantic_annotation.py | **38 passed**（覆盖 §7.1–§7.23，共 23 条断言） |
| tests/test_mcp_fallback_remediation.py | **77 passed** |
| tests/test_live_report_delivery_followup.py | **20 passed** |
| tests/test_p7_source_routing.py | **8 passed** |
| tests/test_p8_source_routing.py | **5 passed** |
| tests/test_p13_evidence_delivery.py | **26 passed** |
| tests/test_evidence_gate.py | **9 passed** |
| tests/test_evidence_authenticity.py | **27 passed** |
| **合计（8 文件）** | **210 passed** |

- `ruff check app tests` → **All checks passed!**
- `python -m compileall -q app` → **exit 0**
- `git diff --check` → **exit 0**（仅有仓库既有的 LF→CRLF 换行提示，非空白错误）
- 既有 warning：1 个 `starlette/testclient.py` 的 `anyio.abc.BlockingPortal DeprecationWarning`，改动前即存在。

### 3.2 全量回归

- `pytest -q`（排除 §6 所述 flaky 用例）：**1766 passed, 1 deselected**。
- 不排除时：**1766 passed, 1 failed**（唯一失败为 §6 环境性 flaky 用例）。

---

## 4. limit-up count fixture（成功 + 拒绝）

### 4.1 成功 fixture（原始字段日期 observed）

输入：`{"source_used":"ashare-master","count":59,"date":"2026-10-15"}`（经 `normalize_tool_result("get_limit_up_count", ...)`）

| 字段 | 值 |
|---|---|
| source | `ashare-master` |
| authority | `ashare-master` |
| date_annotation | `observed`（sidecar 级）；业务 datum `date_annotation=observed` |
| as_of_date | `2026-10-15` |
| business_datum_count | **1**（唯一 metric `limit_up_count`，value=59，`original_field_path="count"`，`semantic_annotation="contract"`） |
| structural_metadata_count | 1（`date` 元数据，不计入业务/证据） |
| result_partial（partial） | **False** |
| evidence_created | **True** |
| fallback_provenance_failures | （不经过 fallback） |
| source_switch | （不经过 fallback） |

日期来源：**原始字段（observed）**——`date` 由 payload 自带，非抓取时间、非请求上下文。

### 4.2 无日期 fallback 被拒 fixture

输入：`{"data":{"count":"59"},"source":"ashare-master"}`（primary 为 timeout error，经 router 走 MCP fallback）

| 字段 | 值 |
|---|---|
| 最终 status | `error`（零 datum） |
| business_datum_count / structural_metadata_count | 0 / 0 |
| evidence_created | **False** |
| fallback_provenance_failures | `fallback_missing_date`, `fallback_partial_datum`, `fallback_missing_required_fields`（另保留兼容汇总码 `fallback_provenance_insufficient`） |
| 最终 source_switch | `True`（现有约定：primary error 且 fallback 被拒时返回零 datum error，route 标记切换；**关键不变式是 fallback 业务数据未被采纳、normalized=[]**） |

说明：`retrieved_at` 虽由 normalizer 写入，但**不充当**观察日；无任何可证明的交易日时整批拒绝。

### 4.3 其他 count 边界（离线验证）

- `count=0`（含 `"data":{"count":"0"}`）：合法业务 datum，value=0；有日期时 success/evidence=True。
- `count` 为 null / 空串 / 非数字 / 未知：`status=error`、business=0、evidence=False，诊断 `mcp_missing_business_value`（非空非法值另有 `mcp_invalid_count:<path>`）。
- 负数 / 小数 / bool / NaN：`status=error`、business=0、evidence=False（bool 不被当作 1）。
- `count=59` 与 `data.count=60` 别名冲突：`mcp_count_alias_conflict` + `mcp_missing_business_value`，business=0。
- 非法日期（`"not-a-date"`）：`mcp_invalid_date` + `mcp_missing_date`，status=partial、evidence=False。
- 真实契约 `date_from_request=False`：仅给 planned/requested 日期时诊断 `mcp_request_date_ignored` + `mcp_missing_date`，**不补日期**（request_context 分支仅在 monkeypatch 翻为 `date_from_request=True` 时启用，对应 §7.2）。
- 其他工具/未知键名的顶层 `count` 不映射为 `limit_up_count`；`data.涨停家数` 等非契约命名指标保留为 payload 业务指标（供异常检测），不被重命名伪造契约家数。

---

## 5. limit-up sectors fixture（成功 + 失败 + 逐行 lineage）

### 5.1 成功 fixture（envelope 日期传播，fallback 被采纳）

输入（primary timeout error，MCP fallback 候选）：
```json
{"source":"ashare-master","date":"2026-10-14",
 "list":[{"plate":"机器人","涨停家数":12},{"plate":"固态电池","涨停家数":8}]}
```

| 字段 | 值 |
|---|---|
| source / authority | `ashare-master` / `ashare-master` |
| date_annotation（sidecar） | `envelope` |
| as_of_date | `2026-10-14` |
| business_datum_count | **4**（2 行 × 身份/结构 2 个字段） |
| structural_metadata_count | 1（envelope `date`） |
| result_partial（partial） | **False** |
| evidence_created | **True** |
| fallback_provenance_failures | `[]` |
| 最终 source_switch | **True**（fallback 被采纳，`datum_count>0`） |

逐行日期 lineage（trusted）：
```
list[0] {date: 2026-10-14, date_annotation: envelope, trusted: true, inherited_from: date}
list[1] {date: 2026-10-14, date_annotation: envelope, trusted: true, inherited_from: date}
```

日期来源：**envelope 传播**——契约保证同一快照批次（`batch_date=True`），envelope 日期经契约验证后传播给缺自有日期的行；该行随后被 strict claim audit 接受为窄事实（示例断言："2026-10-14汽车零部件板块触及涨幅上限的个股数量为12只"，`strict_provenance=True` 下 `supported=True`）。

### 5.2 失败 fixture（真实精简 payload，无日期/无数值结构）

输入（只读 sqlite 确认的真实形状，无日期、无数值家数）：
```json
{"data":{"list":[{"code":"603917","concept":"小米汽车、汽车零部件","plate":"小米汽车"}]},
 "source":"ashare-master","type":"zt_plate"}
```

| 字段 | 值 |
|---|---|
| 最终 status | `error`（零 datum） |
| business_datum_count / structural_metadata_count | 0 / 0 |
| evidence_created | **False** |
| 逐行语义诊断 | 行 partial：缺日期、缺数值结构（`mcp_row_missing_structure` / `mcp_missing_date`）；`plate` 提供身份，**不**误报缺身份 |
| fallback_provenance_failures | `fallback_missing_date`, `fallback_partial_datum`, `fallback_missing_sector_structure`, `fallback_missing_required_fields`（+ 兼容汇总码 `fallback_provenance_insufficient`） |
| 最终 source_switch | `True`（同 §4.2：零 datum，fallback 数据未采纳） |

### 5.3 混合完整 / partial 行 fixture

输入：`{"source":"ashare-master","date":"2026-10-14","rows":[{"板块":"机器人","涨停家数":12},{"板块":"固态电池"}]}`

- 完整行（机器人）datum.partial=**False**；缺结构行（固态电池）datum.partial=**True**；结果级 `partial=True`。
- 按 §6.8「任一业务 datum partial 即整批拒绝」，**整批不采纳**：最终 `status=error`、零 datum、`fallback_provenance_failures=['fallback_partial_datum']`（+ `fallback_provenance_insufficient`）。

### 5.4 其他 sectors 边界

- 空容器 `{"code_count":0,"datas":[]}`：`status=error`、reason=`empty_normalized`、business=0、evidence=False。
- 混合行日期（两行不同 `date`）：`mcp_date_conflict`，status=partial、evidence=False，整批行 partial 不采纳。
- 信封日与单行自有日不同：**行级 observed 日期优先、不被覆盖**（如 envelope 2026-10-14、行 2026-10-13 时以 2026-10-13 为准），保留 `mcp_date_conflict` 诊断；行日期与请求日不同由 `fallback_date_mismatch` 拦截。
- 只有 transport 名（`transport:"mcp"`）：authority=None，`fallback_missing_authority`/`fallback_missing_source`；已核实的 `declared_provider="ashare-master"` 可补齐 authority 并通过。
- 跨日缓存：`market_cache` 以 `model_copy(deep=True)` 存整对象，命中返回同一对象、不重贴日期，保留原观察日与逐行 lineage。

---

## 6. 全量回归中唯一失败用例的定性（非本次改动导致）

- 用例：`tests/test_p9_e2e_acceptance.py::test_sync_blocked_response_has_same_terminal_code_as_sse`。
- 失败现象：`POST /api/ask` 返回 502，日志在 **plan/reasoning LLM 阶段** 抛 `AuthenticationError`（`model=qwen3.7-flash`），未进入任何工具/归一化/证据链路。
- 根因（已复核）：该测试用 monkeypatch 注入 mock `Orchestrator`，但 FastAPI `lifespan` 启动时 `app/main.py:46` 无条件 `_orchestrator = Orchestrator(_settings)` 覆盖了 mock，于是请求实际跑了真实图；当外部 LLM 端点偶发返回认证错误时即 502。`app/main.py`、`app/agent/orchestrator.py` 本次均**未改动**（`git diff --stat` 为空）。
- 复现证据：该用例在代码不变的情况下连续重跑，**run 2 / run 3 均 PASS、仅偶发一次 FAIL**，属外部 LLM 认证的 flaky 行为，与语义补标注无关。
- 处理：该文件其余 17 个用例全部通过；未改动 main.py / 测试基础设施（超出本执行单范围）。

---

## 7. §9 禁止事项自检

- 未把 `mcp` / `http_api` / `ths_skill` / 同花顺问财作为 authority（仅 transport/provider 标签；authority 来自 provider 白名单）。
- 未把 `retrieved_at` 或系统当前日期当交易日；无日期即拒绝（`fallback_missing_date`）。
- 未把所有顶层 `count` 当涨停家数：仅 `get_limit_up_count` 契约允许的 `data.count`/`count` 归一为 `limit_up_count`；其余工具的顶层 count 保持结构元数据。
- 未把板块行改名为市场涨跌家数或完整热点结论。
- 未通过把 partial 改 success 绕过审计；未放宽 generic evidence-only 完整性要求。
- 仅离线 fixture；未改 `memory/`、`.tmp/`、日志、历史 artifact；未执行 commit/push/reset/revert。
- 输入不可变：适配器深拷贝输入，原始 payload 与嵌套行未被修改（§7.19 断言）；幂等（同输入两次结果一致）。

---

## 8. 日期来源结论（本两工具）

- **get_limit_up_count / list_limit_up_sectors 的真实契约均 `date_from_request=False`**，allowed_openapi 无 parameters（"当日最新快照"）：
  - 成功路径只接受两类日期：**原始字段（observed，行级或顶层 date）** 与 **envelope 传播（sectors 经契约验证的同一快照批次日期）**。
  - **不启用 request_context 日期推导**：planner 的研究日期/`requested_as_of_date` 不补观察日（诊断 `mcp_request_date_ignored`）。
  - `retrieved_at` 仅为抓取时间，永不作为交易日。
- 契约依据缺口：真实 MCP 响应不承诺历史日期参数，故历史日期请求不会被"最新快照"满足；若未来确认存在日期参数，需把契约 `date_from_request` 翻为 True 并让日期随 wire 发送后，request_context 分支才会启用（§7.2 已用 monkeypatch 演示该分支，生产配置保持关闭）。

## 9. 后验边界修正

验收复核发现：当 `get_limit_up_count` 同时返回 `count` 与 `data.count` 且两个值相等时，适配器原先会把两个别名都输出为 `limit_up_count` 业务 datum，与“唯一标准指标”的契约不一致。现已修正为只采纳优先级最高的一个路径，其余别名保留为结构元数据；值冲突仍返回 `mcp_count_alias_conflict` 并拒绝业务值。

新增回归 `test_7_1_equal_count_aliases_still_produce_one_business_datum`。修正后八文件验收集合为 **211 passed**（1 个既有 Starlette deprecation warning），语义测试由 38 个增加为 39 个；针对新增文件的 Ruff、`compileall` 与 `git diff --check` 均通过。
