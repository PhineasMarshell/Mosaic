# Tool Registry

第一阶段不是把全部 33 个 Tool 无约束交给 Planner。

核心逻辑工具：

| key | operationId | 用途 |
|---|---|---|
| overview | overview_eastmoney_overview_get | 全市场 |
| sentiment | public_sentiment_ashare_master_sentiment_get | 情绪 |
| limit_up_count | public_limit_up_count_ashare_master_limit_up_count_get | 涨停数量 |
| limit_up_sectors | public_limit_up_sectors_ashare_master_limit_up_sectors_get | 题材 |
| limit_up_pool | public_limit_up_pool_ashare_master_limit_up_pool_get | 涨停池 |
| quote | quote_tencent_quote_get | 行情 |
| longhu | longhu_xueqiu_longhu_get | 龙虎榜 |
| abnormal_reasons | abnormal_reasons_xueqiu_abnormal_reasons_get | 异动 |

后续新增 Tool 时：

1. 在 Registry 增加业务 key。
2. 添加 purpose/domain/priority。
3. Planner Prompt 自动可见。
4. 不修改 Orchestrator 主流程。
