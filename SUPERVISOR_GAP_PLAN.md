# Supervisor 回环缺口闭环 实施计划（SUPERVISOR_GAP_PLAN）

> 本文档是**可执行计划**，由主 agent 完成全部取证后编写，交给子 agent 直接落地。
> 文档内所有 `文件:行` 引用都在下述取证基线上**实际打开核对过**，不是推测。
> 落地时若发现与代码不符 → 见 §4 停止条件第 4 条，停下来回报，不要自行改设计。

## 文档头

**取证基线**：`HEAD = 660da3c`，工作区干净（`git status --porcelain` 为空）。

**基线测试（改动前，必须自己复跑一遍确认）**：

| 命令 | 期望结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest . -q` | `694 passed, 1 warning` |
| `.venv/Scripts/python.exe -m ruff check . --no-cache` | `All checks passed!` |
| `.venv/Scripts/python.exe -m ruff format --check . --no-cache` | `128 files already formatted` |

**执行纪律（硬约束）**：

1. 所有 Python 命令必须用 `.venv/Scripts/python.exe`（不要用全局 python / `py`）。
2. 测试**不得依赖真实网络与真实 LLM**：一切 LLM 走 monkeypatch/fake（范式见 `tests/test_b6_domain_filter.py:79-111` 的 `FakeOpenAI`），纯函数直测（范式见 `tests/test_hk_northbound.py`）。
3. 遵守 `tests/conftest.py:1-57` 的 T35 约定（见 §1 事实表最后一行）：**禁止整体替换被测节点**；任何替换 `__call__` 的地方必须就地写 `# T35-OK: <理由>`；每个新测试文件的模块 docstring 必须声明「mock 了什么 / 因此没覆盖什么」；关键用例必须断言 `errors == []` 或不含兜底降级标记；每写一条用例自问「把对应生产代码改坏，它会不会变红」。
4. 注意 `market_cache` 全局污染：涉及缓存/签名去重的行为必须在**同一条用例内顺序执行**。
5. 收工前 `ruff check . --no-cache` 与 `ruff format --check . --no-cache` 必须全绿；格式化用 `.venv/Scripts/python.exe -m ruff format . --no-cache`。
6. commit message 用中文，前缀 `feat:` / `fix:` / `test:` / `docs:`；**不要 push**。
7. 完成后必须在本文档 §7 追加「执行记录」（实测表 / 实施摘要 / 与计划真值的偏差 / 测试数字）。

**执行顺序裁定（不要改顺序）**：

```
A0 探针闸门（先写红测试，证明缺口存在）
 → A1 新纯函数模块 app/graph/gap_loop.py
 → A2 prompts_graph.py 新增 {revision_context} 槽位
 → A3 supervisor 接线（渲染上下文 + 代码级 drop/补齐）
 → A4 critic 产出结构化 missing_tool_keys + 假注释/假文档修正
 → A5 config + .env.example 新增 gap_max_steps
 → A6 测试补全（节点级 / critic 级）
 → A7 文档同步
 → A8 三步验证 + 双模式全量 + 提交 + §7 执行记录
```

A4 与 A1–A3 有依赖：A3 依赖 A4 产出的 `missing_tool_keys` 字段，所以**先做 A4 的模型字段**（`Critique.missing_tool_keys`）再接线也可以；本文档按上面顺序写，落地时允许把 A4 的「字段新增」提到 A3 之前，但**不得跳过任何一步**。

---

## §0 目标与范围

### 0.1 要解决的缺陷（用户提出的原始断言，已核实为真）

`app/graph/nodes/critic.py` 判 `research_more` 后，`app/graph/builder.py:174-182` 把控制权交回 `supervisor`，但**回环边不携带任何 payload**。`Supervisor._plan()`（`app/graph/nodes/supervisor.py:75-144`）只从 state 读三个字段：`conversation_id`(`:82`)、`domain`(`:93`)、`question`(`:98`) —— 它**不读** `state["critique"]`、不读 `missing_points`、不读 `state["results"]`（上一轮跑过什么）。同时 `state["route"]` 是 list 无 reducer（`app/graph/state.py:119` 注释明说「research_more 回环时整体覆盖」），域过滤在无显式域时是全量注册表（`app/graph/nodes/supervisor.py:94-97`）。

后果（不是「重复证据」，而是「白烧预算 + 缺口永不补上」）：

- `results` / `evidence` / `findings` 有去重 reducer（`app/graph/state.py:44-85`），所以**不会**重复累积；但 `app/graph/nodes/analysts/base.py:208` 的 `seen` 与 `:93` 的 `called_signatures` 都是**节点调用级局部集合**，第二轮选中同一工具会**真的重新执行**；
- `budget_deadline` 是标量、无 reducer、回环覆盖（`app/graph/state.py:111-114`），只在图启动前写一次（`app/agent/orchestrator.py:54`），第二轮拿到的是**第一轮的残值预算**（`base.py:215-225`），更容易撞 deadline 返回 error 结果；
- Critic 真正想要的缺口工具若没被新 plan 随机选中，**就一直空着**；`critic_max_revisions=2`（`app/config.py:75`）让循环有界，结局是「带着同样缺口发报告」。

### 0.2 本计划要达成

1. **信息闭环**：回环轮的 planner prompt 里出现「Critic 的 reason + missing_points + 建议补齐的工具 key + 已执行且已拿到数据的工具清单」。
2. **结构闭环**：Critic 能产出**结构化** `missing_tool_keys`（从本次可见注册表里选），Supervisor 在代码层做两件确定性的事 —— 丢掉「已拿到数据且参数被覆盖」的重复步骤、把 Critic 点名且 LLM 没覆盖的缺口工具**强制补进 plan 头部**（预算耗尽前优先执行）。
3. **顺手修真话**：`app/graph/nodes/critic.py:6` 的「最多 1 次」、`:33` 的假注释、`docs/architecture.md:62/:159` 的「最多 1 次」，全部改成与代码一致的真话。

### 0.3 本计划**不做**（明确排除，越界即停在 §4）

- **不动图拓扑**：`app/graph/builder.py` 的节点与边一律不新增/不删除（尤其不加「Critic → 定向 analyst」的边）。
- **不改 state 结构**：不给 `research_more` 回环引入新的 reducer，不改 `route` / `budget_deadline` 的覆盖语义。
- **不动 analyst 侧去重**：`app/graph/nodes/analysts/base.py` 的 `seen` / `called_signatures` 保持节点调用级局部集合（跨轮去重由 Supervisor 侧的 `drop_repeat_steps` 兜）。
- 不实现 `early_stop`（全仓无消费方），不实现 sentiment 节点，不改 `route_candidate_categories`。
- 不重构 Critic 的 verdict 语义，不改 `error` 安全终止逻辑。

