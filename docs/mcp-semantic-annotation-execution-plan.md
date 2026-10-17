# MCP 市场数据语义补标注执行单

状态：交给子 agent 执行

目标：修复 `get_limit_up_count` 和 `list_limit_up_sectors` 的 MCP fallback。MCP 已经返回了业务结果时，由 MCP 适配层依据工具契约补齐标准字段、观察日期和来源标记，使结果能够通过现有 provenance 与 evidence audit；无法从契约或请求上下文确认的日期继续拒绝，不能用抓取时间猜测交易日期。

## 1. 现象与完成定义

当前 live 日志中出现两种情况：

- `get_limit_up_count` 的候选结果未通过日期/必需字段检查。`{ "count": 59 }` 是本次讨论的映射示例，不是已保存并核实的本轮原始响应；应先确认实际 payload 形状。
- `list_limit_up_sectors` 返回数百条板块行，但日期可能只在响应 envelope 中，或者没有被传播到每一行；结果因此带有 `fallback_provenance_insufficient`，最终 `datum_count=0`。

完成后应满足：

1. `get_limit_up_count` 的工具契约明确声明顶层 `count` 是涨停家数时，归一化为带标准 metric 和日期的业务 datum。
2. `list_limit_up_sectors` 的工具契约或 envelope 声明日期时，把同一可信日期传播到每一条业务板块行，并保留日期来源标记。
3. `source`、`authority`、`retrieved_at`、`as_of_date` 仍然分别表达不同含义；`mcp` 只能是 transport，不能充当 authority。
4. 没有明确观察日、provider 或业务字段时，结果仍被拒绝，并在日志中给出逐项原因。
5. 不把结构元数据 `count/code_count/total` 自动当作业务数值；只有特定工具契约允许的 `count` 才能映射为 `limit_up_count`。
6. MCP fallback 被采纳时，最终结果使用 MCP 业务 datum，`source_switch=true` 且 `datum_count>0`；被拒绝时保留现有零 datum/主源不变式。

## 2. 实现边界与数据模型

### 2.0 先确认工具契约，再启用映射

先查仓库已有实现、安装的 Gateway/开源依赖源码、`allowed_openapi.json` 和已有脱敏 fixture，复用 MCP SDK、Pydantic 与现有日期解析逻辑，不引入新框架。记录两个工具的实现或文档路径、版本、输出字段含义、日期语义、缓存策略，以及是否真的接受历史日期。工具名称、inputSchema 和 planner 的研究日期本身不能证明响应日期。若本地资料不能证明日期语义，仍完成字段映射和原始/envelope 日期支持，禁用请求日期补齐，并在交付中列出缺失的契约依据。

本文替代旧修复单中“板块必须在原始每行重复携带日期”的限制：经契约验证的批次观察日可以适用于全批行。旧约束中禁止用抓取时间冒充观察日、禁止覆盖真实冲突日期的要求继续有效。

### 2.1 新增 MCP 语义适配器

新增 `app/gateway/mcp_semantics.py`，只处理已知 MCP 工具的语义映射，不处理通用 JSON 猜测。提供以下纯函数，名称可以微调但职责必须保留：

```python
annotate_mcp_payload(
    tool_name: str,
    arguments: dict[str, Any],
    payload: Any,
    *,
    declared_provider: str | None = None,
    requested_as_of_date: str | None = None,
) -> Any
```

```python
semantic_annotation_diagnostics(
    tool_name: str,
    payload: Any,
    arguments: dict[str, Any],
) -> dict[str, Any]
```

适配器必须保证输入对象和嵌套行不被修改。返回独立 normalized view 和 annotation sidecar，或等价的类型化结构；`ToolResult.raw` 保留原始响应。不能只浅复制 envelope 后原地修改共享的嵌套行。输出增加标准字段或内部 metadata：

- `metric`：标准指标名，例如 `limit_up_count`；
- `value`：数值；
- `date/as_of_date`：观察日期；
- `source`：响应声明的 provider；
- `authority`：provider 白名单归一结果；
- `semantic_annotation`：`contract`、`payload`、`request_context` 三者之一；
- `date_annotation`：`observed`、`envelope`、`request_context` 或经核实的 `contract_snapshot`；
- `annotation_contract_id`、`original_field_path`：记录映射契约及原始字段路径，不使用固定置信度值作为放行依据。

如果项目已有 `NormalizedDatum` 承载这些字段，优先扩展模型以保存 `semantic_annotation/date_annotation`；如果不需要对外暴露，也可以放在 normalizer 内部 metadata，但日志必须能看到诊断结果。不得把注释元数据写进用户报告正文。

### 2.2 日期来源规则

日期按以下优先级解析：

