# Mosaic 代码审计修复计划（交接给执行 Agent）

> 行号基准：提交 `cabd00c`。若行号已漂移，以「定位锚点」里给的代码片段为准。
> 产出：两轮分模块审计（130 条发现）+ critical/high 的对抗性验证（23 确认 / 1 驳回）+ 本文档作者亲自实测复现。
> 每条任务的「置信度」标明证据强度，**执行前必须先用文中的复现命令确认问题存在**，复现不了就停下来报告，不要照改。

---

## 0. 给执行 Agent 的工作约定

### 0.1 硬性规则

1. **一次只做一个批次，批内一个任务一次提交。** 提交信息用仓库现有风格：`fix: <中文描述>` / `test: <中文描述>`。
2. **每个修复必须附带一个「改坏它会失败」的测试。** 这是本项目的核心教训：现有测试大量假阳性（见批次 4），只补代码不补有效测试，同类 bug 会以同样方式复发。
3. **不要顺手重构无关代码**，不要改 `.env`，不要把密钥写进任何文件，不要 `git add -A`（仓库根有临时目录风险）。
4. **不要动** `app/graph/nodes/critic.py` 的 system message（已修）、`app/memory/storage.py` 的 WAL 逻辑（已回退，不要恢复 fallback）。
5. 每个任务完成后运行：
   ```bash
   ruff check --no-cache app/ tests/
   ruff format --check --no-cache app/ tests/
   pytest -q --tb=short
   ```
   三者全绿才算完成（`--no-cache` 是为了绕开受限环境下 `.ruff_cache` 不可写，CI 上不需要）。
6. **凡是"把异常吞成默认值"的代码**（本项目大量存在），改的时候要保证：失败要么变成明确的 `status=error`/`errors`，要么降级为 `partial` + `note`，**不允许静默变成成功**。

### 0.2 每个任务的固定结构

| 字段 | 含义 |
|---|---|
| 定位锚点 | 文件 + 行号 + 一段可 grep 的代码，防止行号漂移 |
| 现象 | 用户/下游能观察到什么 |
| 复现 | 可直接粘贴运行的命令或脚本 |
| 根因 | 为什么会这样 |
| 修改 | 具体怎么改（给出目标代码形态） |
| 必补测试 | 断言什么，放在哪个文件 |
| 验收 | 改完必须成立的事实 |
| 风险 / 牵连 | 改动会影响谁，改完要顺手确认什么 |

### 0.3 批次划分与依赖

| 批次 | 主题 | 任务 | 依赖 |
|---|---|---|---|
| 1 | **P0 正确性**：正在产生错误结论或污染数据 | T1 T2 T3 | 无，先做 |
| 2 | **P1 高**：功能静默失效 / 可观测性 / 证据链可信度 | T4–T14 | T1、T2 完成后更稳（部分测试会复用它们） |
| 3 | **P2 中**：可靠性、成本、契约一致性 | T15–T27 | 无强依赖 |
| 4 | **测试有效性**：把假阳性测试改成真测试 | T28–T34 | 可与批次 2 并行，但建议在批次 1 之后（批次 1 会给它们提供真实回归场景） |
| 5 | **待人类决策**：需要产品判断，不要自行决定 | D1–D4 | — |

---

## 批次 1 — P0：正在产生错误结论或污染数据

### T1 — 空载荷被归一化为「成功证据」

**置信度**：已实测复现（本文档作者）

**定位锚点**
- `app/gateway/normalizer.py:777` — `if raw is None:`（唯一的判空守卫）
- 下游消费：`app/agent/evidence_gate.py:70`、`app/research/evidence.py:308-323`

**现象**：上游用「200 + 空 body / `{}` / 纯文本」表示查不到数据时，工具仍被记为成功，Evidence Gate 报 `has_evidence=True`，Reasoning LLM 在零数据点的情况下撰写报告。

**复现**
```python
from app.gateway.normalizer import normalize_tool_result
for raw in ({}, [], None, "Server busy", 0):
    r = normalize_tool_result("quote_tencent_quote_get", {}, raw)
    print(f"{raw!r:16} -> status={r.status} n={len(r.normalized)} err={r.error}")
```
当前输出：
```
{}               -> status=success n=0
[]               -> status=success n=0
None             -> status=error   n=0
'Server busy'    -> status=success n=1   ← 裸文本被 str() 成一条"证据"
0                -> status=success n=1
```

**根因**：`normalize_tool_result` 只把 `raw is None` 当失败；空容器走正常路径但产出 0 条 datum；非 dict/list 标量在 `else` 分支被 `str(raw)` 包装成 `metric="response"` 的"证据"。

**修改**
1. 把守卫扩成"无可用数据"的判定，例如：
   ```python
   if raw is None or raw == [] or (isinstance(raw, dict) and not raw):
       return ToolResult(..., status="error", normalized=[],
                         error="Gateway returned no data (empty response)")
   ```
2. 非 `dict`/`list` 标量（`str`/`int`/`bool`）不再包装成证据：返回 `status="error"`，`error` 里带上原文前 200 字（便于定位是上游报错文本还是真的空响应）。
3. 兜底不变式：**构造返回值前，若 `normalized` 为空则强制 `status="error"`**（防御以后新加的分支再犯同样错误）。
4. 同步修 `app/research/evidence.py:308-323`：该处在 `result.normalized` 为空但 `status=="success"` 时会造出一条 `value="success"` 的证据，正是 T1 的产物，应改为 `status="error"` + note，或直接不生成。

**必补测试**：`tests/test_normalizer_enhanced.py`（或新建 `tests/test_normalizer_empty.py`）
```python
@pytest.mark.parametrize("raw", [{}, [], "", "Server busy", 0, False])
def test_empty_or_scalar_payload_is_error(raw):
    r = normalize_tool_result("quote_tencent_quote_get", {}, raw)
    assert r.status == "error"
    assert r.normalized == []
```
并检查 `tests/test_data_integrity.py:47-50` 现有对 `{}` 的断言——它锁的是旧行为，需按新语义更新（若它断言 `success`，那正是 T1 的假阳性）。

**验收**：上面的复现脚本全部输出 `status=error`；`evidence_gate` 对这批结果报 `has_evidence=False`。

**风险 / 牵连**：非交易日上游可能合法返回空 → 改动后会被判 error，这是**期望行为**（无数据本就该是无证据）；但要确认 `reasoning` 不会因此直接崩（它会拿到 0 条证据，走 `data_caveats` 路径，属正常降级）。

---

### T2 — F10「指标 → {value, unit}」被折叠成单条，兄弟指标静默丢失

**置信度**：已实测复现

**定位锚点**
- `app/gateway/normalizer.py:605-613` — `if _is_eastmoney_f10_tool(_tool) and sub_containers: flattened = _deep_flatten_value(value)`
- 辅助函数 `_deep_flatten_value`：`app/gateway/normalizer.py:232-261`（对多键 dict「返回第一个数字」）

