# P8 交接报告（未提交）

本轮在现有工作树直接实施，保留全部既有修改、`memory.db`、`memory/evening/` 及历史验收 JSONL；未执行 reset、revert、commit 或 push。

## 路由与认证策略

- `SettingsConfigDict(env_file=".env")` 现在实际读取 `.env` 中的 `MARKET_PRIMARY_SOURCE=ths_skill`、`MARKET_FALLBACK_SOURCE=mcp`、`MARKET_SOURCE_FALLBACK_ENABLED=true`；相同字段也在 `.env.example` 声明。应用入口 `get_settings()` 显式打开 `MARKET_SOURCE_ROUTING_ENABLED` 运行时 profile；测试/离线 `Settings()` double 保持关闭。路由不再读取 `model_fields_set`。
- `ToolRuntime` 对注册的 A 股路径先调用同花顺 Skill；同花顺明确 `success`/`partial` 时不探测 MCP，只有可重试的 error/empty/timeout 且证据通过 provenance 校验时才使用 MCP。
- 401 采用“不 fallback”策略：保持 `status=error`、`partial=false`、零 datum、零 evidence，追加 `ths_invalid_credentials` 与 `mcp_fallback_not_attempted_auth`，并保留 `fallback_reason=authentication_failure_no_fallback`。缺失 key 为 `ths_missing_credentials`，403 为 `ths_permission_denied`，429 为 `ths_rate_limited`，5xx 为 `ths_upstream_error`；错误文本和响应体均不写入脱敏产物。

## 真实 P8 probe

`memory/p8-acceptance.jsonl` 由 `scripts/p8_acceptance.py --mode probe` 生成，并由 `--mode replay` 校验。探针覆盖指数、A 股筛选、板块筛选、公司经营、机构研究，以及交易日/周末“今天 A 股发生了什么”、quote、kline、涨停池/板块、新闻、公告。每条记录保留 `skill_id`、HTTP endpoint、请求日期、标的、coverage、partial、分页/上游行数和 raw 响应摘要（长度、哈希、顶层键），不保存 raw body 或 credential。

本次真实运行观察到同花顺指数、筛选、板块、公司经营、机构研究和新闻/公告 HTTP 200；其中筛选与研究因上游分页、日期或字段覆盖不足明确标记 `partial`，不能支持强结论。市场 quote/kline/涨停路由观察到 401，均保持零 datum/zero evidence 并记录“不 fallback”原因。MCP 的 TLS/HTTP 5xx/upstream timeout/deadline 细分在本次运行未全部观察到，JSONL 明确标记 `not_observed`，没有将其它错误推断成这些场景。

最新 probe run 为 `ac09aa8ec114`；replay 报告 `rows=23`，未观察场景为 `ths_timeout`、`ths_empty`、`mcp_tls`、`mcp_http_5xx`、`mcp_upstream_timeout`、`deadline`。

## 生产就绪结论

`production_ready=false`。真实认证仍未恢复，MCP TLS/上游关键场景也没有完整证据；因此不能宣称完整端到端交付已通过。tool probe、Critic/Finalize、持久化和 SSE 交付保持明确分离。

## 复现命令

```text
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q app scripts tests
.venv\Scripts\ruff.exe check app scripts tests
git diff --check
.venv\Scripts\python.exe scripts/p8_acceptance.py --mode replay
```

本次结果：`pytest 1444 passed`；compileall、Ruff、P8 replay 通过；`git diff --check` 仅有 Git 的换行提示。`production_ready` 继续为 `false`。