---

## §1 关键真值事实（写代码只用这一节，不要重新调研）

| # | 事实 | 位置 | 取证方式 |
|---|---|---|---|
| 1 | `_plan()` 只读 `conversation_id`(:82) / `domain`(:93) / `question`(:98)，全文再无其他 `state.get` | `app/graph/nodes/supervisor.py:75-144` | 通读函数 |
| 2 | 域过滤：有显式域 → `registry_text(domains=[explicit_domain,"cross"])`；否则全量 | `app/graph/nodes/supervisor.py:94-97` | 通读 |
| 3 | `PLANNER_PROMPT.format(...)` 是**全仓唯一** format 点；占位符只有 `{question}{enabled_domains}{registry}{conversation_history}{max_steps}` | `app/graph/nodes/supervisor.py:100-106`、`app/agent/prompts_graph.py:11-88` | grep `PLANNER_PROMPT` |
| 4 | 截断在域守卫**之前**：`plan.steps = plan.steps[:max_research_steps]`(:135)，域守卫 `f"- {s.tool_key}:" not in registry`(:141-143) | `app/graph/nodes/supervisor.py:135-144` | 通读 |
| 5 | `_plan` 返回 `(plan, filtered_keys)`；`__call__` 组装 `{"intent":…, "route": self._build_route(plan)}`，守卫过滤时写 `errors`，异常兜底 `"Supervisor routing failed: …"` | `app/graph/nodes/supervisor.py:51-73` | 通读 |
| 6 | `Critique` 字段：`verdict`/`reason`/`unsupported_claims: list[str]=[]`/`missing_points: list[str]=[]`；`:33` 注释「research_more 时喂回 Supervisor」描述的数据流**从未实现** | `app/graph/nodes/critic.py:28-34` | 通读 |
| 7 | 模块 docstring 写「research_more 最多 1 次」= **假**，实际上限是 `settings.critic_max_revisions=2` | `app/graph/nodes/critic.py:6`、`app/config.py:75` | 通读+配置 |
| 8 | Critic 看得见「跑过哪些工具」：`_format_evidence_for_review` 读 `gate.successful_tools/partial_tools/error_tools` + `results[:20].normalized[:10]` | `app/graph/nodes/critic.py:107-128` | 通读 |
| 9 | Critic prompt 的 JSON schema 行含 `"missing_points": [...]`；prompt 由 `prompt_pieces` 拼装，末尾追加 JSON 说明 | `app/graph/nodes/critic.py:170-194` | 通读 |
| 10 | Critic `domain = intent.domain or state["domain"] or "a_share"`；`Critique.model_validate` 后返回 `{"critique": critique}` | `app/graph/nodes/critic.py:165`、`:225-239` | 通读 |
| 11 | `RouteResult` 无关；`critic_route_decision`：`research_more` → `end` if 无 report，否则 `supervisor` if `revision_count < critic_max_revisions` else `end` | `app/graph/builder.py:38-85` | 通读 |
| 12 | `_supervisor_fanout`：route 里出现过的 analyst；**route 为空则回退 `["technical","fundamental","moneyflow"]`** | `app/graph/builder.py:134-143` | 通读 |
| 13 | `results` reducer `_merge_results`，key = `(tool, json.dumps(arguments, sort_keys=True, ensure_ascii=False))`；`evidence`/`findings` 各有 key 去重 | `app/graph/state.py:44-85` | 通读 |
| 14 | `route` 无 reducer（注释「整体覆盖」）、`budget_deadline` 无 reducer（注释「回环时整次调查预算不重置」） | `app/graph/state.py:111-119` | 通读 |
| 15 | **`ToolResult.tool` 存的是 gateway operationId（tool_name），不是 registry key**；`execute()` docstring 明确举例 `snapshot_get` | `app/graph/tool_runtime.py:316-334`、`:342-349` | 通读 |
| 16 | tool_name → registry key 必须用 `resolve_tool_by_name(tool_name)`（不在表里抛 `KeyError`） | `app/gateway/tool_registry.py:760-764` | 通读 |
| 17 | `registry_text(domains)` 行格式 `- {key}: {tool_name} [{domain}] — {purpose} [{priority}]`；`resolve_tool(key)` 未知抛 `KeyError` | `app/gateway/tool_registry.py:729-757` | 通读 |
| 18 | `ToolMeta` 有 `.key/.tool_name/.domain/.purpose/.priority/.category`（`_build_route` 用 `.category`） | `app/gateway/tool_registry.py:753-757`、`app/graph/nodes/supervisor.py:154-155` | 通读 |
| 19 | registry 体量实测：全量 `47 行/4371 字符`；`["a_share","cross"]` `27/2424`；crypto `16/1634`；us_stock `11/1556`；hk_stock `11/1329` → 把域过滤注册表喂 Critic 成本可接受 | `registry_text()` | 实跑 |
| 20 | analyst 执行：只跑 `state["route"]` 里 `analyst == self.category` 的 assignment；`budget -= 1` 在真正 execute **之前**；symbol 由守卫注入（`meta.tool_name not in WHITELIST_NO_SYMBOL and not arguments.get("symbol")`）；`seen`/`called_signatures` 均为节点调用级局部集合 | `app/graph/nodes/analysts/base.py:174-272`（:208/:245-251/:253-257/:269） | 通读 |
| 21 | analyst 的 `_build_arguments` 只注入 `symbol` 与 `get_company_finance.periods`；**news 类工具需要 `query`** | `app/graph/nodes/analysts/base.py:274-295`、`app/agent/prompts_graph.py:43` | 通读 |
| 22 | `ToolCallPlan(tool_key, arguments, purpose, priority="medium")`；`ResearchPlan(intent, steps, early_stop=False)` | `app/models/research.py:56-66` | 通读 |
| 23 | 配置：`max_tool_calls=12`(:44)、`max_research_steps=8`(:45)、`research_budget_seconds=300`(:55)、`critic_max_revisions=2`(:75)、`news_enabled=False`(:85)；`.env.example` 同步 `MAX_TOOL_CALLS`(:30)/`MAX_RESEARCH_STEPS`(:31)/`RESEARCH_BUDGET_SECONDS`(:37) | `app/config.py`、`.env.example` | grep |
| 24 | 架构文档待修的真话：`:62`「research_more 时带 missing_points 回 Supervisor 补充研究（最多 1 次）」、`:159` 同类表述、`:55` Critic 表行、`:84` `CRITIC_MAX_REVISIONS` 表行 | `docs/architecture.md` | grep |
| 25 | `tests/test_docs_contract.py:32` 读 `critic.py` 源码文本；`:76-78` 断言 `'"error"' in CRITIC and "Verdict" in CRITIC`（本计划不破坏它） | `tests/test_docs_contract.py` | 通读 |
| 26 | supervisor 单测范本：`FakeOpenAI`（`async def create(...)` 记录 `last_prompt`，返回 `SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.plan_json))])`）+ `_make_node(plan_json)`（`SupervisorNode.__new__` + `SimpleNamespace(max_conversation_turns=10, max_research_steps=8, openai_model="test-model")`，把 `fake.create` 挂到 `node.client.chat.completions.create`） | `tests/test_b6_domain_filter.py:79-111` | 通读 |
| 27 | T35 约定（必须遵守）：禁止整体替换被测节点；替换 `__call__` 处写 `# T35-OK: <理由>`；文件 docstring 声明 mock 范围；关键用例断言 `errors == []`；污染类行为放在一条用例内顺序执行 | `tests/conftest.py:1-57` | 通读 |