**现象**：一次 F10 调用里有多个指标时只保留第一个，其余（PE/PB…）永久丢失；存活那条的 `metric` 是父路径 `data.indicators`，无法判断是哪个指标。

**复现**
```python
from app.gateway.normalizer import normalize_tool_result
raw = {"data": {"indicators": {"ROE": {"value": 12.5, "unit": "%"},
                               "PE":  {"value": 15.2, "unit": "x"}}}}
r = normalize_tool_result("finance_eastmoney_f10_finance_get", {"symbol": "601398"}, raw)
print([(d.metric, d.value) for d in r.normalized])
```
当前输出：`[('data.indicators', 12.5)]`（PE 丢失，metric 无指标名）

**根因**：该分支对整块 `value` 做 `_deep_flatten_value`，而该函数遇到多键 dict 会**返回第一个**数值，然后用父路径落一条 datum。

**修改**
1. 当 `sub_containers` 含 **多个** 子键时，放弃"整块 flatten"，改为逐子键递归（与普通路径一致），使指标名保持 `data.indicators.ROE.value` 这种可读形式。
2. 只在一个子键、或子键名形如 `value`/`val` 时才允许走 flatten 快路径——且必须把命中的子键拼进 `metric`（如 `f"{path}.{hit_key}"`）。
3. `_deep_flatten_value` 返回候选值不唯一时（>1 个数值）不要"取第一个"，返回哨兵让调用方改走逐键展开。
4. `_make_datum`（`app/gateway/normalizer.py:535-547`）目前把 `unit` 硬编码为 `None`，而同级的 `unit` 键在通用路径下会变成一条字符串指标。改为：`_make_datum(..., unit=None)` 增加 `unit` 形参，F10 分支把同级 `unit` 写进 datum 的 `unit`；通用路径遇到 `value` 的同级 `unit` 也合并进该 datum 而不是产出独立指标。

**必补测试**：**强化** `tests/test_normalizer_f10.py:246-262`
```python
metrics = {d.metric: d.value for d in result.normalized}
assert metrics["data.indicators.ROE.value"] == 12.5   # 或修复后的命名
assert metrics["data.indicators.PE.value"] == 15.2
assert len(result.normalized) >= 2
```
**不要**再只用 `assert result.normalized` —— 这正是该缺陷长期存在的原因。
另外确认 `tests/test_normalizer_f10.py:220-224` 声明的单键形状 `{"data":{"indicators":{"PB":{"value":2.5}}}}` 仍然工作。

**验收**：复现脚本输出包含 2 个指标且 metric 名可辨识；F10 相关测试全绿。

**风险 / 牵连**：`unit` 语义变化会影响 `app/research/evidence.py` 的取值展示；改完跑 `tests/test_normalizer*.py` 全部。

---

### T3 — SSE 落库读错字段层级：对话摘要恒空 + 当日状态被空值覆盖

**置信度**：已实测复现（`model_dump()` 顶层无 `state_label`）

**定位锚点**
- `app/main.py:435-445` `_save_turn`：`report_dict.get("state_label", "")`
- `app/main.py:448-471` `_save_research_and_state`：`report_dict.get("state_label"/"strong_areas"/"confidence"/"anomalies")`
- 调用点：`app/main.py:372-375`（`save_result = result.model_dump()`）
- 对照组（正确写法）：`app/main.py:205-227`（`/api/ask`，用 `result.report.state_label`）
- 消费方：`app/scheduler/briefs.py:115`、`:134`、`:146-147`；`app/memory/storage.py:127-165`（**merge 写入**）、`:270-288`

**现象**
- 每轮 SSE 对话存进 `conversations.answer_summary` 的都是 `""` → `get_conversation_history` 只剩问题、没有答案 → 多轮上下文形同虚设且无报错。
- 当日 `daily_states` 被写入 `state_label=""`、`strong_areas=[]`、`confidence=""`；由于 `save_daily_state` 是 `merged.update(data)` 的**合并**语义，空值会覆盖当天先前写入的正确快照 → 简报里"市场当前状态"是空白（`.get("state_label", "N/A")` 拿到 `""`，连 N/A 都不显示）。
- SSE 路径完全没有写 `market_state`（`/api/ask` 有写）。

**复现**
```python
from app.models.response import ResearchResponse, MarketIntelligence
r = ResearchResponse(question="q", report=MarketIntelligence.model_validate(
    {"title":"t","market_state":"弱","state_label":"Neutral",
     "what_happened":"指数普跌","confidence":"low"}))
d = r.model_dump()
print(sorted(d.keys()))          # 无 state_label / what_happened
print(d.get("state_label"))      # None
```

**根因**：`_save_turn` / `_save_research_and_state` 按"扁平 report dict"写，但收到的是 `ResearchResponse` 的 dump（report 嵌在 `report` 键下）。SSE 与同步路径各写了一份持久化逻辑，属于同类"双路径漂移"（与之前 `build_response_from_state` 的 bug 同源）。

**修改**
1. **消除双路径**：抽出唯一的持久化函数，例如放在 `app/agent/` 或 `app/memory/`：
   ```python
   async def persist_research(result: ResearchResponse, question: str, conversation_id: str | None) -> None
   ```
   内部一律用 `result.report`（先判 `None`）读取字段，并**同时**写入 `/api/ask` 现在写的那几项（含 `market_state`）。
2. `app/main.py:205-227`（同步路径）改为调用同一函数；SSE 的 `create_task` 也调用它。
3. `result.report is None` 时：只保存研究记录（便于排查），**不写当日状态、不写对话轮次**，并记 warning。
4. 可选加固：`save_daily_state` 合并时跳过空值（`""`/`[]`）以免任何调用方再次清空当天数据 —— 若做，需单独提交并说明语义变化。

**必补测试**：新建 `tests/test_persistence.py`（需要 sqlite，见附录 A）
```python
async def test_sse_persistence_uses_report_fields(tmp_path):
    # 用真实 ResearchResponse（report 有 state_label="Neutral"）
    # 调用 persist_research 后断言：
    #   memory.get_daily_state()["state_label"] == "Neutral"
    #   memory.get_daily_state()["strong_areas"] == [...]
    #   memory.get_daily_state()["market_state"] == "弱"
    #   memory.get_conversation_history(conv_id) 里包含 answer summary 文本
```
再补一条：先写一份正常快照，再用 `report=None` 的结果调用持久化，断言当天状态**没有被清空**。

**验收**：新测试通过；手工跑一次 SSE 后 `daily_states` 当天行的 `state_label` 非空。

**风险 / 牵连**：`memory.db` 里已有历史空数据，不需要迁移；但 `briefs` 的读法（`briefs.py:115`）依赖扁平字段，改完要保持该形状不变。

---

## 批次 2 — P1 高

