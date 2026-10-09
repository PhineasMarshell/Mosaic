# A 股市场综述审计可靠性改造方案

**状态：** 阶段 0 / 1 / 2 / 3 / 4 / 5（保守版）/ 6 已实施（见 §8 实施记录）  
**适用范围：** `Supervisor → Analysts → Gate → Reasoning → Critic → FinalizeAudit` 图，以及同步/SSE 结果交付与持久化路径  
**触发案例：** “今天 A 股发生了什么？”在两次修订耗尽后仍返回 `verdict=revise` 的报告

## 1. 目标与非目标

### 1.1 目标

将系统从“只要生成了 `report` 就交付”改为“审计状态决定交付方式”。对于市场级问题，确保：

1. 数据缺口触发补充研究，而不是仅要求 LLM 改写。
2. `partial`、空数据、错误参数和语义不匹配的数据，不能被等同为“已经拿到充分证据”。
3. 最终未通过审计的报告不能伪装成正常完成结果，也不得污染每日状态与历史研究库。
4. Reasoning 获得完整、结构化、可执行的修订指令；Critic 审查完整报告与可追溯证据。
5. 日志与 API 能重建一次运行的真实计划、执行、证据覆盖、审计和交付决策。

### 1.2 非目标

- 不在本改造中保证 LLM 对任何外部新闻或上市公司事实的绝对正确性。
- 不把“增大 `CRITIC_MAX_REVISIONS`”作为根治方案；它只能临时提高重试次数。
- 不以删除 Critic 或放宽审计规则来降低 `revise` 频率。
- 不在本次改动中重做所有 Gateway 数据源；只补足市场级 A 股综述必须的最小数据契约。

## 2. 现状与根因

### 2.1 已确认的行为

1. `revise` 只回到 Reasoning；只有 `research_more` 才会回到 Supervisor 和工具执行。
2. `revision_count == critic_max_revisions` 时，即使 Critic 仍返回 `revise`，图也直接结束。
3. 流式接口只要 `report` 非空就发送 `done`、返回 `result` 并异步持久化；不要求审计 `pass`。
4. 缺口工具按“工具是否曾成功/partial 执行”过滤；未区分其参数、结果数量、数据完整性及是否回答了当前缺口。
5. Reasoning 在修订时仅接收 `unsupported_claims`，不会接收 Critic 的完整理由、缺口、优先级或修复建议。
6. Critic 当前只看报告的部分字段，且工具结果最多看 20 个、每个最多 10 条；这会造成漏审或误审。
7. `overview` 被定义为全市场工具，但无股票代码时会因 symbol 守卫跳过；这与市场综述需求相冲突。

### 2.2 设计原则

- **证据充分性不等于调用成功。** 成功调用只是“可用候选”，不是“已解决缺口”。
- **事实错误优先删改，证据缺口优先补研究。** 同一个 Critic 输出可以同时包含两类问题，但路由必须确定主动作。
- **最终状态必须可见。** `pass`、降级交付、未交付、审计器自身失败必须是不同的 API/持久化状态。
- **每个市场级结论必须可追溯。** 报告中的关键表述应能落到具体 evidence id，而不是只看 LLM 自由文本。

## 3. 目标状态

```text
Supervisor → Analysts → Gate → Reasoning → Critic
                                      │        │
                                      │        ├─ pass → verified delivery + persistence
                                      │        ├─ revise → Reasoning（仅可通过删改解决）
                                      │        ├─ research_more → Supervisor（缺少/不充分证据）
                                      │        └─ exhausted → safe degraded delivery OR no delivery
                                      │
                                      └─ 每个工具结果带 key、参数、状态、覆盖度与证据数
```

### 3.1 最终交付状态

新增内部 `delivery_status`，不要复用 `errors` 表达质量状态：

| `delivery_status` | 条件 | SSE/API 行为 | 是否持久化为可复用研究 |
|---|---|---|---|
| `verified` | Critic=`pass` | 正常 `done` 与报告 | 是 |
| `degraded` | 预算/轮次耗尽，但可产出仅含已证实事实的降级报告 | 明示“审计未完全通过”；附完整 critique | 否，或单独标记为 `unverified` |
| `blocked` | 关键市场级数据缺失，无法安全回答 | 返回结构化原因和建议重试 | 否 |
| `failed` | 节点、上游或审计器错误 | 返回 error | 否 |

`errors` 仍只表达运行失败；`verdict=revise` 不是 `errors`，但也绝不等于 `verified`。

### 3.2 A 股市场综述的最小证据集

当 intent 为 `domain=a_share` 且任务是市场综述（例如“今天 A 股发生了什么”）时，计划必须优先获得：

| 维度 | 最低证据 | 不足时行为 |
|---|---|---|
| 大盘 | 至少一个代表性指数的涨跌、成交/量能或全市场概览 | `research_more`，不得写“市场走强/走弱” |
| 涨停生态 | 涨停家数 + 题材分布 | 只能陈述已有数据，不能定义 Risk-On |
| 热点 | 板块分布；若点名个股或“最密集”，还要涨停个股明细或等价量化数据 | `research_more` 或删去强比较表述 |
| 事件 | 带时间与来源的新闻/电报；单源必须标为单源 | 不得从单条电报推出市场主线或因果 |
| 资金/情绪 | 市场情绪数据或明确说明缺失 | 不得仅凭涨停数下结论 |

实现时不应把这张表硬编码成“所有问题都必须全拿到”。它仅用于市场综述的**关键结论约束**：缺哪项，就限制相应的表述，或要求补研究。

## 4. 分阶段实施清单

每个阶段必须独立提交、通过测试后再进入下一阶段。不要同时重写图拓扑、提示词和 Gateway。

### 阶段 0：建立失败回归样本与可观测性基线

**目的：** 先让此次失败可复现、可断言，避免修复只改变措辞。

#### 改动

1. 在 `tests/fixtures/` 新增脱敏状态样本：
   - 有 telegraph、涨停数/板块、`partial` 结果；
   - 首份报告包含无证据个股、强板块和因果结论；
   - Critic 依次输出 `revise`、`research_more`、`pass` 或耗尽路径。
2. 增加结构化运行摘要日志（单行 JSON 或固定字段）：
   - `run_id`、问题、显式 domain、最终 intent domain；
   - plan 中每个 `tool_key`/operationId/arguments；
   - 每次执行的 status、partial、normalized 数量、evidence 数量、缓存命中；
   - 每次 Critic verdict、`missing_points`、`missing_tool_keys`、`unsupported_claims`；
   - 路由决定、`revision_count`、最终 `delivery_status`、是否持久化。
3. 日志不得记录 API key、完整私有会话史或原始大 payload；对文本和数组做长度上限。

#### 验收

- 一次运行可从日志还原“计划了什么、实际跑了什么、为什么没补工具、为何最终交付”。
- 现有日志中的 `errors=0` 不再被用于判断报告通过与否。

### 阶段 1：引入“证据缺口优先”的 Critic 契约

**目的：** 让 Critic 不能把需要新数据的问题输出为纯 `revise`。

#### 改动

1. 扩展 `Critique`，用结构化 issue 取代松散的并列数组；保留旧字段一版以兼容响应：

```python
class AuditIssue(BaseModel):
    kind: Literal["unsupported_claim", "missing_evidence", "invalid_entity", "format"]
    claim: str
    severity: Literal["low", "medium", "high"]
    action: Literal["remove_or_qualify", "research_more", "repair_format"]
    rationale: str
    required_tool_keys: list[str] = []
    required_coverage: dict[str, Any] = {}

class Critique(BaseModel):
    verdict: Verdict
    reason: str = ""
    issues: list[AuditIssue] = []
    # legacy compatibility fields, derived from issues during migration
    missing_points: list[str] = []
    missing_tool_keys: list[str] = []
    unsupported_claims: list[str] = []
```

2. 修改 Critic prompt：
   - `missing_evidence`、`invalid_entity` 且需要工具核查时，必须设 `action=research_more`；
   - 只有“删除、降调、补充风险提示即可修复”的问题允许 `remove_or_qualify`；
   - 一个审计同时有两类 issue 时，`verdict=research_more` 优先；补证据后再审计/改写；
   - `required_tool_keys` 只能从可见 registry 选择，且要说明需要什么覆盖条件。
3. 将 `verdict` 从“模型自由选择的唯一动作”改为代码派生：
   - 任意 `action=research_more` → `research_more`；
   - 否则存在 `remove_or_qualify`/`repair_format` → `revise`；
   - 没有 issue → `pass`。
4. 如果 Critic 返回的 actions 与 verdict 冲突，记录结构化 error 并按 actions 的更保守路径路由。

#### 验收测试

- “缺指数表现”“缺涨停个股明细”“缺板块量化比较”必然走 `research_more`。
- “措辞夸大但现有数据可改写”为 `revise`。
- 同时存在两类问题时，必须先 `research_more`。
- 无法解析的 Critic 输出仍为 `error`，绝不伪造 `pass`。

### 阶段 2：修正工具完成判定与缺口补齐

**目的：** 从“工具名执行过”升级为“此次缺口是否真的被满足”。

#### 改动

1. 给 `ToolResult` 增加可选 `tool_key`：

```python
class ToolResult(BaseModel):
    tool_key: str | None = None
    tool: str
    arguments: dict[str, Any]
    status: Status
    partial: bool = False
    normalized: list[NormalizedDatum] = []
```

分析员创建结果时必须写入原计划的 key。不要再从 operationId 反查唯一 key。

2. 在 `gap_loop.py` 用调用签名和覆盖度判断，不再只返回 `set[str]`：

```python
ExecutionCoverage(
    tool_key="limit_up_pool",
    arguments={},
    status="success",
    partial=False,
    datum_count=74,
    fulfills={"limit_up_stock_details"},
)
```

3. 判定规则：
   - `error`：从未满足，可重试；
   - `partial`：默认不满足高严重度缺口；仅当 issue 明确允许部分数据才可复用；
   - `success` 但 `normalized=[]`：不满足；
   - 相同 tool key 但参数不覆盖 required coverage：不满足；
   - operationId 复用的不同 registry key：互不替代，除非 registry 显式声明等价。
4. `sanitize_missing_tool_keys` 改为返回保留、已覆盖、不可见、预算截断四类原因；这些原因必须进入 Critique 和最终响应。
5. 对“已经执行但不满足”的工具，允许有限重试或更换参数；在每个 signature 上设重试上限，避免循环。

#### 验收测试

- `limit_up_pool` 已执行但返回 partial/空列表时，Critic 仍可要求补拉或降级。
- 相同 operationId 的 sibling registry key 不会彼此误判为已完成。
- 同工具不同参数时，只有参数覆盖 requirement 才被视为满足。
- `get_company_overview` 与 `get_company_detail` 的缺口不会因无关调用被静默丢弃。

### 阶段 3：定义市场级 A 股工具策略

**目的：** 市场综述不依赖用户提供单个股票代码。

#### 改动

