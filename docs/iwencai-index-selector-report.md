# 同花顺指数与选A股接入记录

日期：2026-10-12。基线：main@a938dd7，工作区保留既有 P5/P6 及同时进行的 P7 修改。未执行 reset、revert、commit、push。

结论：两个官方 Skill 已安装并接入 Mosaic，真实 HTTP 调用返回结构化数据；仍有日期、分页及筛选条件验证限制，不能据此认定完整研究交付或稳定主数据源通过。`production_ready=false`。P6 原十个 Skill 的验收范围保留；新增接口不替代 MCP initialize/list_tools/call_tool/业务数据验收。

## 安装与接口契约

| Skill | 版本 | 实际形态 | Mosaic key / operationId | 能力 |
|---|---|---|---|---|
| hithink-zhishu-query | 1.0.0 | 官方 HTTPS API | iwencai_index / internal_iwencai_index_query | index_quote |
| hithink-astock-selector | 1.0.0 | 官方 HTTPS API | iwencai_astock_selector / internal_iwencai_astock_selector | stock_screening |

共同端点为 `POST https://openapi.iwencai.com/v1/query2data`，不依赖 iiix/MCP。输入 schema 为 query（必填）、page、limit、is_cache、expand_index；Mosaic 参数界限为 query≤2000字符、page=1..10000、limit=1..20、两个开关为布尔，HTTP 请求按官方规范发送字符串值。输出包含 datas、code_count、chunks_info 和动态 columns；成功不得由自然语言 text_response 推定。

鉴权为 Bearer；发送各自 Skill ID、版本、normal/retry、插件none及每次尝试独立的64hex trace。官方CLI默认超时30秒；Mosaic沿用请求deadline及有限重试，认证/限流/timeout/结构异常/业务错误区分为稳定reason_codes。真实限流及timeout尚未在本批探针中观测，不能写为“已通过真实验收”。

SkillHub官方入口已核对。首次经环境代理获取安装器返回403；随后直接HTTPS成功，未绕过TLS校验。使用核对后的官方Python CLI配合Windows命令包装安装，仅安装CLI和这两个Skill：

- CLI：`C:\Users\Phineas\.iwencai-skillhub`，版本2026.3.13；命令为 `.local\bin\iwencai-skillhub-cli.cmd`。
- Skill目录：`C:\Users\Phineas\.agents\skills\hithink-zhishu-query`、`hithink-astock-selector`。
- PowerShell profile：`C:\Users\Phineas\Documents\PowerShell\Microsoft.PowerShell_profile.ps1`；新shell已确认命令可用、BASE_URL正确、API_KEY已加载。
- 密钥从本地秘密配置读取，以当前Windows用户DPAPI加密保存，profile仅加载加密凭据；代码、文档及取证文件不含明文密钥。

官方包SHA256：

| 包 | SHA256 |
|---|---|
| CLI | 02959fb9292fa1ceb0fac8beec2d1a3ad08e5477f8801b043d54321bcf368a95 |
| hithink-zhishu-query | e77f50a98498db44356c5ce929e01a2028a5c83941f2926fc67e3a2825da9120 |
| hithink-astock-selector | 8b12b0e96b3c2c6c3617ea3c44f729917fbd0fe1d708f74a87c011331c014809 |

## 真实取证

记录保存在 `memory/iwencai-index-selector-probes.jsonl`。下列是工具探针；Critic verdict、Finalize、Delivery均为 `not_invoked_tool_probe`，`persisted=false` 指研究报告没有写入记忆，并不表示JSONL没有保存。

| 场景 / run_id | 上游HTTP / ToolResult | datum / evidence账本条目数 | 已观测边界 |
|---|---|---|---|
| 上证指数 / 573fd46e56db | 200 / partial | 6 / 6 | SH000001与上证指数由结构化字段配对；4项历史指标明确2026-10-09，2项最新字段无日期 |
| 恒生指数 / 411ccbdc8808 | 200 / partial | 6 / 6 | 返回HSI.GI和HSI.HK，保留各自身份；最新字段无日期，不套用A股休市日 |
| 纳斯达克指数 / 7919f2e61dac | 200 / partial | 6 / 6 | 返回@CCO.NQI、IXIC.GI及“纳斯达克综指”；原代码保留，不合并或猜成纳斯达克100；最新字段无日期 |
| 多条件选A股 / 86a059af67b0 | 200 / partial | 30 / 30 | 请求limit=3，上游返回156行/code_count=156；标准化仅前3个候选，过滤条件未独立逐项验证 |