---

## §2 分步实施

### A0 探针闸门（先红后绿，不得跳过）

目的：用**会失败的测试**证明缺陷真实存在，再动生产代码。

1. 复跑基线三条命令，把输出粘进 §7。
2. 先创建 `tests/test_supervisor_gap_loop.py`，只写第 **T1–T4**（见 A6 清单里的节点级三条 + critic 级一条，其它用例留到 A6）。这三条断言的是「回环轮 prompt 必须含 Critic 缺口与已执行工具」以及「缺口工具必须出现在 route 里」。
3. 跑 `tests/test_supervisor_gap_loop.py` → **必须红**（`revision_context` 还不存在、代码级补齐还不存在）。
4. 把红色输出的关键行粘进 §7（这就是取证）。
5. 若这三条测试**本来就绿** → 停，说明 §1 事实已过期（见 §4 停止条件第 4 条）。

### A1 新增纯函数模块 `app/graph/gap_loop.py`

要求：**纯函数、无 IO、无 LLM、无 async**，便于直测。整文件如下（可直接粘贴，粘贴后跑 ruff 校正格式）：

```python
"""research_more 回环的缺口上下文与步骤修补（纯函数，无 IO / 无 LLM）。

背景：Critic 判 research_more 时只把控制权交回 Supervisor（回环边不携带 payload），
而 Supervisor._plan() 只读 conversation_id/domain/question —— 第二轮既不知道 Critic
指出的缺口，也不知道上一轮跑过哪些工具（route 被整体覆盖）。本模块渲染 planner 可见的
缺口上下文，并在代码层做两件确定性的事：

1. drop_repeat_steps：丢掉「同工具已拿到数据、且计划参数被已执行参数覆盖」的步骤；
2. append_gap_steps：把 Critic 点名、LLM 没覆盖、且在本次注册表文本里的缺口工具补进 plan 头部。

注意 ToolResult.tool 存的是 gateway operationId（tool_name），不是 registry key；
需要 registry key 时一律走 tool_registry.resolve_tool_by_name()。
"""

from __future__ import annotations

from typing import Any

from app.models.research import ToolCallPlan

#: 视为「已拿到数据」的状态 —— error 不算，允许回环轮重试失败的工具
_DONE_STATUSES = ("success", "partial")

#: 这些 tool_name 前缀的工具必须带 query，缺口补齐时用用户问题兜底
_QUERY_TOOL_PREFIXES = ("news_",)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def executed_index(results: Any) -> dict[str, list[dict]]:
    """state["results"] → {tool_name: [arguments, ...]}（只含 success/partial）。"""
    index: dict[str, list[dict]] = {}
    for r in results or []:
        if _field(r, "status") not in _DONE_STATUSES:
            continue
        name = _field(r, "tool", "")
        if not name:
            continue
        args = _field(r, "arguments", {}) or {}
        index.setdefault(name, []).append(dict(args))
    return index


def executed_keys(results: Any) -> set[str]:
    """已拿到数据的工具对应的 registry key 集合（不在注册表中的工具名忽略）。"""
    from app.gateway.tool_registry import resolve_tool_by_name

    keys: set[str] = set()
    for name in executed_index(results):
        try:
            keys.add(resolve_tool_by_name(name).key)
        except KeyError:
            continue
    return keys


def allowed_keys(registry: str) -> set[str]:
    """从 registry_text() 输出里抽出全部 key（行格式 `- {key}: {tool_name} [...]`）。"""
    keys: set[str] = set()
    for line in registry.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            keys.add(stripped[2:].split(":", 1)[0].strip())
    return keys


def render_unexecuted_registry(registry: str, results: Any) -> str:
    """注册表文本去掉「已拿到数据的工具」那些行，供 Critic 挑补充工具。"""
    executed = executed_keys(results)
    lines = [
        line
        for line in registry.splitlines()
        if not (line.strip().startswith("- ") and line.strip()[2:].split(":", 1)[0].strip() in executed)
    ]
    return "\n".join(lines) or "（无可补充的工具）"


def _args_subset(planned: dict, executed: dict) -> bool:
    """planned 的参数是否被 executed 覆盖（`{}` 视为总是被覆盖）。"""
    for k, v in planned.items():
        if k not in executed or executed[k] != v:
            return False
    return True


def is_repeat_step(step: ToolCallPlan, results: Any) -> bool:
    """该 step 是否为「同工具已拿到数据、且参数已被已执行调用覆盖」的重复。"""
    from app.gateway.tool_registry import resolve_tool

    try:
        meta = resolve_tool(step.tool_key)
    except KeyError:
        return False
    for args in executed_index(results).get(meta.tool_name, []):
        if _args_subset(dict(step.arguments or {}), args):
            return True
    return False


def drop_repeat_steps(steps: list[ToolCallPlan], results: Any) -> tuple[list[ToolCallPlan], list[str]]:
    """丢掉重复步骤 → (保留的 steps, 被丢弃的 tool_key 列表)。"""
    kept: list[ToolCallPlan] = []
    dropped: list[str] = []
    for step in steps:
        if is_repeat_step(step, results):
            dropped.append(step.tool_key)
        else:
            kept.append(step)
    return kept, dropped


def sanitize_missing_tool_keys(
    raw: Any, allowed: set[str], executed: set[str]
) -> tuple[list[str], list[str]]:
    """过滤 Critic 给的缺口 key：只留 allowed 里、未拿到数据、去重后的 key。

    Returns:
        (合法 keys, 被丢弃的 keys)
    """
    kept: list[str] = []
    dropped: list[str] = []
    for key in raw or []:
        if not isinstance(key, str) or not key:
            continue
        if key in allowed and key not in executed and key not in kept:
            kept.append(key)
        else:
            dropped.append(key)
    return kept, dropped


def gap_tool_keys(critique: Any, registry: str, results: Any) -> tuple[list[str], list[str]]:
    """Critic 的缺口 key ∩ 本次注册表可见 key − 已拿到数据的 key。"""
    return sanitize_missing_tool_keys(
        _field(critique, "missing_tool_keys", []),
        allowed_keys(registry),
        executed_keys(results),
    )


def append_gap_steps(
    steps: list[ToolCallPlan],
    gap_keys: list[str],
    registry: str,
    max_steps: int,
    max_gap_steps: int,
    question: str = "",
) -> tuple[list[ToolCallPlan], list[str]]:
    """把缺口 key 以 high 优先级补进 steps **头部**（幂等）再截断 → (新 steps, 补入的 key)。"""
    from app.gateway.tool_registry import resolve_tool

    allowed = allowed_keys(registry)
    present = {s.tool_key for s in steps}
    forced: list[ToolCallPlan] = []
    for key in gap_keys:
        if key in present or key not in allowed or len(forced) >= max_gap_steps:
            continue
        try:
            meta = resolve_tool(key)
        except KeyError:
            continue
        arguments: dict[str, Any] = {}
        if meta.tool_name.startswith(_QUERY_TOOL_PREFIXES):
            arguments["query"] = question
        forced.append(
            ToolCallPlan(
                tool_key=key,
                arguments=arguments,
                purpose="Critic 指出的证据缺口（代码级补齐）",
                priority="high",
            )
        )
        present.add(key)
    merged = forced + list(steps)
    return merged[:max_steps], [s.tool_key for s in forced]


def render_gap_context(critique: Any, results: Any, max_steps: int) -> str:
    """渲染回环轮上下文块（塞进 PLANNER_PROMPT 的 {revision_context}）。"""
    if critique is None:
        return "（首轮，无缺口信息）"
    reason = _field(critique, "reason", "") or "（未给出）"
    missing_points = _field(critique, "missing_points", []) or []
    missing_keys = _field(critique, "missing_tool_keys", []) or []
    lines = [
        "Critic 判定: research_more",
        f"Critic 理由: {reason}",
        "Critic 指出的证据缺口（自然语言）:",
    ]
    lines.extend([f"  - {p}" for p in missing_points] or ["  - （无）"])
    lines.append("Critic 建议补充的工具 key（必须全部列入 steps）:")
    lines.extend([f"  - {k}" for k in missing_keys] or ["  - （无）"])
    lines.append("已执行且已拿到数据的工具（不要重复规划这些调用）:")
    index = executed_index(results)
    if index:
        for name, arg_list in sorted(index.items()):
            rendered = "; ".join(str(a) for a in arg_list[:3]) or "{}"
            lines.append(f"  - {name} 参数: {rendered}")
    else:
        lines.append("  - （无）")
    lines.append(f"本轮最多规划 {max_steps} 个步骤。")
    return "\n".join(lines)
```