### T4 — Evidence id 跨 analyst 冲突，导致证据错配

**置信度**：已实测复现
**定位锚点**：`app/research/evidence.py:197`（`counter = 1`）、`:203/:241/:272/:294`（`id=f"evidence-{counter:03d}"`）；写入方 `app/graph/nodes/analysts/base.py:96-110`；消费方 `app/research/reasoning.py:56`（`{e.id: e}`）
**现象**：`build_evidence` 每次从 001 开始编号，三个并行 analyst 合并进同一 `state.evidence` 后 id 重复，`id_to_evidence` 后者覆盖前者 → 报告里引用的 `evidence-003` 可能 note 是 A 的、数值是 B 的，且无任何报错。
**复现**
```python
from app.research.evidence import build_evidence
from app.models.market import NormalizedDatum, ToolResult
def mk(tool, metric, value):
    return ToolResult(tool=tool, arguments={}, status="success",
                      normalized=[NormalizedDatum(metric=metric, value=value, tool=tool)])
print([e.id for e in build_evidence([mk("sentiment", "market_sentiment", 54)])])
print([e.id for e in build_evidence([mk("longhu", "net_inflow", 12.5)])])
# 两次都是 ['evidence-001']
```
**修改**：`build_evidence(results, id_prefix: str = "")` → `id=f"{id_prefix}{counter:03d}"`（或 `f"{id_prefix}-{counter:03d}"`）；`base.py:98` 传入 `self.category`。同时在 `reasoning.py:56` 构造 map 时检测重复 id：重复则记 warning 并退化为 `(id, source_tool)` 匹配。
**必补测试**：`tests/test_evidence.py` — 两次 `build_evidence` 用不同 prefix，断言 id 集合不相交；`reasoning` 层补一条"两路证据合并后按 id 取值唯一"的用例。
**验收**：合并后无重复 id；`reasoning` 的 note 与数值来自同一工具。
**风险**：报告/前端若按 id 渲染，改动后 id 格式变化（`technical-001`）；确认 `app/web/index.html` 与 `app/cli.py` 不硬编码 `evidence-` 前缀。

---

### T5 — `truncate()` 保留最旧 200 条却声称「保留最近 200」，且污染缓存

**置信度**：已实测复现（含 note 文本）
**定位锚点**：`app/graph/tool_runtime.py:88-103`
**现象**：`result.normalized[:200]` 保留的是**最旧**数据（K 线是时间升序），note 却写"保留最近 200"，且不置 `partial`；`market_cache.set` 存的是同一对象（`tool_runtime.py:84`），truncate 会**原地改写缓存**，下次 cache hit 再截断时 note 变成"原始 201 项"，真实条数丢失。
**修改**
- `kept = result.normalized[-200:]`（保留末尾/最新）
- 真实原始条数在切片**之前**取；note 不要把 note 自己算进计数
- 置 `result.status = STATUS_PARTIAL`、`partial=True`
- 不要在共享对象上原地改：缓存里存副本（或在 truncate 时先 copy）
**必补测试**：`tests/test_graph_nodes.py` 或新建 — 250 条 datum → 断言保留的是最后 200（首条 == 原第 51 条）、note 里的原始数 == 250、`status == "partial"`；再断言 cache hit 后 note 不变成 201。
**验收**：对升序 K 线，`candle_summary.price_last` 反映最新价。
**牵连**：`app/research/evidence.py:118` 的 `price_last` 依赖顺序 → 与 T7 一起验证。

---

### T6 — 文档承诺的证据硬上限 `_MAX_EVIDENCE_ITEMS` 从未生效

**置信度**：已实测（全仓只有定义处）
**定位锚点**：`app/research/evidence.py:9-11`（文档）、`:22`（`_MAX_EVIDENCE_ITEMS = 80`）；prompt 侧 `app/graph/nodes/reasoning.py:161`（`normalized_data` 全量 dump）
**现象**：单结果最多 200 条 datum（T5）× 最多 12 个工具全部进 prompt，文档承诺的 149KB 保护不存在。
**修改（二选一，推荐 A）**
- A：在 `build_evidence` 内实现上限：按 `source_tool` 分组、组内保留最新、总量截到 80，并给被截断的结果加 note。
- B：删掉常量与 `:9-11` 的文档，并在 `reasoning.py` 对 `normalized_data` 设上限。
**必补测试**：构造 12 个工具 × 50 条 datum，断言 `len(build_evidence(...)) <= 80`（或 prompt 侧上限生效），且 note 说明截断。
**验收**：prompt 体积有硬上限，且不依赖 mock。

---

### T7 — `candle_summary` 取到开盘价 + 混合载荷丢弃非 K 线指标

**置信度**：已实测复现
**定位锚点**
- `app/research/evidence.py:96-119`（`for price_key in ("c","close","price","last","h","l","o")` 把**所有**存在的键都 append，`price_last = prices[-1]`）
- `app/research/evidence.py:236-254`（`if candle_metrics:` → 追加摘要 → `continue` 位于 `valid_metrics` 循环之前）
**复现**
```python
from app.research.evidence import build_evidence
from app.models.market import NormalizedDatum, ToolResult
kn = [("candles[0].o",1.0),("candles[0].c",2.0),("candles[1].o",2.0),("candles[1].c",4.0)]
tr = ToolResult(tool="klines_market_klines_post", arguments={}, status="success",
      normalized=[NormalizedDatum(metric=m, value=v, tool="k") for m,v in kn]
                 + [NormalizedDatum(metric="openInterest", value=8.5, tool="k")])
for e in build_evidence([tr]): print(e.metric, e.value)
```
当前输出：`candle_summary {'count':2,'price_min':1.0,'price_max':4.0,'price_last':2.0}` —— `price_last` 是 `candles[1].o`（真实最新收盘是 4.0），`openInterest` **完全消失**。
**修改**
- 每根 K 线只取一个价格：按 `c/close/last/price` 优先级取第一个存在的键；`h/l` 单独用于区间字段，不参与 `price_last`。
- `price_last` 明确取最后一根的收盘价。
- 删除/修正 `:254` 的 `continue`，让 `valid_metrics` 正常写入；只跳过单个 candle 指标项。
**必补测试**：`tests/test_evidence.py` — 断言 `price_last == 4.0`、`price_min/max` 语义明确、且 `openInterest` 出现在证据里。
**验收**：混合型 payload 不丢指标。

---

### T8 — symbol 守卫把 news_search / 港股 / 龙虎榜 等无 symbol 工具静默跳过

