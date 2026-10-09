# Sentiment Analyst — 实施计划

> **目标**：完成 Mosaic 的舆情情绪分析员（Sentiment Analyst），接入 MCP 雪球接口
> `list_stock_discussions`，通过 LLM 对评论帖子做情感极性打分，产出结构化 Evidence
> 并集成到 LangGraph 多节点架构。

---

## 1. 设计决策摘要

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 情感分析策略 | **LLM 打分** | 单次 LLM 调用对所有帖子做情感极性分类（正面/负面/中性）+ 关键主题提取 |
| 帖子上限策略 | **单批全送** | 最多 `SENTIMENT_MAX_COMMENTS`（默认 500）条一次性给 LLM |
| 多源支持 | **仅雪球** | 第一版只接雪球 `list_stock_discussions`，架构预留多源扩展点 |
| 注册表处理 | **新增 key** | 保留 `timeline` 旧条目不动，新增 `xq_discussions` → `list_stock_discussions`，category=sentiment |
| 产出物模型 | **复用 Evidence** | 产出 Evidence 条目（metric=`sentiment_bullish` / `sentiment_bearish` / `sentiment_neutral`）+ finding digest |
| 市场级默认 | **不加入最低证据集** | 仅在 Supervisor 主动分配或用户明确问情绪时触发，避免不必要的 LLM 成本 |
| 实现模板 | **对标 NewsAnalyst** | 完全遵循 `news.py` 的可选 analyst 模式 |

---

## 2. 改动清单总览

### 2.1 修改文件清单

| # | 文件 | 改动类型 | 说明 |
|---|------|----------|------|
| 1 | `app/gateway/tool_registry.py` | 修改 | 新增 `xq_discussions` ToolMeta（category=sentiment）；修正 `timeline` 的 purpose/priority 描述 |
| 2 | `app/graph/nodes/analysts/__init__.py` | 修改 | 导出 `SentimentAnalystNode` |
| 3 | `app/graph/nodes/analysts/sentiment.py` | **新建** | SentimentAnalystNode 完整实现 |
| 4 | `app/graph/builder.py` | 修改 | 注册 `sentiment` 节点 + 条件扇出 + 边（完全对标 news） |
| 5 | `app/graph/nodes/supervisor.py` | 修改 | `route_candidate_categories()` 移除 `sentiment` 未实现保护；`_build_route` 支持 sentiment category 分组 |
| 6 | `app/graph/market_plan.py` | 不改 | 不在最低证据集中加入 sentiment |
| 7 | `app/agent/prompts.py` | 修改 | Reasoning Prompt 补充 sentiment 相关证据的评估规则 |
| 8 | `app/graph/nodes/critic.py` | 修改（可选） | 如有需要，补充舆情情感一致性检查规则 |
| 9 | `tests/` | **新建** | `test_sentiment_analyst.py` — 单元测试 + 拓扑测试 |
| 10 | `README.md` | 修改 | 更新项目结构 / 配置说明 |

### 2.2 不改动的文件

- `app/config.py` — 已有 `sentiment_enabled` + `sentiment_max_comments`，无需变动
- `app/graph/state.py` — `AnalystName` 已含 `"sentiment"`，无需变动
- `app/main.py` — `_NODE_PROGRESS` 已有 sentiment 条目，无需变动
- `app/graph/nodes/analysts/base.py` — 通用骨架无需修改
- `app/gateway/normalizer.py` — 已有 `sentiment` 映射

---

## 3. 详细实施步骤

### Step 1：修正 + 新增工具注册表（`app/gateway/tool_registry.py`）

**3.1.1 修正旧 `timeline` 条目描述**（不改 category，只改 purpose）

```python
# 行 266-275，当前：
ToolMeta(
    "timeline",
    "list_stock_discussions",
    "分时/时间线数据",       # ← 错误描述
    domain="a_share",
    priority="low",
    ...
    category="technical",    # 保持不动
),

# 改为：
ToolMeta(
    "timeline",
    "list_stock_discussions",
    "雪球个股评论区讨论帖子（时间线）",
    domain="a_share",
    priority="low",
    ...  # category 保持 technical
),
```

