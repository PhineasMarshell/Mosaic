# 证据真实性与审计可用性修复方案

- **状态：** P0-A 已完成并验收；P0-B、P0-C 待实现
- **实现对象：** 子 agent
- **验收对象：** 主 agent
- **适用范围：** A 股市场综述、个股研究，以及 Reasoning → Critic → FinalizeAudit 交付链路
- **关联事故：** run_id=fb1abaf863bb（见 incident-2026-10-10-ashare-stream-fix.md）

## 1. 目标和原则

这次修复的目标是让系统同时满足两件事：

1. 只有真实、可追溯、时间和标的匹配的证据，才能支撑事实结论。
2. 证据不足时，系统要明确说明缺口并尽量交付已经核实的内容，不能因为实体校验误报或上游工具故障，把所有研究都变成“没有报告”。

修复方向如下：

- 优先修证据真实性和证据链完整性。
- 不通过降低 Critic 的证据要求来提高通过率。
- “未验证”表示当前没有建立校验链，不能直接解释为“股票不存在”或“虚假”。
- “实体存在性”“名称与代码匹配”“当天行情是否成立”是三个不同判定，必须分开。
- Critic 仍然阻断伪造 evidence ID、无证据的当前事实、未经支持的因果和全市场绝对化结论。
- 任何无法解释的阻断都必须带结构化原因；不能只返回空报告。

当前工作区有一批未提交的网关、回环、证据别名和终态修复。实现 agent 不得 reset、revert 或覆盖这些已有修改，只在其上补充本方案要求的改动。

## 2. 当前运行链路

当前图的主要路径是：

    Supervisor
      → Technical / Fundamental / Moneyflow / News / Sentiment
      → Gate
      → Reasoning
      → Critic
      → pass: END
      → revise: Reasoning
      → research_more: Supervisor
      → 其他非 pass: FinalizeAudit

各节点当前职责和风险：

| 节点 | 当前行为 | 风险 |
| --- | --- | --- |
| Supervisor | LLM 规划工具，代码补充部分最低证据集 | 计划参数、市场日期和实际网关契约可能不一致 |
| Analyst | 执行工具并生成 ToolResult | 超时、缓存、partial 重试和参数编码可能导致没有真正拿到新数据 |
| Gate | 主要按 success/partial/error 统计 | 目前不充分检查来源、日期、标的、响应内容和实体真实性 |
| Reasoning | 根据工具结果写 MarketIntelligence，绑定 claims 与 evidence IDs | 模型可能使用不存在的 ID，或把背景知识写成当天事实 |
| Critic | LLM 审查报告与证据，并叠加代码级 issue | 当前“静态股票表 + 后缀抽取 + 无校验源”会误伤真实股票和题材 |
| FinalizeAudit | 非 pass 默认 blocked，清空报告正文 | 一个局部缺口会导致全部内容不可见，用户只能得到“没有结论” |

证据判定必须从“有没有成功工具”升级为“这条证据是否能够支持这条论断”。

## 3. 已确认的问题

### 3.1 实体校验误把“未验证”当成“实体可疑”

app/gateway/stock_codes.py 只有少量常见股票和指数名称，并不是完整 A 股证券名录。

app/research/entity_check.py 当前存在以下问题：

- local_entity_index() 不能覆盖全 A 股；
- evidence_entity_names() 只接收部分工具名称，涨停池、板块、搜索等工具可能被排除；
- 只有代码、没有名称时，无法把代码和报告中的真实公司名关联；
- 失败请求的 instrument 不能作为实体存在性证据；
- 行业或题材词会被公司后缀规则识别成“公司”；
- 当前工作区的 _code_level_issues() 对未验证实体的“上涨/领涨/涨停”等正向句子生成高严重度 invalid_entity，这会把“真实但尚未建立校验链”直接变成阻断。

必须明确：