1. 在 intent 中增加明确任务类型，例如 `market_summary`、`stock_deep_dive`、`event_explain`。
2. 针对 `market_summary` 创建确定性“最低计划前缀”，置于 LLM 计划之前：
   - 情绪、涨停数、题材分布；
   - 全市场概览/指数快照中可安全且有数据量控制的接口；
   - 涨停池仅在需要点名个股、比较密度或 Critic 提出缺口时调用。
3. 处理 `overview` 的矛盾：
   - **首选：** Gateway 新增紧凑型 `get_ashare_market_overview`/指数快照端点，只返回指数、成交额、涨跌家数、两融等聚合字段；加入无需 symbol 白名单。
   - **过渡：** 保持大 `get_company_overview` 不自动调用，但从 registry 与 planner 中移除它作为市场综述首选项，避免“计划了但必跳过”。
4. 对所有无需 symbol 的聚合工具，将“无 symbol 可执行”的能力声明在 `ToolMeta` 中（如 `requires_symbol: bool`），不要维护跨节点硬编码白名单。
5. 补充 Normalizer 合约：市场级工具必须输出带日期/市场范围的标准 datum；空返回必须有明确 note。

#### 验收测试

- 输入“今天 A 股发生了什么？”不含 6 位代码时，计划和实际执行均包含市场级数据；不会出现“计划 overview、执行层静默跳过”的假覆盖。
- 没有指数/概览数据时，报告不得声称大盘整体上涨、风险偏好或量能变化。

### 阶段 4：增强 Reasoning 与 Critic 的可追溯性

**目的：** 降低重复幻觉，避免 Critic 因上下文截断误判。

#### Reasoning 改动

1. 修订上下文传完整、结构化的 `AuditIssue`，而非只传字符串 claims。
2. Prompt 明确要求：
   - 每个具体上市公司名必须来自 evidence 的 `instrument` 或经证据可解析的实体；
   - 每个“最强/最密集/主线/风险偏好/驱动”等比较或因果词必须引用对应 evidence id；
   - 没有证据时删除该句或写为“无法确认”，不得用相近公司名替代；
   - 事件、板块和个股需区分“事实”“推断”“单源消息”。
3. 为关键输出字段引入声明—证据映射，例如：

```json
{
  "claim": "商业航天为当日涨停较集中的题材之一",
  "evidence_ids": ["technical-012", "technical-019"],
  "claim_type": "sector_strength"
}
```

4. 输出后在代码中校验：引用的 evidence id 必须存在；不存在时移除 claim 或将报告降级，不能让 LLM 自行伪造引用。

#### Critic 改动

1. `_format_report_for_review` 必须覆盖全部用户可见字段，特别是 `what_changed`、`what_matters`、`data_caveats` 和报告内 evidence/claim 映射；不再截断单字段至 200 字而不标记截断。
2. `_format_evidence_for_review` 以“报告所引用的 evidence id 优先”的方式提供原始 datum；其余数据给摘要和总数。
3. 若因上下文预算必须截断，向 Critic 明示“未展示的内容不能据此判定无证据”，并把截断信息写入 telemetry。
4. 对实体不存在、代码与名称不一致等问题，优先使用本地 code/name 映射或工具返回实体校验；没有校验源时只标记为“未验证”，不要把模型记忆当事实。

#### 验收测试

- 报告任一关键 claim 的 evidence id 缺失时，不能得到 `pass`。
- Critic 可看到完整的用户可见报告字段。
- “行云科技不存在”一类结论在无实体校验源时被表达为未验证，不被系统当作确定事实。

### 阶段 5：安全终态、SSE 与持久化

**目的：** 避免失败审计污染用户界面、历史记忆和日状态。

#### 改动

1. 在 `ResearchState` 和 `ResearchResponse` 增加：

```python
final_audit_status: Literal["pass", "revise_exhausted", "research_exhausted", "error"]
delivery_status: Literal["verified", "degraded", "blocked", "failed"]
```

2. 修改 Critic 路由：达到轮次/预算上限时不要静默 `END`。先经过一个 `finalize_audit` 节点：
   - `pass` → `verified`；
   - 若所有未解决 issue 都可安全删除/限定，生成一次**确定性降级报告**后标 `degraded`；
   - 若还有高严重度 `missing_evidence` 或 `invalid_entity`，标 `blocked`，不返回正文报告。
3. 暂不实现“自动安全删句”时，采用更保守的第一版：只要耗尽后 verdict 非 `pass`，统一 `blocked`；前端显示“研究未通过证据审计”，并展示可读原因。
4. `persist_research` 仅持久化 `verified`。若业务确实需要保存降级版本，必须独立字段/表标记 `unverified=true`，并禁止其写入 daily state、记忆检索或后续回答上下文。
5. SSE 的 `done` 仅用于 `verified` 或显式 `degraded`；`blocked/failed` 发送对应状态，不能发送“调查完成”。

#### 验收测试

- 两轮 `revise` 后仍未通过：响应带 `delivery_status=blocked`，不持久化，不出现“调查完成”。
- `research_more` 因总预算耗尽：同样不可伪装成功。
- Critic `pass`：保持当前正常 SSE、同步 API 和存储行为。
- 前端/调用方只基于 `delivery_status` 判断是否可展示为可信结论，不能再推断 `errors == []`。

### 阶段 6：轮次、预算与发布策略

**目的：** 限制成本，同时避免把轮次上限当作事实质量判断。

#### 改动

1. 将计数拆分：
   - `rewrite_count`：仅 `revise → reasoning` 增加；
   - `research_round_count`：仅 `research_more → supervisor` 增加；
   - 两者有独立、可配置上限。
2. `research_more` 预留最小剩余预算；低于该预算时不启动半轮工具，直接进入 `finalize_audit`。
3. 初始默认值建议：`max_rewrites=1`、`max_research_rounds=1`。先修正路由与数据覆盖，再根据真实通过率调高，不要盲目设为 3+。
4. 增加指标：审计通过率、各 verdict 分布、耗尽率、降级/阻断率、每类缺口工具补齐成功率、每次研究的 token/工具耗时。

#### 灰度发布

1. 先以 shadow mode 运行新 Critic 分类与 final audit，不影响实际响应，仅记录差异。
2. 收集至少 50 个市场综述和 50 个个股问题，人工抽样审查：错误放行率、错误阻断率、平均延迟。
3. 开启 `blocked` 的前端提示与停止持久化。
4. 最后删除旧的仅按 operationId `executed_keys()` 作为证据充分性判据的路径。

## 5. 建议文件级工作拆分

| 文件/区域 | 主要工作 |
|---|---|
| `app/graph/nodes/critic.py` | `AuditIssue`、prompt、从 issues 派生 verdict、完整审计上下文 |
| `app/graph/nodes/reasoning.py` | 注入完整审计反馈、修订语义 |
| `app/research/reasoning.py` 与 prompts | claim—evidence 映射、实体与因果约束 |
| `app/graph/gap_loop.py` | `ExecutionCoverage`、参数/完整性/语义覆盖判断 |
| `app/models/market.py` | `ToolResult.tool_key` 等可追溯字段 |
| `app/graph/nodes/analysts/base.py` | 写入 tool key；由 metadata 声明参数需求 |
| `app/gateway/tool_registry.py` | `requires_symbol`、市场级紧凑工具、等价性声明 |
| `app/graph/builder.py`、`app/graph/state.py` | 独立计数、`finalize_audit`、终态字段 |
| `app/models/response.py`、`app/main.py`、`app/agent/orchestrator.py` | delivery status、SSE/API、持久化门控 |
| `app/agent/persistence.py` | 禁止未验证结果进入 daily state / memory |
| `tests/` | 本文各阶段的回归与端到端测试 |

## 6. 必跑测试矩阵

在每阶段结束及最终合并前至少运行：

```powershell
pytest tests/test_critic_verdict.py tests/test_critic_gap_keys.py tests/test_supervisor_gap_loop.py tests/test_graph_topology.py tests/test_graph_nodes.py -q
pytest tests/test_market_level_plan.py tests/test_graph_e2e.py -q
pytest tests/test_reasoning_traceability.py -q
pytest tests/test_stream_endpoint.py tests/test_orchestrator.py tests/test_data_integrity.py -q
```

另新增端到端测试，至少覆盖：

1. 无股票代码的“今天 A 股发生了什么”；（阶段 3 已覆盖：`tests/test_graph_e2e.py::test_market_level_question_plans_and_executes_market_tools`）
2. 有个股代码的深度问题，不应被市场综述最低集强行拖慢；（阶段 3 已覆盖：`tests/test_market_level_plan.py::test_no_prefix_for_non_market_questions`）
3. `partial` 涨停池不能满足“最密集板块/个股明细”结论；
4. Critic 同时发现事实夸大和缺数据时，必须先补研究；
5. 耗尽轮次后绝不产生“已验证完成”的 SSE 或持久化记录；
6. operationId 复用的两个 registry key 不互相污染完成状态；
7. 报告引用不存在 evidence id 时不能 pass；（阶段 4 已覆盖：`tests/test_reasoning_traceability.py::TestCriticEndToEnd::test_forged_reference_cannot_pass_even_if_the_model_says_pass`）
8. 运行日志能完整解释一次 `blocked` 结果。（阶段 0 已覆盖：`tests/test_audit_run_log.py`）

## 7. 实施顺序与完成定义

按以下顺序执行：**阶段 0 → 1 → 2 → 5 的保守阻断版 → 3 → 4 → 6**。

其中，阶段 5 的“非 pass 不当成功交付”应尽早上线；它是数据可靠性的底线，不依赖后续复杂的自动降级摘要。

本方案完成的定义：

1. 同类运行若缺大盘/板块/个股明细证据，会补研究或明确阻断，不会靠三次 LLM 改写伪装解决。
2. 每一份被保存并用于后续记忆的研究均有最终 `pass` 审计状态。
3. 用户和调用方可以一眼区分“已验证结论”“降级事实清单”“未能安全回答”。
4. 在日志和测试中，能够证明为什么某个工具被视为满足或不满足某条缺口。

---

## 8. 实施记录（阶段 0 / 1 / 2 / 3 / 4 / 5 保守版）

### 8.1 已落地的行为变化