账本条目数包含保留了未知日期或partial限制的数据，不能解读为已验证claim数量。各次请求实际 `refresh=True`，本地cache_hit=false，HTTP发送is_cache=0；这证明刷新参数和本地绕过路径，不能单凭响应相同/不同证明上游缓存实现。

早期纳斯达克探针 d2afa518f6f6、40f92d172a73 在适配器尚不接受上游@前缀代码时保留了缺instrument限制；后续修复仅接受上游明确结构化代码，不从查询补造身份，历史记录保留。早期选股探针445b6fe0a8c6也保留；之后补充了完整候选身份摘要、能力及参数字段。初步未写JSONL的调试调用不作为验收依据。

## 生产接入与证据边界

统一adapter依据QUERY_TOOLS注册表分发，没有为两个Skill在Evidence Gate增加特殊准入。新增工具可由Supervisor必用规则补齐并由TechnicalAnalyst执行；股票/ETF普通行情、港股/美股筛选、指数成分资料不会误路由到A股筛选。条件及日期写入原query，不执行Skill文档建议的自动放宽条件。

每条datum保留source、source_tool、instrument、instrument_name、as_of_date、retrieved_at、authority、coverage、completeness、partial及原响应。指数只接受明确指数身份；股票表不能证明指数点位。海外特殊代码原样保留。市场值、报告期、静态属性按每条字段分别校验，报告期不能消除下一行情字段的日期要求。

选股始终以 `screening_conditions_not_verified` 标记候选限制；上游超出limit时记录 `upstream_page_size_exceeded`，完整上游返回仍保存在ToolResult.raw。默认报价不证明缺失的ROE、技术形态或概念条件；返回候选数不证明全市场统计或全部条件满足。新增capability未自动宣称普通quote/kline fallback等价。

缺来源的行情claim即使没有指定请求日期也阻断；为旧测试补齐合格的synthetic来源，没有为测试放宽准入。错误日期、错误指数、错误点位、伪造引用、周末“今天”行情、partial全市场强结论仍阻断。失败刷新返回零datum/零evidence且不恢复旧partial。

## 验证与未完成项

针对性回归包含真实adapter/runtime/Supervisor/analyst骨架，HTTP和LLM响应使用mock；完整Critic/Finalize交付及上游长期可靠性未被这些测试覆盖。2026-10-12 收尾验证结果如下：

| 验证 | 实际结果 | 范围与限制 |
|---|---|---|
| 指数/选股专项回归 | 45 passed | 包含身份、日期、来源、数值、partial、分页、失败刷新、实际刷新参数、超时及探针终态 |
| 完整 pytest | 1386 passed，29.93秒 | 1条pytest缓存目录写入警告；此前并行路由修改期间出现的测试失败已修复，最终全量通过 |
| Ruff | All checks passed | `ruff check app scripts tests` |
| compileall | 退出码0 | `python -m compileall -q app scripts tests` |
| git diff --check | 退出码0 | 只有LF/CRLF提示，没有差异格式错误；HEAD仍为a938dd7 |
| 新增探针契约回放 | 7条记录通过 | 检查真实工具探针字段、日期、刷新及未调用交付状态；不是完整研究验收 |
| P5 replay | 326条记录通过 | 已审计run_id：43071b228865、a0da64b19fd6、e31417f0a9df、7aa3cbbb6d67；abnormal-reasons成功/空响应仍not_observed |
| P6 replay | 最新完整run_id 2220b1adff42，32条记录通过 | 原十个Skill的工具探针；`mcp_all_required_success=false`、`mcp_business_success_any=true`、`skill_evidence_success=true`，未观测场景继续not_observed |
| 秘密扫描 | 216个文件，配置密钥出现次数0 | 代码、文档、探针记录未发现该密钥明文 |

两个新增Skill的可用性均为“真实HTTPS结构化数据链路可用，返回partial”；尚无完整Critic/Finalize及sync/SSE交付取证，稳定性成功率与p50/p95长期统计也尚未建立。P6 replay通过仅表示探针记录自洽，不能把其解释为MCP TLS/上游修复或P6整体通过。各项回放均保留 `production_ready=false`。

最小后续修复：要求上游为“最新”字段返回明确观察日期；澄清选股limit/page协议并验证翻页覆盖；为每个筛选条件建立可审计字段和逐项验算；运行完整研究链路的sync/SSE交付取证及独立MCP TLS验收。未完成这些工作前继续 `production_ready=false`。