- 查不到名称 ≠ 股票不存在；
- 没有本地映射 ≠ 虚假股票；
- 有行情证据 ≠ 该句所有事件都已被证明；
- 板块题材 ≠ 上市公司实体。

### 3.2 Evidence Gate 对“真实”检查不足

Gate 目前把任一 success/partial 结果视为有证据，但没有统一检查：

- 是否是成功响应，而不是错误响应携带的请求参数；
- 返回证据是否有实际 datum；
- 标的是否与论断一致；
- 日期是否对应用户问的交易日；
- 来源是否可识别；
- partial 是否足以支持该论断；
- 市场级数据是否被误用于单只股票。

### 3.3 “今天 A 股”缺少休市和最近交易日语义

当前市场级最低证据集仍然要求“当日”行情。2026-10-10 是周六，A 股休市；系统应回答：

> 今日 A 股休市，以下使用最近交易日数据，并明确数据日期。

不能在休市日不断要求不存在的“今日涨跌家数”，再把缺口交给 Critic。

### 3.4 已记录的 A 股阻断不是单一 Critic 问题

fb1abaf863bb 的记录包含：

- get_market_quotes 的 MCP symbols 类型不符合网关 schema；
- 情绪和涨停统计工具超时；
- list_limit_up_sectors 返回 partial 后，补查没有真正刷新；
- Reasoning/Critic 使用了不存在的 evidence ID；
- 报告包含当前 evidence 未支持的外部事实和实体结论；
- 预算耗尽后进入 research_exhausted + blocked。

因此修复必须同时覆盖证据真实性、网关边界、超时、补查和审计输出，不能只调 Critic 轮数。

### 3.5 运行日志不足以回放每次用户失败

当前阻断运行不会持久化研究正文，仓库也没有完整线上 JSONL。因此不能把某一次“真实股票被判假”归因到具体股票，除非有对应 run_id 的 critic、route、finalize 事件。

本方案要求后续所有阻断至少保留脱敏的结构化审计事件，便于区分：

- 实体校验误报；
- 证据缺失；
- 工具失败；
- 预算耗尽；
- Critic 自身错误。

## 4. 目标判定模型

实现时使用三层判定，不得合并成一个布尔值。

### 4.1 实体存在性

建议新增 app/research/entity_registry.py，或在现有 entity_check.py 中实现等价接口。

实体状态至少包含：

- verified：有权威证券主数据、成功行情/F10/搜索结果或明确的 code↔name 证据；
- mentioned_only：证据文本提到该名称，但没有足够的证券主数据确认；
- unverified：当前研究没有建立对应校验链；
- contradicted：有权威来源明确表明名称、代码或市场状态不一致。

不存在权威反证时，系统不得把 unverified 转成“虚假”“不存在”。

### 4.2 证据质量

每条可用于审计的证据应能回答：

- 来自哪个工具和来源；
- 请求针对哪个标的；
- 返回的标的是什么；
- 数据时间是什么；
- 数据是否完整；
- 是否是错误、空结果或 partial；
- 是否可被当前论断引用。

建议在现有 Evidence / NormalizedDatum 上补充或统一以下字段：

- retrieved_at
- as_of_date
- instrument
- instrument_name
- authority
- freshness
- coverage

如果暂时不能扩展模型，至少在实体校验和 Critic 上下文中保留这些信息，不得只看 source_tool 字符串。

### 4.3 论断状态

每条 claim 只能落到以下状态之一：

- supported：引用存在，标的和日期匹配，证据质量足够；
- supported_with_caveat：证据可用，但 partial、单源或日期范围需要限定；
- missing_evidence：需要补充工具或查询，不能当成已证实事实；
- contradicted：证据与论断冲突；
- invalid_reference：引用不存在的 evidence ID。

Critic 路由规则：

- missing_evidence → research_more；
- 可以通过删句或降低措辞解决 → revise；
- contradicted 或 invalid_reference → 高严重度阻断；
- 没有 issue 才能 pass；
- 审计器无法解析 → error，不能伪造 pass。