**置信度**：已实测（逐个查过白名单）
**定位锚点**：`app/graph/nodes/analysts/base.py:195-200`；白名单 `:46-61`；`_extract_stocks` `:134-159`（只匹配 `(?<!\d)(\d{6})(?!\d)` 或 route 里 6 位代码）
**现象**：以下工具的 `tool_name` **不在** `WHITELIST_NO_SYMBOL`，而 `arguments` 里也没有 `symbol`：`news_search`、`search_xueqiu_search_get`、`longhu_xueqiu_longhu_get`、`internal_hk_northbound`、`internal_hk_index`。于是当用户问题不含 6 位 A 股代码（如"今天港股北向资金如何""某股最近有什么新闻"）时，这些步骤被 `continue` 跳过，日志是 **debug 级**，`errors` 为空，"执行了 0 个工具"却看不出功能没跑。
**复现**
```python
from app.graph.nodes.analysts.base import MarketAnalystNode
from app.gateway.tool_registry import resolve_tool
wl = MarketAnalystNode.WHITELIST_NO_SYMBOL
for k in ("news_search","search","longhu","hk_northbound_daily","hk_index_snapshot"):
    m = resolve_tool(k); print(k, m.tool_name, m.tool_name in wl)
# 全部 False
```
**修改（推荐做法）**：给 `ToolMeta` 增加 `requires_symbol: bool`（公司类工具 True，news_search/search/hk internal/longhu/health False），守卫改为 `if meta.requires_symbol and not arguments.get("symbol"): ...`。
短期最小改动：把上述 5 个 `tool_name` 加进 `WHITELIST_NO_SYMBOL`，并把跳过日志从 `debug` 提到 `warning`（带上 analyst 与 tool_key）。
**必补测试**：`tests/test_graph_nodes.py` — 用一个**不含 6 位代码**的问题跑 news analyst（`news_enabled=True`），断言 `news_search` 真的被执行（或至少 `tools_used` 包含它），而不是 `results == []`。现有 `tests/test_graph_topology.py:336-341` 用了带 `600519` 的问题，所以永远测不到这条路径 —— 新增用例必须不带代码。
**验收**：不带代码的问题也能触发 news/hk 工具；跳过时 warning 可见。

---

### T9 — CLI 在 `report is None` 时抛 AttributeError，真实错误被丢弃

**置信度**：静态审查 + 对抗性验证（含实测）
**定位锚点**：`app/cli.py:174` `report = result.report.model_dump()`
**对照**：`app/main.py:197` 已有 `if result.report is None:` 分支（同一次修复的 API 侧），CLI 漏了。
**现象**：推理失败（`app/graph/nodes/reasoning.py:101-112` 写 `report=None`）时，CLI 只打印 `AttributeError: 'NoneType' object has no attribute 'model_dump'`，`result.errors` 里真正的原因永远看不到。
**修改**：dump 前加 `if result.report is None:` → 把 `"\n".join(result.errors)` 打到 stderr 并以非 0 退出码结束（与 `main.py` 顺序一致）。
**必补测试**：`tests/` 里补一条 stub 出 `ResearchResponse(question=..., report=None, errors=["Reasoning engine failed: x"])` 的用例，断言 CLI 输出包含原始错误串且退出码非 0。

---

### T10 — 日志 handler 挂在 `mosaic`，但模块全用 `app.*` → 生产日志静默丢失

**置信度**：代码阅读确认 + 对抗性验证（实测 INFO 不打印）
**定位锚点**：`app/logging_config.py:84-91`（`logging.getLogger("mosaic")` + `propagate=False`）；模块侧 `app/graph/nodes/critic.py:20`、`app/graph/tool_runtime.py:26`、`app/agent/orchestrator.py:14` 等统一用 `logging.getLogger(__name__)`，命名空间是 **`app.*`**；`get_logger()`（`:101-103`）零调用方。
**现象**：CLI 与 FastAPI 进程里 `app.*` 的 INFO 全丢；WARNING+ 只经 root 的 lastResort 以裸文本进 stderr，结构化 JSON 日志承诺失效。`tests/test_ask_endpoint.py:33` 传了 `propagate=True`，把这个掩盖了。
**修改**：把 handler 挂到 root（`logging.getLogger()`），或同时对 `app` 与 `mosaic` 两个命名空间配置；统一改用 `get_logger(__name__)` 或统一用标准 `logging.getLogger(__name__)`（二选一，别混）。
**必补测试**：更新 `test_ask_endpoint.py` 里 `propagate=True` 的取值，并加一条断言：`caplog` 能抓到 `app.graph.nodes.critic` 的 INFO。
**验收**：跑一次 CLI，日志以 JSON 出现在 stdout。

---

### T11 — Critic 的 `verdict` 无约束：`fail` / `PASS` 被静默当成通过

**置信度**：静态审查 + 对抗性验证（实测 `'fail'`/`'PASS'`/`''` 均路由到 END）
**定位锚点**：`app/graph/nodes/critic.py:26`（`verdict: str`）；路由 `app/graph/builder.py:57-73`（未匹配值 `return "end"`，注释 `# "pass" or unknown`）
**现象**：模型返回 `fail`/`reject`/`PASS`/空串时，审计结论被判为通过——不记 errors，只有 `orchestrator.py:61` 一条 warning，报告照常返回。
**修改**
1. `verdict: Literal["pass","revise","research_more"]`，并在 `model_validate` 前归一化：`str(data.get("verdict","")).strip().lower()`。
2. 归一化后仍无法识别时：写 `errors`，并走**安全默认**（终止 + 告警），不要等同 pass。
3. 顺带修 `critic.py:188-207`：审计自身失败（LLM 超时 / JSON 坏）目前也返回 `research_more`，会被当成"证据不足"再跑一到两轮完整工具+LLM（烧钱）。引入独立 verdict（如 `error`）或直接返回原 report 并只记 errors，不要伪造 `missing_points`。
**必补测试**：参数化 `verdict` 为 `pass/revise/research_more/PASS/ fail/''/None`，断言：合法值走对应路由；非法值进 `errors` 且不触发 `research_more` 回环。
**验收**：`tests/test_graph_topology.py` 的 `critic_route_decision` 用例扩充后全绿。

---

### T12 — MCP 客户端从不握手（**先按 D1 决策再动手**）

**置信度**：机制已确认；**影响范围待决**（见下）
**定位锚点**：`app/gateway/mcp_client.py:65-75`（进入 `ClientSession` 后直接 `list_tools()`）；`pyproject.toml:14`（`mcp>=1.12`，当前环境实装 **2.1.1**）
**已确认的事实**
- `mcp==2.1.1` 的 `ClientSession.__aenter__` **只启动 dispatcher，不做握手**；类文档原文："enter as an async context manager, **then call `initialize()`**"。1.x 的 `__aenter__` 才会 `await self.initialize()`。
- 同版本 SDK 服务端有硬门禁：`mcp/server/runner.py:211-213`，未握手时除 `_INIT_EXEMPT = frozenset({"ping"})` 外一律 `MCPError(INVALID_PARAMS, "Invalid request parameters")`。
- 全仓 `app/` **没有任何** `session.initialize()` 调用。
**尚未确认**：`memory.db` 里 10-02 / 10-03 的记录显示 9 个工具全部 `success`、零 error，而 `mcp 2.1.1` 是 9/4 就装好的。唯一解释是当时跑的是 HTTP 模式（`.env` 在最后一次成功之后、10-03 16:05 被改过），或 iiix 服务端对未握手请求宽容。本沙箱禁止带管道 spawn 子进程，无法端到端验证。
**修改（无论 D1 怎么定，前两条都建议做）**
1. `connect()` 在 `list_tools()` 之前补握手并纳入超时预算：
   ```python
   await asyncio.wait_for(self.session.initialize(), timeout=self.settings.research_timeout_seconds)
   ```
   并把该处的 `TimeoutError`/`MCPError` 统一包成 `MCPConnectionError`。