**3.1.2 新增 `xq_discussions` 条目**（在 `_ASHARE_MICRO` 之后或作为独立的 `_SENTIMENT_TOOLS`）

```python
# 取消注释并修改 _SENTIMENT_TOOLS 块（行 640-647）：

_SENTIMENT_TOOLS = [
    ToolMeta(
        "xq_discussions",
        "list_stock_discussions",
        "雪球个股评论区讨论帖子（情感分析源数据）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/market/discussions",
        category="sentiment",
        requires_symbol=True,        # 必须有 symbol
    ),
    # 后续 MCP 就绪后取消注释：
    # ToolMeta("futu_comments", "comments_futu_get", "富途牛牛个股评论区",
    #          category="sentiment", domain="hk_stock", ...),
    # ToolMeta("ths_comments", "comments_ths_get", "同花顺个股/板块评论区",
    #          category="sentiment", domain="a_share", ...),
]
```

**3.1.3 注册进 ALL_TOOLS**

```python
# 行 692 取消注释：
ALL_TOOLS: list[ToolMeta] = (
    ...
    + _NEWS_TOOLS
    + _SENTIMENT_TOOLS         # ← 取消注释
    + _HEALTH_TOOLS
)
```

---

### Step 2：实现 SentimentAnalystNode（新建 `app/graph/nodes/analysts/sentiment.py`）

**核心架构**：继承 `MarketAnalystNode`，完全对标 `NewsAnalystNode` 模式。

```python
"""舆情情绪分析员 — 执行 Supervisor 分配的 sentiment 类工具。

A0 版：仅接入雪球 list_stock_discussions（xq_discussions），
对评论区帖子做 LLM 情感极性打分 + 关键主题提取，
产出 Evidence 条目 + finding digest。

流程：
1. 从 route 获取分配给 category="sentiment" 的 tool_calls
2. 调用 MCP list_stock_discussions → 获取评论帖子列表
3. 截断至 SENTIMENT_MAX_COMMENTS 条
4. 构造 LLM prompt → 单次调用 LLM 产出情感分布 + 主题标签
5. 拆为 Evidence 条目（sentiment_bullish / sentiment_bearish / sentiment_neutral）
6. 生成 ≤200 字 finding digest
"""

class SentimentAnalystNode(MarketAnalystNode):
    category = "sentiment"
    
    def __init__(self, settings: Settings):
        super().__init__(settings)
        # 初始化 OpenAI client（用于情感分析 LLM 调用）
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )
    
    async def __call__(self, state):
        """重写入口：在父类工具执行后追加 LLM 情感分析步骤。"""
        # 1. 调用父类的工具执行（从 MCP 拉取原始评论数据）
        # 2. 从结果中提取评论文本
        # 3. 调 LLM 做情感分析
        # 4. 将情感分析结果转化回 ToolResult.normalized datum
        # 5. 返回 findings + evidence
```

**关键设计细节**：

#### 2.1 评论截断策略
- 从 `list_stock_discussions` 返回的帖子列表中取前 `SENTIMENT_MAX_COMMENTS` 条
- 每条帖子提取 `text` + `created_at`（保留时间上下文供 LLM 理解时效）

#### 2.2 LLM 情感分析 Prompt 设计
```
你是一个金融市场情绪分析助手。以下是从雪球股票评论区采集的 {N} 条用户帖子。

请分析这些帖子的整体情绪状态，返回严格的 JSON：

{
  "sentiment_score": 0.0-1.0（0=极度悲观, 0.5=中性, 1=极度乐观）,
  "bullish_ratio": 0.0-1.0（看涨帖子占比）,
  "bearish_ratio": 0.0-1.0（看跌帖子占比）,
  "neutral_ratio": 0.0-1.0（中性帖子占比）,
  "key_themes": ["主题1", "主题2", ...]（5 个以内的关键讨论主题）,
  "summary": "一句话概述评论区整体情绪（≤50 字）"
}

帖子列表：
---
{每条帖子的文本和时间}
---
```

#### 2.3 Produced Evidence 格式

每个情感维度产出 1 条 Evidence（共 3 条）：