| 阶段 | 关键文件 | 行为变化 |
|---|---|---|
| 0 | `app/graph/run_log.py`（新）、`tests/fixtures/audit_regression_*.json`（新）、`state.py`、`supervisor.py`、`analysts/base.py`、`critic.py`、`builder.py`、`orchestrator.py`、`main.py` | 每次调查一个 `run_id`；`run_start/plan/executions/critic/route/finalize/delivery` 七类单行 JSON 事件；所有字段在 `emit()` 层统一截断脱敏；`tests/test_audit_run_log.py` 用脱敏失败样本端到端复现 blocked |
| 1 | `app/graph/nodes/critic.py`、`tests/test_audit_issue_contract.py`（新） | 新增 `AuditIssue`；verdict 由代码从 issue 的 action 派生；冲突按更保守一侧路由并记结构化错误；legacy 字段由 issues 派生（并集，不覆盖模型值）；无法解析 → `error` |
| 2 | `app/graph/gap_loop.py`、`app/models/market.py`、`analysts/base.py` | `ToolResult.tool_key` 由 analyst 写入；`ExecutionCoverage` / `judge_coverage` / `satisfied_keys` 按「key + status + partial + 数据量 + 参数覆盖」判定；`classify_missing_tool_keys` 返回四类原因并写入 `critique.gap_key_decisions`；`drop_repeat_steps`（别再调一遍）与 `satisfied_keys`（证据够不够）显式分离 |
| 5 | `app/graph/nodes/finalize.py`（新）、`builder.py`、`models/response.py`、`agent/persistence.py`、`main.py`、`cli.py`、`app/web/index.html` | 非 pass 结局一律经 `finalize_audit`；`delivery_status` / `final_audit_status` 落 state 与响应；blocked 清空正文、不持久化、SSE 发 `step=blocked` + `code=blocked`（不发"调查完成"）；同步 API 返回结构化 200；CLI / 前端不再把未验证结果当正常结论 |
| 3 | `app/graph/market_plan.py`（新）、`app/gateway/tool_registry.py`、`analysts/base.py`、`nodes/supervisor.py`、`app/graph/run_log.py`、`app/agent/prompts_graph.py`、`app/gateway/normalizer.py` | "是否需要 symbol"改由 `ToolMeta.requires_symbol` 单一声明；A 股市场级问题由**代码**注入最低证据集（沪深300 + 情绪 + 涨停家数 + 涨停题材 + 快讯）并置于计划最前；"计划了必然跳过"的 step 被剔除并记入 run log 的 `unexecutable_keys`（不再静默跳过）→ 消除假覆盖；市场级 datum 缺时间戳时带明确 note |
| 4 | `app/research/entity_check.py`（新）、`app/research/reasoning.py`、`app/research/evidence.py`、`app/models/response.py`、`app/models/evidence.py`、`app/agent/prompts.py`、`app/graph/nodes/reasoning.py`、`app/graph/nodes/critic.py`、`app/graph/run_log.py` | 报告带 claim—evidence 映射（`claims`），引用由**代码**核对：不存在的 evidence id 被剥离、记入 `evidence_violations`、整份报告降级 `confidence=low`；实体必须有校验源（本地 code/name 映射或证据 `instrument`），无校验源实体记入 `unverified_entities`，"某公司不存在/未上市"一类断言由代码变成 `invalid_entity` issue → 不可能 pass；Critic 审查上下文覆盖全部用户可见字段（含 `what_changed`/`what_matters`/`data_caveats`）与 claim 映射、报告引用的证据优先给完整 datum、截断显式标注并进 telemetry |

### 8.2 与原方案的取舍（冲突处以更保守 / 可验证为准）

1. **verdict 缺省的处理**：模型不再返回 verdict（新版 prompt 只要求 `issues`）。规则为
   「issues 派生 → legacy 缺口数组 → **显式给了 `issues` 键（哪怕空数组）→ pass** → error」。
   把"显式空 issues"判为 pass，是因为那正是"逐条审查后没发现问题"的表态；
   真正的"什么都没说"（无 verdict / 无 issues / 无缺口）判 `error`，绝不伪造 pass。
2. **冲突记录写在 ERROR 级日志而不是 `state.errors`**：方案第 4 条说"记录结构化 error"。
   但 `errors` 在本系统里语义是"运行失败"，而"模型裁决与 action 不一致"是审计质量问题，
   已由保守路由吸收；写进 `errors` 会污染持久化与前端的失败语义。
3. **`blocked` 时清空 `report`**：按方案 §4 阶段 5 第 2/3 条执行。同步 API 因此返回
   **200 + 结构化载荷**（含 `delivery_status` / `delivery_reason` / `retryable` / `critique`）而不是 502——
   blocked 是合法研究结论，不是基础设施故障。
4. **`ResearchResponse.delivery_status` 默认为 `None`（= 未审计）**：手工构造响应不再默认"已验证"，
   持久化门控只放行显式 `verified`。这是对既有调用点的**破坏性变更**，已同步更新
   `tests/test_persistence.py` / `tests/test_cli.py` 等构造点。
5. **未实现 `degraded` 自动降级**：按方案 §4 阶段 5 第 3 条采用保守版——耗尽后非 pass 一律 `blocked`。
   `degraded` 的枚举与放行位已就绪，但需要确定性摘要生成，留待后续。
6. **`executed_keys()` 保留但不再作为证据充分性判据**：它回答的是"跑过没有"，
   仍服务于重复步骤剔除与 planner 上下文；证据充分性改用 `satisfied_keys()`。
   方案 §4 灰度第 4 条要求的"删除旧路径"待 shadow mode 观察后再删。

### 8.3 未实施（需后续决策）

- **阶段 3 剩余的 Gateway 侧改造**：紧凑市场概览端点 `get_ashare_market_overview`
  （指数 + 成交额 + 涨跌家数 + 两融）。本仓库不含 Market Gateway 服务端实现
  （`allowed_openapi.json` 白名单里也没有该路径），故按方案 §4 阶段 3 第 3 条的
  **过渡方案**落地：不新增端点、把 `overview` 从"市场综述首选项"移除并禁止其
  在无 symbol 时进入计划。等 Gateway 提供该端点后，只需把 `MARKET_SUMMARY_MINIMUM`
  的第一条从 `quote(000300)` 换成它，其余防线不用动。
- **阶段 6**：`rewrite_count` / `research_round_count` 拆分与独立上限、research_more 预留预算、
  审计通过率等指标、shadow mode 灰度。
- **实体校验的“校验源”只覆盖本地映射与证据**：`local_entity_index()` 基于
  `STOCK_CODE_MAP_A_SHARE`（当前 23 个股票名 + 指数名）。名单之外的**真实**公司也会被判为
  “无校验源”——这是保守方向（报告只能说“未验证”，不能说“不存在”），代价是措辞收紧。
  等 Gateway 提供名称/代码校验端点后可扩大校验源，无需改动判定逻辑。
- **指标与 shadow mode**：本次只落地了可解释的结构化日志，还未接入指标聚合与新旧裁决差异比对。

### 8.4 验收阻塞项修复记录（4 项）

针对 §8.1 落地后的复核，确认并修复了 4 项缺陷。**审计标准未被放宽**：`CRITIC_MAX_REVISIONS`
未调整，Critic 规则未删除或放宽，未通过审计的报告仍不会被当作正常报告交付。

| # | 级别 | 缺陷 | 关键文件 | 修复方式 |
|---|---|---|---|---|
| 1 | P0 | 非对象 `issues` 元素被静默过滤后误判 `pass` | `app/graph/nodes/critic.py` | `parse_issues()` 对非 list 及**任一**非 dict 项 `raise _issues_error(...)`，不再 `if isinstance(item, dict)` 跳过；节点捕获后返回 `verdict="error"` + `errors=["Critic 输出无法解析为合法结论: ..."]` |
| 2 | P1 | 冲突解决无条件采信 action 派生值，把 `research_more` 降级为 `revise` | `app/graph/nodes/critic.py` | 新增 `_VERDICT_CONSERVATISM = {"pass":0,"revise":1,"research_more":2,"error":3}` 与 `more_conservative()`；`resolve_conflicts()` 取两者中更保守者写回 `critique.verdict`，`conflicts[0]["resolved"]` 记录真实路由结果 |
| 3 | P1 | `AuditIssue.required_coverage` 未进入缺口满足判定 | `app/graph/nodes/critic.py`、`app/graph/gap_loop.py`、`app/graph/nodes/supervisor.py` | `coverage_requirements(critique)` 建立「缺口 key → required_coverage（按最严合并）」映射；`satisfied_keys(results, requirements)` 逐 key 按 `min_datum_count` / `allow_partial` / `arguments` 判定；`gap_tool_keys()` 接入该映射，不满足的 key 保留并由 `append_gap_steps()` 代码级强制补进计划头部 |
| 4 | P1 | 同步 `/api/ask` 的 `delivery` 结构化日志传 `run_id=None` | `app/main.py` | 三个分支（blocked / no-report / 成功）改传 `{"run_id": result.run_id}`，与 `run_start` / `plan` / `executions` / `critic` / `route` / `finalize` 同 id；`run_id` 仍是内部字段，对外 JSON 与 SSE 载荷继续剔除 |

**缺陷 3 的补充修复**：`render_unexecuted_registry()` 的旧名字容易被理解成「只渲染没执行过的工具」，
从而退化成「执行过就删掉」。现由 `render_registry_for_critic()` 保留**全部**条目，已执行的只追加
`[已调用：status=... / 现有数据 N 条；尚未满足缺口，可点名]` 标注——prompt 组装时 `required_coverage`
尚不存在（它正是本次要产出的东西），此时删除条目会让 Critic 永远看不见该工具、缺口永远补不上；
真正的过滤发生在落库前，由 `classify_missing_tool_keys()` 按逐条 issue 的要求精确判断。

