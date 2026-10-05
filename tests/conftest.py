"""Pytest 全局配置 — 环境占位 + 测试有效性约定（T35 机制性防线）。

环境占位
--------
节点 __init__ 时创建 OpenAI / Gateway client，SDK 会立即校验 key。
CI 是干净环境，没有 .env 文件；测试均通过 monkeypatch 替换为 fake client，
不会真的发请求，占位 key 即可让初始化通过。

测试有效性约定（T35，违反会被 tests/test_conventions.py 的机械检查拦下）
------------------------------------------------------------------------

1. **禁止把被测节点整体替换掉**。monkeypatch ``__call__`` 只能用于"非本次
   验证目标"的节点，且每个替换点必须带 ``# T35-OK: <理由>`` 行内豁免标记。
   被测节点要像真的一样跑：要么给 stub 补全生产消费的方法/属性，要么让
   用例走生产骨架（参考 tests/test_graph_nodes.py 的 ``_NoGatewayRuntime``）。
2. **每个测试文件必须在模块 docstring 里声明**"本文件 mock 掉了什么、
   因此没有覆盖什么"（纯单元测试也要声明"不 mock 任何东西"）。
3. **关键用例必须断言成功分支**：凡跑节点/图骨架的用例，断言
   ``errors == []`` 或不含兜底降级标记（"… failed: …"），防止异常被节点
   兜底 ``except`` 吞成 failed finding 后用例照样绿。
4. **每个新增测试自问："把对应生产代码改坏，它会不会变红？"** 不会就重写。

S1–S6 验收沉淀的四条假阳性教训（评审清单，写用例前对照）
--------------------------------------------------------

- **模式①（整体替换被测对象）**：stub 把 ``_runtime`` 换成 None/缺方法的假
  对象 → 异常被节点兜底 except 吞成 "failed finding"，用例断言却照过
  （T18T）。防线 = 约定 1 + 3。
- **模式②（autouse 清状态 + @parametrize）**：每条参数化用例都在干净状态
  下跑，"第 N 次调用受第 N-1 次影响"的跨调用污染（前值串台、缓存复用、
  签名去重）根本不会发生（T22c 初版被反向验证抓出）。凡是污染类行为，
  **必须放在一条用例内顺序执行**。
- **模式③（fixture 手工造数据、绕开真实入口）**：手写
  ``NormalizedDatum(metric="openInterest")`` 而生产 metric 是 normalizer
  产出的 "data.openInterest"，T22b 的漏报就是这么漏过去的。凡是断言
  "某条规则会命中"的用例，**必须经过 normalize_tool_result**。
- **反向验证的正确姿势**：必须**忠实复现旧行为**（退回旧实现本身），而不是
  "随便把新代码弄坏"——T22c 第一次弱回退只红了 2 条，忠实回退才红 5 条。
  **探针要走公开路径驱动行为**（如连续调用 detect_anomalies），不要手工写
  内部状态（如直接往 market_cache 塞 ``__anomaly_prev__:*``）——键格式一变
  探针就静默过期（S4 教训）。

节点兜底降级的统一标记（供约定 3 的断言使用，**不许改回**）
----------------------------------------------------------
analyst → ``"<category> analysis failed: …"``（finding.failed=True）
gate    → ``"Gate failed: …"``
critic  → ``"Critic audit failed: …"``（critique.verdict="error"）
reasoning → ``"Reasoning engine failed: …"`` / ``"Reasoning node failed: …"``
supervisor → ``"Supervisor routing failed: …"``
注意：T15 证据门降级写的是 ``"Evidence gate: …"``，**不是**失败标记——
零证据产出低置信报告是合法降级，不在此列。
"""

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("MARKET_GATEWAY_API_KEY", "test-placeholder")