**硬要求**：

- 保持 `from app.gateway.tool_registry import ...` 的**函数内延迟导入**（模块顶层导入 registry 会拖入 gateway 依赖，延迟导入与 `supervisor.py:148` 的既有风格一致）。
- `_QUERY_TOOL_PREFIXES` 用 `str.startswith(tuple)`（`tool_name.startswith(_QUERY_TOOL_PREFIXES)`）。
- 不要在这里写日志；日志留在 A3/A4 的节点里。

### A2 `app/agent/prompts_graph.py`：新增 `{revision_context}` 槽位

在 `PLANNER_PROMPT`（`app/agent/prompts_graph.py:11-88`）的**对话历史段之后**插入：

```
--- 补充研究轮上下文（回环轮才有内容） ---

{revision_context}

硬要求：若上面列出了 "Critic 建议补充的工具 key"，你必须把这些 key **全部**列入 steps；
          不要重复规划 "已执行且已拿到数据的工具" 里的调用（它们本轮不会带来新数据）。
```

同时把第三步/输出要求里已有的「不要重复调用同一工具，最多规划 {max_steps} 个步骤」（`:83`）保留不动 —— 两句不冲突，一句讲通用去重，一句讲回环轮硬要求。

**硬要求**：只加 `{revision_context}` 这一个占位符；`PLANNER_PROMPT` 里已有的 `{{` 转义（JSON 输出说明）不要动。改完用 A0 的红测试验证提示词生效。

### A3 `app/graph/nodes/supervisor.py`：接线

1. 文件顶部加 `import logging` 与 `logger = logging.getLogger(__name__)`（该文件目前没有 logger）。
2. `app/graph/nodes/supervisor.py:98` 之后、`:100` 之前插入（沿用 `state` 既可能是 dict 也可能是模型的双分支写法）：

```python
        critique = state.get("critique") if isinstance(state, dict) else getattr(state, "critique", None)
        results = state.get("results", []) if isinstance(state, dict) else getattr(state, "results", [])
        revision_context = render_gap_context(critique, results, self.settings.max_research_steps)
```

3. `PLANNER_PROMPT.format(...)`（`:100-106`）增加一个实参：`revision_context=revision_context,`。
4. `return plan, filtered_keys`（`:144`）之前插入（**必须在域守卫之后**，这样缺口 key 也受同一套域可见性约束）：

```python
        # 回环轮（research_more）的代码级闭环：先丢掉已拿到数据的重复步骤，
        # 再把 Critic 点名、LLM 没覆盖、且在本次注册表文本里的缺口工具补到最前面
        # （预算耗尽前优先执行）。快乐路径不写 errors，保住 errors == [] 断言。
        gap_keys, invalid_gap_keys = gap_tool_keys(critique, registry, results)
        if invalid_gap_keys:
            logger.warning("Supervisor: Critic 缺口 key 被丢弃（不可见或已拿到数据）: %s", invalid_gap_keys)

        plan.steps, dropped_repeat = drop_repeat_steps(plan.steps, results)
        if dropped_repeat:
            logger.info("Supervisor: 回环轮丢弃已拿到数据的重复步骤: %s", dropped_repeat)

        plan.steps, forced_gap = append_gap_steps(
            plan.steps,
            gap_keys,
            registry,
            max_steps=self.settings.max_research_steps,
            max_gap_steps=self.settings.gap_max_steps,
            question=question,
        )
        if forced_gap:
            logger.info("Supervisor: 代码级补齐 Critic 缺口工具: %s", forced_gap)
```

5. 顶部 import 区加 `from app.graph.gap_loop import append_gap_steps, drop_repeat_steps, gap_tool_keys, render_gap_context`（与既有 import 风格保持一致即可）。

**硬要求**：