2. `pyproject.toml` 把依赖收成大版本区间：`mcp>=2,<3`（或 `<2` 以保留 1.x 语义——**取决于 D1**）。
3. 顺带修 `mcp_client.py:47-84` 的清理：`connect()` 失败时 `stdio` 子进程不会被回收——`close()` 里 `if self.session is not None` 会跳过整个 `AsyncExitStack`，而 `__aenter__` 抛错后 `__aexit__` 不会执行。改法：`connect()` 用 try/except 包住，失败先 `await self.close()`；`close()` 去掉 session 判空、无条件 `await self.stack.aclose()`、并把 `stack` 重建为新的 `AsyncExitStack()`；清理失败日志从 `debug` 提到 `warning`。
**必补测试**：至少一个**真正走 `connect()`** 的测试（in-memory server 或 stdio 假 server），断言握手后 `list_tools()` 可用；现有 `tests/test_data_integrity.py:146-175` 直接注入 `FakeSession`，绕过了 `connect()`，所以 CI 永远绿 —— 不要沿用那个模式。

---

### T13 — `app/evaluation.py` 给出假验收结论（两条 critical）

**置信度**：对抗性验证 confirmed（含实测）
**定位锚点 1**：`app/evaluation.py:171-176`（`response.report.model_dump()` + `len(response.used_tools)`）、兜底 `:215-218`
**定位锚点 2**：`app/evaluation.py:272-277`（用结果下标取 `CASES[i]`）、`:364-374`（`all(...)` 空集为真）
**现象**
- `used_tools` 属于 `MarketIntelligence`，`ResearchResponse` 上没有该字段（字段列表见 `app/models/response.py:38-46`）→ 每个走通图的 case 都 AttributeError 记失败，`response.errors` 里的真实原因被丢弃；`report` 又允许为 `None`，`report.model_dump()` 同样会炸。
- `all(... for cid, r in zip(...) if r.success)` 在无成功案例时对空生成器求值为 **True** → 4 个 case 全失败仍打印 `Tool Accuracy: PASS`。
- `--cases 003` 子集运行按结果位置取 `CASES[i]`，与错误 case 的期望值比较。
**修改**
1. 先判 `response.report is None`，把 `"; ".join(response.errors)` 写进 `error` 字段；工具名从 `report.used_tools` 或 `tool_results` 统计，不要读 `response.used_tools`。
2. 评分改为 `if failed: FAIL`（空集不得为真）；期望值用 `{c["id"]: c for c in CASES}` 按 id 查；分母排除设计上无报告的空问题 case。
**必补测试**：`tests/test_evaluation.py`（新建）—— stub 出 `ResearchResponse(report=None, errors=[...])` 断言记录的是原始错误；构造 0 个成功 case 断言输出为 FAIL；用 `--cases` 子集断言与正确 case 比较。

---

### T14 — 定时简报几乎不会触发，且时区错误

**置信度**：对抗性验证 confirmed（实测 60 个启动相位仅 4 个命中）
**定位锚点**：`app/scheduler/briefs.py:63`（`datetime.now(UTC)`）、`:68` / `:70`（`now.second < 2`）、`:90`（`await asyncio.sleep(30)`）；相关 `:24-30` `_next_run` 的 naive/aware 混用
**现象**：2 秒触发窗口配 30 秒轮询 → 命中率约 1/15 且与启动相位绑定，漏一次等 24 小时；即使命中，UTC 9:15/15:30 是北京 17:15/23:30。`memory/morning`、`memory/evening` 目录为空可佐证从未生成。
**修改**：改为「算到目标时刻的剩余秒数再 sleep」，目标时间用 `Asia/Shanghai`（修正 `_next_run` 的 naive/aware 混用 TypeError），并记录当日已触发，避免重复/漏触发。
**必补测试**：注入可控时钟，断言在目标时刻前后能触发且同一日只触发一次；断言时区换算（UTC 01:15 == 北京 09:15）。

---

## 批次 3 — P2 中（可靠性 / 成本 / 契约）

> 这些不需要一次做完，按需挑；每条仍要求「改完补一个能失败的测试」。

### T15 — Evidence Gate 没有门控力（**需 D2 决策**）
`app/agent/evidence_gate.py:18-20` 文档说"`has_evidence=False` 时永远不应判 sufficient"，但全仓**无生产消费方**（只有测试引用）；`app/graph/builder.py:159` `gate → reasoning` 是无条件边。零证据时仍产出 200 + 完整报告。两条路：真的门控（`has_evidence=False` → 短路 END 或强制 `confidence=low` + `data_caveats` + errors），或删掉该字段与文档承诺。**不要留下"文档说门控、代码不门控"的状态。**

### T16 — 回边 reducer 累加，results/evidence/findings 成倍重复
`app/graph/state.py:50-64` 的 `add` reducer 在 `research_more` 回环时再追加一份；`route` 明确写了"不加 reducer，回环时整体覆盖"，这几个字段没有对应处理。消费方 `app/research/reasoning.py:161` 会把同一份行情数据重复塞进 prompt。改法：回边时用 `langgraph.types.Overwrite` 覆盖，或改成按 `(tool, arguments)` 去重的 reducer；`state.tool_results` 无任何读取方，可直接删除。

### T17 — `news_search` 的 6 小时 TTL 被 30 秒覆盖
`app/graph/tool_runtime.py:268-269` 用 `settings.news_search_ttl_seconds`（21600）写缓存，但 `execute()` 在 `:81-84` 又写一遍同一 key，`_resolve_ttl`（`app/cache.py:127-152`）没有 `news_search` 项 → 落回默认 30 秒，限流保护失效。改法：给 `_resolve_ttl` 加该键（需把 settings 传进去），或内部工具已缓存则不再覆盖。

### T18 — 每次工具调用都新建 Gateway 客户端（MCP 即每次 spawn 子进程）
`app/graph/tool_runtime.py:133-158` 每个 tool 一次 `async with gateway_cls(...)`；MCP 客户端的 `connect()` 会启动 `iiix mcp serve market-gateway` 并 `list_tools`（`app/gateway/mcp_client.py:47-75`）。一轮 12 个工具 = 12 次握手，回边再翻倍；HTTP 模式则每次新建 `AsyncClient`，连接池失效。改法：按 analyst 运行级别复用客户端（`ToolRuntime` 持有，节点结束/异常时关闭）。