## 5. 实施任务

### P0-A：修复实体真实性和误报

涉及文件：

- app/research/entity_check.py
- app/graph/nodes/critic.py
- 新增 app/research/entity_registry.py（如需要）
- app/gateway/stock_codes.py（保留常见映射，但不得再被当成全量名单）
- tests/test_reasoning_traceability.py
- 新增 tests/test_entity_registry.py

要求：

1. 接入完整 A 股 code↔name 数据源，带 as_of 和市场/状态信息。数据源不可用时记录“未验证”，不得猜测。
2. 先用结构化 code/name 证据匹配实体，再使用文本抽取。
3. 成功或 partial 的实际返回数据可以用于实体存在性；status=error 的请求参数不能作为校验源。
4. 扩展可识别工具范围，至少覆盖 quote、klines、F10、finance、news、telegraph、search、limit-up、sector、longhu、abnormal 等工具。
5. 区分“公司实体”和“题材/行业/板块”。至少加入常见题材词排除机制；更优先使用工具的结构化字段判断。
6. unverified_entities 只表示“当前未建立校验链”，本身不生成 issue。
7. 删除或改写当前 _code_level_issues() 中“未验证实体只要出现上涨/领涨/涨停就生成 invalid_entity”的规则：
   - 有具体 claim 但无标的证据 → missing_evidence + research_more；
   - 只是“尚未验证、无法确认”说明 → 不生成 issue；
   - 明确断言“某股票不存在/未上市”但没有权威反证 → 只能要求改成“未验证”，不能声称该股票是假的；
   - 只有权威数据明确冲突时才允许 invalid_entity。
8. invalid_entity issue 必须包含验证来源或冲突来源，不能只写“本地名单没有它”。
9. 不能用一个全篇“可能/无法确认”来豁免另一句明确断言；限定语必须按句、按实体判断。

### P0-B：建立证据真实性 Gate

涉及文件：

- app/agent/evidence_gate.py
- app/research/evidence.py
- app/models/evidence.py
- app/models/market.py
- 新增 tests/test_evidence_authenticity.py

要求：

1. status=error、空 normalized、只有请求参数的结果不能计入实体校验或有效证据。
2. 对每条证据校验：
   - source/tool_key 存在；
   - status 允许；
   - instrument 与 claim 的代码或名称匹配；
   - 时间戳和 as_of_date 与用户问题一致，或明确标为历史/最近交易日；
   - partial 只能支持有限观察；
   - market-level 数据不能支撑单股结论。
3. 将“工具有结果”和“这条结果支撑该 claim”分开记录。
4. Gate 的输出至少增加：
   - valid_evidence_count
   - invalid_evidence_count
   - stale_evidence
   - unmatched_instruments
   - partial_only
   - reason_codes
5. Gate 仍然允许 Reasoning 继续生成带 caveat 的草稿，但 Critic 必须看到这些代码级限制。

### P0-C：加入交易日和 as-of 语义

涉及文件：

- app/graph/market_plan.py
- app/models/research.py
- app/graph/state.py
- 新增 app/research/trading_calendar.py
- app/agent/prompts.py
- tests/test_trading_day_semantics.py

要求：

1. 对“今天 A 股发生了什么”计算 requested_date、as_of_date 和 market_closed。
2. 周末和已知休市日不要求不存在的“当日盘面”；自动选择最近交易日，或在无法取得最近交易日时明确返回“暂无可用交易日数据”。
3. 报告标题、what_happened 和 data_caveats 明确写出：
   - 今日是否休市；
   - 使用哪一个交易日；
   - 数据是否完整。
4. Critic 的同日覆盖规则改为检查 as_of_date，不能把周六当作必须存在的交易日。
5. “今天”只表示用户语义；证据日期必须以工具返回的时间和 as_of_date 为准。

### P0-D：完成已存在的网关和回环修复验收

涉及文件：