```python
# 示例：sentiment analyst 产出的 evidence 条目
[
    Evidence(
        id="sentiment-001",
        source_tool="xq_discussions",
        domain="a_share",
        instrument=symbol,
        metric="sentiment_bullish",
        value=0.45,                             # LLM 返回的 bullish_ratio
        timestamp=datetime.now(UTC).isoformat(),
        status="success",
        note="看涨帖子占比: 45/100"
    ),
    Evidence(
        id="sentiment-002",
        source_tool="xq_discussions",
        domain="a_share",
        instrument=symbol,
        metric="sentiment_bearish",
        value=0.30,
        ...
        note="看跌帖子占比: 30/100"
    ),
    Evidence(
        id="sentiment-003",
        source_tool="xq_discussions",
        domain="a_share",
        instrument=symbol,
        metric="sentiment_neutral",
        value=0.25,
        ...
        note="中性帖子占比: 25/100"
    ),
    # 可选：额外一条 composite 指标
    Evidence(
        id="sentiment-004",
        source_tool="xq_discussions",
        domain="a_share",
        instrument=symbol,
        metric="sentiment_score",
        value=0.62,                             # LLM 返回的综合分数
        ...
        note="关键主题: 财报预期, 政策利好, 行业竞争 | 整体偏乐观"
    ),
]
```

#### 2.4 Finding Digest 格式（≤200 字）

```
sentiment: 雪球评论100条，情绪偏乐观(sentiment_score=0.62, 看涨45%/看跌30%/中性25%)；热门主题: 财报预期、政策利好、行业竞争
```

---

### Step 3：集成进 LangGraph（`app/graph/builder.py`）

**完全对标 NewsAnalystNode 的注册模式**：

```python
# 3.1 节点注册（在 builder.add_node 块中，行 167 之后）：
if settings.sentiment_enabled:
    sentiment_node = SentimentAnalystNode(settings)
    builder.add_node("sentiment", sentiment_node)

# 3.2 扇出映射（在 _fanout_map 中，行 197 之后）：
if settings.sentiment_enabled:
    _fanout_map["sentiment"] = "sentiment"

# 3.3 执行边（在 gate 边块中，行 210 之后）：
if settings.sentiment_enabled:
    builder.add_edge("sentiment", "gate")
```

同时，删除 builder.py:150-153 的 warning 日志行（sentiment_enabled=true 不再是无实现状态）。

```python
# 删除这三行：
if settings.sentiment_enabled:
    logger.warning(
        "sentiment_enabled=true, but the sentiment node is not implemented; this setting will be ignored."
    )
```

---

### Step 4：更新 Supervisor 路由（`app/graph/nodes/supervisor.py`）

```python
# 4.1 route_candidate_categories()（行 38-50）：
# 移除 sentiment 不可用的保护注释，改为：
def route_candidate_categories(settings) -> tuple[str, ...]:
    cats = ["technical", "fundamental", "moneyflow"]
    if getattr(settings, "news_enabled", False):
        cats.append("news")
    if getattr(settings, "sentiment_enabled", False):
        cats.append("sentiment")
    return tuple(cats)

# 4.2 _build_route()：
# category → analyst 映射已通过 by_category["sentiment"] 自动生效，
# 无需额外修改。确认 planner prompt 已包含 sentiment 工具 → 检查 prompts_graph.py。
```

**检查 `app/agent/prompts_graph.py`**：确认 PLANNER_PROMPT 中的工具列表已覆盖 sentiment category。因为 `_SENTIMENT_TOOLS` 取消注释后，`ALL_TOOLS` 会自动包含 sentiment 工具，`registry_text()` 会正确渲染它们。

---

### Step 5：更新 Prompt 和 Critic 规则

#### 5.1 `app/agent/prompts.py` — Reasoning Prompt 补充

在 REASONING_PROMPT 的证据评估部分添加：

```text
- 舆情情绪类证据（source_tool=xq_discussions, metric=sentiment_bullish/bearish/neutral/sentiment_score）：
  来自雪球个股评论区，代表散户/个人投资者的情绪倾向。注意：
  - 评论样本可能存在幸存者偏差（极端情绪用户更倾向发言）
  - 单只股票的评论情绪不能代表整体市场
  - 结合龙虎榜资金流向做多空交叉验证
```

