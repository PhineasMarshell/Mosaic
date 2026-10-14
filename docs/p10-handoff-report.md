# P10 真实端到端研究验收交接报告

## 验收范围

本轮使用真实 FastAPI `/api/ask` 与 `/api/ask/stream` 端点，复用 P9 探针并写入脱敏、追加式 JSONL：

- Run：`6c75656326e2`
- Artifact：[memory/p10-e2e-acceptance.jsonl](../memory/p10-e2e-acceptance.jsonl)
- 入口：`.venv\\Scripts\\python.exe scripts\\p10_e2e_acceptance.py --mode probe|replay`
- 生产开关：`production_ready=false`

覆盖场景为交易日与周末“今天 A 股发生了什么”、quote、kline、涨停池、涨停板块、新闻、公告、指数、板块筛选、公司经营和机构研究。每个场景均独立执行 sync 与 SSE。

## 真实结果

| 检查项 | 结果 |
| --- | --- |
| 场景数 | 12 |
| sync 终态 | 12/12 observed |
| SSE 终态 | 12/12 observed |
| not_observed | 0 |
| HTTP 状态 | 24 次均为 200 |
| verified / degraded / blocked / failed | 0 / 0 / 24 / 0 |
| persisted=true | 0 |
| evidence_count>0 | 0（24/24 均为零） |
| claims_valid=true | 0 |
| source_switch | 12 true，12 false |
| stage | finalize 21，reasoning 3 |

主要实际原因码包括 `ths_invalid_credentials`、`gateway_auth_error`、`mcp_fallback_not_attempted_auth`、`research_budget_exhausted`、`unknown_date`、`missing_as_of_date`、`stale_date`、`partial_only`、`expanded_rows_coverage_unknown`、`missing_publication_date` 和 `reasoning_timeout`。周末场景与机构研究场景各有推理超时终态；其余场景主要因认证、日期、覆盖或研究预算不足而被阻断。

本轮没有观察到稳定的完整认证行情证据，也没有观察到完整端到端 verified 交付。对 401、TLS、5xx、upstream timeout、deadline 或 SSE timeout，本轮 artifact 没有对应的 transport 终态，因此只能记录为 `not_observed`，不能推断其成功或失败；若这些原因出现，回放规则要求失败工具自身零 datum/evidence 且 `persisted=false`；无法归属到具体工具的响应级失败才要求整份 evidence 为零。

补修后的聚焦真实验收另存为 [memory/p10-focused-acceptance.jsonl](../memory/p10-focused-acceptance.jsonl)，覆盖公告和行情各一场景的 sync/SSE。4/4 请求均 `observed`，累计工具 datum 174 条；4/4 仍为 `blocked`、`persisted=false`，没有观察到 verified/degraded。公告请求真实取得每次 20 条结果，行情请求多次取得非空结果，但日期、覆盖和研究审计仍不足，因此不能把采集到数据推断为交付成功。

该聚焦 run 的 JSONL 已完整写入，但执行发生在 best-effort cleanup wrapper 加入前，进程最后因 Windows MCP 临时目录权限错误返回非零；其 replay 已通过。wrapper 已加入后续探针路径，未为清理问题重复消耗同一组外部请求。

## P9 replay 证据修复

- 探针启动时显式把项目根目录加入 `sys.path`。
- sync 非 JSON、请求异常、SSE 无终态都会写入脱敏 `not_observed` 记录，并继续下一个 transport 和场景。
- 探针临时目录清理由 best-effort wrapper 负责；Windows MCP 子进程持有句柄时不会把已写完的 JSONL 误报为进程失败。
- sync 与 SSE 每个场景分别写入真实终态；旧版已写入的 P10 记录在 replay 时依据 SSE heartbeat 字段兼容识别 transport。
- 失败工具自身必须零 datum/evidence；独立成功来源仍可支持受控 degraded claim，且 degraded/blocked/failed 均禁止持久化。
- strict provenance 校验现在要求 evidence ID、有效 source/authority、`retrieved_at`、显式 `as_of_date` 和日期匹配；个股 claim 还要求 instrument/instrument_name 匹配。
- 新闻或公告文本不能支持价格变化；partial、unknown_date、coverage 不足、伪造来源、错误日期和错误标的均不能使 claim 变为 verified。
- verified 需要 Finalize audit pass、claims 完整绑定且每条 claim 都通过证据校验；degraded、blocked、failed 均禁止持久化。

新增失败测试覆盖伪造 source、错误日期、错误标的、news-only、partial，以及缺失 `retrieved_at`/unknown coverage。

## 验证记录

已执行并通过：

- `.venv\\Scripts\\python.exe scripts\\p10_e2e_acceptance.py --mode replay`
- `.venv\\Scripts\\python.exe -m pytest -q`
- `.venv\\Scripts\\python.exe -m compileall -q app scripts tests`
- `.venv\\Scripts\\ruff.exe check app scripts tests`
- `git diff --check`

本轮补修后的完整 pytest 为 1465 passed；聚焦真实 JSONL 的自定义 replay 也通过，输出 `not_observed=[]`，但交付终态仍全部 blocked。

没有执行 reset、revert、commit 或 push；当前工作树、memory JSONL 和其它运行时文件均保留。