- app/gateway/arguments.py
- app/gateway/mcp_client.py
- app/gateway/http_client.py
- app/graph/gap_loop.py
- app/graph/nodes/supervisor.py
- app/graph/nodes/analysts/base.py
- app/research/reasoning.py
- app/graph/nodes/critic.py

要求：

1. MCP 根据实际 schema 把 canonical symbols: list[str] 编成 string 或 array；内部 state 保持 canonical 形状。
2. HTTP/MCP 每次请求都受整次研究 deadline 限制，不能因分块响应持续到达而超过总预算。
3. partial 或覆盖不足的结果必须允许 refresh 重拉；不能被普通缓存或“已执行过”判据吞掉。
4. Reasoning/Critic 只向模型展示短 alias，代码校验前解析回真实 evidence ID；未知 alias 继续阻断。
5. route、finalize、delivery 使用同一个结构化 blocked reason。
6. 所有网关日志继续脱敏，不能把用户查询原文写入日志。

### P1-E：审计策略调整为“证据优先，标准不降低”

涉及文件：

- app/graph/nodes/critic.py
- app/agent/prompts.py
- app/graph/nodes/finalize.py
- app/models/response.py
- 相关审计测试

要求：

1. Critic prompt 明确：
   - 只审查当前 evidence 能否支持当前 claim；
   - 不能因为本地静态名单没有某名称就判股票虚假；
   - 不能把未展示内容当成无证据；
   - 需要新数据的问题必须输出 research_more；
   - 只能通过删改解决的问题才输出 revise。
2. 代码级 issue 必须优先于模型自由发挥，但只使用已验证的代码事实。
3. 保留以下硬门禁：
   - 不存在的 evidence ID；
   - 无证据的当前事实；
   - 无直接证据的强因果/资金流向；
   - partial 数据推出完整市场结论；
   - 权威数据与报告冲突。
4. 不通过增大 max_rewrites、max_research_rounds 或删除 Critic 规则解决问题。
5. 在核心 P0 修复完成前，非 pass 仍可保持 blocked，但 delivery_reason 必须可读且包含未解决 issue。
6. P0 稳定后实现受控 degraded 交付：
   - 只保留有真实 evidence 引用的 claims；
   - 删除或改写未支持的段落；
   - 明确列出缺口、日期和 partial 限制；
   - 含 invalid reference、权威冲突、审计器 error 或高风险未解决 issue 时仍然 blocked；
   - degraded 不持久化为 verified 研究记忆。

## 6. 测试和验收

### 6.1 必须新增的实体回归

至少覆盖：

1. “广合科技今日涨停”由成功的涨停池证据提供名称时，不得进入 unverified_entities。
2. “胜宏科技”只有成功 quote 的代码和完整 code↔name 主数据时，名称代码匹配成功。
3. status=error 且带请求 instrument 的结果不能证明实体存在。
4. “商业航天今日走强”来自 list_limit_up_sectors 时，应识别为题材，不应作为公司实体。
5. “寒武纪”这类没有公司后缀的真实股票，不能被文本后缀规则漏掉；如果没有证据，状态应是未验证/缺证据，而不是虚假。
6. “行云科技尚未验证，无法确认其行情”不产生阻断 issue。
7. “行云科技今日领涨”但没有对应证据时，产生 missing_evidence/research_more 或可解释的限定要求，不能生成“虚假股票”的结论。
8. 明确有权威来源表明代码与名称不一致时，才产生 invalid_entity。

### 6.2 必须新增的证据和日期回归

至少覆盖：

1. 空响应、error 响应、只有请求参数的 result 不计入有效证据。
2. partial 数据不能支撑“全市场全部上涨”“最强主线”等绝对结论。
3. 2026-10-10（周六）市场综述标记 market_closed=true，使用最近交易日或明确无数据。
4. 报告中所有日期与 evidence 的 as_of_date 一致。
5. claim 引用错误标的、错误日期或错误域时不能 pass。

### 6.3 必须保留的安全回归

