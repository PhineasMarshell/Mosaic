# P15 主 agent 独立验收

验收日期：2026-10-14（Asia/Shanghai）。结论：P15 审计漏洞修复通过本地独立验收；真实网络及生产就绪结论留待 P16。

## 核对结果

- Finalize 对每个 claim 收集严格 matcher 的拒绝原因，无引用正向事实不再绕过 `missing_reference`。
- 混合报告只保留合格 claim 并进入 degraded，缺引用原因继续透出；degraded 不持久化。
- 明确的休市/数据缺口模板可作为结构事实；在限制语旁拼接价格、公告、实体正向事实仍拒绝。
- persistence 在初始化默认 memory 和任何写入前再次检查，直接伪造 verified 标签不能使无引用 claim 落库。
- 显式空账本不能由报告中自带的 evidence 替代；严格模式下另一条好引用不能掩盖坏引用。
- P14 采集数、账本数、报告数分别从对应日志/报告取值；日志采集兼容直接事件和包装事件。

## 主 agent 补修

独立检查发现 P14 live probe 仍在清理前写入 `cleanup_status=pending`。已在 `scripts/p14_acceptance.py` 缓冲场景记录，在 finally 中移除日志 handler、执行清理，然后把最终 `ok/warning` 写入新记录。已完成的 sync 记录在后续 SSE 异常时也会保存；请求异常继续抛出，不被清理告警覆盖。

在 `tests/test_p15_verified_boundary.py` 增加 3 个回归场景：正常清理、清理权限告警、请求异常与清理告警同时发生。均使用假的网络客户端和临时文件，不连接外部服务。

## 独立验证

- P13/P14/P15 聚焦集合（补修前）：108 passed。
- P15 集合（补修后）：68 passed。
- 全量 pytest（补修后）：1595 passed，1 条第三方 BlockingPortal 弃用警告。
- 全仓 Ruff、compileall、`git diff --check`：通过。diff check 仅有既有换行转换提示。
- P9/P10 离线 replay：通过。P9 仍是历史 not_observed artifact；P10 仍是历史观察结果，本轮没有新增真实网络成功证据。
- P15 七场景独立回放：通过，新增结果放在 `.tmp/p15-main-review/acceptance.jsonl`，没有改写子 agent JSONL。
- 全量测试使用 `MOSAIC_MEMORY_DB=D:\Mosaic\.tmp\p15-main-review\memory.db`；相对 P15 开始时受保护文件清单复核 39/39 哈希一致。

未执行 commit 或 push。真实 THS/LLM sync/SSE 未在本轮重新请求；离线 verified 与临时 SQLite 写入成功不代表真实生产通过，`production_ready=false` 保持。