### T19 — 缓存无上限 + `ttl=0` 失效
`app/cache.py:46-65`：`_store` 无容量上限，过期项只在读到**同键**时才删；`ttl or self._default_ttl` 使 `ttl=0`（想关缓存）落回默认值。改法：`set` 时顺带清理过期项 + `max_entries` LRU 淘汰；TTL 判断改 `ttl if ttl is not None else ...`。

### T20 — `MarketIntelligence` 必填字段导致整份报告被丢弃
`app/models/response.py:23-25`：`market_state`、`what_happened` 无默认值。LLM 少写一个或写 `null` → ValidationError → `app/research/reasoning.py:240-242` 转成 `LLMOutputError` → 节点返回 `report=None` → 整份报告和此前所有已付费的工具调用作废（调用方拿到 502 或空报告）。改法：给空串默认值，或在 reasoning 后处理里 `payload[k] = str(payload.get(k) or "")` 并写 `data_caveats`。注意 `reasoning.py:221-238` 的 `_ensure_list` 白名单里也没有 `anomalies`，而 `MarketIntelligence.anomalies` 的 prompt 从未请求过它（该字段恒空，前端 `index.html:411` 的 "What's Unusual" 永远不显示）——要么把 anomalies 加进 prompt + `_ensure_list`，要么用 `app/detector/anomaly.py` 的 `detect_anomalies` 在代码里填（它目前**没有任何生产调用点**）。

### T21 — SQLite 连接跨线程复用 + 写入失败只记 debug
`app/memory/storage.py:103-113` 在导入期建连接（`app/main.py:73` 的 `get_memory()`），实测 TestClient 下 async 端点运行于另一线程会抛 `sqlite3.ProgrammingError`；而 `main.py` 里所有写入失败都吞成 `logger.debug`（`:224`/`:243`）。改法：用 `threading.local` 或短连接按线程取连接（或 `check_same_thread=False` + 锁），失败至少记 WARNING。

### T22 — 异常检测阈值按百分比设定，但输入是绝对值
`app/detector/anomaly.py:96-168` 文案含 `{:.1f}%`、阈值 5/10，而 normalizer 给的是原始绝对值且 `unit=None`（`app/gateway/normalizer.py:536-547`）→ 真实 OI（≥1e9）每次快照都误报 critical；`fundingRate 0.0001 < 0.15` 永不触发。测试用 `MockDatum("openInterest", 8.5)` 这类假百分比值所以全绿。改法：按变化率或带 unit 的量级匹配，metric 精确匹配替代 `'oi'` 子串匹配，fixture 改用 normalizer 的真实输出。

### T23 — HTTP 重试不感知总预算
`app/gateway/http_client.py:91-176`：`max_retry_per_tool=1` → 2 次 × 30s 超时 + 429 退避（最多再加 ~12s）≈ 单工具 70s；`analysts/base.py:182-202` 串行执行最多 8 个工具 → 单 analyst 最坏 480s+，而 `main.py:192-195` 的总预算是 300s，结果是整次调查 504 而不是降级。改法：传入 deadline（剩余预算按剩余工具数均分），重试前检查剩余时间，超预算就以 `error` ToolResult 返回。

### T24 — 工具注册表：域过滤与注释相反、HK 工具 domain/category 漂移
`app/gateway/tool_registry.py:631-637`：注释说"始终包含 health 工具"，代码却把 `domain == "unknown"`（health 两个工具）过滤掉；域过滤为空时 `or ALL_TOOLS` 静默返回全部 40 个工具（实测 `registry_text(domains=["us_stock"])` 返回全量）。改法：按注释实现 `filtered + [t for t in ALL_TOOLS if t.domain == "unknown"]`，删掉 `or ALL_TOOLS`。
另 `:275-298`：`hk_quote`/`hk_search` 的 `domain="a_share"`（应为 `hk_stock`），且 `hk_search` 的 `category` 与 A 股同名工具不一致 → 同一 operationId 因 key 不同被分派给不同 analyst。`BY_NAME`（`:599`）按 tool_name 去重时"后注册者静默胜出"，建议改成多值表或直接删掉重复占位。

### T25 — 前端：并发请求互相踩计时器 + 单条异常毁掉整份报告
`app/web/index.html:241`/`:546-600`：`lastErrorTimeout` 是全局单值，调查中回车追问会并发两条 SSE，先结束者的 `finally` 清掉后到者的计时器 → 后到请求失去唯一中止手段；两行 loader 共用 `id="ai-loading"`。`ask()` 没有重入判断。
`app/web/index.html:391` `a.severity.toUpperCase()`：`anomalies` 是 `list[dict[str, Any]]`（后端无结构约束），`severity` 为数字/布尔时抛 TypeError，被 `ask()` 的 catch 变成"调查失败"，三分钟的成功报告整份丢弃。改法：按请求持有局部 controller/timer、`ask()` 开头防重入、`String(a.severity ?? '')` + 白名单类名、渲染循环单条 try/catch 降级。

### T26 — SSE 端点的错误分支不可达 + 后台任务只 cancel 不 await
`app/main.py:479-490`：`return StreamingResponse(_stream_research(...))` 在 try 内，但 async generator 体要到响应开始后才执行 → `except ValueError/Exception` 永不触发，图构建失败只得到 200 + SSE error 事件；而前端仅在非 2xx 时回退 `/api/ask`，于是永不回退。改法：在 try 内提前 `graph = _get_orchestrator()._ensure_graph()` 并读 settings，让 400/500 分支真实可达。
`app/main.py:314`/`:418-420`：pump 任务只 `cancel()` 不 `await`，取消结果无人取回，收尾异常落在无人 retrieve 的 task 上。改法：`finally` 里 `task.cancel()` 后 `await asyncio.gather(task, return_exceptions=True)`。
（注：审计曾报"`create_task` 不持引用会被 GC 导致落库丢失"，**该条已被对抗性验证驳回**：这两个协程体内没有 `await` 挂起点，创建后同一轮就执行完，不存在可被 GC 的窗口。仅"失败只记 debug"这一点值得改。）

### T27 — `called_signatures` 的"去重"契约不成立
`app/graph/tool_runtime.py:160-176`：签名只在**缓存命中**时才登记（`:174`），缓存未命中时从不登记 → 「已执行签名」集合无法阻止重复调用；更糟的是同参数第 3 次调用会因 `signature in called_signatures` 直接 `return None`，**绕过缓存**再打一次真实网关。改法：真实执行成功后立即登记签名，并在 `base._execute_tools` 用 `(tool_name, sorted(args))` 的 seen 集合真正跳过重复。

---