**新增 / 补强的回归测试**：

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_audit_issue_contract.py` | 缺陷 1：参数化 `{"issues": ["缺数据"]}` / `[null]` / `[123]` / `[["nested"]]` / 混合项 → `error` + `errors`；`{"verdict":"pass","issues":["缺指数数据"]}` → `error` 且错误信息含 `issues[0]`。缺陷 2：双向参数化冲突（`pass`/`revise` vs `research_more`，`research_more` vs `remove_or_qualify`/`repair_format`）断言 `critique.verdict` 与 `conflicts[0]["resolved"]`，另加 `test_more_conservative_ordering`。缺陷 3：同 key success 但 datum 数不足 / 参数不匹配 → `gap_tool_keys` 仍保留；数量与参数都满足 → `already_satisfied`；`coverage_requirements` 最严合并；`test_unexecuted_registry_alias_keeps_low_coverage_tools_visible`（按旧名调用，断言低覆盖 success 仍可见并可点名）。**56 passed** |
| `tests/test_ask_run_id_correlation.py`（新） | 缺陷 4：走真实 `TestClient(main.app).post("/api/ask")`，断言 7 类事件同 `run_id`、`delivery` 不为 `None`、每个事件恰好 1 条；另断言对外响应不含 `run_id`。**5 passed** |

**红证据（确认新测试真能抓住缺陷，而非恒真）**：逐一临时改回旧行为后跑测试，确认失败点与缺陷一一对应，
随后完整还原并校验 sha256 一致：

| 临时改坏的旧行为 | 期望失败的测试 | 实测 |
|---|---|---|
| `parse_issues()` 里 `if not isinstance(item, dict): continue`（静默过滤） | 缺陷 1 的 7 条 | 失败 |
| `resolve_conflicts()` 里 `resolved = structured`（无条件采信 action 派生值） | 缺陷 2 的反向 2 条 | 失败 |
| `satisfied_keys()` 里 `judge_coverage(..., None)`（不传 `required_coverage`） | 缺陷 3 的 3 条（datum 不足 / 参数不匹配 / research_loop 保留） | 失败 |
| `app/main.py` 三处 `{"run_id": None}` | `tests/test_ask_run_id_correlation.py` 的 2 条 | 失败 |

三者同时改坏时 `tests/test_audit_issue_contract.py` → **12 failed, 44 passed**；
单改 `app/main.py` 时 `tests/test_ask_run_id_correlation.py` → **2 failed, 3 passed**。
还原后校验 `sha256(app/graph/nodes/critic.py) = 785841F791F323641120AFBE41ECA03C937E0BB87E1C30F013B1682310F7A4EE`、
`sha256(app/graph/gap_loop.py) = 495E57020DD719001737FF3E9E4732CBA1F28B76D6CD8DFB12130C8F6FA56DF8`、
`sha256(app/main.py) = F519AFB6C4459FE241990A42D82B1CD8318A07AF6E044E79F95E2E2D2C04FD54`（均与改坏前一致），
复跑相关 13 个测试文件 → **185 passed, 2 deselected**。

**沙箱权限限制（已解除）**：本轮取证初期，受限会话下 `D:\Mosaic\memory\` 与 `$env:TEMP\pytest-of-Phineas`
的 ACL 拒绝写入/枚举，导致

- 导入期 `app.main` 需 `MOSAIC_MEMORY_DB` / `MOSAIC_DATA_DIR` 指向可写目录（如 `D:\Mosaic\.tmp`）才能成功；
- 依赖 `tmp_path` / `tempfile.mkdtemp()` 的测试无法运行（`PermissionError: [WinError 5]`，`--basetemp` 预先创建亦无效）。

当时全量 `pytest tests` 为 **6 failed, 755 passed, 2 deselected, 43 errors**（`tests/test_conversation.py`、
`tests/test_market_memory.py`、`tests/test_memory_thread_safety.py`、`tests/test_persistence.py`、
`tests/test_data_integrity.py`、`tests/test_deployment_config.py`），**无一是代码回归**。

会话切换到完全权限后重跑，上述失败与错误**全部消失**，确认为沙箱文件策略产生的假故障而非代码缺陷：

- `pytest tests -q`（无任何环境变量绕道，使用默认 `memory/memory.db`）→ **806 passed**；
- `ruff check .` → **All checks passed**（默认缓存目录亦可写）。

过程中顺手清掉了 `tests/test_audit_run_log.py` 里 5 处 lint 问题（用例内冗余 `import app.main as main`
造成 F401/F811 与 2 处 import 排序），模块导入期的那次导入保留（`setup_logging` 需要提前安装 handler）。

### 8.5 安全边界补修记录（2 项：显式 null 的 issues、同 key 冲突参数）

§8.4 落地后复核又发现两处仍会**悄悄放宽审计**的边界。两处都属"输入非法/不可满足时，
系统选择了更容易判通过的一侧"，因此都往保守方向收紧。仍未调整 `CRITIC_MAX_REVISIONS`，
未删除或放宽任何 Critic 规则，`pass` 快路径、blocked 交付门控、SSE 与同步 API 行为不变。

#### 8.5.1 缺陷 A：显式 `issues: null` 会退化成 `pass`

`parse_issues(None)` 曾返回 `[]`；随后 `_verdict_from_payload({"issues": null}, [])` 只看
`"issues" in data`，于是"模型把结论写成 null"被当成"逐条审查后没发现问题"。

修法是把**字段缺失**与**显式 null** 分开：

| 输入 | 旧行为 | 现行为 |
|---|---|---|
| 缺 `issues` 字段 | `[]` | `[]`（合法，`_MISSING_FIELD` 哨兵） |
| `"issues": null` | `[]` → 派生 `pass` | `raise _issues_error("issues=NoneType（显式 null）…")` → `verdict="error"` + `errors` |
| `"issues": []` | `pass` | `pass`（不变：这是"逐条审查后没问题"的正式表态） |
| `"issues": ["缺数据"]` / `[null]` / `[123]` / `[["x"]]` | `error` | `error`（不变，§8.4 缺陷 1） |

关键点：

- 调用点改为 `parse_issues(data.get("issues", _MISSING_FIELD))`——用哨兵而不是 `.get()` 的 `None`
  才能区分两者（`.get()` 会把它们都变成 `None`，这正是缺陷成因）。
- 纵深防御：`_verdict_from_payload()` 也改成只认**合法数组**
  （`if "issues" in data and data.get("issues") is not None`），即使将来谁绕过 `parse_issues`，
  也不会重新长出"键存在就算通过"的退化路径。
- 报错文本把类型名放在最前面（`issues=NoneType …`）：pydantic 渲染 `ValidationError` 时会把
  `input_value` 截断到约 25 字符，`NoneType` 必须落在截断点之前才可被断言与检索。
- `_ISSUE_RULES` 顺带写清契约：`issues` 必须是数组、元素必须是对象，要表达"没有 issue"请给 `[]`。

#### 8.5.2 缺陷 B：同 key 的冲突 `arguments` 被后者覆盖

旧 `coverage_requirements()` 对同一 key 的 `arguments` 做 `merged.update(args)`。于是

- issue A 要求 `date=2026-10-07`，issue B 要求 `date=2026-10-08`；
- 合并结果只剩 `10-08`；
- 任意一条 `10-08` 的历史结果就令该 key 进入 `satisfied_keys()`，
  **A 的缺口凭空消失**，`classify_missing_tool_keys()` 还会把它标成 `already_satisfied`。

修法是保留每条 issue 的要求，不做参数合并：

- `coverage_requirements()` 顶层仍输出 `min_datum_count`（取最大值）与 `allow_partial`
  （一票否决），语义不变；新增 `argument_variants: list[{arguments, min_datum_count, allow_partial}]`，
  **每个不同的 `arguments` 取值各留一条**（参数完全相同的多条 requirement 合并为一个变体，
  条数取最大）；未声明 `arguments` 的 issue 记为 `{}` 变体，这样它自己的条数/partial 要求
  不会被别的 issue 吞掉。
- `satisfied_keys()` 改为**逐变体**判定：`argument_variants` 里**每一个变体都要有至少一条记录满足**
  （`_key_satisfied` / `_variant_satisfied`），而不是"任意一条记录满足合并后的要求"。因此
  冲突参数无法由单次调用蒙混过关，而两天的数据分别到位时（多次调用）可以判为满足。
- `judge_coverage()` 开头清空 `record.satisfies` / `record.fulfills`：同一条记录会被反复拿去
  判定不同变体，不清空会留下上一变体的过期结论。

#### 8.5.3 采用的冲突参数执行策略：多调用（multi-call），不静默合并

冲突参数**不**在计划里被合并成"一次调用"，而是让多次不同参数的调用真实进入可执行计划：

1. `append_gap_steps()` 新增可选 `requirements` / `results`：对 `gap_keys` 里的 key，
   **为每个尚未被满足的变体各补一条独立步骤**（带该变体自己的 `arguments`，news 类工具用
   `setdefault("query", question)` 不覆盖显式 query）；已满足的变体不重复规划；不传
   `requirements` 时保持旧的"按 key 补一条空参数步骤"行为。
2. `app/graph/nodes/supervisor.py` 的回环轮调用点传入
   `requirements=coverage_requirements(critique)` 与 `results=results`，使"同一工具、不同参数"
   的补证步骤真正落到 `plan.steps` 头部并被优先执行。
3. `render_gap_context()` 增列「Critic 对参数/数据量的逐条要求（**每一条都必须被满足**）」，
   列出每个尚未满足的变体及其 `arguments` / `min_datum_count`；出现多条时追加提示
   「同一 key 参数互相冲突时，单次调用无法同时满足；请为每个不同的参数各规划一次调用」，
   让 planner 不会为了迁就单次调用而主动丢掉其中一个取值。**注意顺序**：该提示在
   「已执行…不要重复规划」之后，是"你仍需重拉"的补充说明，不改既有语义。
4. 若某个冲突变体始终无法满足（例如 10-07 的数据源不可得），该 key 会一直留在
   `gap_tool_keys()` 结果里 → 每轮继续补研究 → 预算耗尽后由 `finalize_audit` 走
   `blocked` 安全阻断，**不会被静默当成正常报告交付**。

#### 8.5.4 新增 / 补强的回归测试

| 测试（均在 `tests/test_audit_issue_contract.py`） | 覆盖 |
|---|---|
| `test_explicit_null_issues_is_illegal_not_pass` | 节点级：`{"issues": null}` 无 verdict → `error` + `errors` 含 `issues=NoneType` |
| `test_explicit_null_issues_cannot_be_bypassed_by_pass_verdict` | `{"verdict":"pass","issues":null}` → 仍 `error` |
| `test_missing_issues_field_stays_legal` | 对照：缺字段 + `verdict=pass` → `pass`、`errors == []`（哨兵不误伤） |
| `test_verdict_derivation_never_turns_explicit_null_into_pass` | 纵深防御：`_verdict_from_payload` 参数化（`null` → error，`[]` → pass，`{}` → error） |
| `test_parse_issues_distinguishes_missing_field_from_explicit_null` | 哨兵语义单元锚点：`_MISSING_FIELD` → `[]`，`None` → `ValidationError` 含 `NoneType` |
| `test_same_key_identical_argument_requirements_merge_into_one_variant` | 同 key 同参数多条要求 → 合并为一个变体、条数取最大、一次调用可满足 |
| `test_conflicting_argument_requirements_are_kept_apart_not_merged` | 冲突 `date` 各自保留；只有任一天的结果 → 均不满足 |
| `test_conflicting_argument_gap_is_never_marked_already_satisfied` | 冲突参数下 `gap_tool_keys()` 仍保留该 key，`dropped == []`（绝不被标 `already_satisfied`） |
| `test_conflicting_arguments_are_satisfied_by_multiple_calls` | 多调用正向：两天数据分别到位 → 满足、落库 `already_satisfied` |
| `test_gap_backfill_plans_one_call_per_unsatisfied_argument` | `append_gap_steps()` 只补未满足变体；全未满足时排两次不同参数的调用 |
| `test_gap_context_tells_the_planner_to_plan_one_call_per_argument` | 回环轮上下文摊开冲突参数并提示"各规划一次调用" |
| `test_critic_keeps_conflicting_argument_gap_for_research_loop` | 节点级集成：冲突参数 → `research_more`、`missing_tool_keys` 保留、只补未满足的那个参数 |

既有语义的回归保护：`min_datum_count` 取最大与 `allow_partial` 一票否决
（`test_coverage_requirements_are_merged_strictest_first` 保持不变），partial / 空数据 /
参数不匹配 / operationId sibling / 无 tool_key / 旧名 `render_unexecuted_registry` 可见性等
用例全部继续通过。

#### 8.5.5 红证据与最终结果

临时改回旧的宽松行为，确认新测试确实会失败（而非恒真），随后从备份完整还原并校验 sha256 一致：

| 临时改坏的旧行为 | 实测失败 |
|---|---|
| `parse_issues(None) → []` 且 `_verdict_from_payload` 退回 `"issues" in data` | 缺陷 A 的 5 条（节点级 2 条 + `_verdict_from_payload` 参数化 2 条 + `parse_issues` 哨兵单元 1 条） |
| `coverage_requirements()` 把冲突参数 `update` 合并成一个变体 | 缺陷 B 的 7 条（冲突参数 5 条 + 受影响的既有用例 2 条：参数合并后 `min_datum_count` 被清空） |

**两者同时改坏时 `tests/test_audit_issue_contract.py` → 12 failed, 59 passed**（5 + 7，无交叉归因）；
还原后同文件 **71 passed**。
还原校验：`sha256(app/graph/nodes/critic.py) = 4E96EA677C3C0A3726F68D90F3DF67A9C5A97B4CF353E82D059E2020BCC5048F`、
`sha256(app/graph/gap_loop.py) = 56B98381434DDA89D0E0820ACFB7884B96520B1B63FB0BA82A0D448416C5C186`
（即上表改坏前的值；§8.4 记录的这两个 hash 是那一轮的还原校验值，本轮再次改动后以本节为准）。

本轮最终验证（非受限会话，无环境变量绕道）：

- `pytest tests -q` → **821 passed**（§8.4 时为 806，本轮 +15 条新用例）；
- `ruff check .` → **All checks passed**。

遗留（未在本轮改动）：`argument_variants` 让"同 key 多参数"成为常态后，回环轮预算
（`gap_max_steps` / `max_research_steps`）可能不够同时覆盖多个变体，此时仍按既有逻辑
截断并由 `finalize_audit` 安全阻断——**不会**退化成"少补一个参数也算满足"。是否需要
为多调用场景单独预留预算，属阶段 6（轮次与灰度）的决策范围。

---

### 8.6 安全边界补修记录（第 3 项：key 级 `allow_partial` 被参数变体绕过）

#### 8.6.1 缺陷 C：参数变体让 key 级 `allow_partial` 一票否决失效

§8.5 引入 `argument_variants` 后，`coverage_requirements()` 依旧在 key 级汇总出
`allow_partial`（任一 issue 声明 `allow_partial=false` 即整条 key 取 `false`），但
`_key_satisfied()` 进入变体分支后只把**变体自己**的 `allow_partial` 交给
`judge_coverage()`，key 级限制在这个分支里完全没有被使用。

可复现的最小情形：

| | issue A | issue B | 实际结果 |
|---|---|---|---|
| 声明 | `date=2026-10-07`，`allow_partial=false` | `date=2026-10-08`，`allow_partial=true` | key 级 `allow_partial=false` |
| 执行 | `success`（30 条） | `partial`（30 条） | `satisfied_keys()` **错误地包含该 key** |

`date=08` 的变体拿 partial 结果顶包，于是"禁止 partial"这条 issue 的安全要求被
**另一条 issue 的参数取值**绕过。修复后同一情形下该 key 仍未满足，回环轮继续补研究，
最终若始终拿不到 success 数据则由 `finalize_audit` 安全阻断。

#### 8.6.2 修复方式：key 级策略下压到变体（`_with_key_policy`）

`app/graph/gap_loop.py` 新增：

```python
def _with_key_policy(variant: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    if not spec.get("allow_partial", True):
        return {**variant, "allow_partial": False}
    return dict(variant)
```

- **只下压 `allow_partial`**（key 级"一票否决"）。`min_datum_count` 故意**不**下压：
  它按 §8.5 的语义在"同参数变体"内取最大，跨变体取全局最大值会无端加严另一条 issue
  的要求（例如 A 要 ≥20 条 `date=07`、B 只要 ≥5 条 `date=08`，不应把 20 强加给 `date=08`）。
- 三个消费点统一走该函数，保证"判定"与"补研究"不会各说各话：
  `_key_satisfied()`（变体分支）、`append_gap_steps()`（筛选未满足变体，需同时用
  `spec` 取 key 级值）、`render_gap_context()`（渲染未满足要求时也按收紧后的策略显示
  "（允许 partial）"标记）。
- key 级 `allow_partial` 在映射里仍然可见（`requirements[key]["allow_partial"]`），
  与无变体分支的通用规则判定一致。

语义保持不变（均有回归保护）：同 key 不同 `arguments` 仍必须由**多次执行**分别满足；
同参数变体的 `min_datum_count` 仍取最大；没有任何 issue 禁止 partial 时，显式
`allow_partial=true` 的变体仍可复用 partial 数据；未声明 `allow_partial` 时默认禁止复用
partial；未满足的变体继续进入 `append_gap_steps()` 并生成正确 `arguments` 的补研究调用。

#### 8.6.3 新增回归测试（均在 `tests/test_audit_issue_contract.py`）

| 测试 | 覆盖 |
|---|---|
| `test_key_level_allow_partial_veto_cannot_be_bypassed_by_a_variant` | 复现场景：`date=07` success + `date=08` partial → `satisfied_keys()` **不含**该 key；`gap_tool_keys()` 保留该 key 且 `dropped == []`（绝不被标 `already_satisfied`） |
| `test_key_level_allow_partial_veto_still_backfills_the_blocked_variant` | 被禁令挡下的变体必须生成 `arguments == {"date": "2026-10-08"}` 的补研究调用（只补被挡的那一个） |
| `test_key_level_allow_partial_veto_does_not_block_success` | 对照：两个日期都 success → 该 key 可满足，`dropped == ["limit_up_pool"]` |
| `test_partial_is_reusable_when_every_issue_allows_it` | 对照：所有相关 issue 显式 `allow_partial=true` → partial 结果可满足 |
| `test_partial_still_unsatisfying_when_undeclared` | 对照：未声明 `allow_partial` → 默认仍禁止复用 partial |
| `test_key_level_veto_applies_to_the_forbidding_issues_own_variant` | 禁令同样作用于**禁止 partial 那条 issue 自己的变体**（不只是跨变体场景） |

#### 8.6.4 红证据与最终结果

把 `_with_key_policy()` 临时改成直接 `return dict(variant)`（即"只看变体自己的
`allow_partial`"，等价于缺陷 C 的旧行为），`tests/test_audit_issue_contract.py`
→ **2 failed, 75 passed**，失败正是：

- `test_key_level_allow_partial_veto_cannot_be_bypassed_by_a_variant`
- `test_key_level_allow_partial_veto_still_backfills_the_blocked_variant`

随后从备份完整还原并校验 `sha256(app/graph/gap_loop.py) = 1DE086B719C5249CA314F0D164FAC179503BE862656A598F2AE6FE08CEDE2C8D`
（与改坏前一致）。本轮最终验证（非受限会话，无环境变量绕道）：

- `pytest tests -q` → **827 passed**（§8.5 时为 821，本轮 +6 条新用例）；
- `ruff check .` → **All checks passed**。

至此"审计结论可达性"与"安全终态"两条边界（非对象/显式 null 的 `issues`、冲突
`arguments`、key 级 `allow_partial` 一票否决）均已由代码级判定 + 回归测试双重固定。

### 8.7 阶段 3 实施记录：A 股市场级工具策略

方案 §4 阶段 3 的六条要求全部落地。核心问题（§2 根因）：**计划层与执行层对同一份
计划给出不同结论** —— 不含 6 位代码的"今天 A 股发生了什么？"会让 planner 计划
`overview`（`get_company_overview`，必须有 symbol），analyst 的符号守卫拿不到 symbol
便打一条 warning 后 `continue`。计划里写着"看概览"，实际一条概览数据都没有，
报告却照写"大盘整体上涨"。

#### 8.7.1 落地内容

| # | 方案要求 | 落地位置 | 做法 |
|---|---|---|---|
| 1 | intent 增加明确任务类型 | `app/models/research.py` | `ResearchIntent.task` 已含 `market_summary` / `market_diagnosis` / `theme_analysis` / `company_research` / `anomaly_detection`，无需新增字面量；市场级判定用 `MARKET_TASKS = {market_summary, market_diagnosis}` |
| 2 | 确定性"最低计划前缀" | `app/graph/market_plan.py`（新） | `MARKET_SUMMARY_MINIMUM` 五条：`quote(000300)` 大盘基准 → `sentiment` 情绪 → `limit_up_count` 涨停家数 → `limit_up_sectors` 题材分布 → `telegraph` 盘面快讯；`apply_market_summary_prefix()` 用代码口径**置于计划最前**，planner 的同名 step 被替换（口径由代码定，不由 LLM 猜） |
| 3 | 处理 `overview` 矛盾 | `prompts_graph.py`、`market_plan.py` | 走**过渡方案**：`overview` 不再出现在"A 股市场级路径"里，prompt 明确它必须有 symbol、大盘问题里不是可用工具；代码再兜一层——真被计划了也会被剔除并记账 |
| 4 | 无 symbol 可执行声明进 `ToolMeta` | `tool_registry.py`、`analysts/base.py` | 新增 `requires_symbol: bool = True` 与 `market_level: bool = False`；`_SYMBOL_FREE_TOOLS` 是唯一真相，`ALL_TOOLS` 定义后统一回填；`analysts/base.py` 的 `WHITELIST_NO_SYMBOL` 降级为**派生只读别名**（兼容期），符号守卫改读 `meta.requires_symbol` |
| 5 | Normalizer 合约 | `normalizer.py` | 市场级工具（`market_level=True`）的 datum 缺时间戳 → 逐条追加 `_MARKET_LEVEL_NO_TIMESTAMP_NOTE`；空载荷 → `_EMPTY_PAYLOAD_NOTE`；能解析但零 datum 的分支也从"裸错误"改为带明确 note |
| 6 | 可解释性 | `run_log.py`、`supervisor.py` | `plan` 事件新增 `prefix_keys`（代码注入的 key）与 `unexecutable_keys`（被剔除的"必然跳过"step），使"计划与实际执行不一致"永远可解释 |

`is_market_level_question()` 三个条件同时满足才注入：`domain == "a_share"`、
`task ∈ MARKET_TASKS`、问题里**没有** 6 位证券代码。第三个条件是有意的权衡：
问题里点名了个股就是"深挖单标的"，硬塞市场级工具只会挤掉用户真正要的调查预算
（方案 §6 要求"有个股代码的深度问题不被市场综述最低集拖慢"）。

#### 8.7.2 取舍

1. **不新增 Gateway 端点**：方案首选 `get_ashare_market_overview`，但 Market Gateway
   是外部服务（本仓库无服务端实现，`allowed_openapi.json` 里也没有该路径），
   新增一个永远 404 的工具比不新增更糟。改走过渡方案，并把接缝留在
   `MARKET_SUMMARY_MINIMUM` 的第一条（未来只换一行）。
2. **前缀不含 `limit_up_pool`**：涨停池明细是高成本、大体积数据，方案明确它只在
   "需要点名个股 / 比较板块密度 / Critic 点名缺口"时调用，属 planner 的自由裁量，
   因此不进最低证据集（测试 `test_market_question_injects_minimum_evidence_set_before_llm_steps` 断言这一点）。
3. **大盘基准用 `quote(000300)` 而不是 `overview`**：`000300`（沪深300）在
   `stock_codes.INDEX_CODE_MAP_A_SHARE` 与 `PLANNER_PROMPT` 里都被承认是合法指数代码，
   `get_market_quotes` 对指数同样可用；而 `overview` 依赖 symbol 且语义是个股 F10。
4. **剔除"必然跳过"的 step 只在市场级问题上做**：个股路径上"LLM 计划了但问题里没有
   代码"是另一种情形（用户没给标的），在那里静默剔除会损失"提示用户补充标的"的信息；
   市场级问题的判定是确定的（代码注入的最低集已经覆盖市场维度），剔除才是安全的。
5. **`WHITELIST_NO_SYMBOL` 保留为派生别名而非删除**：它是 `analysts/base.py` 的公开类
   属性，两条既有测试（`tests/test_graph_nodes.py`、`tests/test_gateway_tool_names_321.py`）
   把它当作契约。改成从 `ALL_TOOLS` 派生的 `frozenset` 后，名字与类型都还在，
   但"哪 17 个工具无需 symbol"不再有第二份真相。
6. **`requires_symbol` 与 `market_level` 分成两个字段**：`news_search` 不需要 symbol，
   但它返回的不是"整市场横截面"数据，不该被加上"无法确认是否为当日"的市场级提示；
   反过来 `internal_market_telegraph` 两者都是。混成一个标志会直接产生错误的 note。

#### 8.7.3 行为变化

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| "今天 A 股发生了什么？"（无代码） | planner 计划 `overview` 等；执行层静默跳过 → 零市场级数据 | 代码注入 5 条最低证据集于计划最前；`overview` 被剔除并记入 `unexecutable_keys`；路由首 5 条即最低集 |
| "600519 今天怎么样？"（含代码） | 不变 | 不变（不注入前缀、不剔除 step） |
| crypto / 个股研究问题 | 不变 | 不变 |
| 市场级工具返回无时间戳数据 | datum 无日期锚点，报告可能拿旧数据说"今日" | 每条 datum 带"上游未提供时间戳……不得据此断言「今日」" |
| 上游 200 + 空 body | `status=error`（无 note） | `status=error` + 明确 note |
| LLM 计划了需要 symbol 的工具但问题无代码 | 执行层 warning 后跳过（计划/执行不一致） | 市场级问题上：从计划中剔除 + run log 记账；有 symbol 参数或问题含代码时照常保留 |

#### 8.7.4 测试

新增 `tests/test_market_level_plan.py`（24 条）：注入判定的三条件、前缀置顶与参数口径、
planner 同名 step 被替换、三种"不注入"场景（含参数化）、必然跳过的 step 被剔除且 `longhu`
保留、`unexecutable_steps()` 与 analyst 守卫同源、注册表不可见时跳过、`max_steps` 截断下
前缀存活、`ToolMeta` 元数据与 `model_dump()`、白名单派生别名、市场级 datum 缺时间戳加 note、
有时间戳不加 note、非市场级工具不加 note、空载荷/零 datum 的 note、上游 note 不被覆盖、
`plan` 事件含 `prefix_keys`/`unexecutable_keys` 两个字段。

`tests/test_graph_e2e.py` 新增 `test_market_level_question_plans_and_executes_market_tools`：
真图（mock LLM + mock tools）跑"今天A股发生了什么？"，断言路由前 5 条 == 最低集、
`overview` 不在路由里、最低集的 operationId 全部出现在 findings 的 `tools_used` 里
（计划侧与执行侧同时验证，直接对应方案 §6 的端到端要求）。

按"行为按设计变了、断言要跟着更新"同步调整 7 处既有用例（**未放宽任何生产标准**）：

| 文件 | 调整 |
|---|---|
| `tests/test_b6_domain_filter.py` | `_plan()` 返回值由 2 元组变 3 元组 |
| `tests/test_graph_nodes.py` | `_build_arguments` 的 fake `meta` 补 `requires_symbol=False`（不再靠 `getattr` 兜底默认值） |
| `tests/test_graph_routing.py` | 分组用例改用含 6 位代码的个股问题（保持"三组"验证目标）；空 steps 用例改用 crypto 域（保持"route 为空"验证目标） |
| `tests/test_graph_topology.py` | 全上/降级两条用例改用非市场级 task 与含代码问题，使 route 真的为空/非空 |
| `tests/test_graph_e2e.py` | 全链路用例改用含代码问题（保持"三个 analyst 全上"的验证目标），市场级路径由新用例承担 |

#### 8.7.5 红证据与最终结果

把三处防线同时改坏（`apply_market_summary_prefix` 直接早返回 = 不注入也不剔除、
`unexecutable_steps` 结果置空、`_market_level_datum_note` 结果置 `None`），
`pytest tests/test_market_level_plan.py tests/test_graph_e2e.py -q` →
**8 failed, 20 passed**，失败正是：

- `test_market_question_injects_minimum_evidence_set_before_llm_steps`
- `test_planner_quote_step_is_replaced_by_the_code_owned_benchmark_symbol`
- `test_steps_needing_a_symbol_are_dropped_instead_of_silently_skipped`
- `test_prefix_key_invisible_in_registry_is_skipped`
- `test_prefix_survives_max_steps_truncation`
- `test_market_level_datum_without_timestamp_carries_an_explicit_note`
- `test_plan_event_records_prefix_keys_and_unexecutable_keys`
- `test_market_level_question_plans_and_executes_market_tools`（图级：计划/执行都拿不到市场级数据）

随后从备份完整还原并按 sha256 校验：

- `sha256(app/graph/market_plan.py) = 4C31C8EF48B0925C40D193835D2D5D6DF4E2F8CCBB5DF1EC4BC2A654F6AB6907`
- `sha256(app/gateway/normalizer.py) = 3F75B2829411E6AEE8D741A33288D6DDC0D6A42A844BC5E9A7EE47DADA8296DB`

最终验证：

- `pytest tests -q` → **852 passed, 1 warning**（阶段 3 改动前 827，本轮 +25 条用例）；
- `ruff check app tests` → **All checks passed**。

### 8.8 阶段 4 实施记录：Reasoning 与 Critic 的可追溯性

#### 8.8.1 落地内容

| 方案 §4 阶段 4 要求 | 落地位置 |
|---|---|
| Reasoning ① 修订上下文传完整结构化 `AuditIssue`，而不是仅字符串 claims | `app/graph/nodes/reasoning.py`：`__call__` 读出 `critique["issues"]` 并交给 `_build_revision_context(unsupported_claims, issues)`；新增模块级 `_issue_field` 与 `_format_issue_for_revision`，逐条渲染 `[{severity}/{kind}] claim`、审计理由、`action` → 必须动作（`_ACTION_INSTRUCTIONS`）、点名数据、数据量要求（`required_coverage` 原样 JSON） |
| Reasoning ② prompt 约束：公司名必须来自证据；比较/因果词必须引用证据；无证据删除或写“无法确认”；区分事实/推断/单源 | `app/agent/prompts.py`（`REASONING_PROMPT`）：新增 `claims` 规格段 + 三条硬约束（不得用相近公司名替代、无校验源不得断言“不存在/未上市”、单源消息必须写明来源与时间） |
| Reasoning ③ claim—evidence 映射 | `app/models/response.py`：`ClaimType` / `ClaimEvidence`，`MarketIntelligence.claims`；`app/research/reasoning.py`：`_parse_claims`（形状错丢弃、`claim_type` 非法→`other`） |
| Reasoning ④ 代码校验引用的 evidence id 必须存在，否则移除 claim 或降级报告 | `app/research/reasoning.py`：`_validate_claims(claims, valid_ids)` → 剥离不存在的 id 记入 `evidence_violations`；引用被剥空的 claim 整条移除；有 violation 时 `confidence="low"` + `data_caveats` 说明 + `logger.warning` |
| Critic ① `_format_report_for_review` 覆盖全部用户可见字段，不再无标记截断 | `app/graph/nodes/critic.py`：重写为返回 `(文本, 截断记录)`；覆盖 标题/置信度/市场状态/综合状态/发生了什么/原因分析/强势方向/`what_changed`/`what_matters`/风险反证/`data_caveats` + 代码级校验结论 + claim 映射 + 报告自带证据 id；单字段上限 `_MAX_REVIEW_FIELD_CHARS = 2000`，超出即标注 `…[已截断：仅展示前 N 字，共 M 字]` 并记入 `truncations` |
| Critic ② `_format_evidence_for_review` 以报告引用的 evidence id 优先 | 同上：`_referenced_evidence_ids` + 三段式输出（引用优先给完整 datum / 其余摘要最多 60 条 / 原始 normalized 摘要最多 20 工具 × 10 条），账本中不存在的引用写 `!! {id}: 不在证据账本中（该引用无法核实）` |
| Critic ③ 截断必须向 Critic 明示且写入 telemetry | 同上：`_has_truncation` / `_truncation_summary` + prompt 里的 `=== 审查上下文截断说明 ===`（含“**未展示的内容不能据此判定为「无证据」**”）；`context_truncation` 经 `run_log.log_critic(..., context_truncation=...)` 进 `critic` 事件 |
| Critic ④ 实体不存在/名称不一致优先用本地映射或工具实体校验，无校验源只标“未验证” | `app/research/entity_check.py`（新）+ `app/graph/nodes/critic.py::_code_level_issues`：`unverified_entities` 中命中“不存在/未上市”断言的 → `AuditIssue(kind="invalid_entity", severity="high", action="remove_or_qualify")` |
| （上述两处代码级结论如何阻止 pass） | `critic.py::__call__` 把 `_code_level_issues(report)` 并入 `issues` → `derive_verdict_from_issues` 得 `revise` → `resolve_conflicts` 取更保守一侧；**不写 `errors`**（审计未通过不是运行失败） |

#### 8.8.2 取舍

1. **`claims` 形状错一律丢弃、不抛 `ValidationError`**：阶段 1 已经吃过“一个字段毁掉整份报告”
   的亏（T20）。claim 是**附加**可追溯信息，形状错时丢该条并保持报告可用，比让整次调查 502 更合理。
2. **报告降级写 `confidence="low"` + `data_caveats`，而不是把 `evidence_violations` 塞进 `errors`**：
   `errors` 的语义是运行/审计器错误；伪造引用是**报告的**问题，应由 Critic 强制 `revise`（且确实拦住了 pass）。
3. **实体校验只做“有校验源 / 无校验源”两分，不做“公司是否真存在”的判决**：本仓库没有权威名称库，
   任何“这公司不存在”的结论都超出本地数据能支持的范围。因此只把“无校验源”记下来，
   禁止报告把“我没查到”说成“它不存在”——**不放宽，也不越权**。
4. **无校验源实体本身不算 issue**：名单只有 23 个股票名，真实公司大量不在其中。若“未验证”即判 issue，
   会把正常报告全部打成 `revise`（审计噪声 → 用户失去对 `delivered` 的信任）。只有**基于无校验源实体
   断言“不存在/未上市”**才升格为代码级 issue。
5. **截断上限取 2000 字（旧实现 200 字）而不是“不截断”**：Critic 的上下文预算有限，全量喂入会让
   长报告把证据挤出窗口。真正的修法是“截断必须可见 + 未展示不得当作无证据”，而不是提高上限。
6. **`_format_evidence_for_review` 保留旧的 normalized 摘要视图**：报告引用的证据优先只解决
   “这句论断引用的那条看不到”，Critic 仍需要一眼扫过全部执行产出来判断“缺口是否值得再拉一次”。

#### 8.8.3 行为变化

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| 报告引用 `technical-999`（账本里没有） | 无人核对；Critic 可能判 `pass` | 引用被剥离、`evidence_violations` 记录、`confidence` 降为 `low`、`data_caveats` 明说；Critic 收到代码级 issue → `revise`，**不可能 pass** |
| 报告对“行云科技”断言“不存在/未上市” | 无校验，可能作为结论交付 | `unverified_entities` 记录 + 命中断言 → `invalid_entity` issue → `revise`；prompt 要求改写为“未验证” |
| 报告写了 `what_changed` / `what_matters` / `data_caveats` | Critic 看不到这些字段（等于不存在） | 全部进入审查上下文，逐条可核对 |
| 报告字段超过 200 字 | 静默截断到 200 字，Critic 不知道有内容被丢掉 | 上限 2000 字，超出即标注“已截断：仅展示前 N 字，共 M 字”并给 Critic 一段截断说明 |
| 报告引用的那条 datum 不在前 20 工具 × 前 10 条里 | Critic 看不到，可能判“无证据” | 引用条目优先给完整 datum；确实账本里没有的显式标为“无法核实” |
| 上下文被截断 | Critic 把“没展示”读成“没证据” | prompt 明说“未展示的内容不能据此判定为「无证据」”，并写入 `critic` 事件的 `context_truncation` |

#### 8.8.4 测试

新增 `tests/test_reasoning_traceability.py`（27 条，模块 docstring 声明 mock 范围：只替换两个节点的
`client`，节点骨架与全部后处理真实运行）：

| 分组 | 用例 |
|---|---|
| claim—evidence 映射 | `test_valid_reference_is_kept`、`test_forged_reference_is_stripped_and_report_degraded`（断言 claims 清空 + `confidence=low` + data_caveats）、`test_partially_valid_reference_keeps_only_the_valid_id`、`test_claim_without_reference_is_not_a_violation`、`test_illegal_claim_type_falls_back_to_other`、`test_malformed_claims_cannot_destroy_the_whole_report` |
| 实体校验 | `test_unknown_entity_is_recorded_as_unverified`、`test_locally_known_entity_is_not_flagged`、`test_entity_backed_by_evidence_instrument_is_not_flagged`、`test_two_entities_in_one_sentence_are_both_extracted`、`test_check_report_entities_marks_the_unverified_one`、`test_nonexistence_assertion_is_detected_next_to_the_entity`、`test_nonexistence_assertion_is_not_invented_far_away`、`test_shorter_name_inside_a_longer_one_is_dropped` |
| Critic 上下文 | `test_every_user_visible_field_is_reviewed`、`test_missing_claim_mapping_is_called_out`、`test_truncation_is_marked_on_the_field`、`test_code_level_findings_are_shown_to_the_critic`、`test_referenced_evidence_gets_full_datum_first`、`test_reference_missing_from_ledger_is_flagged`、`test_omitted_content_is_never_read_as_absent` |
| 代码级 issue | `test_forged_reference_becomes_a_high_severity_issue`、`test_nonexistence_claim_about_unverified_entity_becomes_an_issue`、`test_verified_entity_mention_is_not_an_issue` |
| 端到端 | `test_forged_reference_cannot_pass_even_if_the_model_says_pass`（fake LLM **故意返回 `verdict=pass`**，仍必须判非 pass 且不写 `errors`）、`test_clean_report_can_still_pass`（反向控制：pass 快路径没被堵死）、`test_truncation_is_disclosed_to_the_model_and_the_telemetry` |

无需调整任何既有用例（阶段 4 是**新增**校验，不改变既有输入下的判决）。

#### 8.8.5 红证据与最终结果

把三处防线同时改坏（`_validate_claims` 调用退化为 `_parse_claims` + `violations = []`、
`nonexistence_assertions` 直接返回 `[]`、`_code_level_issues` 直接返回 `[]`），
`pytest tests/test_reasoning_traceability.py -q` → **6 failed, 21 passed**，失败正是：

- `test_forged_reference_is_stripped_and_report_degraded`
- `test_partially_valid_reference_keeps_only_the_valid_id`
- `test_nonexistence_assertion_is_detected_next_to_the_entity`
- `test_forged_reference_becomes_a_high_severity_issue`
- `test_nonexistence_claim_about_unverified_entity_becomes_an_issue`
- `test_forged_reference_cannot_pass_even_if_the_model_says_pass`（`assert 'pass' != 'pass'`）

随后从备份完整还原并按 sha256 校验（`Select-String RED-EVIDENCE` 无残留）：

- `sha256(app/research/reasoning.py) = E9C81C2E891ECBAACAAE96B194170354948C49B7928A96079A3A1BA469F53453`
- `sha256(app/research/entity_check.py) = 61771C85B4CC1643C0F3B99B5BE8AF85A4E5B55458D7F7894BFDB70C3DBB3FBB`
- `sha256(app/graph/nodes/critic.py) = 0CC0F562C01D36601C6E5F45044A457648F3EC550980D5F6C1771C89BD00983B`

最终验证：

- `pytest tests -q` → **879 passed, 1 warning**（阶段 4 改动前 852，本轮 +27 条用例）；
- `ruff check app tests` → **All checks passed**。

### 8.9 阶段 6 实施记录（轮次、预算与发布策略）

阶段 6 的四项都在**不改动任何审计规则**的前提下落地：拆分的是"额度怎么算"，
预留的是"预算不够时别硬启一轮"，新增的是"把已经发生的事量化出来"，
删除的是"一个已经被证伪的充分性判据"。

#### 8.9.1 落地清单

| 阶段 6 要求 | 落地位置与做法 |
|---|---|
| ① 计数拆分：`rewrite_count` 只由 `revise → reasoning` 递增，`research_round_count` 只由 `research_more → supervisor` 递增，两者独立可配置 | `app/graph/state.py`：新增 `rewrite_count` / `research_round_count` 两个独立计数器（`revision_count` 保留为"两者之和"的兼容镜像，**路由不再读它**）；`app/config.py`：`max_rewrites=1` / `max_research_rounds=1` + 两个 `effective_*` 属性作为唯一读取入口；`app/graph/builder.py::critic_route_decision` 分别比对两个计数器与两个上限 |
| ① 增量来源必须精确（不能把 research_more 回环误记成改写） | `app/graph/nodes/reasoning.py`：`is_rewrite = state["report"] is not None and critique.verdict == "revise"` —— 只有 Critic 明确判 `revise` 的打回才算改写额度；`research_more` 之后的"补完证据重新成文"（`is_followup`）记为 0。`app/graph/nodes/supervisor.py`：只有 `state["critique"]` 存在（即回环重新规划）时才把 `research_round_count + 1` |
| ② `research_more` 预留最小剩余预算 | `app/graph/builder.py::remaining_budget_seconds(state)`（无 `budget_deadline` → `None`，不臆断"预算充足"）+ 路由分支 `remaining < research_round_min_remaining_seconds`（默认 60s）→ `finalize_audit`，原因码 `insufficient_remaining_budget` |
| ③ 初始默认 `max_rewrites=1` / `max_research_rounds=1` | `app/config.py` 两个字段默认值即 1；旧配置名 `CRITIC_MAX_REVISIONS` 保留为 `int \| None = None`，显式设置时经 `_clamp_cap` 取 `min()` —— **只能收紧、不能放宽**（老部署不会因为它而多跑轮次） |
| ④ 指标：通过率 / verdict 分布 / 耗尽率 / 降级阻断率 / 每类缺口补齐成功率 / token 与工具耗时 | 新增 `app/graph/metrics.py`（纯函数、只读 JSONL 事件流）：`summarize_run` / `aggregate_runs` / `summarize_events` / `render_report`，并带 `python -m app.graph.metrics <run_log.jsonl>` 入口。数据源补齐：`run_log.log_llm_call`（新 `llm` 事件，token 量不到写 `None` 而非 0）、`ToolResult.duration_ms` + `executions` 事件每行的 `duration_ms`（`app/graph/nodes/analysts/base.py` 实测耗时） |
| 灰度第 4 条：删除"仅按 operationId 的 `executed_keys()`"作为证据充分性判据 | `app/graph/gap_loop.py::executed_keys` 改为抛 `NotImplementedError`（保留符号让旧调用点立刻报错），充分性判据只剩 `satisfied_keys(results, requirements)` |
| 路由日志可解释"为什么提前结束" | `run_log.log_route` 新增 `reason` / `rewrite_count` / `research_round_count` / `max_rewrites` / `max_research_rounds`；原因码：`no_critique` / `critic_error` / `revise_allowed` / `rewrite_budget_exhausted` / `no_report_to_revise` / `research_budget_exhausted` / `insufficient_remaining_budget` / `research_allowed` / `audit_passed` / `unknown_verdict` |

配套文档同步：`README.md`（图注、Critic 闭环说明、配置示例）与 `docs/architecture.md`
（T4 闭环说明、配置表、流程图）中的 `critic_max_revisions=2` 口径改为 `max_rewrites` /
`max_research_rounds`，并把旧名标为"兼容、只能收紧"。

#### 8.9.2 取舍

1. **保留 `CRITIC_MAX_REVISIONS` 但降级为"只能收紧"的兼容字段**，而不是直接删掉：
   删掉会让老部署的环境变量**静默失效**（配置还在、行为变了，最难查的一类问题）。
   取 `min()` 而不是"以旧字段为准"，是因为本阶段的目的正是**收紧**回环次数，
   兼容路径绝不能在无人察觉的情况下把上限放大——这与 §1.2"不靠增大 `CRITIC_MAX_REVISIONS` 根治"一致。
2. **`revision_count` 保留为兼容镜像，但注释明确"路由不得读它"**：它仍是有用的
   展示/日志聚合量（"这次一共回环了几轮"），但任何**判据**都不得再读它——
   那正是阶段 6 要修的根因（两类回环抢同一个额度）。
3. **改写判定必须看 `critique.verdict == "revise"`，不能只看"带了上一版报告"**：
   `research_more` 回环同样会带着上一版报告进 Reasoning（补完证据要重新成文）。
   只看 `report is not None` 会让一次补证据白吃一份改写额度——首轮实现正是这么写的，
   被 `test_research_more_loop_reinvokes_supervisor` 抓到（`revision_count` 得到 2 而非 1）。
4. **剩余预算未知时选择"允许回环"而不是"保守终止"**：`budget_deadline` 缺失
   （单元测试、直接调用节点、非图入口）时 `remaining_budget_seconds` 返回 `None`，
   路由按"预算充足"处理。理由：预算预留的目的是**避免跑不完的半轮**，
   在拿不到预算信息时用一个假想的余量去终止，会把正常调用全部变成 `blocked`——
   这是"为了安全而制造失败"，不是安全。
5. **指标里的 `None` / `0.0` 严格区分**：`None` = 没采到样本，`0.0` = 真的量到 0。
   灰度评审看"通过率 0.0%"和"无样本"是完全不同的结论，两者混淆会直接导致错误的发布决策。
   token 用量在打桩 / 兼容端点下拿不到 `usage` 时记 `None`，绝不写 0 冒充"零消耗"。
6. **缺口"补齐成功"= 最后一次 Critic 裁决不再把它当缺口**：只看"计划里有它"会把
   "计划了但工具没跑出东西"也算成补齐（这正是 §2 记录过的假覆盖）；只看第一轮裁决
   又会漏掉后面几轮才暴露的缺口。因此口径固定为"`plan` 事件的 `forced_gap_keys` 并集
   − 最后一次 `critic` 事件的仍缺集合"。
7. **`executed_keys()` 选择"抛异常"而不是"直接删掉函数"**：删除会让任何残留调用点
   在运行时变成 `AttributeError` / `ImportError`（还得靠运气才发现），抛
   `NotImplementedError` 带明确替代方案，能在测试里立刻定位。

#### 8.9.3 行为变化

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| 一次 `revise` + 一次 `research_more` | 共用一个 `revision_count`，改写轮次会被补研究挤掉（或反之） | 各自计数、各自最多 1 轮，互不挤占 |
| `research_more` 回环后 Reasoning 重新成文 | 记一次改写消耗 | 记 0 次改写（`is_followup`），研究额度才 +1 |
| 剩余预算不足以跑完一轮工具 + reasoning + critic | 仍然启动回环，跑不完 → 预算耗尽 | 直接进 `finalize_audit`（`insufficient_remaining_budget`），落 `blocked`/`failed` 而不是硬撑 |
| `CRITIC_MAX_REVISIONS=5`（旧部署） | 改写上限 5 | 改写上限仍是 1（`min(1, 5)`）——旧变量只能收紧 |
| `CRITIC_MAX_REVISIONS=0` | 无改写轮次 | 仍为 0（收紧生效） |
| 想知道"这次为什么没通过" | 只有 verdict 与轮次，耗尽原因不可区分 | `route` 事件的 `reason` 明确区分：改写额度耗尽 / 研究额度耗尽 / 剩余预算不足 / 无正文可改写 / 审计器错误 |
| 想知道灰度指标 | 只能人工翻日志 | `python -m app.graph.metrics run_log.jsonl` 输出通过率、verdict 分布、耗尽率、降级阻断率、按 key 的缺口补齐率、token 与工具耗时 |
| 估算"缺口补了有没有用" | 只能看"这个工具跑过没有" | 按 key 统计"被代码级补进计划 → 最后一次裁决不再算缺口"的比例；无样本时显示"无样本"而不是 0% |

#### 8.9.4 测试

新增 `tests/test_research_rounds.py`（11 条）与 `tests/test_metrics.py`（11 条），
两者都在模块 docstring 里声明 mock 范围（前者只打桩 `node._plan` / `node._engine`
与 `client`，不替换任何节点的 `__call__`；后者是纯函数级，只喂人造事件）。

| 分组 | 用例 |
|---|---|
| 配置（`test_research_rounds.py`） | `test_default_caps_are_one_round_per_path`、`test_legacy_critic_max_revisions_can_only_tighten`（含 `5→1`、`0→0`、`min` 语义、负值归零）、`test_unknown_legacy_env_is_ignored_but_new_keys_work` |
| 计数拆分（同上） | `test_supervisor_counts_research_rounds_only_on_replan`（首次规划不计数；回环 +1 且 `revision_count` 为两者之和）、`test_reasoning_counts_rewrite_only_for_revise`（revise=1 / research_more 跟进=0 / 首次=0）、`test_revision_count_mirrors_both_paths`、`test_reasoning_state_defaults_split_counters` |
| 预算与路由（同上） | `test_route_reason_codes_are_logged`（`reason == "rewrite_budget_exhausted"` + 上限与计数器进 payload） |
| 指标数据源（同上） | `test_tool_result_records_duration_ms`、`test_executions_log_records_tool_duration`、`test_llm_call_log_separates_unmeasured_from_zero`（量到 vs 没量到） |
| 路由（既有文件补 2 条） | `tests/test_graph_topology.py`：`test_critic_route_revise_counters_do_not_share_a_budget`、`test_critic_route_research_more_reserves_minimum_remaining_budget`（低于预留线→`finalize_audit`、已超时→`finalize_audit`、充足→`supervisor`、无 `budget_deadline`→`supervisor`） |
| 旧判据删除 | `tests/test_supervisor_gap_loop.py::test_legacy_executed_keys_path_is_gone`（`executed_keys` 必须抛 `NotImplementedError`，同时确认 `resolve_tool_by_name` 反查仍可用） |
| 聚合（`test_metrics.py`） | `test_parse_lines_skips_broken_and_non_object_lines`、`test_group_by_run_keeps_order_and_marks_missing_run_id`、`test_summarize_run_blocked_case`（verdict/终态/交付/token/工具耗时/时长/缺口）、`test_gap_backfill_needs_the_last_critic_to_stop_calling_it_missing`、`test_aggregate_rates_and_distributions`、`test_rates_are_none_without_samples`、`test_gap_backfill_rate_is_per_key`、`test_unassigned_events_are_counted_not_attributed`、`test_render_report_marks_missing_samples_and_lists_keys`、`test_main_reads_jsonl_file`、`test_main_without_argument_explains_usage` |

需要同步调整的既有用例（都是"计数口径变了"的必然结果，断言强度未降低）：
`tests/test_critic_verdict.py`（`_route_state` 改带 `rewrite_count` / `research_round_count`）、
`tests/test_delivery_status.py:159`、`tests/test_graph_topology.py`（两个 `fake_reasoning`
按 `critique.verdict` 计改写；新增 `_critique_verdict` 辅助）、`tests/test_graph_e2e.py`
（`fake_reasoning_call` 同上）、`tests/test_graph_nodes.py`（stub settings 改 `max_rewrites`）、
`tests/test_audit_run_log.py`（blocked fixture 显式 `Settings(max_research_rounds=2)`，
因为该 fixture 需要两轮研究额度才能走到"补两轮仍不通过"）。

#### 8.9.5 红证据与最终结果

把四处防线同时改坏：

1. `app/graph/builder.py`：`revise` 分支改回共享额度（`rewrite_count + research_round_count < max_rewrites`），
   并删掉 `insufficient_remaining_budget` 分支；
2. `app/graph/nodes/reasoning.py`：`is_rewrite` 退化为 `report is not None`；
3. `app/graph/run_log.py`：`log_llm_call` 的 token 用 `or 0`（"没量到"写成 0）；
4. `app/graph/metrics.py`：`_rate` 分母为 0 时返回 `0.0`；`app/graph/gap_loop.py`：
   `executed_keys` 恢复成可用的旧实现。

结果：

- `pytest tests/test_metrics.py tests/test_research_rounds.py tests/test_graph_topology.py -q`
  → **6 failed, 37 passed**，失败正是：
  `test_rates_are_none_without_samples`、
  `test_render_report_marks_missing_samples_and_lists_keys`、
  `test_reasoning_counts_rewrite_only_for_revise`、
  `test_llm_call_log_separates_unmeasured_from_zero`、
  `test_critic_route_revise_counters_do_not_share_a_budget`、
  `test_critic_route_research_more_reserves_minimum_remaining_budget`；
- `pytest tests/test_supervisor_gap_loop.py::test_legacy_executed_keys_path_is_gone -q`
  → **1 failed（`DID NOT RAISE NotImplementedError`）**。

随后从 `D:\Mosaic\.tmp\revi4\` 完整还原并按 sha256 校验（备份目录已删除）：

- `sha256(app/graph/builder.py) = 9D75ADA8E6C9431FE2F2D1504AE3CA29B589DEC9F2AFC59BE980B0DC96BA4B48`
- `sha256(app/graph/metrics.py) = 5B140E3DC7E151ACA49006F1C96C8373A1196A475368C45177EF73229477DD76`
- `sha256(app/graph/run_log.py) = 7F6484420E95657A2F0A0FC7AA6D780904D17EC677070CB89056980F0262FD4C`
- `sha256(app/graph/gap_loop.py) = A8D9C54DB2855630F8B623C9E4DC62FDC194413731C806EB8023221239E1E8FB`
- `sha256(app/graph/nodes/reasoning.py) = 3906FCC71F4B1A63BD98593DE9FF19075321CFA8BA89F1DEFA1C5411A8B624ED`

最终验证（阶段 6 全部改动落地后）：

- `pytest tests -q -p no:cacheprovider` → **903 passed, 1 warning**；
- `ruff check app tests` → **All checks passed**。

#### 8.9.6 遗留（不属于本阶段代码范围）

- **灰度步骤被提前到代码里执行，顺序与 §4 的"最后再删"相反**：`executed_keys()`
  是灰度第 4 步（"最后删除旧的仅按 operationId 的 `executed_keys()` 路径"），
  本次与其余三项一起删掉了。这属于**有意偏离**：该路径已在阶段 2 被证伪
  （会把兄弟 key 的缺口误判成已满足），留着它反而给后续维护者一个"还能这么判"的暗示。
  代价是回滚需要 revert 代码（不再有配置开关），且删除发生在 shadow mode 抽样之前。
- **灰度本身没有执行**：本阶段交付的是"能算出灰度指标"的能力与"旧判据已删除"，
  §4 阶段 6 要求的"先 shadow mode 收集 ≥50 个市场综述 + ≥50 个个股问题、人工抽样
  统计错误放行率 / 错误阻断率 / 平均延迟"需要真实流量与人工评审，仓库内无法完成。
- **`blocked` 的前端提示与"停止持久化"上线开关**：持久化侧已在阶段 5 落地
  （非 `verified` 不落库），前端提示仍是既有实现，未按灰度节奏分阶段开启。
- **token 用量的覆盖度取决于上游**：打桩客户端与部分兼容端点不返回 `usage`，
  此时指标显示 `None`（"没采到"），不会伪造 0。