#### 5.2 `app/graph/nodes/critic.py`（可选）

在 critic 的 `_evaluate_rule()` 或对应规则区补充：

```python
# 如果报告中使用了 sentiment 证据但未做交叉验证，标记为 warning：
"- 舆情结论（声称'散户情绪乐观'）如果仅依赖 xq_discussions 单源、"
"  没有与资金流 / 行情做关联验证，应在 critique 中标注为'单源、需与其他数据交叉确认'"
```

---

### Step 6：测试（新建 `tests/test_sentiment_analyst.py`）

| 测试用例 | 类型 | 覆盖内容 |
|----------|------|----------|
| `test_sentiment_node_skips_when_disabled` | 拓扑 | `sentiment_enabled=false` 时图结构和扇出不含 sentiment |
| `test_sentiment_node_registered_when_enabled` | 拓扑 | `sentiment_enabled=true` 时图结构含 sentiment 节点 + 扇出 + 边 |
| `test_sentiment_tool_in_registry` | 单元 | `xq_discussions` 在 `by_category["sentiment"]` 中 |
| `test_sentiment_digest_format` | 单元 | `_make_digest` 产出 ≤200 字正确格式摘要 |
| `test_sentiment_evidence_format` | 单元 | 情感分析结果正确转为 Evidence 条目（id、metric、value） |
| `test_sentiment_max_comments_truncation` | 单元 | 帖子数超过 `SENTIMENT_MAX_COMMENTS` 时正确截断 |
| `test_sentiment_llm_parse` | 单元 | LLM 返回的 JSON 正确解析为情感指标 |
| `test_sentiment_route_includes_sentiment_category` | 路由 | `route_candidate_categories` 在开关打开时返回含 `sentiment` 的元组 |
| `test_market_summary_minimum_no_sentiment` | 回归 | 市场级最低证据集**不**含 sentiment 工具 |

---

## 4. 架构集成图

```text
                    Supervisor (LLM 路由)
                      │
                      │ Send() 扇出
        ┌─────────────┼─────────────┬─────────────┬──────────────┐
        ▼             ▼             ▼             ▼              ▼
   technical    fundamental    moneyflow      news           sentiment
   (默认开)      (默认开)       (默认开)      (NEWS_ENABLED)  (SENTIMENT_ENABLED)
                                                              │
                                              ┌───────────────┘
                                              ▼
                                   ① execute: xq_discussions
                                      → MCP list_stock_discussions
                                      → 原始评论帖子列表
                                              │
                                              ▼
                                   ② truncate to SENTIMENT_MAX_COMMENTS
                                              │
                                              ▼
                                   ③ LLM 情感分析（单次调用）
                                      → {sentiment_score, bullish_ratio,
                                         bearish_ratio, neutral_ratio,
                                         key_themes, summary}
                                              │
                                              ▼
                                   ④ 拆为 Evidence 条目（3-4 条）
                                      + finding digest（≤200 字）
                                              │
                                              ▼
                                        Evidence Gate
                                              │
                                              ▼
                                        Reasoning (LLM)
                                        （与其他 analyst 证据合并）
```

---

## 5. 数据流约定

### 5.1 MCP `list_stock_discussions` → 实测返回结构

```
GET /market/discussions?symbol=SH600519
```

**已实测**（2026-07-21, iiix 0.8.4 + market-gateway, symbol=SH600519）：

```json
{
  "category": "...",
  "count": 10,
  "items": [
    {
      "author": "用户昵称",
      "author_id": 4589018077,
      "id": 411699738,
      "like_count": 0,
      "reply_count": 0,
      "repost_count": 0,
      "published_at": 1791530172000,
      "text": "帖子正文内容",
      "title": "与 text 相同",
      "url": "https://xueqiu.com/4589018077/411699738"
    }
  ],
  "max_page": ...,
  "page": 1,
  "sort": "...",
  "source": "xueqiu",
  "symbol": "SH600519"
}
```

