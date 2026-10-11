# P2 真实 Gateway/MCP/LLM 线上验收取证

验收基线为 `main@08f7824`。本次没有 reset、revert、commit、push，没有修改 `.env`，也没有写入运行时 `memory.db`；临时 HTTP Settings 与 SSE 运行均未持久化可信结果。

## 依赖结论

- **LLM 可用**：`.env` 提供 key；DashScope OpenAI 兼容端点 `qwen3.7-flash` 的 JSON 探针成功（约 3.8 秒，返回合法 JSON）。
- **HTTP Gateway 可用**：`/health`、`/market/health`、`/ashare-master/limit-up/count`、`/ashare-master/sentiment`、代码行情均返回 200；代码行情归一化后有 43 条 datum。
- **MCP/iiix 不可用**：`iiix 0.8.4` 的 `whoami`、`plugin status`、`plugin verify market-gateway` 均在初始化日志时返回 `Access is denied`；Mosaic MCP spawn 失败为 `PermissionError [WinError 5]`。因此本次不能声称 MCP 真实链路通过，MCP 登录/鉴权状态也只能记为 unknown（CLI 尚未进入鉴权检查）。
- **缺失/阻塞项**：MCP 进程需要能写入 `C:\Users\Phineas\AppData\Roaming\iiix\logs\iiix.log`（或提供可写日志目录/权限）；若运维改用 HTTP 模式，仍需保证 `MARKET_GATEWAY_HTTP_URL` 与 `MARKET_GATEWAY_API_KEY` 可注入。当前 LLM 的 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY` 已加载。

依赖检查与 schema 摘要保存在 `memory/p2-real-acceptance.jsonl` 的 `dependency_check` 记录中；完整 schema 来自本机 market-gateway OpenAPI 3.1 快照（43 paths）。

## 真实运行结果

| 场景 | run_id | 结果 | 关键取证 |
|---|---|---|---|
| 交易日 `2026-10-09 A 股发生了什么` | `745cb6fa5dc1` | `blocked / research_exhausted` | Critic 要求 `klines`；总预算不足，且报告含 `invalid_reference`；未返回报告 |
| 周末/休市日 `今天 A 股发生了什么` | `85029e265c91` | `blocked / research_exhausted` | `requested_date=2026-10-11`、`as_of_date=2026-10-09`、`market_closed=true`；`domain_mismatch`，未把周末消息当交易日价格事实 |
| 静态映射外真实 A 股 `688981` | `244226a99726` | 调查失败 | 真实代码 quote 43 datum、symbol news 10 datum；`get_stock_abnormal_reasons` 两次 5 秒超时。随后图异常被 Orchestrator 的 `ResearchState` 展开错误掩盖，未形成最终 Critic/Finalize 终态 |
| code-only quote | `26bd4a5378fa` | `success` 工具结果 | Gateway 接受代码并返回 43 datum；没有因“只有代码”虚构名称或日期 |
| 名称直接作为 quote 目标 | `aeeee1f8cd14` | `error` | Gateway 真实返回 `422 Invalid parameters`，normalized datum 为 0 |
| partial 后 refresh | `1dfb78a6a06c` | 两次 `partial` | `get_ashare_sentiment` 原始 26,527 条，截断为 201；refresh 绕过同签名去重并重新取数，仍保留 `partial=true` |
| Gateway deadline | `572d319edd98` | `error` | 无 datum，`budget exhausted: no time left for another attempt` |
| 真实 SSE | `2fe48662f659` | `failed / timeout` | 45 秒总 deadline；3 个 heartbeat，最终 result `code=timeout`、`persisted=false` |

每个 run 的 `plan`、`executions`、LLM、Critic、route、Finalize、delivery 结构化事件均按 run_id 关联；自由文本和工具参数已脱敏。HTTP 请求摘要、status、normalized datum 数量、source/instrument/as_of_date/retrieved_at/partial 也已保存。

## 验收断言结果

现有安全回归与本次 replay 共 102 项通过（本次新增 3 项，相关既有测试 99 项）。已验证：错误请求参数不会成为 evidence；未验证实体不会被判为“虚假”；真实标的/日期/域不匹配、news-only 证明价格变化、partial 强结论、forged reference、权威冲突、审计器 error 均不会进入 verified；混合 claim 只有在可逐条证明时才可进入 controlled degraded，且 `persisted=false`。休市日明确保留最近交易日语义。

## 仍需修复的最小代码问题

1. `app/agent/orchestrator.py` 的异常处理对 `ResearchState` 使用 `{**state}`；真实标的场景出现异常时又触发 `TypeError: 'ResearchState' object is not a mapping`，掩盖了原始阶段/异常。应改为 `state.model_dump(...)`（或等价安全转换），并补回原始错误类型的终态日志。
2. Gateway 侧 `get_stock_abnormal_reasons` 在当前上游对 `SH688981`/`SH600519` 发生真实超时；这是上游/网络可用性问题，不能通过放宽 Critic 或增加回环次数解决。
3. MCP 仍受 iiix 日志文件权限阻断；修复本机权限/日志路径后必须重新跑同一套真实 smoke，不能用 HTTP 成功替代 MCP 通过。