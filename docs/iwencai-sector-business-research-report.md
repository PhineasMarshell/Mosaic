# 同花顺板块筛选、公司经营与机构研究接入报告

日期：2026-10-12。基线仍为 `main@a938dd7`，保留此前与同时进行的工作区修改。未执行 reset、revert、commit、push。

本批三个官方 Skill 已安装并接入 Mosaic，registry 现为15个 Skill。真实 HTTPS 数据链路均可用，返回结构化数据，但三者都存在 partial/覆盖限制；机构研究最新响应的研报发布日仍不能确认。因此不是稳定主数据源或受控 fallback 的生产验收，继续 `production_ready=false`。Skill 成功不等于 Critic/Finalize 通过，也不证明 MCP TLS/上游修复。

## 安装与真实调用契约

| Skill | 版本 | Mosaic key / operationId | 能力 / 分析员 |
|---|---|---|---|
| hithink-sector-selector | 1.0.0 | iwencai_sector_selector / internal_iwencai_sector_selector | sector_screening / technical |
| hithink-business-query | 1.0.0 | iwencai_business / internal_iwencai_business_query | company_operations / fundamental |
| hithink-insresearch-query | 1.0.0 | iwencai_insresearch / internal_iwencai_insresearch_query | institutional_opinions / fundamental |

真实形态均为官方 HTTP/API：`POST https://openapi.iwencai.com/v1/query2data`，不依赖 iiix/MCP。官方 CLI 脚本只是同一 HTTPS 接口的本地包装，不是独立本地数据源或仅提示词能力。

输入为 query（必填）、page、limit、is_cache、expand_index；Mosaic保持既有边界 query≤2000字符、page=1..10000、limit=1..20、开关为布尔，HTTP按官方规范传字符串。输出为 datas/code_count/chunks_info 和动态 columns；datas 中名称/文本不自动证明价格、筛选条件、因果或评级预测已实现。

每次请求使用 Bearer、对应 Skill ID、1.0.0版本、normal/retry、插件none、独立64hex trace。官方CLI默认超时30秒；Mosaic使用既有deadline、有限重试及脱敏错误格式。鉴权、限流、timeout、HTTP错误、业务错误、结构错误分别记录 reason_codes；失败/空响应/参数错误保持 status=error、normalized=[]、datum_count=0、零evidence。

复用已安装的 CLI 2026.3.13：`C:\Users\Phineas\.iwencai-skillhub`；三个 Skill 安装于 `C:\Users\Phineas\.agents\skills` 对应同名目录。PowerShell profile 已加载正确 BASE_URL 与 API_KEY；凭据沿用当前Windows用户DPAPI加密配置，未向profile写入明文密钥。新Skill下一轮可发现；正在运行的Mosaic进程需重启以加载代码改动。

下载使用官方SkillHub CLI元数据中的同花顺商店地址的HTTPS版本。首次默认User-Agent返回403；按CLI User-Agent重试成功，TLS校验保持开启。官方包在安装前核对了SKILL.md及scripts/cli.py，再由官方CLI从已下载包安装，没有执行包内查询脚本。官方文档建议空结果时放宽/简化条件，与用户要求冲突，Mosaic未采用该建议。

| 官方安装包 | SHA256 |
|---|---|
| hithink-sector-selector.zip | 9fb2757fa9d8739ce9b10e477df5fda3330945d56c9f65a4d0ca4457b70be18c |
| hithink-business-query.zip | 42e10db680a2cf0c8e769e6b9cdc4d0f6c46b4c7a9d3230295cfb517cf865df3 |
| hithink-insresearch-query.zip | f1ed6e3f828148f58ddacffd4fe36dd376d9605d30f2b814fa254232f342f8bb |

## 真实取证与可用性

记录：`memory/iwencai-sector-business-research-probes.jsonl`。每次均通过真实 ToolRuntime，实际 `refresh=True`、cache_hit=false，query2data发送is_cache=0；不能由此证明上游缓存实现。

这些是工具探针，Critic、Finalize、Delivery明确为 `not_invoked_tool_probe`，route为direct_skill_tool_probe，persisted=false表示研究报告未写入记忆。JSONL及原始响应另存本地不构成研究交付持久化。

| Skill / 最终run_id | HTTP / ToolResult | 上游行数 / code_count | datum / evidence账本数 | 审计边界 |
|---|---|---|---|---|
| 板块筛选 / 297edfe3c142 | 200 / partial | 63 / 63 | 27 / 27 | limit=3，仅标准化前3个板块；返回881102.TI、881103.TI、881105.TI及名称，明确分类为同花顺二级行业指数；历史指标2026-10-09，最新字段无日期；全部筛选条件未独立验算 |
| 公司经营 / 16f5ade53c45 | 200 / partial | 7 / 1 | 39 / 39 | SZ300033/同花顺；产品项目名称、分类标准、业务收入/成本/利润、占比/毛利率分别关联2025-12-31；展开行不证明全部业务覆盖，code_count不是业务项目总数 |
| 机构研究 / 1fb2a0fb81b1 | 200 / partial | 40000 / 1 | 1400 / 80 | SH600519/贵州茅台；保留研究机构、研报标题/链接、预测净利润和2026-12-31预测期；标准化最多200行，证据账本最多80条；只有“公告日期”，元数据标通用交易日期，研报发布日未确认，不能支持已验证观点claim |