- **不得**在快乐路径写 `state["errors"]`；`filtered_keys` 的既有行为与文案（`f"tool_call_filtered: {key}（不在本次域过滤后的注册表文本中，疑似幻觉/串域，已跳过）"`）保持不变。
- `_build_route` 不动：缺口步骤同样经 `resolve_tool(...).category` 分组，unknown key 兜底 `technical` 即可。
- 顺序不能反：`drop_repeat_steps` 必须在 `append_gap_steps` **之前**（否则补进来的缺口步骤可能被 drop 规则误伤 —— 缺口 key 都是「未拿到数据」的，理论上不会被 drop，但顺序固定更稳）。

### A4 `app/graph/nodes/critic.py`：结构化缺口 + 修真话

1. `Critique`（`:28-34`）新增字段（放在 `missing_points` 之后）：

```python
    missing_tool_keys: list[str] = []  # 建议补充的工具 registry key（research_more 时喂回 Supervisor，代码级补齐）
```

2. 模块 docstring（`:6`）把「research_more 最多 1 次」改成真话，例如：「`research_more` 回环由 `settings.critic_max_revisions` 限次（默认 2）；回环轮 Supervisor 会读到本节点的 `missing_points` / `missing_tool_keys` 与已执行工具清单」。

3. `:33` 的 `missing_points` 注释保持（本计划落地后它才成立），并把新增字段的注释写成上面那条。

4. prompt 拼装（`:170-194`）：在「=== 实际证据 ===」之后、`if rules:` 之前插入「可补充的工具」小节：

```python
            from app.gateway.tool_registry import registry_text

            prompt_pieces.extend(
                [
                    "\n=== 可补充的工具（本域注册表，已排除已拿到数据的工具）===\n",
                    render_unexecuted_registry(registry_text(domains=[domain, "cross"]), results),
                ]
            )
```

> 延迟 import 写在 `__call__` 内部（与既有 `critic.py` / `supervisor.py:148` 风格一致）；`render_unexecuted_registry` 从 `app.graph.gap_loop` 顶层导入即可。

5. JSON schema 行（`:189-190`）改为：

```python
                    '{"verdict": "pass|revise|research_more", "reason": "...", '
                    '"missing_points": [...], "missing_tool_keys": [...], "unsupported_claims": [...]}',
```

6. 并在 `:186-188` 的说明文字里补一句硬要求：`missing_points 用自然语言描述缺口；missing_tool_keys 只能从上面「可补充的工具」小节里挑 key（可多个；只有 verdict=research_more 时填）。`

7. `Critique.model_validate`（`:225-235`）之后、返回之前，做落库前过滤（用**与 prompt 同一份**可见集合）：

```python
            gap_allowed = allowed_keys(registry_text(domains=[domain, "cross"]))
            critique.missing_tool_keys, invalid_gap_keys = sanitize_missing_tool_keys(
                critique.missing_tool_keys, gap_allowed, executed_keys(results)
            )
            if invalid_gap_keys:
                logger.warning("Critic: 丢弃不可见或已拿到数据的缺口 key: %s", invalid_gap_keys)
```

> 若该文件没有 logger，同样补 `import logging` + `logger = logging.getLogger(__name__)`。
> 注意 `critique` 是 `Critique` 实例（pydantic 模型默认允许赋值，仓库既有代码已这么做；若校验报 `frozen`/`validate_assignment`，改用 `critique = critique.model_copy(update={"missing_tool_keys": kept})`）。
> `registry_text` 此时需要在 `__call__` 内可见：第 4 步已在 `__call__` 内 import 过，**不要重复 import**，直接复用。

**硬要求**：`_format_evidence_for_review` 不动；`:150-157` 的 report 为 None 分支可以顺手补 `missing_tool_keys=[]`（默认值即可，不必显式写）。

### A5 `app/config.py` + `.env.example`

1. `app/config.py`：在 `critic_max_revisions`（`:75`）附近新增

```python
    gap_max_steps: int = 3  # research_more 回环轮最多代码级补齐几个缺口工具
```

2. `.env.example`：在 `CRITIC_MAX_REVISIONS`（如存在）或 `MAX_RESEARCH_STEPS`(:31) 附近加一行 `GAP_MAX_STEPS=3`。
3. 若 `app/config.py` 有配置项列表/文档映射（`docs/architecture.md:84` 一带的表），同步加一行。

### A6 测试补全

**新文件 1：`tests/test_supervisor_gap_loop.py`**

模块 docstring 必须写明：mock 了 LLM（fake chat.completions.create 返回固定 plan JSON）；**未覆盖**真实 LLM 输出、真实 gateway 调用、analyst 实际执行链路、真实网络。

纯函数用例（不 mock 任何东西）：

| ID | 用例 | 断言要点 |
|---|---|---|
| T1 | `test_executed_index_only_counts_data` | `success`/`partial` 进 index，`error` 不进（允许回环重试） |
| T2 | `test_executed_keys_maps_tool_name_to_key` | 用 `app.gateway.tool_registry.ALL_TOOLS[0]` **探测式**取 `(tool_name, key)` 对，构造 `ToolResult(tool=..., status="success")` → `executed_keys() == {key}`（**不要硬编码 key**） |
| T3 | `test_drop_repeat_steps_subset_rule` | 已执行 `{"symbol":"600519"}`；计划 `{}` 与 `{"symbol":"600519"}` → 丢弃；`{"symbol":"000001"}` → 保留 |
| T4 | `test_drop_repeat_steps_keeps_retry_after_error` | 状态为 `error` 的同工具同参数 → 保留 |
| T5 | `test_append_gap_steps_prepends_and_idempotent` | 缺口 key 出现在 steps **首位**、`priority == "high"`；对已含该 key 的 steps 再调一次 → 不重复补 |
| T6 | `test_append_gap_steps_rejects_unknown_and_truncates` | 幻觉 key 不补；`max_gap_steps` 生效；结果长度 ≤ `max_steps` |
| T7 | `test_append_gap_steps_fills_query_for_news_tools` | 从 `ALL_TOOLS` 里探测一个 `tool_name.startswith("news_")` 的 key → 补入步骤的 `arguments["query"] == question` |
| T8 | `test_sanitize_missing_tool_keys_filters_and_dedups` | 非字符串/空串/不可见/已执行/重复 → 进 `dropped`，其余进 `kept` |
| T9 | `test_render_gap_context_first_round` | `critique=None` → `"（首轮，无缺口信息）"` |
| T10 | `test_render_gap_context_contains_gap_and_executed` | 文本含 `reason`、`missing_points` 原文、`missing_tool_keys`、已执行工具的 tool_name |

节点级用例（用 `tests/test_b6_domain_filter.py:79-111` 的 `FakeOpenAI` + `_make_node` 范式；`settings` 记得带上 `gap_max_steps=3`）：