1. 业务行或业务字段自身的 `date/trade_date/trading_date/as_of_date/日期/交易日`；标记 `date_annotation=observed`。
2. MCP envelope 的明确数据日期字段，例如 `data_date/observation_date/as_of_date/trade_date`；标记 `date_annotation=envelope`。
3. 请求参数中的 `as_of_date/date/trade_date`，且该工具契约声明请求日期就是数据观察日；标记 `date_annotation=request_context`。
4. 以上都没有时，不得使用 `retrieved_at`、系统当前日期或自然语言 note 补日期；保持 fallback 拒绝。

`planned_as_of_date` 表示研究目标，必须以独立调用上下文传入，不得伪装为 MCP 不支持的 wire 参数。只有实际向上游发送了日期，且已确认接口按该日查询和缓存隔离时，才能启用第 3 类来源。当前代码会剥除这两个工具的 date 参数，因此不能仅增加一个 `date_from_request=true` 就认为上游遵守该日期。

如果上游只有“最新快照”接口，可另定义经过源码/文档证明的 `contract_snapshot` 日期策略：由交易所日历、Asia/Shanghai 时区、开收盘边界和上游快照刷新/缓存规则确定实际快照交易日，记录该策略依据。周末、节假日、盘前和跨日缓存都必须有测试；任何条件不明确就保持日期未知。历史请求不得把最新快照重新贴成历史日期。

有效的行级日期永远优先，不覆盖不同日期；已提供但无法解析的日期要报错，不能静默换成请求日期。批次日期只适用于契约保证同一快照的行。添加日期要在完整性计算之前完成，不得在最终结果上无条件清除 partial。语义标注字段不能被扁平化成新的业务 datum。

### 2.3 provider 与 authority 规则

- `transport=mcp` 只写入 transport 字段。
- provider 优先取 raw root 的 `source/source_used/provider`，再取约定 envelope 的 `metadata.provider`、`meta.source`、`result.source_used`。
- `authority` 只允许来自现有 provider 白名单，例如 `tencent`、`ashare-master`、`eastmoney`、`xueqiu`、`sina`、`cninfo` 等。
- `同花顺问财`、`mcp`、`http_api`、`ths_skill` 不得写成 MCP authority。
- 响应没有 provider 时，复用现有 `declared_provider_for_operation` 及已核实的 provider 契约；响应与契约冲突时保留冲突并拒绝覆盖。响应和契约都不能确定 provider 时，诊断包含 `fallback_missing_authority`。

## 3. 工具契约改动

修改 `app/gateway/tool_registry.py`，为 MCP 可调用工具增加显式 semantic contract。可采用现有 registry 数据结构新增字段，示意如下：

```python
MCP_SEMANTIC_CONTRACTS = {
    "get_limit_up_count": {
        "capability": "limit_up_count",
        "top_level_count_metric": "limit_up_count",
        "count_is_business_value": True,
        "date_from_request": False,  # 核实支持日期参数及缓存语义后才能启用
        "required_value_fields": ("limit_up_count", "涨停家数", "涨停数"),
    },
    "list_limit_up_sectors": {
        "capability": "limit_up_sectors",
        "row_identity_fields": ("sector", "industry", "concept", "theme", "板块", "行业", "概念", "题材"),
        "row_structure_fields": ("count", "limit_up_count", "涨停家数", "涨停数", "rank", "排名", "pct", "涨幅"),
        "date_from_request": False,  # 无参数最新快照不能直接使用研究日期
    },
}
```

具体字段名应与当前 registry 和 MCP schema 对齐。`count_is_business_value` 只允许在 `get_limit_up_count` 这一个契约中生效；不得做成“所有顶层 count 都是业务字段”。

## 4. `get_limit_up_count` 实现要求

修改 `app/gateway/mcp_client.py`、`app/gateway/normalizer.py`：

1. MCP JSON 保留完整 root/nested envelope，不能只提取 `datas/items`。
2. 在 MCP payload 进入通用 normalizer 前调用 `annotate_mcp_payload`。
3. 对契约允许的 payload 形状：

   ```json
   {"source_used":"ashare-master", "date":"2026-10-15", "count":59}
   ```

   归一为等价业务事实：

   ```json
   {
     "metric":"limit_up_count",
     "value":59,
     "as_of_date":"2026-10-15",
     "source":"ashare-master",
     "authority":"ashare-master",
     "semantic_annotation":"contract",
     "date_annotation":"observed"
   }
   ```

4. 若 payload 没有日期，严格按 2.2 的实际传输与契约条件补齐日期；仅有本地研究日期不能补齐。允许的补齐必须记录契约与日期依据。
5. 若 `count` 同时出现在业务 `items/rows` 内，按行字段处理；只有 envelope 顶层 `count` 才使用 `top_level_count_metric` 映射。
6. 家数必须是有限非负整数或可无歧义解析为非负整数的字符串，拒绝 bool、负数、小数、NaN、Infinity、null、空字符串和“未知”。`count=0` 是有效业务事实，不能当空结果。canonical `limit_up_count` 与原始 `count` 不得重复生成两个业务 datum；多个别名值冲突时显式报错，不得任选一个。
7. 保留原始 count 诊断，增加 `structural_metadata_count` 与 `business_datum_count`，方便审计区分。

