"""Pytest 全局配置 — 测试环境变量占位。

节点 __init__ 时创建 OpenAI / Gateway client，SDK 会立即校验 key。
CI 是干净环境，没有 .env 文件；测试均通过 monkeypatch 替换为 fake client，
不会真的发请求，占位 key 即可让初始化通过。
"""

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
os.environ.setdefault("MARKET_GATEWAY_API_KEY", "test-placeholder")