## 批次 4 — 测试有效性（系统性短板）

> **本项目的根本教训**：`MarketAnalystNode.__call__`、`GateNode`、`CriticNode` 都把异常吞成 `errors` 继续跑，于是「节点内部炸了」在测试里可能表现为"通过"。下面每条都是已验证的假阳性，请逐个改成真测试。

### T28 — `tests/test_graph_e2e.py:161-178`：断言只查类型，实际从未走到成功分支
fake 是 `lambda *a, **k: ([])`（不是 awaitable）→ `await` 抛 TypeError 被 `base.py:116` 吞掉、`failed=True`，而断言只有 `isinstance(f["failed"], bool)`，两种结果都通过。改法：`async def _execute_tools(self, state, sigs): return []`，断言 `f["failed"] is False` 且 `f["tools_used"] == []`，并删掉 `:162` 的 skip。

### T29 — `tests/test_reasoning_parsing.py:207-220`：测试自带一份 `_ensure_lists` 副本，断言与生产相反
副本把非法 `confidence` 置 `medium`，生产 `app/research/reasoning.py:231-234` 置 `low`。该类 6 个用例从不调用真的 `reason()`。改法：删掉副本，照 `tests/test_data_integrity.py:353-375` 的 `_stub_reasoning` 模式真调 `await ReasoningEngine.reason()`，断言 `confidence == "low"`。

### T30 — `tests/test_graph_nodes.py:93-99`：`assert node._execute_tools.__code__.co_varnames` 恒真
budget 守卫（`base.py:180-185`）删掉也不报错。补测：构造 `budget=1` 而 `tool_calls` 有 3 个的 route，断言只执行 1 次 / `len(results) == 1`。顺带修 `base.py:180` 的 `int(mine.get("budget") or len(...))`：显式 `budget=0` 会被 `or` 变成"全部执行"，这本身就是个 bug（见 T31 备注）。

### T31 — `tests/test_graph_e2e.py:118-136`：唯一的真实全链路 e2e 被 skip，理由已过期
skip 理由写"P0 图为 supervisor→kernel"，但 `builder.py:111-120` 早已注册三个 analyst。去掉 skip 即可通过。请更新/删除理由让它真跑；确不打算跑就改 `xfail(strict=False)` 并在 `PROJECT_STATUS.md` 标注未覆盖。

### T32 — `tests/test_market_memory.py:225-233`：名为"唯一约束"却从不触发约束
只连插三条并断言 `idx3 == 3`。补测：直接 INSERT 重复 `(conversation_id, turn_index)` 断言 `sqlite3.IntegrityError`；再加两条连接并发 `save_turn` 的用例（`storage.py:276-288` 的 `MAX+1` 存在 TOCTOU）。

### T33 — `tests/test_models_multi_domain.py:20-30`：循环体是运行时 no-op
`_: MarketDomain = val` 不产生任何校验，加个 `"bogus"` 也通过。改成真实例化断言（合法值通过、非法值 `ValidationError`）或对 `get_args(MarketDomain)` 做集合断言。

### T34 — 其余三条低价值假阳性（顺手修）
- `tests/test_graph_nodes.py:24-41`：`fake_execute` 缺 `self` 参数，永远无法生效（实测 `takes 3 positional arguments but 4 were given`），而用例又用 `FakeRuntime` 覆盖了 `_runtime`，该 fixture 完全空转。补 `self` 并让用例真正依赖它。
- `tests/test_graph_routing.py:146-156`：`FakeOpenAI(raise_on=...)` 对任何输入都抛，用例只测了 mock 的行为。补非空 question 的对照断言。
- `tests/test_tool_registry_multi_domain.py:104-110`：`assert A or B` 应为 `and`（或 `assert not {...} & set(...)`），并补 `registry_text(domains=["us_stock"])` 的用例以暴露 `or ALL_TOOLS` 回退。
- `tests/test_news_search.py:37-68`：错误路径用例打真实 DDGS 且断言恒真（`if count==0: isinstance(meta.get("error"), str) or meta.get("status")`，而 status 恒存在）。改为 monkeypatch `DDGS` 抛异常/返回空，断言 `meta.error`；顺带补 `news_search.py:56-66` 的空 query 分支。

### T35 — 机制性防线（建议在批次 4 一起做）
1. 在 `tests/conftest.py` 加一个约定/检查：**禁止把被测节点整体替换掉**（monkeypatch `__call__` 只能用于"非本次验证目标"的节点），至少在每个文件顶部写明"本文件 mock 掉了什么、因此没有覆盖什么"。
2. 给 `MarketAnalystNode.__call__` / `GateNode` / `CriticNode` 的兜底 `except` 分支加统一标记（例如 `errors` 前缀 + `finding.failed=True`），并**在关键测试里断言 `errors == []`**（`tests/test_graph_topology.py` 已开始这么做，请推广到所有 e2e 用例）。
3. 每个新增测试都自问：**"把对应生产代码改坏，它会不会变红？"** 不会就重写。

---

## 批次 5 — 需要人类决策（不要自行决定）

| ID | 问题 | 选项 |
|---|---|---|
| **D1** | MCP 通道实际用哪种模式？`pyproject.toml:14` 的 `mcp>=1.12` 收成 `>=2,<3` 还是 `<2`？ | A) 以 2.x 为准，补 `initialize()` 并按 v2 API 适配；B) 锁 `<2` 回到 1.x 语义（当时工具确实跑通过）。**注意 `.env` 现在写的是 `MARKET_GATEWAY_MODE=mcp`**，而 `memory.db` 里最后一次成功记录（10-03 08:10）早于 `.env` 改动（10-03 16:05）——建议先手工跑一次 MCP 模式确认现状。 |
| **D2** | Evidence Gate 要真门控还是降级为提示？ | A) `has_evidence=False` → 短路并返回明确错误；B) 只强制 `confidence=low` + `data_caveats` + errors；C) 删掉 `has_evidence`/`reason` 与文档承诺。 |
| **D3** | `anomalies` 字段怎么填？ | A) 加进 reasoning prompt 与 `_ensure_list`；B) 用 `app/detector/anomaly.py::detect_anomalies` 在代码里填（需设计调用点，它目前无生产调用）；C) 从前端与模型里删掉该字段。 |
| **D4** | `budget` / `max_tool_calls` 语义 | `config.py:27` 的 `max_tool_calls` 只出现在 `/health`，graph 路径实际不受它约束；`_execute_tools` 的 `budget` 恒等于 `len(tool_calls)`（恒真守卫）。要不要把两者真正接上？ |

---

## 附录 A — 在受限环境跑完整测试

正常情况下 `pytest -q` 即可。**在本项目当前的 Windows 沙箱会话里**，`memory/` 等子目录缺少写入授权，sqlite 相关模块（`test_ask_endpoint` / `test_stream_endpoint` / `test_market_memory` / `test_conversation` / `test_data_integrity`）会以 `unable to open database file` 失败——这是环境问题，不是代码问题。可用下面的 stub 把默认库重定向到可写根目录后运行（**不要提交这个文件**）：

