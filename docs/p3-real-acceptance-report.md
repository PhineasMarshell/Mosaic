# P3 真实 iiix / MCP 验收报告

验收基线为 `main@e3393cb`。本次保留工作区已有的 `memory.db` 与 `memory/evening/`，没有执行 reset、revert、commit 或 push。脱敏取证在 [memory/p3-real-acceptance.jsonl](../memory/p3-real-acceptance.jsonl)，离线回放入口为 `python scripts/p3_real_acceptance.py --replay`。

## 插件、授权与 MCP

- `iiix whoami`：`passed`，账号状态 `ACTIVE`（身份已脱敏）。
- `iiix plugin install market-gateway`：`passed`，版本 `3.3.0`，bundle 校验有效；安装目录在项目可写的 `D:\Mosaic\.tmp\p3-iiix-localappdata\iiix\plugins\market-gateway`。
- `iiix plugin status`：`passed`。
- `iiix plugin verify market-gateway`：`passed`，上游 reachable。
- `iiix plugin setup ... --client codex --yes`：用户级 Codex 配置注册被 `Access is denied` 阻断；这不影响显式 `iiix plugin serve market-gateway`，因此该阻断已单独记录，不能冒充 setup 注册成功。
- Mosaic MCP spawn：`passed`。本机原生 asyncio Windows 管道返回 `WinError 5`，客户端使用同一命令、同一真实 stdio MCP 协议的 blocking bridge 重试；`initialize`、`tools/list` 和真实工具调用均成功。
- MCP `list_tools`：`passed`，40 个工具。
- MCP health：用真实 `verify_upstream` 工具复验，`passed / reachable`。

为使项目安装目录对 spawned iiix 生效，`MarketGatewayClient` 同时透传项目 `APPDATA` 和 `LOCALAPPDATA`；原生管道被系统拒绝时才启用同步管道兼容层，不会切换为 HTTP，也不会放宽证据门禁。

## 真实工具取证

同一批 MCP 复验记录了：

- quote `600519`：`success`，43 datum，source `tencent`，instrument `sh600519`。
- code-only quote `688981`：当前复验 `success`，43 datum，source `tencent`，instrument `sh688981`。
- sentiment：`success`，原始 26,527 datum，source `ashare-master`。
- limit-up count：`success`，2 datum，source `ashare-master`。
- abnormal-reasons `SH688981`：真实 Cloudflare/origin incomplete error，0 datum，`gateway_error`，未生成 evidence。
- Gateway deadline：`error`，0 datum，`deadline_exhausted`，未生成 evidence。

一次单独的 partial-refresh 复跑在 sentiment 上遇到真实 `gateway_timeout`，两次均 0 datum、未生成 evidence；已有真实成功截断复验 `run_id=1dfb78a6a06c` 仍保留在同一 JSONL，初始和 refresh 均 `partial=true`、原始数量大于保留数量。没有用增加回环次数或部分数据生成强结论。

## 场景 run_id 与终态

| 场景 | run_id | 终态 |
|---|---|---|
| 交易日“今天 A 股发生了什么”（此前真实交易日取证） | `745cb6fa5dc1` | `blocked / research_exhausted`，Critic 缺口未满足 |
| 周末/休市日“今天 A 股发生了什么”（本次真实重跑） | `522039732684` | `failed / timeout`，`requested_date=2026-10-11`、`as_of_date=2026-10-09`、`market_closed=true`、`persisted=false` |
| 688981 静态映射外股票 | `e8671a1b66d7` | Gateway abnormal-reasons 真实 error，0 datum；另有 code-only quote 成功 |
| code-only quote | `012fdf02be9c` | `success`，43 datum |
| partial refresh | `838c57822c8f` | 本次复跑 `gateway_timeout / blocked`，0 datum；成功截断复验 `1dfb78a6a06c` 为 `partial` |
| Gateway deadline | `4589e3c88432` | `error / deadline_exhausted`，0 datum |
| 真实 SSE | `2fe48662f659` | `failed / timeout`，heartbeat 已发送，`persisted=false` |

每个场景的 tool status、datum 数量、partial、source、instrument、as_of_date、retrieved_at，以及 reasoning/critic/route/finalize/delivery 摘要均在 JSONL 中按 run_id 关联。失败、超时、空响应和错误日期/标的均没有进入 evidence；partial 只保留已验证 claim，且 degraded/failed 结果不持久化。

## 尚存阻塞与上线结论

- **MCP**：真实链路已通过；仅保留本机原生 asyncio 管道权限限制的兼容层，以及 Codex 用户级 setup 注册权限阻断。
- **Gateway**：`get_stock_abnormal_reasons` 仍有真实上游 Cloudflare/origin incomplete；sentiment partial-refresh 复跑出现真实 TLS/gateway timeout。两者都不能当作 verified evidence。
- **LLM/SSE**：当前研究重跑在既定总预算内超时，SSE 真实取证仍为 timeout/failed；没有伪装成 verified。

因此当前**不具备生产上线条件**：MCP 插件与真实 spawn 已通过，但 Gateway 上游稳定性、LLM 总预算内完成研究、SSE 成功终态仍未达到验收标准。