现有测试不能回退：

- forged evidence ID 永远不能 pass；
- Critic 非法 JSON / 非法 issues 必须是 error；
- 无证据的 ASML、地方补贴、C50 信贷等外部当前事实不能 pass；
- partial 涨停池不能支持全市场比较；
- 非 verified 结果不进入 verified 持久化；
- route/finalize/delivery 的 blocked reason 一致；
- MCP/HTTP 日志不泄露用户查询。

### 6.4 事故回放

使用 tests/fixtures/incident_ashare_stream_fix.json 做脱敏回放，验收以下结果：

- quote 参数边界不再因 MCP string/array 误配失败；
- timeout 不超过整次研究 deadline；
- partial 补查会真实 refresh；
- alias 能解析合法引用；
- 无据外部事实仍被标记；
- 如果仍有不可补齐缺口，返回 blocked，但 delivery_reason 必须明确指出缺口；
- P0 实体修复后，不能再因真实 A 股名称仅不在静态名单而生成 invalid_entity。

## 7. 主 agent 验收清单

子 agent 完成后，主 agent 按以下顺序验收：

1. 先读 diff，确认没有通过降低 Critic 标准、放宽 evidence ID、跳过日期校验或删除安全门禁来“修复通过率”。
2. 运行实体真实性回归，重点检查真实表外股票、题材词、error instrument 和 code-only evidence。
3. 运行交易日/as-of 回归，检查周末和最近交易日。
4. 运行事故回放和网关边界测试。
5. 运行完整测试集：
   .venv/Scripts/python.exe -m pytest -q tests -p no:cacheprovider --tb=short
6. 对修改文件运行 Ruff；基线已有 Ruff 错误必须单独列出，不能把它们伪装成新修复通过。
7. 检查至少一条 pass、一条 research_more、一条 revise、一条 blocked、一条 error 的结构化日志。
8. 检查阻断运行是否保留脱敏的 run_id、critic issues、route reason 和 finalize reason。
9. 如果没有真实 MCP/Gateway/付费模型可用，只能声明“本地模拟验收通过，线上链路未验证”。
10. 最终验收结论必须回答：
    - 真实 A 股是否还会因不在静态名单而被判虚假；
    - 题材词是否还会被识别为公司；
    - 错误请求参数是否还能被当成实体证据；
    - 周末“今天 A 股”是否能正确说明休市；
    - 无据事实和伪造 evidence ID 是否仍会被阻断；
    - 用户是否至少能得到可解释的部分结果或明确缺口。

## 8. 完成标准

只有满足以下条件，才可标记完成：

- 实体存在性、名称代码匹配和当天行情证据已经分层；
- 未验证实体不再自动被描述为虚假；
- 真实表外 A 股、涨停池名称和 code-only quote 有可追溯校验路径；
- 题材/行业词不再被代码级规则当成公司；
- Evidence Gate 检查来源、状态、标的、日期和完整性；
- 周末/休市查询使用明确的最近交易日语义；
- MCP/HTTP、deadline、partial refresh、evidence alias 和 blocked reason 回归通过；
- Critic 的安全门禁仍然有效；
- 至少通过本地脱敏事故回放；
- 线上真实 Gateway/LLM 若未连接，验收报告明确注明未验证；
- 未经 verified 审计的结果不会被当作 verified 研究记忆持久化。

## 9. 交给实现子 agent 的执行顺序

1. 先写并运行 P0-A 实体真实性测试，再实现实体注册和校验分层。
2. 再实现 P0-B Evidence Gate 质量字段与代码级约束。
3. 实现 P0-C 交易日和 as-of 语义。
4. 对照 P0-D 验收已有网关、超时、回环和 alias 改动。
5. 最后做 P1-E Critic/Finalize 调整；先保持安全 blocked，再单独实现受控 degraded。
6. 每一步都保留结构化日志和失败原因，不用提高回环次数掩盖数据链路缺陷。