```python
# run_tests_local.py  （放在仓库根，用完删）
import os, pathlib, sqlite3, sys
os.environ.setdefault("TEMP", r"D:\Mosaic\.tmp")
os.environ.setdefault("TMP", r"D:\Mosaic\.tmp")
import app.memory.storage as storage
ROOT = pathlib.Path(r"D:\Mosaic")

def factory(path):
    p = pathlib.Path(path)
    if p.parent.name == "memory":          # 默认项目库 -> 重定向到可写根目录
        p = ROOT / "__ci_memory.db"
    conn = sqlite3.Connection(str(p), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

storage._sqlite_connection_factory = factory
import pytest
sys.exit(pytest.main(sys.argv[1:] or ["-q", "tests/"]))
```
```bash
python run_tests_local.py -q                       # 全量
python run_tests_local.py tests/test_graph_nodes.py -q   # 单文件
```
注意：使用 `tempfile.mkdtemp()` 的用例在沙箱内仍会失败（新建子目录同样没有写入授权）；这类失败在 CI（ubuntu）上不会出现。`ruff` 同理，用 `--no-cache`。

## 附录 B — 每条修复的复现命令速查

```bash
# T1 空载荷
python -c "from app.gateway.normalizer import normalize_tool_result as n; [print(repr(r), (lambda x:(x.status,len(x.normalized)))(n('quote_tencent_quote_get',{},r))) for r in ({},[],None,'Server busy',0)]"

# T2 F10 折叠
python -c "from app.gateway.normalizer import normalize_tool_result as n; r=n('finance_eastmoney_f10_finance_get',{'symbol':'601398'},{'data':{'indicators':{'ROE':{'value':12.5,'unit':'%'},'PE':{'value':15.2,'unit':'x'}}}}); print([(d.metric,d.value) for d in r.normalized])"

# T3 字段层级
python -c "from app.models.response import ResearchResponse, MarketIntelligence as M; d=ResearchResponse(question='q',report=M.model_validate({'title':'t','market_state':'弱','state_label':'Neutral','what_happened':'跌','confidence':'low'})).model_dump(); print(sorted(d.keys()), d.get('state_label'))"

# T4 evidence id 冲突
python -c "from app.research.evidence import build_evidence as b; from app.models.market import NormalizedDatum as D, ToolResult as T; mk=lambda t,m,v:T(tool=t,arguments={},status='success',normalized=[D(metric=m,value=v,tool=t)]); print([e.id for e in b([mk('a','x',1)])],[e.id for e in b([mk('b','y',2)])])"

# T7 candle_summary
python -c "from app.research.evidence import build_evidence as b; from app.models.market import NormalizedDatum as D, ToolResult as T; kn=[('candles[0].o',1.0),('candles[0].c',2.0),('candles[1].o',2.0),('candles[1].c',4.0)]; tr=T(tool='klines_market_klines_post',arguments={},status='success',normalized=[D(metric=m,value=v,tool='k') for m,v in kn]+[D(metric='openInterest',value=8.5,tool='k')]); [print(e.metric,e.value) for e in b([tr])]"

# T6/T8 静态检查
grep -rn "_MAX_EVIDENCE_ITEMS" app/ | grep -v "= 80"
python -c "from app.graph.nodes.analysts.base import MarketAnalystNode as M; from app.gateway.tool_registry import resolve_tool as r; wl=M.WHITELIST_NO_SYMBOL; [print(k, r(k).tool_name in wl) for k in ('news_search','search','longhu','hk_northbound_daily','hk_index_snapshot')]"

# T11 verdict 归一化
python -c "from app.graph.builder import critic_route_decision as d; from app.config import Settings; s=Settings(); [print(repr(v), d({'critique':{'verdict':v},'revision_count':0,'report':{'what_happened':'x'}}, s)) for v in ('pass','revise','research_more','PASS','fail','',None)]"
# 期望（修复前）：'pass'->end  'revise'->reasoning  'research_more'->supervisor
#                 'PASS'/'fail'/''/None -> end（= 静默当通过，问题所在）

# T12 MCP 握手现状（静态）
grep -rn "initialize" app/ | grep -v "logging\|schema"
python -c "import importlib.metadata as m, inspect; from mcp import ClientSession; print(m.version('mcp'), 'initialize in __aenter__:', 'initialize' in inspect.getsource(ClientSession.__aenter__))"
```

## 附录 C — 修复顺序速览（可直接当 checklist）

```
批次 1（P0，必须连续做完）
[ ] T1 空载荷判失败            + 测试
[ ] T2 F10 折叠 / unit         + 强化 test_normalizer_f10
[ ] T3 SSE 落库字段层级        + 新建 test_persistence  （顺手确认 briefs 读法不变）

批次 2（P1）  建议顺序：T11 → T12 → T9 → T10 → T4 → T5 → T7 → T6 → T8 → T13 → T14
[ ] T4  evidence id 唯一化
[ ] T5  truncate 保留末尾 + partial + 不污染缓存
[ ] T6  evidence 条数上限（或删常量）
[ ] T7  candle_summary 取值 + 混合指标
[ ] T8  symbol 守卫白名单 / requires_symbol
[ ] T9  CLI report=None
[ ] T10 日志命名空间
[ ] T11 Critic verdict Literal + 失败语义
[ ] T12 MCP 握手 + 依赖区间 + 失败清理（先看 D1）
[ ] T13 evaluation 假验收
[ ] T14 briefs 调度时区与窗口

批次 3（P2）
[ ] T15 Evidence Gate 门控力（先看 D2）   [ ] T16 回边 reducer
[ ] T17 news_search TTL                  [ ] T18 复用 Gateway 客户端
[ ] T19 缓存上限                          [ ] T20 必填字段 / anomalies（先看 D3）
[ ] T21 sqlite 线程与日志                 [ ] T22 anomaly 阈值单位
[ ] T23 重试感知预算                      [ ] T24 注册表域过滤 / HK 漂移
[ ] T25 前端并发与渲染健壮性              [ ] T26 SSE 错误分支与任务收尾
[ ] T27 called_signatures 去重

批次 4（测试有效性）  T28 T29 T30 T31 T32 T33 T34 + T35 机制性防线

批次 5（决策）  D1 MCP 版本与模式   D2 Gate 是否门控   D3 anomalies   D4 budget/max_tool_calls
```

**完成定义**：三个批次全绿（`ruff` × 2 + `pytest`），每条修复都带一个「改坏它就会红」的测试，并且 `PROJECT_STATUS.md` 里对 `has_evidence` / `anomalies` / 证据上限 / MCP 模式的描述与代码实际行为一致（不再有"文档承诺、代码没有"的项）。