账本数不是已验证claim数量，包含partial或未知日期的字段。三条最终响应的原文与SHA256已保存；路径在JSONL的raw_response_path，位于 `.tmp/iwencai-p5/responses`。该目录是本地取证附件，Git忽略；清理前需保留原始响应。

历史探针5851998a0e65、45f37e306315、c26350e9e10c和6cc31cfcf109保留。其中机构研究expand_index=true返回40000行，后来仅改为expand_index=false，原query/市场/条件/limit不变，仍返回40000行。首次部分响应使用“报告日期”，后续响应使用“公告日期”，不能混用两者，也不能把报告期截止日或字段预测期作为发布日。历史经营记录尚未保留完整项目上下文的限制由后续修复，不改写历史记录。

真实上游timeout、empty、upstream error、auth failure和rate limit本批均为 `not_observed`；相应不变式使用mock回归验证。该批没有运行完整研究链路或sync/SSE交付，也没有新增MCP transport调用。

## 接入与证据边界

新增能力通过统一QUERY_TOOLS、Skill registry及THSSkillAdapter接入，Supervisor必用规则和technical/fundamental分析员可执行对应工具；没有在Evidence Gate增加三套按Skill放行的分支。板块默认TTL60秒，经营/机构研究3600秒；TTL=0可禁用本地缓存，失败refresh不恢复旧partial。

每条datum保留source/source_tool、instrument/instrument_name、as_of_date/retrieved_at、authority、coverage、completeness、partial及原响应来源链路。三种新增能力均未注册为quote/kline/market-breadth等价fallback；它们不会降低既有受控路由的日期/标的/来源/冲突准入，也不能覆盖已有权威冲突。

板块只接受上游明确行业/板块/指数身份，不从默认股票表或query补造；分类按实际字段保留，不推断申万/中信。筛选候选始终partial，记录screening_conditions_not_verified；超出limit记录upstream_page_size_exceeded。历史日期不借给“最新”字段，周末“今天”不被历史板块指标证明。

经营数值每条附带项目/交易对手上下文；generic名称不能填公司名称。报告期、公告日和合同事件日分别保留。业务字段缺失不能由默认报价替代；没有用返回行数声称业务/客户/供应商全部覆盖。

机构观点使用结构化data_kind、opinion_kind、institution_name、publication_date、target_period、report_title/report_url，预测期与发布日分开。通用claim校验阻断缺机构/发布日、错误预测年度、未注明真实机构以及将预测写为已实现利润/保证收益。目标价不能证明实际行情。无法确认实体仍是未验证，不判为虚假。

## 验证与最小后续修复

本批新增50项回归；与指数/选股及P6适配器合计98项通过。测试实际执行adapter、Runtime、Supervisor与分析员，HTTP及LLM响应使用mock；它们不证明上游长期可靠性或真实Critic/Finalize交付。初次回归有1个测试把Evidence层“unknown”预期为None，已改为分别检查NormalizedDatum的None和Evidence的unknown，未放宽准入。

完整pytest已通过1439项；Ruff、compileall、git diff --check及新增原始响应回放通过。秘密扫描检查63个工作区文件，配置密钥出现0次。新增回放检查全部7条工具状态、4份原文哈希，并重放三个最终run_id的真实原始响应，核对当前解析的status/partial/count/date/instrument；不会自造或验收verified/degraded交付终态。

P5 replay保留abnormal-reasons成功/空响应not_observed；P6 replay最新完整run_id仍为2220b1adff42，mcp_all_required_success=false，Skill成功与MCP未通过分开。未对该结论做生产就绪声明。

最小后续修复：

1. 同花顺明确板块limit/page及expand_index协议，提供可核对的分页覆盖；逐项验证筛选条件，不能靠上游文本或候选数证明满足条件。
2. 经营展开表提供完整覆盖/业务项目标识，以及客户、供应商、参控股公司、合同各自的报告期或事件日；这些子能力尚未真实逐项取证。
3. 机构研究明确发布日、预测期、研报唯一ID及记录总数，限制展开数量，消除4万行及潜在多表展开关联不明；评级、ESG/信用/主体/基金评级、券商金股尚未分别真实验收。
4. 运行完整Critic/Finalize研究链路和sync/SSE终态取证，确认degraded只含已验证claim且persisted=false；MCP另行完成initialize、list_tools、call_tool及真实业务数据验收。

上述工作完成前，保持 `production_ready=false`。