**关键字段映射**（`_extract_posts` 依据）：
- 文本字段：`text`（第一候选）或 `title`
- 时间字段：`published_at`（毫秒时间戳）→ 存为字符串；备选 `created_at` / `time`
- 容器键：`items`
- 每页默认 10 条，可通过 `page` 翻页；Mosaic 只取第一页

### 5.2 Normalizer 路径

`list_stock_discussions` 的 normalizer 处理：检查 `app/gateway/normalizer.py` 中 `_NORMALIZERS` 注册。如果当前没有注册 → 子 agent 需添加。参考 `timeline` key 可能已有一条 normalizer 路径（因为它已在注册表中）。子 agent 需验证并确保 normalizer 产出正确的 `NormalizedDatum`。

### 5.3 ToolResult 结构调整

工具执行产出的 `ToolResult` 应包含：
- `tool = "xq_discussions"`
- `tool_key = "xq_discussions"`
- `arguments = {"symbol": "SH600519"}`
- `normalized = [...]` — 包含原始帖子数据的 datum 条目（供 sentiment node 内部使用）
- `status = "success" | "partial" | "error"`

---

## 6. 异常处理和降级

| 场景 | 处理方式 |
|------|----------|
| MCP `list_stock_discussions` 返回空（无评论） | status=success，datum 为空，finding digest: "sentiment: 无评论数据" |
| MCP 调用失败（network/gateway error） | status=error，不阻塞推理，finding digest 标记失败 |
| LLM 情感分析超时 | 回退为规则统计（帖子数 + 时间分布），finding digest 注明"LLM 不可用，降级为规则统计" |
| LLM 返回 JSON 解析失败 | 记录错误，不产出 sentiment evidence，仅在 finding 中报告工具已执行但分析失败 |
| 帖子数超过 token 预算 | 按 `SENTIMENT_MAX_COMMENTS` 硬截断，每条帖子截断 text 至 N 字符（建议 200 字/条） |
| `openai_base_url` 未配置 | 使用配置的 OpenAI client；与 reasoning/critic 共用同一 client 配置 |

---

## 7. 验收标准

1. **开关控制**：`SENTIMENT_ENABLED=true` 时 sentiment 节点参与扇出；`false` 时完全不影响三 analyst 基线
2. **SSE 进度**：`progress{step:"tool_call", node:"sentiment", message:"舆情分析员采集中"}` 正确推送
3. **Evidence 产出**：sentiment analyst 产出的 Evidence 出现在 `state.evidence` 中，格式与其他 analyst 一致
4. **Reasoning 引用**：Reasoning 节点的报告中能看到 sentiment 相关结论（如果 Supervisor 分配了 sentiment 工具）
5. **Critic 兼容**：Critic 对 sentiment 证据不产生误报（不会因为不认识 source_tool 而打回）
6. **降级不短路**：sentiment 失败时（MCP 错误 / LLM 超时），不影响整体报告产出
7. **所有现有测试通过**：在 sentiment_enabled=false 时零回归

---

## 8. 执行顺序建议

```
Step 1: tool_registry.py          ← 注册工具，让 Supervisor 能"看到" sentiment 工具
Step 2: sentiment.py              ← 核心实现（可与 Step 3 并行开发）
Step 3: builder.py                ← 图集成（依赖 Step 1+2）
Step 4: supervisor.py             ← 路由更新（依赖 Step 1）
Step 5: prompts.py + critic.py    ← Prompt/Critic 规则（可与 Step 4 并行）
Step 6: tests/                    ← 测试（依赖 Step 2-5）
```

---

## 9. 给子 Agent 的执行说明

1. **严格遵循以上设计决策**，不自由发挥
2. **NewsAnalystNode (`app/graph/nodes/analysts/news.py`) 是唯一参照模板** — sentiment node 的结构应尽可能对齐它
3. **不要修改 base.py 的通用骨架** — 所有个性化逻辑在 sentiment.py 子类中实现
4. **运行现有测试套件验证零回归** — 在 `sentiment_enabled=false` 下所有测试必须通过
5. **如有任何实现细节与设计冲突**，停下来记录冲突并在最终报告中标明
6. **先实测 `list_stock_discussions` 的返回结构**（通过 Gateway 或直接看 normalizer 路径），不要把"推测结构"当成事实写进代码