# P4 Gateway / LLM / SSE 交接报告

验收基线为 `main@f5b7134`。本次没有执行 reset、revert、commit 或 push。脱敏取证保存在 [memory/p4-real-acceptance.jsonl](../memory/p4-real-acceptance.jsonl)，离线检查入口为 `python scripts/p4_real_acceptance.py --replay`。

实现结果：Gateway transport 继续保留 canonical 参数，Cloudflare/origin incomplete 现在归类为 `gateway_upstream_incomplete`；`get_stock_abnormal_reasons` 的错误、超时、空响应都返回 0 datum、无 evidence，并带 `optional_evidence_unavailable`。ToolRuntime 记录 `cache_hit` / `refresh_requested`，refresh 绕过同轮去重与共享缓存。partial 仍只能进入受控 degraded，不能支撑比较、因果或全市场绝对结论。

LLM 阶段使用同一个总 deadline，并在 plan、tools、reasoning、critic、finalize 之间预留时间。reasoning 的原始 datum 按工具和总量截断，Critic 默认只展示有限的未引用 evidence 与每工具 datum；审计账本仍保留完整结果。现场交易日 SSE 运行 `run_id=004ab1485e77`：plan 20.8s，工具约 18.4s，reasoning 53.2s，critic 43.5s，总耗时 142.3s；reasoning prompt 24,905 tokens、critic 26,787 tokens。作为优化前同类现场基线，P3 交易日运行总耗时 141.8s、reasoning 81,221 tokens、critic 46,005 tokens；因此本次主要收益是上下文规模下降，墙钟耗时仍受真实 Gateway/LLM 延迟主导，并没有宣称已改善。

真实 MCP smoke 使用 40 个工具并通过 `verify_upstream`；Windows 原生管道仍因 WinError 5 使用 blocking stdio 兼容层。真实 Gateway 当前仍有上游不稳定：`get_stock_abnormal_reasons` 返回 Cloudflare origin incomplete，`get_ashare_sentiment` 在 8–12 秒边界超时；本次没有观察到 abnormal-reasons 的真实 success 或真实空载荷，因此这两种场景在 JSONL 中明确标为 `not_observed`，不被当作成功。

SSE 真实运行发送了 heartbeat，并在预算内发送 `blocked` 终态，包含 run_id、reason_codes、elapsed_ms、remaining_budget、persisted=false；同步与 SSE 共享同一 response builder 和终态字段。所有 blocked/degraded/failed 结果均不持久化。

仍存阻塞：Gateway 上游 Cloudflare/origin incomplete 与 sentiment 超时；真实 LLM 仍可能在模型端点延迟较高时把研究推入 `tool_call_budget_insufficient`，因此交易日和周末现场运行本次均为 blocked。正常数据链路的本地审计、deadline、partial refresh、伪造引用、日期/标的错配和 SSE 终态回归已通过，但在真实上游 success 与稳定 LLM 总耗时复验完成前，尚未达到生产上线条件。