| ID | 用例 | 断言要点 |
|---|---|---|
| T11 | `test_revision_round_prompt_includes_gap_section` | state 带 `critique`（`missing_points=["缺少资金费率数据"]` + 探测式合法 `missing_tool_keys`）与 `results`（已执行）→ `fake.last_prompt` 含 `"缺少资金费率数据"`、含该 tool_name、含「Critic 建议补充的工具 key」；且 `result["errors"] == []` |
| T12 | `test_first_round_prompt_has_no_gap_section` | 无 `critique` → prompt 含 `"（首轮，无缺口信息）"`，且不含 `"Critic 建议补充的工具 key（必须全部列入 steps）"` 之外的任何缺口文本（用 T11 的缺口字符串做否定断言） |
| T13 | `test_planner_ignoring_gap_still_gets_tool_in_route` | fake plan JSON 只返回「已执行过的那个工具」→ 产物 `route` 里必须出现缺口 key；重复的那个 tool_key 不在 route 里；`result["errors"] == []` |
| T14 | `test_gap_key_not_in_registry_is_not_forced` | Critic 给幻觉 key → 不补、`errors == []`、只写 logger.warning |

**新文件 2：`tests/test_critic_gap_keys.py`**

模块 docstring 声明：mock 了 LLM（fake client 返回固定 Critique JSON）；未覆盖真实 LLM 判断质量与真实 gateway。

> 写之前先读 `tests/test_critic_verdict.py` 顶部的 fake client fixture 与 `_make_node` 写法，**照搬那套范式**（不要另造一套）。
>
> ⚠️ 实测坑：该文件的 `_FakeClient`（`:96-106`）**不记录 prompt** —— `_Completions.create(self, **kwargs)`（`:85-88`）把 kwargs 直接丢掉。T15 需要断言 prompt 内容，所以要在照搬时给 `_Completions` 加一个 `self.last_prompt = None`，并在 `create` 里 `self.last_prompt = (kwargs.get("messages") or [{}])[-1].get("content", "")`，之后用 `node.client.chat.completions.last_prompt` 读回（与 `tests/test_b6_domain_filter.py:79-111` 的 `FakeOpenAI.last_prompt` 同一手法）。

| ID | 用例 | 断言要点 |
|---|---|---|
| T15 | `test_critic_prompt_lists_supplementary_tools` | fake 返回 `verdict=research_more` + 合法 `missing_tool_keys` → 发出的 prompt 含「可补充的工具」小节；**已拿到数据的工具那一行不在 prompt 里**；返回的 `critique.missing_tool_keys == [该 key]`；`errors == []` |
| T16 | `test_critic_drops_hallucinated_and_executed_gap_keys` | fake 返回 `[幻觉 key, 已执行 key, 合法 key]` → 落库只剩 `[合法 key]`；`errors == []`（过滤不是错误） |

**硬要求**：所有探测式取 key 都要走 `ALL_TOOLS` / `registry_text()`，**不得硬编码工具 key 字符串**（工具集会变，硬编码会随注册表演进腐烂）。所有节点级断言都要包含 `errors == []`（T14 是唯一例外，因为它验证的是 warning 路径 —— 此时仍应断言 `errors == []`，因为过滤只写 logger）。

### A7 文档同步

- `docs/architecture.md:62`：`research_more 时带 missing_points 回 Supervisor 补充研究（最多 1 次）` → 改成真话：`research_more 时回 Supervisor 补充研究（≤ CRITIC_MAX_REVISIONS 次，默认 2）；回环轮 Supervisor 会读到 Critic 的 missing_points / missing_tool_keys 与已执行工具清单，并在代码层补齐缺口工具、丢弃重复步骤`。
- `docs/architecture.md:159`：同样把「（最多 1 次）」改成「（≤ CRITIC_MAX_REVISIONS 次）」。
- `docs/architecture.md:55`（Critic 表行）：补一句「输出 `missing_points` / `missing_tool_keys` 供回环轮补齐」。
- `docs/architecture.md:84`（配置表）：补 `GAP_MAX_STEPS` 行。
- grep 全仓「最多 1 次」与「missing_points」，把其余不一致处一并对齐（README 若有同样表述也要改）。
- `app/graph/nodes/critic.py:6` 的假 docstring 已在 A4 修，此处只需确认没有第二处。

### A8 验证与提交

1. **三步验证**（全部必须绿）：
   - `.venv/Scripts/python.exe -m pytest . -q` → 期望 `passed = 694 + 新增用例数`，`0 failed / 0 error`；
   - `.venv/Scripts/python.exe -m ruff check . --no-cache`；
   - `.venv/Scripts/python.exe -m ruff format --check . --no-cache`（不绿就先 `ruff format . --no-cache` 再复查）。
2. **双模式全量测试**（`news_enabled` 会影响 `route_candidate_categories`，必须两种都跑）：
   - 默认模式（上面第 1 条即默认）；
   - `$env:NEWS_ENABLED="true"; .venv/Scripts/python.exe -m pytest . -q; Remove-Item Env:NEWS_ENABLED`。
3. **A0 红→绿复测**：A0 里的三条测试现在必须全绿，把「红输出行 + 绿输出行」都贴进 §7。
4. **真链路 e2e（可选）**：仅当仓库存在既有、可离线或已配好 `.env` key 的真链路脚本时才跑一次；否则在 §7 注明「未跑 + 原因」。**不要为了跑 e2e 新建脚本、不要新增网络依赖**。
5. **提交**：中文 commit message，建议 1–2 个提交，例如
   - `feat: 回环轮把 Critic 缺口与已执行工具喂给 Supervisor，并代码级补齐缺口工具`
   - `test: 覆盖回环轮缺口闭环（prompt 注入 / 重复步骤丢弃 / 缺口强制补齐 / Critic key 过滤）` + `docs: 修正 research_more 回环的真实语义（≤2 次）并补 GAP_MAX_STEPS`
   **不要 push。**
6. **§7 执行记录**必填四项：实测表（命令 → 结果）、实施摘要（改了哪些文件、加了多少用例）、**与计划真值的偏差**（任何与 §1 不符的地方都要写清楚）、测试数字（前 694 → 后 N，ruff 状态）。

---

## §3 交付物清单

| 类型 | 路径 | 说明 |
|---|---|---|
| 新增 | `app/graph/gap_loop.py` | 纯函数缺口闭环（A1） |
| 修改 | `app/agent/prompts_graph.py` | `{revision_context}` 槽位 + 回环硬要求（A2） |
| 修改 | `app/graph/nodes/supervisor.py` | 渲染上下文 + drop/补齐接线（A3） |
| 修改 | `app/graph/nodes/critic.py` | `missing_tool_keys` + 可补充工具小节 + 过滤 + 修真话（A4） |
| 修改 | `app/config.py`、`.env.example` | `gap_max_steps` / `GAP_MAX_STEPS`（A5） |
| 新增 | `tests/test_supervisor_gap_loop.py` | T1–T14（A6） |
| 新增 | `tests/test_critic_gap_keys.py` | T15–T16（A6） |
| 修改 | `docs/architecture.md` | 回环语义与配置表（A7） |
| 追加 | 本文档 §7 | 执行记录（A8） |