## 5. `list_limit_up_sectors` 实现要求

修改 `app/gateway/mcp_client.py`、`app/gateway/normalizer.py`、必要时 `app/models/market.py`：

1. 识别 `rows/items/datas/data/results` 中的板块业务行。
2. 从每行自身或 envelope 读取日期；若两者都没有但请求带日期且契约允许，使用 request context 日期。
3. 对每一行传播日期，但只传播到该行的 datum，不能把一行日期传播到其他行。
4. 每一行至少要有：
   - 一个身份字段：板块、行业、概念、题材、sector、industry、concept、theme；
   - 一个数值结构字段：涨停家数、涨停数、count、rank、排名、涨幅或 pct；
   - 可审计的日期、source、authority、retrieved_at。
5. 行自身缺身份或结构字段时，将该行标记 `partial=true`，但不要让其他完整行全部变成 partial。
6. 顶层 `count/code_count/total` 保留在 normalized 结果中，标记 `structural_metadata=true`，不参与业务行 provenance；如果 payload 只有这些字段，结果必须是零业务 datum 且不生成 evidence。
7. 对空业务容器 `datas=[]`，保持 `empty_normalized`，不能仅因请求上下文有日期而伪造板块行。`code_count=0` 与非空 rows 同时出现时记录不一致，不能只看 count 就丢弃真实行。

## 6. fallback 与审计改动

修改 `app/gateway/controlled_fallback.py`、`app/graph/tool_runtime.py`、`app/agent/evidence_gate.py`：

1. `_has_provenance` 和 `fallback_satisfies_provenance` 继续只接受业务 datum；结构元数据不阻塞真实业务行。
2. 对 `limit_up_count` 接受契约映射后的 `metric=limit_up_count`，但仍要求日期、authority、source、retrieved_at、非 partial 和数值。
3. 当前 `_rows_have_own_dates` 直接检查 raw 行，会拒绝合法的批次日期继承。改为检查原始行或与该行绑定、经受信适配器生成的日期 lineage；不得修改 raw 来绕过检查，也不得信任 payload 自己写的 annotation 标记。raw 没有可识别列表不等于自动通过，仍须验证 normalized 业务 datum。身份/结构字段按同一行检查，并明确“至少一条完整业务行”只支持窄事实，不证明完整板块排名。
4. `fallback_provenance_failures` 必须把以下诊断写入 result 和 attempt log：

   - `fallback_missing_source`
   - `fallback_missing_authority`
   - `fallback_missing_retrieved_at`
   - `fallback_missing_date`
   - `fallback_date_mismatch`
   - `fallback_missing_required_fields`
   - `fallback_structural_metadata_only`
   - `fallback_partial_datum`

5. 新增诊断字段：`date_annotations`、`authority_set`、`business_datum_count`、`structural_metadata_count`、`semantic_annotation_counts`。日志只记录集合和计数，不记录完整 MCP payload。
6. `evidence_created` 只在至少一条业务 datum 通过完整性、日期和来源检查时为 true。
7. semantic annotation 不能放宽 generic evidence-only fallback；它只是把工具契约已经知道的事实转换为标准证据。

8. 当前 fallback 会因为任一业务 datum.partial 拒绝整个候选。这次保持此采纳规则；逐行 partial 分离不等于整批自动放行。若混合完整/不完整行，报告具体拒绝原因。部分行采纳需要独立设计，不纳入本执行单。
9. 复用 `app/graph/run_log.py` 的显式日志白名单，确保新增诊断没有再次进入 `_dropped_arg_keys`。`fallback_missing_required_fields` 目前也会因缺日期返回，应把缺日期、缺身份、缺数值的独立检查拆开，日志给出真实缺口，保留兼容汇总码。
10. 同步核查 `app/gateway/arguments.py`、缓存读写和 ToolResult/evidence 序列化。日期上下文不能改变无参数工具的 wire arguments 或去重签名；缓存保留原始观察日和 lineage，不得在下一天命中时重新贴日期。若缓存格式变更，用版本区分，不删除既有缓存/历史 artifact。

## 7. 测试任务

新增或扩展 `tests/test_mcp_semantic_annotation.py` 和 `tests/test_mcp_fallback_remediation.py`，全部使用本地 dict，不调用真实网络、THS、MCP 或 live acceptance。

至少覆盖以下断言：

