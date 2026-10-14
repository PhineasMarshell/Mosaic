# P9 交接报告（未提交）

本轮在当前工作树直接实施，保留全部已有修改、`memory.db`、历史运行目录和 JSONL；未执行 reset、revert、commit 或 push。`production_ready=false` 继续保留。

新增 [scripts/p9_e2e_acceptance.py](/D:/Mosaic/scripts/p9_e2e_acceptance.py) 作为主路由真实端到端取证入口。`--mode probe` 逐个驱动交易日/周末“今天 A 股发生了什么”、个股 quote/kline、涨停池/板块、新闻/公告，以及指数、板块筛选、公司经营、机构研究场景，同时请求 sync 与 SSE；只记录 `run_id`、终态字段、工具状态、`source_switch`、`reason_codes`、耗时和持久化状态，不保存用户问题、模型正文、原始响应或凭据。`--mode replay` 对最新完整 run 做脱敏回放校验。

P9 还补齐了主链路契约：最终响应汇总工具级 `reason_codes` 和 `source_switch`，执行与 delivery 日志记录来源切换与 fallback 原因；同步 blocked 响应现在与 SSE 一样带 `code=blocked`。401 保持零 datum、`mcp_fallback_not_attempted_auth` 和 `authentication_failure_no_fallback`，timeout、fallback 失败和非 verified 终态统一 `persisted=false`。

新增回归覆盖了新闻不能证明价格、缺日期不能支撑当日行情、source switch/401 诊断汇总、同步 blocked 终态，以及 P9 场景目录和 replay 缺 transport 失败。现有 Evidence Gate、Critic、Finalize 的强结论、partial、日期、标的和伪造 evidence 约束保持不变。

验证命令：

```text
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q app scripts tests
.venv\Scripts\ruff.exe check app scripts tests
git diff --check
.venv\Scripts\python.exe scripts/p9_e2e_acceptance.py --mode replay
```

本轮新增回归与现有稳定性回归已通过；真实 `probe` 需要当前环境的认证、上游行情证据和完整 LLM 链路，未将未观察场景推断为成功。没有真实认证、关键行情 evidence、完整 Critic/Finalize/Response 取证前，不提升 `production_ready`。

当前脱敏取证 [memory/p9-e2e-acceptance.jsonl](/D:/Mosaic/memory/p9-e2e-acceptance.jsonl) 明确记录 12 个场景各自的 sync/SSE `not_observed`，因此 replay 能通过结构与安全性检查，但不会伪称真实交付已通过。获得认证后运行 `--mode probe` 会追加新的 `probe_run_id`，再用同一 `--mode replay` 验证真实终态。