---

## §4 停止条件（触发即停，不要自行扩大范围）

1. 全量 pytest 出现任何 `failed` 或 `error`（除已知环境问题外）→ 停止并回报，**不得带着红测试收工**。
2. `ruff check` / `ruff format --check` 不绿且无法在不改设计的前提下修好 → 停止并回报。
3. 发现必须**改图拓扑**、**新增 state reducer**、或在 analyst 侧改去重才能达成 §0.2 → 停止并回报（超出本计划范围）。
4. 发现 §1 任一「真值事实」与代码不符（尤其：回环轮 prompt 已经带了缺口信息 / `ToolResult.tool` 其实是 registry key）→ 停止并回报，本计划取证过期。
5. 需要在测试里真实调用 gateway / 真实 LLM 才能验证 → 停止（禁止），改为纯函数直测 + fake。
6. 发现新增用例在**改动前就已经绿**（即测试没抓到缺陷）→ 停止并回报，重新设计用例（T35 第 ④ 条）。

---

## §5 已知边界（本计划**不解决**，无需在实现中处理）

1. `budget_deadline` 仍是整次调查的标量预算：回环轮跑的是**残值预算**，`research_budget_seconds=300` 用完后第二轮工具会撞 deadline 返回 error。本计划**不重置**它（重置会破坏「整次调查预算」的既有语义，属另一议题）。
2. `state["route"]` 无 reducer、整体覆盖：回环轮的 route 只反映最新 plan。`results`/`evidence`/`findings` 不丢（有去重 reducer），所以历史数据仍在。
3. route 为空时 `_supervisor_fanout` 回退 `["technical","fundamental","moneyflow"]`（`builder.py:134-143`），三个 analyst 会空跑（无 assignment）。若 A3 的 drop 把 steps 全清空，就会出现一次空跑 —— 视为可接受成本，不修。
4. analyst 侧 `seen` / `called_signatures` 仍是节点调用级局部集合（`base.py:93/:208`）：同一轮内去重、跨轮不去重；跨轮重复由 Supervisor 的 `drop_repeat_steps` 兜底。
5. Critic 只能从**本域注册表（domain + cross）**里挑缺口 key，跨域缺口（例如 a_share 问题里想要 crypto 工具）选不到 —— 这是域可见性的延续，符合既有设计。
6. `early_stop` 依旧无消费方。
7. 缺口工具被补进 steps 头部 → 在预算耗尽前优先执行；但若 `gap_max_steps` 大于该 analyst 的 `budget`（`max_tool_calls=12` 与显式 budget 取 min），仍可能被 budget 截断。
8. `drop_repeat_steps` 允许「不同参数」的重复调用（如换 symbol），也允许重试 `error` 的工具；这是「参数子集」规则的直接后果。

---

## §6 待确认开放问题（实现时若遇到，按此默认，不要停下来问）

1. `gap_max_steps=3` 是否合适？→ 默认按 3 实现，写在 §7 里作为可调项。
2. Critic 的 `missing_tool_keys` 是否允许点名「已 error 的工具」？→ **允许**（`executed_keys` 只统计 success/partial），这是有意的重试通道。
3. 回环轮 drop 后 steps 为空是否要让 Supervisor 再问一次 LLM？→ **不问**（会翻倍 LLM 成本），接受一次空跑。
4. `render_unexecuted_registry` 是否要把 `purpose` 一起给 Critic？→ 给整行（key + tool_name + domain + purpose + priority），成本已实测可接受（§1 事实 19）。

---

## §7 执行记录（由子 agent 完成实现后追加，不得留空）

### 7.1 基线复测（改动前）

取证方式：`git worktree add .a0_probe HEAD`（HEAD=660da3c，detached）跑一次，**不动主工作区的未提交改动**，跑完 `git worktree remove .a0_probe --force`。基线 pytest 全量在 worktree 里跑；ruff 两条命令排除了拷进 worktree 的两个新测试文件与探针脚本本身（否则量到的是新代码，不是基线）。

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest . -q`（干净 HEAD） | `694 passed, 1 warning in 11.05s` ✅ 与计划 §文档头一致 |
| `.venv/Scripts/python.exe -m ruff check . --no-cache` | `All checks passed!` |
| `.venv/Scripts/python.exe -m ruff format --check . --no-cache` | `128 files already formatted` |

**A0 红测试输出关键行**（干净 HEAD 上跑 `tests/test_supervisor_gap_loop.py` 的两条节点级用例）：

```
>       assert _GAP_TEXT in fake.last_prompt
E       AssertionError: assert '缺少资金费率数据' in '你负责为 Mosaic 制定研究计划。\n\n用户问题：\n贵州茅台今天怎么样\n
        当前启用的市场域：a_share, crypto, hk_stock, commodities, us_stock\n\n可用的工具注册表（按域分组...Z 前缀）\n
        不要重复调用同一工具，最多规划 8 个步骤。\n\n--- 输出要求 ---\n...'
        # ↑ HEAD 的 planner prompt 里完全没有「补充研究轮上下文」这一段
>       assert gap_meta.key in routed
E       AssertionError: assert 'limit_up_count' in ['sentiment']
E        +  where 'limit_up_count' = <app.gateway.tool_registry.ToolMeta object ...>.key
        # ↑ 缺口工具没进 route，反倒是 LLM 规划的「已执行过的重复工具」原样留着
