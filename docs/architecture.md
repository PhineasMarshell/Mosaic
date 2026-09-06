# Mosaic 第一阶段架构

## Runtime

```text
Browser / CLI
      ↓
FastAPI
      ↓
Orchestrator
      ↓
Planner ─────→ OpenAI-compatible LLM
      ↓
Tool Registry
      ↓
MCP Client
      ↓
iiix mcp serve market-gateway
      ↓
Market Gateway
```

数据回来以后：

```text
MCP Raw Response
      ↓
Normalizer
      ↓
Common Market Data
      ↓
Evidence Engine
      ↓
Reasoning Engine ─────→ OpenAI-compatible LLM
      ↓
Market Intelligence
```

## 为什么这样设计

设计文档要求第一阶段保持 One Orchestrator + Planner + Tool Executor + Evidence + Reasoning，而不是一开始做 Multi-Agent。

Registry 将“业务意图”与真实 MCP operationId 解耦。

Normalizer 防止 Reasoning Engine 直接依赖各 Tool 的原始 JSON。

Evidence Engine 保留 Tool、metric、timestamp、source、status、partial。

## MCP 与 HTTP API

第一阶段默认走 MCP：

```bash
iiix login
iiix mcp install market-gateway
iiix mcp serve market-gateway
```

Mosaic 不重新实现 Market Gateway，也不把 API Key 写进代码。

HTTP API 可以作为未来测试/诊断 adapter，但不作为当前默认数据通道。