1. `get_limit_up_count` 顶层 `{count: 59, date: D, source_used: provider}` 归一为一条 `limit_up_count` 业务 datum。
2. `get_limit_up_count` 顶层 `{count: 59}` 在带契约允许的 `as_of_date=D` 请求下补齐 `date_annotation=request_context`。
3. 没有请求日期、没有 payload 日期时，仍返回 `fallback_missing_date`。
4. 其他工具同级有列表容器的顶层 count 保持结构元数据；未知工具的孤立 count 不自动映射为涨停家数，也不改变原有通用规则。
5. `count: null`、空字符串和非数字值不生成 evidence。
6. `list_limit_up_sectors` envelope 日期传播到每个 rows datum，并保留每行板块身份与结构字段。
7. 每行日期缺失且没有可信 envelope/request 日期时，返回 `fallback_missing_date`。
8. 只有板块名称没有数值结构字段时，返回 `fallback_missing_required_fields`。
9. 一行 partial 不会把其他完整行全部变成 partial。
10. `datas=[]` 且只有结构元数据时业务 datum 数为 0、`evidence_created=false`；单独验证 count 工具的 `count=0` 可用。
11. root provider、`metadata.provider`、`result.source_used` 都能产生 authority。
12. 只有 `transport=mcp` 且不存在可核实的 declared provider 契约时返回 `fallback_missing_authority`；有合法 declared provider 时按既有契约补齐。
13. `retrieved_at` 存在但没有 observation date 时仍拒绝，不允许用抓取时间替代交易日期。
14. 合格 MCP fallback 被采纳时 `source_switch=true` 且最终 `datum_count>0`。
15. 不合格 fallback 时保留 `fallback_provenance_insufficient` 兼容码和逐项失败码。
16. strict evidence audit 接受契约补齐的完整窄事实，但仍拒绝 `partial` datum 和无日期 datum。
17. 无日期 wire 参数的工具仅收到 `planned_as_of_date` 时不得补日期；模拟真正支持日期的契约与实际传输路径，验证 request_context 分支。
18. 原始行日期与请求日不同、非法日期、混合日期、历史请求撞最新快照均不被覆盖或错误通过。
19. 输入原始 dict/嵌套 rows 不变，重复适配幂等；标注元数据不会变成额外证据。
20. 数量 0、负数、小数、bool、NaN、别名冲突按第 4 节处理。
21. envelope 日期继承的板块结果经过真实 MCP 边界、normalizer、fallback、ledger 和 strict claim audit 后通过；不是仅单测 helper。
22. 完整/partial 行混合时不意外放行整批，日志保留逐项诊断且不被白名单裁剪。
23. 跨日缓存命中保留原观察日，日期错配不能被重新标注掩盖。

## 8. 验收命令

在 `D:\Mosaic` 执行：

```powershell
$env:RUFF_CACHE_DIR="$env:TEMP\mosaic-ruff-cache"
$env:PYTHONPYCACHEPREFIX="$env:TEMP\mosaic-pycache"

.venv\Scripts\python.exe -m pytest `
  tests/test_mcp_semantic_annotation.py `
  tests/test_mcp_fallback_remediation.py `
  tests/test_live_report_delivery_followup.py `
  tests/test_p7_source_routing.py `
  tests/test_p8_source_routing.py `
  tests/test_p13_evidence_delivery.py `
  tests/test_evidence_gate.py `
  tests/test_evidence_authenticity.py `
  -q

.venv\Scripts\ruff.exe check app tests
.venv\Scripts\python.exe -m compileall -q app
git diff --check
```

子 agent 必须报告：测试通过数、既有 warning、每个 fixture 的 `source`、`authority`、`date_annotation`、`as_of_date`、`business_datum_count`、`structural_metadata_count`、`partial`、`result_partial`、`evidence_created`、`fallback_provenance_failures` 和最终 `source_switch`。

## 9. 禁止事项与交付检查

- 不把 `mcp`、`http_api`、`ths_skill` 当 authority。
- 不把 `retrieved_at` 或系统当前日期当作交易日期。
- 不把所有顶层 `count` 自动解释为涨停家数。
- 不把行业/板块候选行改名为市场涨跌家数或完整热点结论。
- 不通过把 `partial` 改成 `success` 绕过审计。
- 不放宽通用 evidence-only fallback 的完整性要求。
- 本执行单的开发与回归使用离线 fixture。此前真实调用的结果不能作为本改动已通过 live 验收的证据；子 agent 不自行重跑整套生产调用。
- 不修改 `memory/`、`.tmp/`、日志和历史 artifact。
- 不执行 commit、push、reset、revert。

最终交付必须包含：修改文件清单、测试清单和通过数、一个 limit-up count fixture、一个 limit-up sectors fixture、失败 fixture 的逐项诊断，以及明确说明日期是原始字段、envelope 传播还是请求上下文推导。