FAILED tests/test_supervisor_gap_loop.py::test_revision_round_prompt_includes_gap_section
FAILED tests/test_supervisor_gap_loop.py::test_planner_ignoring_gap_still_gets_tool_in_route
2 failed in 2.35s
```

Critic 侧在 HEAD 上的等价红证据（`tests/test_critic_gap_keys.py` 在 HEAD 上是**收集期 ImportError**：`ModuleNotFoundError: No module named 'app.graph.gap_loop'`，说明新模块根本不存在；为了让红证据落在行为上而不是缺模块上，另跑了一个临时探针脚本，对真实 `CriticNode.__call__`（fake client）断言三件事）：

```
Critique 字段: ['missing_points', 'reason', 'unsupported_claims', 'verdict']   # 没有 missing_tool_keys
prompt 含「可补充的工具」小节: False                                            # 没有结构化缺口入口
落库 critique 有 missing_tool_keys 属性: False
```

### 7.2 实施摘要

| 文件 | 动作 | 内容 |
|---|---|---|
| `app/graph/gap_loop.py` | 新增（210 行） | 纯函数模块：`executed_index` / `executed_keys`（operationId→registry key，延迟 import）、`allowed_keys` / `render_unexecuted_registry`、`is_repeat_step` / `drop_repeat_steps`、`sanitize_missing_tool_keys` / `gap_tool_keys`、`append_gap_steps`、`render_gap_context` |
| `app/agent/prompts_graph.py` | 修改 | `PLANNER_PROMPT` 在对话历史段之后插入 `{revision_context}` 槽位 + 回环轮硬要求（只加这一个占位符，JSON 说明的 `{{` 转义未动） |
| `app/graph/nodes/supervisor.py` | 修改 | 新增 `logger`；`_plan()` 读 `state["critique"]` / `state["results"]` 渲染 `revision_context` 并传入 `.format()`；域守卫之后依次 `gap_tool_keys` → `drop_repeat_steps` → `append_gap_steps`，异常/幻觉只写 logger 不写 `errors` |
| `app/graph/nodes/critic.py` | 修改 | `Critique.missing_tool_keys`；模块 docstring「最多 1 次」修真话；prompt 追加「可补充的工具（本域注册表，已排除已拿到数据的工具）」小节 + JSON schema 与说明文字；`model_validate` 之后用**同一份** `gap_registry` 过滤落库 key |
| `app/config.py` / `.env.example` | 修改 | `gap_max_steps: int = 3`（`GAP_MAX_STEPS`） |
| `tests/test_supervisor_gap_loop.py` | 新增（14 条） | T1–T10 纯函数直测（不 mock 任何东西）+ T11–T14 节点级（fake client 记录 `last_prompt`，跑真实 `SupervisorNode.__call__`） |
| `tests/test_critic_gap_keys.py` | 新增（2 条） | T15 / T16，本文件**自洽**定义 `_Msg/_Choice/_Resp/_Completions/_Chat/_FakeClient/_make_node/_BASE_STATE`（不跨测试模块 import），`_Completions.create` 记录 `last_prompt` |
| `docs/architecture.md` / `README.md` | 修改 | 见 7.2 上一提交（A7） |

新增用例合计 **16 条**（694 → 710）。

### 7.3 与计划真值的偏差

1. **§1 事实表：全部核对无误**，无一条与代码不符。特别核实了事实 15（`ToolResult.tool` 存的是 gateway operationId，不是 registry key）——`ToolResult.tool` 为自由字符串，`executed_keys()` 一律走 `resolve_tool_by_name()`，测试也按 operationId 构造。
2. **A3 第 4 步用 `getattr(self.settings, "gap_max_steps", 3)` 而非计划里的 `self.settings.gap_max_steps`**：既有测试 `tests/test_b6_domain_filter.py:100-104` 用 `types.SimpleNamespace` 构造 settings（只有 3 个字段），直接取属性会 AttributeError。配置项本身已在 `app/config.py` 加好，`getattr` 只是兜底，值仍是 3；备选方案是去改既有测试的 SimpleNamespace，侵入更大，故保留兜底。
3. **`app/graph/gap_loop.py` 的模块 docstring 比计划原文多了一段**：实测发现同一个 operationId 可被多个域条目复用（quote/search、klines/snapshot/window），`resolve_tool_by_name` 取的是规范条目，所以「已拿到数据」的判定对复用同一 operationId 的兄弟 key 是**粗粒度**的。这只会让补齐更保守（少补不乱补），不构成造假，已在 docstring 里写明。
4. **A0 取证方式与计划略有出入**：本机安全策略禁止把文件拷到工作区之外，故 worktree 建在工作区内（`git worktree add .a0_probe HEAD`），跑完 `git worktree remove --force` 清理；全程未用 `git stash`，主工作区改动零丢失。
5. **Critic 侧红证据不是「测试变红」而是「测试收集失败」**：HEAD 上 `tests/test_critic_gap_keys.py` 因缺 `app.graph.gap_loop` 直接 ImportError，已如实记录，并补跑等价行为探针脚本（见 7.1）补上行为层红证据。Supervisor 侧两条节点级用例则是**真实行为红**（prompt 无缺口段 / 缺口 key 不进 route）。
6. **T12 的否定断言按计划原文写**（断言模板硬要求句的完整串不出现在首轮 prompt）。该句本身是 `PLANNER_PROMPT` 的静态文本、与 `revision_context` 取值无关，所以这条断言强度有限；首轮真缺口的否定断言由同用例里的 `（首轮，无缺口信息）` 存在断言 + `_GAP_TEXT` 不出现断言承担。
7. **`NEWS_ENABLED=true` 模式的 3 条失败不是回归**：`tests/test_graph_topology.py` 的三条用例直接 `Settings()` 并断言 `settings.news_enabled is False`（`:310-316`、`:319-331`、`:378-399`），与本次改动无关。已在干净 HEAD worktree 上复现同样 3 条失败（见 7.4），未为此改任何生产代码。
8. 未触碰计划 §0.3 划出的禁区：`builder.py` 的节点与边、`state.py` 的 reducer、`analysts/base.py` 的去重逻辑均零改动。

### 7.4 测试数字（改后）

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest . -q` | **前 694 → 后 710**（新增 16 条），`710 passed, 1 warning in 10.24s`，0 failed / 0 error |
| `$env:NEWS_ENABLED="true"` 模式全量 | `3 failed, 707 passed` —— 失败项恰为 `test_news_node_absent_when_disabled` / `test_sentiment_enabled_warns_and_does_not_crash_graph` / `test_default_settings_e2e_matches_p25_baseline`，**已核实为预存在**：在干净 HEAD worktree 上以同环境变量跑 `tests/test_graph_topology.py` 得到完全相同的 `3 failed, 15 passed`（与本次改动无关，未改生产代码） |
| `.venv/Scripts/python.exe -m ruff check . --no-cache` | `All checks passed!` |
| `.venv/Scripts/python.exe -m ruff format --check . --no-cache` | `131 files already formatted`（基线 128 + 新增 3 个文件：`gap_loop.py` 与两个测试文件） |
| 真链路 e2e | **未跑**。原因：仓库唯一的全链路 e2e 是 `tests/test_graph_e2e.py`，它 mock 掉 LLM 与 `ToolRuntime.execute`，已随全量跑绿；真实链路需要真实 LLM / gateway key 与外网，按硬性纪律禁止，故未新建脚本、未新增网络依赖 |
| commit hash + message | `856ef33` feat: 回环轮把 Critic 缺口与已执行工具喂给 Supervisor 并代码级补齐缺口工具<br>`7c04a12` test/docs: 覆盖回环缺口闭环并订正 research_more 真值语义、补 GAP_MAX_STEPS<br>`（本条 §7 执行记录所在的提交）` docs: SUPERVISOR_GAP_PLAN §7 执行记录 —— 未 push |
