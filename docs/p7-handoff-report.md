# P7 交接报告（未提交）

本工作树保留了既有 P6 修改、`memory.db`、`memory/evening/` 和验收 JSONL；本轮未执行 reset、revert、commit 或 push。

## 已完成

- 增加配置驱动的 `MARKET_PRIMARY_SOURCE=ths_skill`、`MARKET_FALLBACK_SOURCE=mcp` 和受控 fallback 开关。A 股 quote、kline、market breadth、limit-up count/pool/sectors、industry/theme、finance、news、announcement 等能力通过注册表路由到同花顺 Skill；缺少严格标的或日期条件时不伪造 Skill 成功。
- ToolRuntime 先调用同花顺。只有 Skill 返回 error、empty 或 unsupported 且剩余预算允许时才调用 MCP；partial 保留已验证 datum。MCP 成功结果带 `primary_source=ths_skill`、`fallback_source=mcp`、`source_switch=true`，MCP 失败保持零 datum、零 evidence。
- fallback 保留 `run_id`、`refresh`、deadline、source、authority、retrieved_at、as_of_date、coverage、partial，并拒绝错误标的或日期的 fallback evidence。
- 周末 Gate 对 `requested_date=2026-10-11` 只接受明确 `as_of_date=2026-10-09` 的行情；缺失日期追加 `missing_as_of_date`、`unknown_date`，不允许声称已取得前一交易日行情。
- MCP 诊断区分 `stdio_init`、`initialize`、`list_tools`、`call_tool`、HTTP 5xx、upstream timeout 和 deadline exhausted；每次 attempt 保留 stage、endpoint、attempt、elapsed_ms、reason_codes。Skill 成功不会标记 MCP 已修复。
- P6 replay 改为从 JSONL 选择最新完整 run，并校验 run_id、场景、周末语义和实际缓存/错误结果；tool probe 与真实 Orchestrator/Critic/Finalize 交付明确分开，`production_ready` 继续为 `false`。

## 验证结果

- `pytest -q`: **1378 passed**（2 个环境警告）。
- `python -m compileall -q app scripts tests`: 通过。
- `ruff check app scripts tests`: 通过。
- `git diff --check`: 通过（仅 Git 的换行提示）。
- `python scripts/p6_ths_skill_acceptance.py --mode probe`: 完成真实 probe，最新完整 run 为 `2220b1adff42`，结果已追加到 [p6-ths-skill-acceptance.jsonl](/D:/Mosaic/memory/p6-ths-skill-acceptance.jsonl)。
- `python scripts/p6_ths_skill_acceptance.py --mode replay`: 通过；`mcp_all_required_success=false`、`skill_evidence_success=true`、`production_ready=false`。

## 未解决问题

- 真实 MCP 仍有 upstream timeout/HTTP 5xx 取证，Windows stdio 初始化曾触发权限拒绝后进入 blocking bridge；MCP 没有达到生产就绪标准。
- 同花顺真实环境本轮返回 401，因而 quote/market/limit-up 等生产 evidence 覆盖仍不足；缺少日期的返回严格降级，不会猜测交易日。
- 真实 probe 是 tool probe，不代表完整 Orchestrator/Critic/Finalize 研究交付；生产条件未全部满足，必须继续保持 `production_ready=false`。
