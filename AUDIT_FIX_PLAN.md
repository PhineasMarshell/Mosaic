# Mosaic 代码审计修复计划（交接给执行 Agent）

> **本次重写日期**：2026-10-04，由主 Agent 在独立验收批次 2 遗留（T14b/T13b/T5b）与批次 3 前半（T16/T17/T18）之后重写。
> **最近一次更新**：2026-10-05 —— S1（修红）已由执行方完成、**主 Agent 已独立验收通过**（见 §0.4 "S1 验收复核记录"）；
> 本次验收同时新发现两件事并立了新任务：**T18c**（并发请求会话串台，P1）、**T37**（打包/CI flat-layout 失败），另有 **T12b**（依赖清单漂移）。
> **行号基准**：`44c307f`（`0965da6` 之后的改动未影响 §2 以后任务的锚点）。行号已相对初版 `cabd00c` 漂移，
> 本文所有锚点均已按当前代码校正；若再次漂移，以「定位锚点」里给的代码片段 grep 为准。
> **当前 HEAD**：`0965da6`（见 §0.4）。
> **怎么用这份文档**：**§0.5 决定"你这一轮做哪一段"——先看它，再读你那段指定的章节，不要通读全文。**
> §1 是验收结论（谁改了什么、还差什么、哪些结论不要动），
> §2 是**必须先做完的修红**，§3 是批次 3 剩余任务，§4 是批次 4，§5 是收尾。
> 每条任务都必须先跑「复现」确认问题存在——**复现不了就停下来报告，不要照改**。
> 已完成的 T1–T18 细节不再全文保留，压缩进 **附录 D 契约备忘**；原始描述可用
> `git log --oneline` + 对应提交号查回（每个任务一个提交）。

---

## 0. 给执行 Agent 的工作约定

### 0.1 硬性规则

1. **一次只做一个批次，批内一个任务一次提交。** 提交信息用仓库现有风格：`fix: <中文描述>` / `test: <中文描述>` / `chore: <中文描述>`。
2. **每个修复必须附带一个「改坏它会失败」的测试。** 这是本项目的核心教训：现有测试大量假阳性（见批次 4），只补代码不补有效测试，同类 bug 会以同样方式复发。
3. **不要顺手重构无关代码**，不要改 `.env`，不要把密钥写进任何文件，不要 `git add -A`（仓库根有临时目录风险：`.tmp/`、`__pycache__/`）。
4. **不要动** `app/graph/nodes/critic.py` 的 system message（已修）、`app/memory/storage.py` 的 WAL 逻辑（已回退，不要恢复 fallback）。
5. 每个任务完成后运行（`--no-cache` 是为了绕开受限环境下 `.ruff_cache` 不可写，CI 上不需要）：
   ```bash
   ruff check --no-cache app/ tests/
   ruff format --check --no-cache app/ tests/
   pytest -q --tb=short
   ```
   三者全绿才算完成。受限会话跑全量的办法见 **附录 A**。
6. **凡是"把异常吞成默认值"的代码**（本项目大量存在），改的时候要保证：失败要么变成明确的 `status=error`/`errors`，要么降级为 `partial` + `note`，**不允许静默变成成功**。
7. **已完成任务的裁决语义是契约，不要改回。** 动手前先读 **附录 D**。
   若某条与你手上的新需求冲突，**先报告**，不要自己回退。
8. **提交纪律（本次新增，因为刚吃过亏）**：**全量测试不是全绿就不许提交**。
   提交前必须自己看过 `pytest -q` 的尾部汇总行。若确有与本次改动无关的失败要先提交，
   必须在提交信息里写清"失败的是哪条、为什么与本提交无关、归属哪个任务"。
   T17（`2bf11b7`）与 T18（`44c307f`）两次提交时全量测试里各有一条失败没有被先查明，这是流程失误，不要重复。
9. **测试修复纪律（本次新增）**：测试红了，**先判断是产品 bug 还是测试 bug**。
   是测试 bug（stub 缺方法、patch 打错目标、断言错）就改测试；
   **不许为了让测试变绿而在产品代码里加"没有 runtime 就跳过"之类的兜底分支**——
   那会把真实的装配错误变成静默跳过。§2 的 T18T 就是这种情形，已按"改 stub"拍板。
10. **`APPROVAL/COMMIT 纪律`**：`AUDIT_FIX_PLAN.md` 的更新（§0.4、新任务裁决）单独一个 `chore:` 提交，不要和代码修复混在一起。

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

| 批次 | 主题 | 任务 | 状态 |
|---|---|---|---|
| 1 | **P0 正确性**：正在产生错误结论或污染数据 | T1 T2 T3 | ✅ 完成并验收 |
| 2 | **P1 高**：功能静默失效 / 可观测性 / 证据链可信度 | T4–T14 | ✅ 完成并验收 |
| 2.5 | **批次 2 遗留**：T14b / T13b / T5b | 3 条 | ✅ 已实现，**验收已通过** |
| 3 | **P2 中**：可靠性、成本、契约一致性 | T15–T27 | 🔶 部分完成：T16 T17 T18 已提交；**T19–T27 未做**；T15 需 D2（已定） |
| 3.0 | **修红（本次新增，必须最先做）** | T18b / T17T / T18T | ✅ 完成（T18b=`5647a4a`、T17T=`b1b06fa`、T18T=`f2e4da1`、T12=`0965da6`），双模式全绿 |
| 4 | **测试有效性**：把假阳性测试改成真测试 | T28–T35 | ⬜ 未开始（**必须在 S1 修红之后**，见 §0.5 的 S6/S7） |
| 5 | **待人类决策** | D1–D4 | ✅ **本次全部拍板，见 §1.4** |

### 0.4 当前进展

> **当前 HEAD：`7dce255`（S2 收尾）。** S1（修红）+ S2（T18c/T19/T27/T23）均已完成：
> **436 passed + 2 skipped，`mcp` 与 `http` 两种 gateway 模式下全量测试均全绿**（2026-10-05，S2 会话实测）。
> ruff check / ruff format --check 全绿。
> 行号基准 = `44c307f`（S1/S2 的改动未影响 §3 以后任务的锚点；但 `tool_runtime.py` 的
> `execute` / `_do_execute` / `_call_gateway` 已带 `deadline` 参数、会话状态在模块级 ContextVar `_session`，
> T20/T15 等动手时仍以锚点片段 grep 为准）。
> 备注：S1/S2 执行会话是**完全访问**会话（MCP 子进程可 spawn），§1.2 所述"mcp 模式下 6 条连带红"在这些会话不出现；
> 验收者已在受限会话复跑确认连带红消失（见"S1 验收复核记录"）。
> `pip install -e .` 的 flat-layout 失败 = 既有问题（**T37**）；`requirements.txt` 漏改 = **T12b**。

| 任务 | 状态 | 提交 | 备注 |
|---|---|---|---|
| T1–T14 | ✅ 完成并验收 | 见附录 D | 细节压缩，契约见附录 D |
| **T14b 简报过点不补** | ✅ 已实现并**验收通过** | `7e87c6c` | GRACE=5min；3 条新用例含等待秒数断言 |
| **T13b evaluation 兜底** | ✅ 已实现并**验收通过** | `fca2025` | 抽 `_meets_expected_min_tools`，未知 case_id 按 FAIL |
| **T5b truncate note 文案** | ✅ 已实现并**验收通过** | `7e87c6c` | 实测 250 条 → 保留 201（200+说明条），`status=partial` |
| **T16 回边 reducer 去重** | ✅ 已实现并**验收通过** | `df757a5` | 已独立复现"改回 `add` 必红" |
| **T17 news_search TTL** | ✅ 产品修复 + 测试已修（T17T） | `2bf11b7` / `b1b06fa` | patch 目标改为模块全局 `_search_news`，离线可跑；反向验证（改回无 settings 版 TTL 解析）必红 |
| **T18 analyst 级复用 Gateway** | ✅ 完成并验收（T18b 按需连接 + T18T stub + **T18c 每请求会话**） | `44c307f` / `5647a4a` / `f2e4da1` / `f3b76e4` | 会话状态在模块级 `ContextVar _session`；并发复现脚本 `实例数 1→2`、B 的在途调用不再被关；反向验证（换回实例属性实现）①②必红 |
| **T12 收尾（D1）** | ✅ 完成（但漏了 `requirements.txt`，见 **T12b**） | `0965da6` | `mcp>=2,<3`；已装 2.1.1；pip install -e . 的 flat-layout 失败是既有问题（见 **T37**） |
| **T19 缓存** | ✅ 完成 | `bc4849d` | `ttl=0` 立即过期（`ttl or` 反向验证必红）；`max_entries=512` + LRU 淘汰 + set 顺带清理过期 + stats 加 `max_entries`/`evictions` |
| **T27 去重** | ✅ 完成 | `e6a04e5` | 同签名 3 次调用真实网关 2→1 次；重复调用返回 `partial` + note；route 层 seen 集合提前跳过；stash 旧实现 3 条全红 |
| **T23 重试预算** | ✅ 完成 | `f119f51` + `7dce255` | deadline 全链路下发（base 均分 → execute → gateway.call）；忽略 deadline 反向验证 3/4 条红；测试 stub 的 call/execute 签名已补 `deadline=None` |
| **T20–T22、T24–T26、T15** | ⬜ 未开始 | — | 下一段 S3：T22 → T20（含 anomalies）→ T15 |
| **T12b** requirements 同步 | ⬜ 未做（S7） | — | `requirements.txt:8` 仍是 `mcp>=1.12` |
| **T37** 打包/CI flat-layout | ⬜ 未做（S8） | — | 已被验收者直接复现：flat-layout 发现 `app` + `memory` 两个顶层包 |
| **T36 文档同步** | ⬜ 未做 | — | §5，S8 |
| **T28–T35** | ⬜ 未做 | — | §4 |

**S1 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- 逐条读了 5 个提交的 diff：T18b 的按需连接实现与规格一致（`_in_session` 可重入；`_ensure_gateway` 在
  `__aenter__` 失败时把 `_gateway` 复位并**抛出**异常，没吞成默认值；`_close_gateway` 先复位再关闭、
  关闭异常只记 warning）；T17T 的 patch 目标与关键字签名都改对了；T18T 的 stub 提供 `entered` 计数并被断言；
  T12 只改了一行。
- **反向验证（我自己做的）**：把 `await self._ensure_gateway()` 临时挪回 `gateway_session()` 的进入点 →
  `test_gateway_reuse.py` 立即 2 failed（`assert 1 == 0`，两条新防线用例），其余 3 条仍绿；还原后
  `git diff` 为空、23 passed。
- **最强证据（执行方会话里无法产生的）**：本验收会话是 workspace-write 且**沙箱禁止 spawn 子进程**
  （`MCPConnectionError: ... [WinError 5] 拒绝访问`），正是 §1.2 P1 里那 6 条连带红的发生环境。
  修完后在 `mcp` 模式下跑 `test_news_no_symbol + test_graph_topology + test_graph_nodes +
  test_gateway_reuse + test_tool_runtime_ttl` → **43 passed**，连带红彻底消失（修复前同一命令全红）。
- **全量测试，两种跑法都对得上**：
  ① 完全访问会话（`memory/` 可写、MCP spawn 可用）：`pytest -q` → **`421 passed, 2 skipped`**，
  与执行方报告**逐字相符**（`mcp` 与 `http` 两种模式都跑过）。
  ② 受限会话（附录 A 的 runner）：`5 failed / 380 passed / 2 skipped / 36 errors`，
  5 个 failed 与 36 个 error **逐条核对后全部落在附录 A 记录的环境副作用清单内**，无真实失败。
- 复现脚本：`opened = 1 → 0` ✓；已装 `mcp 2.1.1` 满足 `>=2,<3` ✓。
- **未采信、已另立任务的两点**：`requirements.txt` 漏改（T12b）；`pip install -e .` 的 flat-layout
  失败（T37 —— 我用系统 setuptools 81 直接复现：`FlatLayoutPackageFinder.find()` 返回
  `['app', 'memory', ...]` 两个顶层包，正是 setuptools 报 "Multiple top-level packages" 的条件，
  **确认是既有问题、与 T12 那行无关**，但 CI 的 `pip install -e ".[dev]"` 会因此失败，必须修）。
- **验收者新发现（P1-b，见 §1.2）**：T18/T18b 的会话状态放在 `ToolRuntime` 实例属性上，而
  `_get_orchestrator()` 是进程级单例、编译图只构建一次 → 节点实例被并发请求共享。已写出确定性复现，
  另立 **T18c**（S2 第一个任务）。

---

### 0.5 执行分段与会话交接（**每个子 agent 只做一段**）

> **为什么分段**：§2 + §3 + §4 共 20 个任务，一个会话跑完必然上下文溢出。
> 每个任务都要"读代码 → 复现 → 改 → 写测试 → 跑三道门禁"，批次 4 还要逐个读测试文件。
> **分段机制**：每段是一个**全新子 agent**，本文档是唯一交接物，`§0.4` + `git log --oneline` 是状态载体。
> 因此——**每段结束必须更新 `§0.4` 并单独 `chore:` 提交**，否则下一段无法判断哪些已完成。

| 段 | 任务（按顺序） | 读本文档哪些部分 | 为什么这么切 | 结束门槛 |
|---|---|---|---|---|
| **S1 修红**（独占一段，阻塞一切） | T18b → T17T → T18T → T12 收尾 | §0 全部 + §1 全部 + §2 全部 + 附录 A | T18b（产品）与 T18T（测试）互相牵制，必须在同一会话里把基线跑绿；T12 只是一行 `pyproject`，顺手 | **`mcp` 与 `http` 双模式全绿**；4 个提交 |
| **S2 运行时 / 缓存 / 成本** | **T18c（并发串台，必须最先）** → T19 → T27 → T23 | §0 + §1.2（P1-b）+ §2 的 T18c + §3 的 T19/T27/T23 | T18c 与 T19/T27 都在 `tool_runtime.py` / `cache.py`，同批文件；T18c 是 P1 级并发缺陷，优先于同段的其它任务 | 全绿；4 个提交 |
| **S3 检测与报告契约** | T22 → T20（含 anomalies）→ T15 | §0 + §1.4 + 附录 A/B + §3 的 T22/T20/T15 | **T22 必须先于 T20 的 anomalies 半条**（否则误报，D3 已定）；三者共同决定"报告里写什么" | 全绿；3 个提交 |
| **S4 注册表** | T24（**把 T34 里 `test_tool_registry_multi_domain.py` 的断言并入本条**） | §0 + 附录 A/B + §3 的 T24 + §4 的 T34 | 它牵动全量 `tool_registry`，单独一段便于跑 normalizer / analyst 相关回归 | 全绿；1 个提交 |
| **S5 请求生命周期** | T21 → T26 → T25 | §0 + 附录 A/B + §3 的 T21/T26/T25 | sqlite 跨线程、SSE 收尾、前端并发，都是"一次请求从进到出"；T25 是前端，放最后 | 全绿 + 手工看一眼界面；3 个提交 |
| **S6 测试有效性（上）** | T28 → T29 → T30（含 D4 budget）→ T31 | §0 + 附录 A/B + §4 的 T28–T31 | 4 条都在 graph e2e / reasoning / graph_nodes，文件重合度高 | 全绿；4 个提交 |
| **S7 测试有效性（下）** | T32 → T33 → T34（**不含注册表那条，已并入 S4**）→ T35 → T12b | §0 + 附录 A/B + §4 + §3 的 T12b | T34 与 T35 是同文件收尾；T12b 是一行依赖同步，顺手 | 全绿；5 个提交 |
| **S8 打包与收尾** | T37 → T36 → 最终全量验收 | §0 + §5 + §3 的 T37 | T37（打包/CI）会动 `pyproject.toml`，必须在所有代码改动之后；T36 文档同步放最后 | 全绿；`§0.4` 定稿；CI 安装步骤可通过 |

**硬性顺序约束（不要打乱）**：
1. **S1 必须最先**（✅ 已完成并验收），S2 的 **T18c 是 P1 级并发缺陷，必须先于 S2 其它任务**。
2. S3 内 **T22 早于 T20 的 anomalies 半条**。
3. S5 内 **T21 早于 T26**（SSE 的落库断言需要可写的 sqlite 路径）。
4. S6/S7 建议在 S1 之后：T31 解 skip 后会真跑到 analyst 节点，依赖 T18b 已修。
5. **S8 必须最后**：T37 会改 `pyproject.toml` 的打包配置，改完要重跑一次全量。
6. 段的**内部**仍然遵守规则 1：一个任务一次提交。

**每段的开场指令模板**（把 `{Sx}` 换成具体段号）：

> 读 `D:\Mosaic\AUDIT_FIX_PLAN.md` 的 **§0.5 里 {Sx} 行指定的章节**（不要通读全文，尤其 S3 之后不要读 §1.1/§1.2——那是"修红"的语境，红修完就没用了）。
> 只做 {Sx} 列出的任务，**做完就停**，不要顺手往下一段走。
> 状态以 `§0.4` + `git log --oneline` 为准。每完成一个任务一个提交；**全量测试不全绿不许提交**（规则 8）。
> 迭代时用 `pytest -q --tb=line -p no:cacheprovider 2>&1 | Select-Object -Last 15`，或单文件 / `-k` 缩小范围；
> **只在任务收尾时跑一次全量**。先判断本会话能否写 `memory/`，再决定用 `pytest -q` 还是附录 A 的 runner。
> 每段结束：更新 `§0.4`（任务打 ✅ + 提交号 + 测试数字），**单独 `chore:` 提交**，然后停下报告三道门禁的输出尾部。

**段与段之间的验收**：每段结束后把结果交回主 Agent 复核一次（像 §1 那样：读 diff + 自己跑 + 对关键修复做「改坏必红」的反向验证）。
**不要连续跑两段再验收**——错误基线会一路滚下去。

**每段的开工检查清单**：
1. `git status --porcelain` 应只有 `M AUDIT_FIX_PLAN.md`（上一段的 `§0.4` 更新）或为空；有其它未提交代码 = 上一段没做完，**先问再动**。
2. `git log --oneline -5` 对得上 `§0.4` 里记的提交号。
3. `.tmp/` 里的临时脚本（`run_tests_local.py` / `verify_gateway_lazy.py`）**不要提交**。

---

## 1. 交接验收结论（主 Agent 独立复核）

> 复核方式：不采信执行方报告，自己读 diff、跑测试、写脚本复现、并对关键修复做「改坏 → 必须变红」的反向验证。
> 复核环境：workspace-write 会话，sqlite 无法写 `memory/`（见附录 A），因此分两种 gateway 模式各跑一遍。

### 1.1 复核通过项（结论：可以采信）

| 项 | 我实际做的事 | 结果 |
|---|---|---|
| **T16 reducer 去重** | 读 `app/graph/state.py:16-88` diff；跑 `tests/test_graph_state_reducers.py`；**把三个字段的 reducer 临时改回 `operator.add` 再跑** | 正常 2 passed；改回 `add` 后 `test_same_signature_loop_back_overwrites` 变红（`assert 2 == 1`）→ **测试真的能防回归**；改回后已恢复 `git diff` 为空 |
| **T16 删除 `state.tool_results`** | 全仓 grep `tool_results`，确认写入方（`analysts/base.py`）与读取方 | 无任何读取方：`app/models/response.py:66` 从 `state["results"]` 组装，`app/graph/nodes/gate.py:27` 读 `results` → **删除安全** |
| **T17 产品修复** | 用自己的脚本（patch 目标是模块全局，写法正确）跑 `runtime.execute("news_search", {...})` | 缓存剩余 TTL = **21600s**（旧实现 ≈30s）；`_resolve_ttl("news_search", Settings()) == 21600.0` → **修复正确** |
| **T17 改动兼容性** | grep `_resolve_ttl` 的全部调用方（`test_cache_multi_domain.py` 只传 1 个参数） | 新签名 `settings=None` 向后兼容 → 无回归 |
| **T18 运行级复用** | 读 diff；跑 `tests/test_gateway_reuse.py` | 3 工具 → 1 个客户端、异常时 `__aexit__` 被调用 → **该测试有效且通过** |
| **T14b 过点不补** | 读 `briefs.py` diff 与 3 条新用例 | 23:00 不补发、09:18 触发、09:30 不触发，且断言了等待秒数（能同时防"严格相等永不触发"的反向回归）→ **有效** |
| **T13b / T5b** | 读 diff；跑 `tests/test_evaluation.py`、`tests/test_truncate.py` | 抽函数正确，note 文案与实际条数一致 → **有效** |
| **批次 2 遗留三条的联合回归** | 跑 `test_gateway_reuse + test_brief_scheduler + test_evaluation + test_truncate + test_news_search + test_evidence + test_cache_multi_domain` | **37 passed** |

### 1.2 未收尾问题清单（必须先处理）

**P0 — 修红项（3 条红）→ ✅ 全部由 S1 修复并验收（`5647a4a` / `b1b06fa` / `f2e4da1`）**

| # | 测试 | 性质 | 归属任务 |
|---|---|---|---|
| 1 | `tests/test_graph_nodes.py::test_analyst_with_no_matching_tools_returns_empty` | 测试 stub 缺陷（fixture 把 `_runtime` 设成 `None`，而 `__call__` 现在无条件调 `self._runtime.gateway_session()`）→ AttributeError 被节点兜底 except 吞成 `failed=True` → 用例取 `result["results"]` 得 `KeyError: 'results'` | **T18T**（§2） |
| 2 | `tests/test_graph_nodes.py::test_execute_skips_non_whitelist_no_stock` | 同上（`FakeRuntime` 缺 `gateway_session`） | **T18T**（§2） |
| 3 | `tests/test_tool_runtime_ttl.py::test_news_search_cache_ttl_survives_execute` | **测试自身缺陷**：用 `runtime._search_news = fake` 打补丁，但生产代码读的是模块全局 `_search_news`（`app/graph/tool_runtime.py:304`），patch 无效 → 测试实际打真实 DDGS 网络，网络失败就红。**产品代码（T17 修复）没有问题** | **T17T**（§2） |

**P1-a — 急切连接（T18 引入）→ ✅ 已由 `5647a4a`（T18b）修复并验收**

`ToolRuntime.gateway_session()`（`app/graph/tool_runtime.py:62-89`）在**进入时就**创建并连接 Gateway。
修改前，只分配内部工具（`news_search` / `internal_hk_northbound` / `internal_hk_index`）的 analyst
根本不会碰 Gateway（`_do_execute` 里 INTERNAL 分支在任何 Gateway 代码之前 return）。
修改后这类 analyst 也会 spawn MCP 子进程 + `initialize()` + `list_tools`；**网关不可用时整个 analyst 直接失败**。

我的实测证据：
- 脚本计数：`async with runtime.gateway_session(): await runtime.execute("news_search", ...)`
  → **Gateway 被创建/进入 1 次**（期望 0）。
- 本会话直接开一次会话：`MCPConnectionError: MCP server startup failed: [WinError 5] 拒绝访问`
  （沙箱禁止 spawn 子进程；`.env` 里 `MARKET_GATEWAY_MODE=mcp`）。
- 因此在 `mcp` 模式下，除上面 2 条 stub 用例外，**另有 6 条测试连带变红**，且红的原因与它们要测的东西完全无关：
  `test_graph_nodes.py::test_analyst_exception_degrades_to_error`（报的是 "MCP server startup failed" 而不是 "intentional failure"）、
  `test_graph_nodes.py::test_analyst_evidence_is_evidence_instances`、
  `test_graph_topology.py` 4 条（`test_fanout_routes_to_analyst_when_route_nonempty`、`test_fanout_falls_back_to_all_analysts_when_route_empty`、`test_news_node_executed_when_enabled`、`test_default_settings_e2e_matches_p25_baseline`）、
  `test_news_no_symbol.py` 2 条。
  切到 `MARKET_GATEWAY_MODE=http` 后这 6 条全绿 → 证明它们只是被"急切连接"放大，根因是同一处。

**修复验收（2026-10-05，验收者在本会话复跑）**：沙箱同样禁止 spawn（`[WinError 5]`），
`mcp` 模式下 `test_news_no_symbol + test_graph_topology + test_graph_nodes + test_gateway_reuse +
test_tool_runtime_ttl` → **43 passed**（修复前同一命令全红）。复现脚本 `opened = 1 → 0`。
反向验证：把建连挪回会话进入点 → 两条新防线用例立刻 `assert 1 == 0` 变红。

**P1-b — 并发请求共享 `ToolRuntime`，会话状态串台（T18 引入，T18b 未修，**待做 = T18c**）**

`app/main.py:86-92` 的 `_get_orchestrator()` 是**进程级单例**，`Orchestrator._ensure_graph()`
（`app/agent/orchestrator.py:23-26`）只构建一次编译图 → **所有并发请求共用同一批节点实例**
（`self._runtime = ToolRuntime(settings)` 是节点实例属性，见 `analysts/base.py:73`）。
T18/T18b 把会话状态放在 `ToolRuntime` 的**实例属性**上（`_in_session` / `_gateway` / `_available_tools`），
于是两个并发调查会互相踩：

- 第二个请求进入 `gateway_session()` 时看到 `_in_session=True`，被当成"嵌套会话"→ **复用第一个请求的客户端**；
- 第一个请求先结束时 `_close_gateway()` 会关掉这个客户端 → 第二个请求**在途**的调用随即失败。

**我的确定性复现**（`.tmp/verify_concurrent_session.py`，同一个节点实例并发跑两条 route）：
```
并发两请求共用节点 → gateway 实例数 = 1 (期望 2，每请求一个)
A 结束后客户端 is_closed = True
A errors: []
B errors: ['technical analysis failed: gateway closed while quote_tencent_quote_get in flight']
B findings failed: [{'analyst': 'technical', 'digest': '分析失败: ...', 'tools_used': [], 'failed': True}]
```
**触发条件在生产里是常态**：uvicorn 单进程 + 单例图，前端在调查中回车追问（T25 正是这件事）、
两个标签页、同步 `/api/ask` 与 SSE 并发、或两个用户同时提问，都会命中。
**T18 之前没有这个问题**：旧实现每个工具调用各自 `async with gateway_cls(...)`，无共享状态。
→ **修法见 §2 的 T18c（S2 的第一个任务）。**

**P2 — 流程失误（已记录，不单独提交代码）**

- T17（`2bf11b7`）与 T18（`44c307f`）两次提交时，全量测试里已有 1 条失败未查明就提交。
  当时那条失败就是 P0-3（坏测试），不是产品回归；但正确做法是先查明归属再提交。已写入规则 8。

### 1.3 未做任务的复现预跑（主 Agent 已逐条确认，附实测输出）

> 目的：执行方不必再花时间判断"这条问题到底存不存在"。下面这些**都已复现**，可以直接进入修复。
> 若你跑出来和这里不一致，说明代码已漂移，**先报告再动手**。

| 任务 | 我跑的命令 | 实测输出（= bug 存在） |
|---|---|---|
| T19 `ttl=0` | `Cache().set("k", 1, ttl=0); get("k")` | 返回 `1`（还在，30s 后才过期）→ 期望 `None` |
| T19 无上限 | 连 `set` 2000 个键后 `stats["size"]` | `2001` → 期望 ≤ `max_entries` |
| T20 必填字段 | `MarketIntelligence.model_validate({"title":"t","state_label":"Neutral"})` | `ValidationError: 2 validation errors ... market_state Field required / what_happened Field required` |
| T21 跨线程连接 | 另一线程调 `MarketMemory.save_turn` | `ProgrammingError: SQLite objects created in a thread can only be used in that same thread. The object was created in thread id 27032 and this is thread id 29528.` |
| T22 阈值单位 | `detect_anomalies` 喂 `openInterest=3.2e9, fundingRate=0.0001` | 输出 **两条** `critical` + `high`，描述是 `OI 剧烈增长 3200000000.0%`；`fundingRate` 一条都没触发 |
| T24 域过滤 | `registry_text(domains=["us_stock"])` | `40` 行 = `len(ALL_TOOLS)`，且含 `[a_share]` → 静默回退全量 |
| T24 health | `registry_text(domains=["hk_stock"])` | `2` 行、**不含** `[unknown]` → 与"始终包含 health 工具"的注释相反 |
| T5b（已修，回归确认） | 250 条 datum → `truncate` | `kept: 201`，`status: partial`，note 写明 250/200 ✓ |
| T17（已修，回归确认） | `execute("news_search", ...)` 后看缓存 | 剩余 TTL `21600s` ✓ |

**尚未复现（执行时自己确认）**：T23（重试预算，需 stub transport 计数）、T25（前端并发/渲染，需浏览器或文本契约）、
T26（SSE 错误分支，需 TestClient + monkeypatch）、T27（`called_signatures`，需计数 Gateway stub）。
T27 的根因已由阅读确认：`_check_cache` 只在**缓存命中**时 `called_signatures.add(...)`，
而"签名已存在"时 `return None` 被调用方当成"无缓存"→ 真的再打一次网关。

### 1.4 已拍板决策（不要再问、不要再改）

| ID | 决策 | 落地位置 |
|---|---|---|
| **D1** | **以 MCP 2.x 为准**：`pyproject.toml` 把 `mcp>=1.12` 收成 **`mcp>=2,<3`**，保留 `initialize()` 握手 | 批次 3 的 T12 收尾项（§3 开头） |
| **D2** | **Gate 不短路**：`has_evidence=False` 时**仍产出报告**，但强制 `confidence=low` + `data_caveats` + 写 `errors`；并把 `evidence_gate.py` 的文档承诺与实际行为改成一致 | **T15**（§3） |
| **D3** | **`anomalies` 由代码填**：用 `app/detector/anomaly.py::detect_anomalies` 在代码里生成，**不要**交给模型。**必须先做完 T22**（阈值/单位）再接入，否则每次快照都误报 critical | **T20 后半 + T22**（§3） |
| **D4** | **接通预算语义**：`config.max_tool_calls` 成为真实上限；`budget` 显式值优先（`budget if budget is not None else len(tool_calls)`）；`budget=0` 表示"不执行"，修掉 `or` 吞掉 0 的 bug | **T30 顺带 + §3 的 T20 备注** |
| **stub 修法** | **改测试 stub，不改产品代码迁就测试**（见规则 9） | **T18T**（§2） |

**已由仓库主人拍板、不要改回的其它契约**（完整版见附录 D）：

| 议题 | 结论 |
|---|---|
| Critic「审计失败」语义 | 不归为 `research_more`；`verdict="error"` → 安全 END + 保留 report + 写 `errors`；`/api/ask` 仍 200 |
| 简报触发 | 只认 09:15 / 15:30，**过点不补**（GRACE=5min 只容忍抖动） |
| `PROJECT_STATUS.md` | 已由主人删除，**不要再创建**；状态以 §0.4 为准 |
| evidence id | `{category}-NNN`（T4 之后），不要再退回 `evidence-NNN` |
| `truncate` | 保留**末尾** 200 条 + 1 条说明，`status=partial`，缓存存深拷贝 |

---

## 2. 批次 3 前置：修红（**必须最先做完，全绿前不要开始 T19**）

> 本节的 3 个任务可以合成 1–3 个提交，但**必须三条命令全绿之后再提交**。
> 建议顺序：T18b（产品）→ T17T（测试）→ T18T（测试）。

### T18b — `gateway_session()` 改按需连接（修复 T18 的急切连接回归）

**定位锚点**
- `app/graph/tool_runtime.py:62-89` — `gateway_session()`（`await gateway.__aenter__()` 在进入时就执行）
- `app/graph/tool_runtime.py:177-210` — `_do_execute` / `_call_gateway`
- `app/graph/nodes/analysts/base.py:90-98` — `async with self._runtime.gateway_session():`

**现象**
只用到内部工具的 analyst（news / hk）也会连接 Gateway；网关不可用（未装 iiix、沙箱禁 spawn、网络或鉴权故障）时整个 analyst 直接 `failed=True`，而在 T18 之前这些 analyst 完全不依赖 Gateway。MCP 模式下还会白 spawn 一次子进程 + 握手 + `list_tools`。

**复现**
```python
# 期望输出 opened == 0；当前输出 1
import asyncio
from app.config import Settings
from app.graph.tool_runtime import ToolRuntime
import app.graph.tool_runtime as tr

async def fake_search_news(query, *, max_results=5, time_limit="d"):
    return {"news": [{"date": "d", "title": "t", "body": "b", "url": "u"}], "meta": {"status": "ok"}}
tr._search_news = fake_search_news

class CountingGateway:
    opened = 0
    def __init__(self, settings): pass
    async def __aenter__(self):
        CountingGateway.opened += 1
        self.tools = []
        return self
    async def __aexit__(self, *exc): return None
    async def call(self, name, args): raise AssertionError("内部工具不该走 gateway")

async def main():
    rt = ToolRuntime(Settings())
    rt._gateway_class = lambda: CountingGateway
    async with rt.gateway_session():
        await rt.execute("news_search", {"query": "q"}, set())
    print("opened =", CountingGateway.opened)

asyncio.run(main())
```
配套证据（本会话实测，`MARKET_GATEWAY_MODE=mcp`）：
`pytest tests/test_news_no_symbol.py tests/test_graph_topology.py -q` 全红，错误为
`MCPConnectionError: MCP server startup failed: [WinError 5] 拒绝访问`。

**根因**：`gateway_session()` 把"会话边界"和"建立连接"合并成同一件事，进入即连接。

**修改**（只动 `app/graph/tool_runtime.py`）
1. `__init__` 增加 `self._in_session = False`。
2. `gateway_session()` 改为**只标记会话**、不连接：
   ```python
   @asynccontextmanager
   async def gateway_session(self):
       if self._in_session:            # 可重入
           yield self._gateway
           return
       self._in_session = True
       try:
           yield self._gateway          # 进入时是 None，由 _ensure_gateway 按需建立
       finally:
           self._in_session = False
           await self._close_gateway()
   ```
3. 新增 `async def _ensure_gateway(self)`：`self._gateway is None` 时创建、`await __aenter__()`、解析
   `self._available_tools`（mcp：`{t.name for t in gateway.tools}`；http：`{t.tool_name for t in self._http_allowed_tools()}`）。
   **`__aenter__` 失败时要把 `self._gateway` 复位为 `None` 并把异常抛出去**，不要吞成默认值（规则 6）；
   `__aenter__` 成功才允许 `_close_gateway()` 调 `__aexit__`（失败路径由 T12 的 `mcp_client.close()` 自清理）。
4. 新增 `async def _close_gateway(self)`：`self._gateway` 非 None 才 `await gateway.__aexit__(None, None, None)`，
   异常只记 `logger.warning`；结束时把 `_gateway` / `_available_tools` 都复位。
5. `_do_execute` 的会话分支改成按需：
   ```python
   if getattr(meta, "http_method", None) == "INTERNAL":
       return await self._execute_internal(tool_name, arguments)

   if self._in_session:
       gateway = await self._ensure_gateway()
       return await self._call_gateway(gateway, self._available_tools, tool_name, arguments)

   gateway_cls = self._gateway_class()          # 会话外：保持旧的逐次新建路径
   async with gateway_cls(self.settings) as gateway:
       return await self._call_gateway(gateway, None, tool_name, arguments)
   ```

**必补测试**（`tests/test_gateway_reuse.py` 追加，复用现有 `FakeGateway` / `_state`；
注意 `test_gateway_reuse.py` 现有的 `_state()` 是给 `TechnicalAnalystNode` 用的，
route 里加内部工具只需把 `{"tool_key": "news_search", "arguments": {"query": "q"}}` 放进同一条
`analyst: "technical"` 的 `tool_calls`——`_execute_tools` 按 `route.analyst == self.category` 取分配，
不按工具类别过滤，所以这样混是合法的）
- ① **只用内部工具**：route 只含 `news_search`，monkeypatch `app.graph.tool_runtime._search_news`
  返回一条假新闻 → 断言 `FakeGateway.instances == []`（**改坏前这条必红**）、
  `findings[0]["failed"] is False`、`"news_search" in findings[0]["tools_used"]`。
- ② **混合 route**（`news_search` + `quote`）→ 断言仍然 `len(FakeGateway.instances) == 1`、`exited == 1`、
  `len(out["results"]) == 2`（复用能力没被按需连接破坏）。
- ③ **网关必炸但 route 只用内部工具**：`_gateway_class` 指向一个 `__aenter__` 立刻抛 `RuntimeError`
  且把构造次数记下来的类 → 断言 `构造次数 == 0`、analyst 成功（`failed is False`、`errors == []`）。
  这条防的是"以后有人把连接又挪回会话进入点"。

**验收**
- 上面的复现脚本输出 `opened = 0`。
- 原 2 条 T18 用例仍绿（`len(instances) == 1`、异常时 `exited == 1`）。
- **`MARKET_GATEWAY_MODE=mcp` 与 `=http` 两种模式下**，`pytest tests/test_graph_topology.py tests/test_news_no_symbol.py tests/test_graph_nodes.py -q` 都只剩 T18T 要修的那 2 条（修完 T18T 后全绿）。

**风险 / 牵连**：`_in_session` / `_ensure_gateway` 是新状态。

> **⚠️ 本节规格里的一个错误假设（验收时更正）**：本节原文写的是"三个 analyst 节点各有自己的
> `ToolRuntime` 实例，所以不存在跨节点共享"。**这句话只在一个请求内部成立**——
> `_get_orchestrator()` 是进程级单例、编译图只构建一次，所以**并发请求会共用同一批节点实例**
> （= 同一批 `ToolRuntime`）。T18b 按此思路把会话状态留在实例属性上，因此留下了 **P1-b 并发串台**
> （已在 §1.2 记录、并已用确定性脚本复现）。**T18c 必须作为 S2 的第一个任务把会话状态改成
> 每请求（per asyncio task）一份**；在 T18c 完成之前，S2 的其它任务不要动 `gateway_session` 的这层语义。

**✅ 已完成（`5647a4a`）**：按需连接；`_in_session` 只标记会话，`_ensure_gateway` 首次网关调用才建连，
`_close_gateway` 保证关闭；`_do_execute` 的 INTERNAL 分支在会话分支之前 return。
3 条新回归用例（①内部工具零连接 ②混合 route 仍复用 1 个 ③网关必炸但内部工具照常成功）已验收通过。

### T18c — 会话状态必须"每请求一份"（**P1-b 并发串台；S2 的第一个任务**）

**定位锚点**
- `app/graph/tool_runtime.py:64` — `self._in_session = False`（实例属性）
- `app/graph/tool_runtime.py:66-84` — `gateway_session()` 用 `self._in_session` 判可重入
- `app/graph/tool_runtime.py:86-113` — `_ensure_gateway` / `_close_gateway` 读写 `self._gateway`、`self._available_tools`
- `app/graph/tool_runtime.py:203-220` — `_do_execute` 用 `self._in_session` 选路径
- 共享来源：`app/main.py:86-92` `_get_orchestrator()`（单例）+ `app/agent/orchestrator.py:23-26` `_ensure_graph()`（只构建一次）
- 节点持有 runtime：`app/graph/nodes/analysts/base.py:73` `self._runtime = ToolRuntime(settings)`

**现象**：两个并发调查共用同一个 Gateway 客户端，先结束者把它关掉，另一个请求**在途**的调用失败，
整个 analyst 降级为 `failed=True`（用户看到"某维度分析失败"，且原因与真实数据无关）。

**复现**（把下面脚本存成 `.tmp/verify_concurrent_session.py` 运行；验收者已跑过，输出见 §1.2 P1-b）
```python
# 关键：同一个 node 实例被两个 asyncio 任务并发调用（模拟单例图）
node = TechnicalAnalystNode(Settings())
node._runtime._gateway_class = lambda: SlowGateway
task_a = asyncio.create_task(node(_state("600519")))
task_b = asyncio.create_task(node(_state("000001")))
...
# 当前输出：gateway 实例数 = 1（期望 2）；B errors = ['... gateway closed while ... in flight']
```
（完整脚本可直接照 `.tmp/verify_concurrent_session.py` 抄；它用两个 symbol 不同的 `quote` 调用 +
两个 `asyncio.Event` 精确控制交错。**注意 route 里必须带 `symbol`**，否则会被 T8 的 symbol 守卫跳过。）

**根因**：会话状态放在 `ToolRuntime` 实例属性上，而实例被并发请求共享。

**修改（推荐 A，二选一）**
- **A（推荐，改动最小）**：会话状态改放 `contextvars.ContextVar`，语义天然"每个 asyncio task 一份"
  （`asyncio.create_task` 会复制当前 context，子任务看到会话、兄弟任务看不到）：
  ```python
  _session: ContextVar[dict | None] = ContextVar("gateway_session", default=None)

  @asynccontextmanager
  async def gateway_session(self):
      if _session.get() is not None:      # 同一 task 内嵌套 → 复用
          yield
          return
      token = _session.set({"gateway": None, "available": None})
      try:
          yield
      finally:
          session = _session.get()
          _session.reset(token)
          if session and session["gateway"] is not None:
              await self._close_gateway(session)
  ```
  `_ensure_gateway()` 读写 `_session.get()` 里的字典；`_do_execute` 用 `_session.get() is not None` 判路径。
  **不要**把每任务的会话状态继续留在 `self.*` 上。
- **B（备选）**：让 `Orchestrator` 每个请求构建自己的图（去掉 `_graph` 缓存，或按请求 clone 节点）。
  这样能顺带避免其它节点实例状态被共享，但每次请求多一次 `build_graph`，且不能防止"同一请求内
  同一节点被并发调用"（正常不会发生）。选 B 必须在提交信息里说明为什么不用 A。

**必补测试**（`tests/test_gateway_reuse.py` 追加，用上面的脚本改写成 pytest）
- ① 同一个节点实例 + `asyncio.gather` 两个并发 route（symbol 不同）→ 断言 `len(FakeGateway.instances) == 2`、
  `FakeGateway.exited == 2`、**两个结果都 `errors == []`、`failed is False`**。
- ② 交错验证（防"先结束者关掉别人的客户端"）：A 的调用返回并结束会话后，B 的在途调用必须仍然成功。
- **反向验证**：把会话状态改回 `self._in_session` / `self._gateway` → ①② 必红（当前实测就是红）。

**验收**：复现脚本输出 `gateway 实例数 = 2`、`A errors: []`、`B errors: []`；
`mcp` 与 `http` 两种模式下全量测试全绿；`test_gateway_reuse.py` 原有 5 条仍绿。

**风险 / 牵连**：
- `ContextVar` 在**同一个 task 内串行**开两次会话（一个 analyst 跑完再跑下一个）必须正确复位 ——
  用 `reset(token)` 保证；测试要覆盖"连续两次会话各自建连又各自关闭"。
- 会话外直接调 `execute()`（单工具调用方、`tests/test_truncate.py` 等）仍走旧的逐次新建路径。
- 这条是**架构性共享**的一个实例，别顺手去重构整个 orchestrator 的单例（不在本任务范围）。

---

### T17T — 修正 T17 测试的 patch 目标（测试缺陷，非产品问题）

**定位锚点**
- `tests/test_tool_runtime_ttl.py:31-45` — `async def test_news_search_cache_ttl_survives_execute()`，其中 `:37` 是 `runtime._search_news = fake_search`
- 生产代码：`app/graph/tool_runtime.py:25` `from app.research.news_search import search_news as _search_news`；`:304` 调用的是**模块全局** `_search_news`

**现象**：patch 打在实例属性上，生产代码从不读 `self._search_news` → 测试实际发起真实 DDGS 网络请求。
之前"通过"是网络恰好成功（`status=success`、正常写缓存），网络失败时 `status=error`、不写缓存，
于是 `market_cache._store[key]` 抛 `KeyError` → 红。**这条红与 T17 的产品修复无关**。

**复现**
```bash
pytest tests/test_tool_runtime_ttl.py -q          # 断网/网络受限时 red，日志里能看到真实 DDGS 请求
grep -n "_search_news" app/graph/tool_runtime.py  # 25 行 import，304 行用全局；全文件没有 self._search_news
```

**修改**（只动测试）
```python
async def test_news_search_cache_ttl_survives_execute(monkeypatch):
    runtime = ToolRuntime(Settings())

    async def fake_search(query, *, max_results=5, time_limit="d"):
        return {"news": [{"title": "t", "body": "b", "url": "u", "date": "2026-10-04"}], "meta": {"status": "ok"}}

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search)   # ← 正确目标
    result = await runtime.execute("news_search", {"query": "A股"}, set())
    assert result.status == "success"
    ...
```
（删掉 `runtime._search_news = fake_search` 这一行；`tests/test_news_no_symbol.py:40` 与 `tests/test_news_search.py:102` 已是正确写法，可照抄。）

**必补测试**：本用例自身必须在**离线**下通过。顺手补一条"旧实现会红"的反向断言：
`monkeypatch.setattr(app.cache, "_resolve_ttl", lambda tool, settings=None: 30.0)` 时该用例必须失败——
不强制要求，但若要加，必须在用例内注释说明它防的是哪次回归。

**验收**：断网跑 `pytest tests/test_tool_runtime_ttl.py -q` 全绿；把 `app/graph/tool_runtime.py:119` 的
`_resolve_ttl(tool_name, self.settings)` 改回 `_resolve_ttl(tool_name)` 后该用例必红。

**风险 / 牵连**：无产品改动。改完确认 `tests/test_news_search.py` 的用时明显下降（不再打真实网络）。

---

### T18T — 修两条 stub 测试（**改测试，不改产品**）

**定位锚点**
- `tests/test_graph_nodes.py:44-52` — `base_node` fixture，`:51` `node._runtime = None`
- `tests/test_graph_nodes.py:135-146` — `node` fixture，`:145` `n._runtime = None`
- `tests/test_graph_nodes.py:205-223` — 用例内联的 `class FakeRuntime`（没有 `gateway_session`）
- `tests/test_graph_e2e.py:167-171` — 同样 `node._runtime = None`（当前被 `@pytest.mark.skip`，见 T31）
- 产品代码：`app/graph/nodes/analysts/base.py:92` `async with self._runtime.gateway_session():`

**现象**：`AttributeError: 'NoneType' object has no attribute 'gateway_session'`（或 `'FakeRuntime' object ...`）
被 `base.py:124` 的兜底 `except` 吞成 `failed=True` + `errors`，用例随后取 `result["results"]` →
`KeyError: 'results'`，**报错信息完全指不到真正原因**。这正是本项目"异常被吞成降级"导致排障困难的又一次实例。

**根因**：测试用"整体替换 `_runtime`"的方式断言节点行为，而 `_runtime` 的接口在 T18 变宽了（多了 `gateway_session`）。

**修改（决策：改 stub）**
1. 在 `tests/test_graph_nodes.py` 顶部加一个最小 stub，供两个 fixture 与 FakeRuntime 复用：
   ```python
   from contextlib import asynccontextmanager

   class _NoGatewayRuntime:
       """只提供 analyst 骨架需要的接口：会话（空实现）+ truncate。"""
       def __init__(self):
           self.sessions = 0
       @asynccontextmanager
       async def gateway_session(self):
           self.sessions += 1
           try:
               yield None
           finally:
               self.sessions -= 1
       def truncate(self, result):
           pass
   ```
2. `base_node` / `node` fixture：`node._runtime = _NoGatewayRuntime()`（用例本意"没有工具可执行"仍然成立，
   因为 `category="nonexistent"` 不匹配任何 route）。
3. `test_execute_skips_non_whitelist_no_stock` 的内联 `FakeRuntime` 补 `gateway_session`（可从
   `_NoGatewayRuntime` 继承），并保留它现有的 `execute` 记录行为。
4. `tests/test_graph_e2e.py:170` 改为同样的 stub（该用例同时是 T31 的对象，一起处理）。
5. **不要**在产品代码里加 `if self._runtime is None or not hasattr(...)` 之类的兜底（规则 9）。

**必补测试**：这 3 条用例各自追加 `assert result.get("errors") == []` ——
这样将来任何"被兜底 except 吞掉"的回归都会以**明确断言**的形式红，而不是变成难懂的 `KeyError`。
`test_analyst_with_no_matching_tools_returns_empty` 还应断言 stub 的会话确实被进入过（`sessions` 计数），
证明它走的仍是生产 `__call__` 骨架。

**验收**：`pytest tests/test_graph_nodes.py -q` 在 `mcp` 与 `http` 两种模式下都全绿（T18b 修完后 `mcp` 模式也绿）。

**风险 / 牵连**：`tests/test_graph_nodes.py` 里还有 T11 有意反转过的用例（critic 语义），不要顺手改；
批次 4 的 T34 会回头清理这个文件里其它空转 fixture，届时与本文件的 stub 合流。

---

## 3. 批次 3 剩余任务

> 顺序建议：T12 收尾（D1）→ T19 → T20（先 D4 部分，anomalies 等 T22 后）→ T21 → T22 → T23 → T24 → T25 → T26 → T27 → T15（D2）→ T36（§5）。
> 每条仍然要求「改完补一个能失败的测试」。

### T12 收尾（D1=A）— 依赖区间收口 → ✅ `pyproject.toml` 已完成（`0965da6`），**`requirements.txt` 漏改，见 T12b**
`pyproject.toml:14` 把 `mcp>=1.12` 改为 **`mcp>=2,<3`** ✅；`app/gateway/mcp_client.py` 保留
`await asyncio.wait_for(self.session.initialize(), timeout=...)` 握手（T12 已实现）。
**验收**：已装 `mcp 2.1.1` 满足区间（`pip install -e .` 本身因 **T37** 的打包问题失败，不能作为验收手段）；
`tests/test_data_integrity.py` 中 MCP 相关用例仍绿。

### T12b — `requirements.txt` 与 `pyproject.toml` 依赖漂移（S7，一行）
`requirements.txt:8` 仍是 `mcp>=1.12`，而 `pyproject.toml:14` 已是 `mcp>=2,<3`（T12 收口时漏了这个文件）。
该文件是 `[project].dependencies` 的镜像清单，留着旧区间会让"文档承诺、代码不一致"多一处。
**修改**：`requirements.txt:8` 改为 `mcp>=2,<3`；顺手核对其余 8 行与 `pyproject.toml` 是否逐条一致
（有出入就一并同步，并在提交信息里列出）。
**验收**：`diff` 两份依赖清单无差异。**必补测试**：无（纯清单同步）；
若要机械化，可在 T37 里顺带加一条"两份清单一致性"的检查（可选，不要为它引入新依赖）。

### T15 — Evidence Gate 门控力（D2=B：降级不短路）
**定位锚点**：`app/agent/evidence_gate.py:18-20`（文档承诺"`has_evidence=False` 时永远不应判 sufficient"）、
`app/graph/builder.py` 的 `gate → reasoning` 无条件边、`app/research/reasoning.py` 的 `confidence`/`data_caveats` 组装点。
**现象**：`has_evidence` 全仓无生产消费方（只有测试引用）→ 零证据时仍产出 200 + 完整报告 + 正常 confidence。
**修改（D2=B）**
1. 在 `gate → reasoning` 之间或 reasoning 内部消费 `state["gate"]`：`has_evidence=False` 时
   强制 `confidence="low"`（覆盖模型输出）、往 `data_caveats` 追加一条"证据链为空，结论不可作为依据"、
   并写 `errors`（例如 `"Evidence gate: no evidence collected; report is unverified"`）。
2. **保留** `has_evidence` 字段与「不应判 sufficient」的文档，但把文档改成
   "降级为 `confidence=low` + `data_caveats` + `errors`，不短路"——文档与行为必须一致。
3. 同步 `docs/architecture.md` 与 README 里对 gate 的描述。
**必补测试**：`tests/test_evidence_gate.py`（或现有文件）——构造空证据的 state 跑 reasoning（stub LLM 返回
`confidence="high"`），断言最终 `report.confidence == "low"` 且 `data_caveats`/`errors` 非空，**且 report 不为 None**。
**验收**：零证据路径仍然返回报告，但 confidence 恒为 low、errors 非空；把降级删掉用例必红。

### T19 — 缓存无上限 + `ttl=0` 失效
**定位锚点**
- `app/cache.py:64-65` — `def set(...): self._store[key] = _Entry(value, ttl or self._default_ttl)`
- `app/cache.py:46-48` — `__init__(self, default_ttl=30.0)`，无容量上限
- `app/cache.py:52-62` — 过期项只在读到**同键**时才删
- `app/cache.py:29-40` — `_Entry.__slots__ = ("value", "expires_at")`
**现象**
- `ttl=0`（调用方想关缓存）被 `or` 当成 falsy → 落回默认 30s。
- 长期运行的进程里 `_store` 只增不减：不同 query 的 `news_search`、不同 symbol 的行情都永久占内存。
**复现**
```python
from app.cache import Cache
c = Cache(default_ttl=30.0)
c.set("k", 1, ttl=0)
print(c.get("k"))        # 旧实现：1（还在，30s 后才过期）；期望：None
for i in range(2000):
    c.set(f"key{i}", i)
print(c.stats["size"])   # 期望有上限，旧实现 2001
```
**修改**
1. `set`：`ttl if ttl is not None else self._default_ttl`。
2. `__init__(self, default_ttl: float = 30.0, max_entries: int = 512)`；`_Entry.__slots__` 增加 `last_access`，
   `get` 命中时更新它（LRU）。
3. `set` 时顺带清理**过期**条目；若仍超过 `max_entries`，按 `last_access` 淘汰最久未访问的，
   淘汰数量记入 `self._evictions` 并 `logger.debug`（这一层是纯内存缓存，用 debug 即可，但要可观测）。
4. `stats` 增加 `max_entries` / `evictions`。
**必补测试**：新建 `tests/test_cache_capacity.py`
- `ttl=0` → `cache.get("k") is None`（且 `invalidate` 语义不受影响）；
- `ttl=None` → 仍用默认 TTL（防止把 `None` 也当 0）；
- 塞 `max_entries + 50` 个键 → `stats["size"] <= max_entries`，且**最近访问过的键还在**、
  最久未访问的被淘汰（先 `get` 一个老键再继续 set，断言它没被淘汰）；
- 先塞一个即将过期的键（`ttl=0.01` + `time.sleep(0.02)`）再 `set` 新键 → 过期键已从 `_store` 消失。
**验收**：复现脚本第一条输出 `None`、第二条输出 ≤ `max_entries`；把 `ttl if ttl is not None` 改回 `ttl or` 用例必红。
**风险 / 牵连**：`market_cache` 是全局单例（`app/cache.py` 末尾），改容量后在长调查里可能出现
"刚取过的行情被淘汰"→ 只影响性能不影响正确性；`stats` 的形状被 `/health` 或日志读取，加字段是兼容的。

### T20 — `MarketIntelligence` 必填字段导致整份报告被丢弃（+ D3 的 anomalies 接入）
**定位锚点**
- `app/models/response.py:23-25` — `market_state: str`、`what_happened: str`（无默认值）；`:33` `anomalies: list[dict[str, Any]] = Field(default_factory=list)`
- `app/research/reasoning.py:33` `_ensure_list(val, default=None)`、`:247` 的 `payload[key] = _ensure_list(...)` 白名单、`:262`、`:265-269`（`ValidationError → LLMOutputError`）
**现象**：模型少写 `market_state` / 写 `null` → ValidationError → `LLMOutputError` → 节点返回 `report=None`
→ **整份报告和此前所有已付费的工具调用作废**（调用方拿到 502 或空报告）。
**复现**
```python
from app.models.response import MarketIntelligence
MarketIntelligence.model_validate({"title": "t", "state_label": "Neutral"})  # ValidationError: market_state / what_happened
```
**修改**
1. `market_state: str = ""`、`what_happened: str = ""`（或保留必填但在 `reasoning.py` 归一化时
   `payload[k] = str(payload.get(k) or "")` —— **二选一，推荐前者 + 归一化兜底双保险**）。
2. 归一化里缺字段要写 `data_caveats`（例如 `"模型未给出 what_happened，已置空"`），
   **不允许静默变成正常报告**（规则 6）。
3. **D4 相关**：把 `anomalies` 加进 `_ensure_list` 的键集合（现在 `:247` 的白名单里没有它）。
4. **D3（必须在 T22 之后做）**：在报告产出后调用 `app/detector/anomaly.py::detect_anomalies` 填 `report.anomalies`。
   调用点建议：`app/models/response.py::build_response_from_state`（同步/SSE 双路径共用，且能拿到 `state["results"]`），
   或在 `ReasoningNode` 产出 report 之后。**不要**同时让模型也填（会双写）。
**必补测试**：`tests/test_reasoning_parsing.py`（真调 `reason()`，见 T29 的写法）
- 模型 payload 缺 `market_state` → 仍产出 report，且 `data_caveats` 里说明降级；
- 完整 payload → `anomalies` 是 `detect_anomalies` 的产出（构造一条超阈值 datum，断言非空），
  并且**不再**依赖模型输出。
**验收**：缺字段不再抛 `LLMOutputError`；`report is not None` 且能指出降级原因。
**风险 / 牵连**：`app/web/index.html:394` 渲染 `anomalies[].severity`（T25 会修健壮性）；
`anomalies` 一旦非空，前端 "What's Unusual" 才会第一次真正出现 —— 手工看一眼渲染。

### T21 — SQLite 连接跨线程复用（**"写入失败只记 debug"那半条已被 T3 提前修掉，不要再改**）
**定位锚点**
- `app/memory/storage.py:99-113` — `__init__` 里 `self._conn = None`，`:110-113` 懒建并缓存同一连接
- `app/memory/storage.py:21-42` — `_sqlite_connection_factory`（`isolation_level=None` + WAL + busy_timeout）
- `app/memory/storage.py:349-373` — `get_memory()` 进程内共享单例 + `_shared_memory_lock`
**现象**：导入期建连接（`app/main.py` 的 `get_memory()`）后，async 端点可能运行在另一个线程 →
`sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。

> **核查更正（2026-10-04，主 Agent）**：初版 T21 还写了"`main.py` 里所有写入失败都吞成 `logger.debug`（:224/:243）"。
> 这条**已经不存在了**：T3 把持久化收敛到 `app/agent/persistence.py`，三处落库失败都是
> `logger.warning(...)`（`:77-78` / `:92-93` / `:97-98`），且 `report is None` 时也有 warning；
> 全仓 `app/` 已无任何 `logger.debug`（T10 一并清了）。**本任务只需要处理跨线程连接，不要把日志改回去。**
**复现**（受限会话里 sqlite 不可写，先在可写会话跑）
```python
import threading, time
from pathlib import Path
from app.memory.storage import MarketMemory
m = MarketMemory(Path("./__t.db"))
err = []
def worker():
    try: m.save_turn("c", "q", "a")
    except Exception as e: err.append(e)
threading.Thread(target=worker).start(); time.sleep(0.5)
print(err)   # 期望：ProgrammingError（证明跨线程不可用）
```
**修改**（只做跨线程连接这一件事）
1. 连接按线程取：`threading.local()` 存连接，或改成"每次操作开短连接"（WAL + `busy_timeout` 已具备）。
   若选 `check_same_thread=False`，必须配锁（`storage.py:352` 已有 `_shared_memory_lock` 可复用）并说明理由。
2. `close()` 要覆盖所有线程连接（`threading.local` 只能看到当前线程 → 需要额外的连接登记表）。
3. **不要**动日志：落库失败已是 `logger.warning`（T3），WAL 逻辑不要动（规则 4）。
**必补测试**：`tests/test_market_memory.py` 或新建 —— 两个线程各跑一次 `save_turn`，断言两条都落库、
无异常；再补一条"写入失败时 `caplog` 里出现 WARNING"（monkeypatch 让底层抛错，防止将来有人把它降回 debug）。
**验收**：跨线程写入不再抛 `ProgrammingError`；`caplog` 里仍能看到落库失败的 WARNING。
**风险 / 牵连**：`storage.py` 的 WAL 逻辑**不要动**（规则 4）；改连接策略后 `test_persistence.py`、
`test_data_integrity.py` 里"两条连接看同一文件"的用例必须仍然绿。

### T22 — 异常检测阈值按百分比设定，但输入是绝对值（**D3 的前置**）
**定位锚点**
- `app/detector/anomaly.py:96-140` — 规则阈值 `5.0` / `10.0`（OI，"日变化 %"）、`0.15` / `0.5`（fundingRate，"%")
- `app/detector/anomaly.py:235-241` `_matches_metric`（`any(p in metric_lower for p in rule.metric_pattern.split("|"))`，子串匹配）
- `app/detector/anomaly.py:297-301` — `if rule.direction == "gt" and num_value > rule.threshold`，直接拿**绝对值**比
- 数据来源：`app/gateway/normalizer.py` 的 `_make_datum`（`unit=None`，value 是原始绝对值）
**现象**：真实 OI（≥1e9）每次快照都 > 10 → 每条都误报 `critical`；`fundingRate 0.0001 < 0.15` → 永不触发。
测试用 `MockDatum("openInterest", 8.5)` 这类假百分比值，所以一直全绿。
**复现**
```python
from app.detector.anomaly import detect_anomalies
from app.models.market import NormalizedDatum, ToolResult
tr = ToolResult(tool="derivatives", arguments={}, status="success",
                normalized=[NormalizedDatum(metric="openInterest", value=3.2e9, tool="derivatives")])
print([a.to_dict() for a in detect_anomalies([tr], domain="crypto")])
# 旧实现：critical + high 两条（把 3.2e9 当成 3.2e9% 变化）
```
**修改**
1. 规则语义改成**变化率**或**带量级的阈值**：优先用同一 metric 的前后两个快照算 `%` 变化
   （需要设计状态存放点：可用 `market_cache` 里上一次的值，或让 normalizer 附带 `unit`/`prev_value`）。
   **若无法拿到前值**，退而求其次：按量级匹配（OI 用 `> 前值的 x%`；没有前值就不触发，宁可不报不要误报）。
2. `_matches_metric` 改为**精确 metric 匹配**（或明确的别名表），去掉 `'oi'` 这种子串匹配
   （否则 `openInterestRate`、`noise` 之类都会被 `oi` 命中）。
3. 文案里的 `{:.1f}%` 与实际单位一致；`normal_range` 同步。
4. fixture 改用 `normalizer` 的真实输出（`normalize_tool_result` 的结果），不要再手搓百分比假值。
**必补测试**：`tests/test_anomaly_detector.py`
- 真实量级 OI（`3.2e9`，无前值或前值相同）→ **不触发**；
- 构造 OI 前值 1e9 → 现值 1.2e9（+20%）→ 触发 high/critical，且没有"同一 datum 报两条"的重复；
- `fundingRate = 0.002`（0.2%）→ 触发 high；`fundingRate = 0.0001` → 不触发；
- `metric="noise"`（含 `oi` 子串）→ 不触发。
**验收**：复现脚本不再输出 critical；用例在旧实现下必红。
**风险 / 牵连**：`detect_anomalies` 目前**无生产调用点**（T20/D3 会接上），所以改动只影响测试；
接上之后 `memory` 里的 `anomalies` 表会开始有记录，前端 "What's Unusual" 会亮 —— 属预期。

### T23 — HTTP 重试不感知总预算
**定位锚点**
- `app/gateway/http_client.py:91-139` — `max_attempts = self.settings.max_retry_per_tool + 1`，`:132` `backoff = min(2**attempt, 10)`
- `app/graph/nodes/analysts/base.py` 的 `_execute_tools` 串行执行工具（`budget` 上限个）
- `app/main.py:219` / `:353` — 整次调查预算 `settings.research_budget_seconds`（超时抛 `TimeoutError` → 504）
**现象**：2 次 × 30s 超时 + 429 退避（最多再 ~12s）≈ 单工具 70s；串行 8 个工具 → 单 analyst 最坏 480s+，
总预算是 300s → 结果是整次调查 504，而不是"超预算就降级返回 error ToolResult"。
**复现**（stub 一个总是超时/429 的 httpx transport 即可，不要打真实网络）
```python
# 断言：给定 deadline=2s、单工具超时 30s 时，第二次重试不再发生，直接返回 status="error"
```
**修改**
1. `execute` 增加可选 `deadline: float | None`（monotonic 秒），由 analyst 按"剩余预算 ÷ 剩余工具数"均分下发。
2. 重试前检查剩余时间：不够一次超时就**不再重试**，直接返回 `status=error` + `error="budget exhausted"`；
   退避时间也不得超过剩余预算。
3. 超预算必须以 error ToolResult 返回（规则 6），不能抛出去炸整条链。
**必补测试**：`tests/test_http_client_budget.py` —— 用 `httpx.MockTransport` 计数请求次数，
断言 deadline 用尽后请求次数 == 1，且返回 `status == "error"`、`errors` 语义明确。
**验收**：复现里的请求次数断言成立；正常（预算充足）路径的重试行为不变。

### T24 — 工具注册表：域过滤与注释相反、HK 工具 domain/category 漂移
**定位锚点**
- `app/gateway/tool_registry.py:625-639` `registry_text`：`:634` 先按 domain 过滤，
  `:636` `filtered = [t for t in filtered if t.domain != "unknown"] or ALL_TOOLS` ← 注释说"始终包含 health"，
  代码却把 `unknown`（两个 health 工具）**排除**，且域过滤为空时 `or ALL_TOOLS` 静默回退到全量 40 个工具
- `app/gateway/tool_registry.py:279-298` — `hk_quote` / `hk_search` 的 `domain="a_share"`（`hk_northbound_daily` 是 `hk_stock`），
  `hk_search` 的 `category="moneyflow"` 与 A 股同名工具不一致
- `app/gateway/tool_registry.py:599` — `BY_NAME: dict[str, ToolMeta] = {x.tool_name: x for x in ALL_TOOLS}`（后注册者静默胜出）
**现象**：`registry_text(domains=["us_stock"])` 返回**全部** 40 个工具（实测）；health 工具反而拿不到；
港股工具被算进 A 股域、被分派给错误的 analyst。
**复现**（已实测，见 §1.3）
```python
from app.gateway.tool_registry import ALL_TOOLS, registry_text
us = registry_text(domains=["us_stock"]).splitlines()
print(len(us), len(ALL_TOOLS))               # 旧实现：40 40 → us_stock 没有注册工具，被 `or ALL_TOOLS` 静默回退成全量
print(any("[a_share]" in l for l in us))     # 旧实现：True → us_stock 域里混进 A 股工具
hk = registry_text(domains=["hk_stock"]).splitlines()
print(len(hk), any("[unknown]" in l for l in hk))   # 旧实现：2 False → 注释承诺"始终包含 health"，实际被过滤掉
```
**修改**
1. 按注释实现：`tools = filtered + [t for t in ALL_TOOLS if t.domain == "unknown"]`；**删掉 `or ALL_TOOLS`**
   （域过滤为空时返回"只有 health 工具"或直接返回空 + warning，**不要**静默回退全量）。
2. `hk_quote` / `hk_search` 的 `domain` 改成 `"hk_stock"`，`category` 与同类工具对齐
   （确认 `by_category` 索引与 `resolve_tool` 的 key 唯一性不受影响）。
3. `BY_NAME` 的重复 `tool_name`：要么改成多值表（`dict[str, list[ToolMeta]]`）并让 `resolve_tool_by_name` 报歧义，
   要么删掉重复占位条目；**不要**保留"后注册者静默胜出"。改完同步 `:275-278` 与 `:678-689` 的注释。
**必补测试**：`tests/test_tool_registry_multi_domain.py`（同时修 T34 里那条恒真的 `assert A or B`）
- `registry_text(domains=["us_stock"])` 里**不含** a_share/crypto 的工具，且含 health；
- 未知域（`domains=["bogus"]`）→ 只含 health（或空）+ 有 warning，**不**等于全量；
- `BY_NAME` 无重复键（`len(BY_NAME) == len({t.tool_name for t in ALL_TOOLS})`），或按新语义断言歧义被显式处理。
**验收**：复现脚本两个断言都成立。

### T25 — 前端：并发请求互踩计时器 + 单条异常毁掉整份报告
**定位锚点**
- `app/web/index.html:241` `let lastErrorTimeout = null;`、`:570` `lastErrorTimeout = setTimeout(...)`、`:596` `clearTimeout(lastErrorTimeout)`
- `app/web/index.html:321` / `:332` / `:349` / `:369` — 多个 loader 共用 `id="ai-loading"`
- `app/web/index.html:394` — `${a.severity ? `<span class="anomaly-severity ${esc(a.severity)}">${esc(a.severity.toUpperCase())}</span>` : ''}`
- `app/web/index.html:546-600` — `async function ask()`，无重入判断
**现象**
- 调查中回车追问 → 两条 SSE 并发，先结束者的 `finally` 清掉后到者的 `lastErrorTimeout`
  → 后到请求失去唯一中止手段（只能等服务端超时）。
- `anomalies` 是 `list[dict[str, Any]]`（后端无结构约束），`severity` 为数字/布尔时
  `toUpperCase()` 抛 TypeError → 被 `ask()` 的 catch 变成"调查失败" → **三分钟的成功报告整份丢弃**。
**修改**
1. 计时器与 `AbortController` 改为**按请求持有**（局部变量 / `Map<requestId, ...>`），
   `finally` 只清自己的。
2. `ask()` 开头防重入：若已有进行中的请求，**中止旧的**（或禁用输入并提示），不要并发跑两条。
3. `id="ai-loading"` 改 class 或加唯一 id，`removeLoading()` 只操作自己那一行。
4. 渲染健壮化：`String(a.severity ?? '')`、类名白名单（`low/medium/high/critical`，未知 → 默认样式）、
   渲染循环**单条 try/catch** 降级（一条坏数据不让整份报告消失）。
5. 后端侧（可选，非本任务必需）：给 `anomalies` 一个 Pydantic 结构约束。
**必补测试**：`tests/test_web_frontend_contract.py`（新建，纯文本断言）
- `index.html` 内不再存在全局单值 `lastErrorTimeout`（或断言它按请求作用域出现）；
- `severity.toUpperCase()` 直接调用不存在，改为 `String(...)`；
- `anomalies` 渲染处存在白名单函数名。
  （前端没有 JS 测试环境，这类"文本契约测试"是当前项目可行的最小防线；若你引入 node 测试要单独提交并说明。）
**验收**：文本契约测试绿；手工在浏览器里发两条并发追问，先结束者不再影响后到者；
把 `severity` 手工改成 `1` 时界面仍能渲染出报告（不显示徽章即可）。

### T26 — SSE 端点的错误分支不可达 + 后台任务只 cancel 不 await
**定位锚点**
- `app/main.py:408-424` `ask_stream`：`try:` 里只有 `return StreamingResponse(_stream_research(...))`，
  async generator 体要到响应开始后才执行 → `except ValueError` / `except Exception` **永不触发**
- `app/main.py:254-393` `_stream_research`：内部自己 try/except 并把错误作为 SSE `error` 事件 yield
- `app/main.py:286` `task = asyncio.create_task(_pump())`；`:296` / `:391-393` `finally: if not task.done(): task.cancel()`（**只 cancel，不 await**）
- 前端 `app/web/index.html` 只在**非 2xx** 时回退 `/api/ask` → 因为永远是 200，所以永不回退
**修改**
1. 在 `try` 内**提前**做会失败的事并让分支可达：`_parse_ask_payload`（已验证会抛 ValueError）、
   `settings = _get_orchestrator().settings`、`graph = _get_orchestrator()._ensure_graph()`
   —— 图构建失败应当在 HTTP 层就变成 500，而不是 200 + SSE error。
2. 任务收尾：`finally` 里 `task.cancel()` 后加 `await asyncio.gather(task, return_exceptions=True)`。
3. 保留 SSE 内部的 per-step error 事件（运行期错误仍要走 200 + event，这是既有契约，不要改成非 2xx）。
**必补测试**：`tests/test_stream_endpoint.py`（受限会话需附录 A）
- monkeypatch 让 `_ensure_graph()` 抛异常 → 断言响应是 **500**（旧实现是 200）；
- monkeypatch 让 `_pump` 抛异常 → 断言收尾后没有 "Task exception was never retrieved" 警告
  （可用 `caplog` 或 pytest 的 `recwarn`/`asyncio` 日志捕获）。
**验收**：两条用例在旧实现下必红。

### T27 — `called_signatures` 的"去重"契约不成立
**定位锚点**
- `app/graph/tool_runtime.py:212-229` `_check_cache`：
  ```python
  signature = f"{tool_name}:{sorted(arguments.items())}"
  if signature in called_signatures:
      return None                      # ← 返回 None 表示"无缓存"，调用方于是真的去打网关
  ...
  if cached is not None:
      called_signatures.add(signature)  # ← 只在缓存命中时才登记
      return cached
  return None
  ```
- 调用方 `execute()`（`:95-122`）与 `app/graph/nodes/analysts/base.py::_execute_tools`
**现象**：缓存未命中时签名从不登记 → 「已执行签名」集合阻止不了重复调用；
更糟的是同参数第 3 次调用会因 `signature in called_signatures` 直接 `return None` →
**绕过缓存**再打一次真实网关（既慢又浪费，且与"去重"意图相反）。
**复现**
```python
# 同一 (tool, arguments) 连调 3 次，统计真实网关调用次数
# 期望：1 次（第 2、3 次命中缓存或直接跳过）；旧实现：2 次
```
**修改**
1. `_check_cache` 里"签名已见过"应表达为明确的**跳过**语义（返回一个 `SKIP` 哨兵或让 `execute`
   直接返回一条 `status="partial"/"error"` 的空结果），不要用 `None` 混同"无缓存"。
2. 真实执行**成功后立即**登记签名（`execute` 里 `_do_execute` 返回后 `called_signatures.add(signature)`）。
3. `base._execute_tools` 用 `(tool_name, sorted(args))` 的 seen 集合真正跳过重复的 `tool_calls`（route 里重复时）。
**必补测试**：新建 `tests/test_tool_runtime_dedup.py` —— 用计数 Gateway stub，
断言同签名 3 次调用只发生 **1** 次真实调用，且 3 次返回的 `status` 都不是"静默成功"。
**验收**：计数断言在旧实现下必红（旧实现为 2）。

---

## 4. 批次 4 — 测试有效性（系统性短板）

> **本项目的根本教训**：`MarketAnalystNode.__call__`、`GateNode`、`CriticNode` 都把异常吞成 `errors` 继续跑，
> 于是「节点内部炸了」在测试里可能表现为"通过"。下面每条都是已验证的假阳性，请逐个改成真测试。
> 建议顺序 T28→T34，最后做 T35 机制性防线。
> **注意**：T18T（§2）已经给 `tests/test_graph_nodes.py` 加了 `_NoGatewayRuntime` stub，
> T34 清理同文件其它空转 fixture 时请复用它，不要另造一套。

### T28 — `tests/test_graph_e2e.py` 的 finding 结构用例只查类型，从未走到成功分支
fake 是 `lambda *a, **k: ([])`（不是 awaitable）→ `await` 抛 TypeError 被 `base.py` 的兜底 `except` 吞掉、
`failed=True`，而断言只有 `isinstance(f["failed"], bool)`，两种结果都通过。
改法：`async def _execute_tools(self, state, sigs): return []`，断言 `f["failed"] is False` 且 `f["tools_used"] == []`，
并删掉 `@pytest.mark.skip(reason="P3 特性 — MarketAnalystNode 依赖 by_category")`。

### T29 — `tests/test_reasoning_parsing.py:207-220` 自带一份 `_ensure_lists` 副本，断言与生产相反
副本把非法 `confidence` 置 `medium`，生产 `app/research/reasoning.py` 置 `low`。该类 6 个用例从不调用真的 `reason()`。
改法：删掉副本，照 `tests/test_data_integrity.py:353-375` 的 `_stub_reasoning` 模式真调 `await ReasoningEngine.reason()`，
断言 `confidence == "low"`。

### T30 — `tests/test_graph_nodes.py` 的 `assert node._execute_tools.__code__.co_varnames` 恒真
budget 守卫删掉也不报错。补测：构造 `budget=1` 而 `tool_calls` 有 3 个的 route，断言只执行 1 次 / `len(results) == 1`。
**顺带做 D4**：`base.py` 的 `budget = int(mine.get("budget") or len(mine["tool_calls"]))` 改成
`raw_budget = mine.get("budget")` + `budget = len(tool_calls) if raw_budget is None else int(raw_budget)`，
使**显式 `budget=0` 表示不执行**（现在被 `or` 吞成"全部执行"）；并让 `config.max_tool_calls` 成为真实上限
（在 `_execute_tools` 或 supervisor 侧与 `budget` 取 min，并补 `/health` 之外的真实消费点）。

### T31 — `tests/test_graph_e2e.py:118-136` 唯一的真实全链路 e2e 被 skip，理由已过期
skip 理由写"P0 图为 supervisor→kernel"，但 `app/graph/builder.py` 早已注册三个 analyst。去掉 skip 让它真跑；
确不打算跑就改 `xfail(strict=False)` 并在本文档 §0.4 标注未覆盖（**不要**创建 `PROJECT_STATUS.md`）。

### T32 — `tests/test_market_memory.py` 名为"唯一约束"却从不触发约束
只连插三条并断言 `idx3 == 3`。补测：直接 INSERT 重复 `(conversation_id, turn_index)` 断言 `sqlite3.IntegrityError`；
再加两条连接并发 `save_turn` 的用例（`storage.py` 的 `MAX+1` 存在 TOCTOU）。

### T33 — `tests/test_models_multi_domain.py:20-30` 循环体是运行时 no-op
`_: MarketDomain = val` 不产生任何校验，加个 `"bogus"` 也通过。改成真实例化断言（合法值通过、非法值 `ValidationError`）
或对 `get_args(MarketDomain)` 做集合断言。

### T34 — 其余低价值假阳性（顺手修）
- `tests/test_graph_nodes.py` 的 `fake_execute` 缺 `self` 参数 → 永远无法生效；而用例又用 `FakeRuntime` 覆盖了 `_runtime`，
  该 fixture 完全空转。补 `self` 并让用例真正依赖它（**复用 §2 T18T 的 `_NoGatewayRuntime`**）。
- `tests/test_graph_routing.py:146-156`：`FakeOpenAI(raise_on=...)` 对任何输入都抛，用例只测了 mock 的行为。补非空 question 的对照断言。
- `tests/test_tool_registry_multi_domain.py:104-110`：`assert A or B` 应为 `and`（或 `assert not {...} & set(...)`），
  并补 `registry_text(domains=["us_stock"])` 的用例以暴露 `or ALL_TOOLS` 回退（与 T24 合并做）。
- `tests/test_news_search.py:37-68`：错误路径用例打真实 DDGS 且断言恒真
  （`if count==0: isinstance(meta.get("error"), str) or meta.get("status")`，而 status 恒存在）。
  改为 monkeypatch `DDGS` 抛异常/返回空，断言 `meta.error`；顺带补 `news_search.py` 的空 query 分支。

### T35 — 机制性防线（建议在批次 4 一起做）
1. 在 `tests/conftest.py` 加约定/检查：**禁止把被测节点整体替换掉**（monkeypatch `__call__` 只能用于"非本次验证目标"的节点），
   至少在每个文件顶部写明"本文件 mock 掉了什么、因此没有覆盖什么"。
2. 给 `MarketAnalystNode.__call__` / `GateNode` / `CriticNode` 的兜底 `except` 分支加统一标记
   （例如 `errors` 前缀 + `finding.failed=True`），并**在关键测试里断言 `errors == []`**
   （`tests/test_graph_topology.py` 已开始这么做，请推广到所有 e2e 用例；§2 的 T18T 已经给三条用例加上了）。
3. 每个新增测试都自问：**"把对应生产代码改坏，它会不会变红？"** 不会就重写。

---

## 5. 收尾

### T36 — 文档与实现同步
- `docs/architecture.md` / `README.md`：补 Critic 的内部 `error` 语义（T11）；补 gate 的降级语义（T15/D2）。
- `app/agent/prompts.py` 的 evidence id 示例仍是 `evidence-001`，实际已是 `{category}-NNN`（T4 之后）。
- `app/gateway/tool_registry.py` 的域过滤注释与实现相反（T24 修完同步）。
- 完成后做一次全局检查：**不再存在"文档承诺、代码没有"的项**——重点核对四处：
  `has_evidence`（T15 已定：降级不短路）、`anomalies`（D3：代码填）、证据条数上限（T6）、MCP 模式与版本区间（D1：`mcp>=2,<3`）。

### T37 — 打包 / CI：setuptools flat-layout 自动发现失败（S8，**已被验收者直接复现**）
**定位锚点**
- `pyproject.toml:1-3`（有 `[build-system]` 但**没有 `[tool.setuptools]` 段**）
- `pyproject.toml:30-32`（`[tool.pytest.ini_options]` 是唯一的 tool 段）
- `.github/workflows/ci.yml` 的安装步骤：`pip install -e ".[dev]"`
**现象**：CI 的安装步骤会失败，测试根本没机会跑。
**复现（验收者已用 pip 本体跑出，不只是推断）**
```bash
.venv\Scripts\python.exe -m pip install -e . --no-deps --dry-run
# ERROR: Failed to build 'file:///D:/Mosaic' when getting requirements to build editable
#   ... setuptools will not proceed with this build.
#   1. set up custom discovery (`find` directive with `include` or `exclude`)
```
失败发生在 **"getting requirements to build editable"** 阶段 —— 连依赖都还没解析，
所以**与 `mcp>=2,<3` 那一行完全无关**，是仓库既有问题。
再用 setuptools 的发现器单独看顶层包：
```bash
python -c "from setuptools.discovery import FlatLayoutPackageFinder as F; print(F.find(where='.'))"
# ['app', 'memory', 'app.agent', ..., 'memory.evening', 'memory.morning']
```
顶层包有**两个**（`app` 与 `memory`）→ setuptools 的 flat-layout 自动发现报
`Multiple top-level packages discovered in a flat-layout: ['app', 'memory']`。
注意 `memory/morning/`、`memory/evening/` 是**简报 JSON 数据目录**，被 PEP 420 当成命名空间子包了。
**根因**：仓库是多顶层目录布局，却没有显式声明要打包哪些包。
**修改**
1. 在 `pyproject.toml` 加显式打包声明，例如：
   ```toml
   [tool.setuptools.packages.find]
   include = ["app*"]
   exclude = ["memory*", "tests*", "scripts*"]
   ```
   （或 `[tool.setuptools] packages = ["app"]` + 子包列表）。
2. `app/web/index.html` 是运行时依赖的文件（`app/main.py` 的 `INDEX`）——若要做**非** editable 安装，
   必须用 `[tool.setuptools.package-data]` 把它带上；editable 安装不受影响。
   顺手确认 `app/web/__init__.py` 是否存在（不存在则 `app.web` 不是包）。
3. 改完在**完全访问**会话里真跑一次 `pip install -e ".[dev]"`，确认通过；
   CI 的 `.github/workflows/ci.yml` 不需要改（它本来就是对的，是仓库配置缺声明）。
**验收**：`pip install -e ".[dev]"` 成功；`python -c "import app, memory"` 行为不变；
`pytest -q` 全绿；`importlib.metadata.version("mosaic-market-intelligence")` 能查到。
**风险 / 牵连**：这是**既有问题，不是 S1 引入的**（`FlatLayoutPackageFinder` 的结果与 `mcp` 依赖行无关）。
改 `pyproject.toml` 会影响 CI、以及任何 `pip install` 的使用者；改完必须重跑一次全量测试。
**不属于本任务**：不要顺手把仓库拆成 `src/` 布局（那会动所有 import，超出范围）。

**完成定义**：批次 3–4 全绿（`ruff check` + `ruff format --check` + `pytest -q`），
每条修复都带一个「改坏它就会红」的测试，文档与代码实际行为一致。
`PROJECT_STATUS.md` 已被仓库主人删除，**不要再创建**；状态以本文档 §0.4 为准。

---

## 附录 A — 在受限环境跑完整测试

正常情况下 `pytest -q` 即可。**但主 Agent 复核时实测：workspace-write 会话里 `memory/` 下的 sqlite 打不开**
（`sqlite3.OperationalError: unable to open database file`；`Get-Acl memory` 能看到沙箱的 deny ACE），
表现为 `test_ask_endpoint.py` / `test_stream_endpoint.py` **收集期就 error**，
`test_conversation.py` / `test_market_memory.py` / `test_data_integrity.py` / `test_persistence.py` 大量 error。
**这是环境问题，不是代码问题，不要为了让它变绿去改产品代码或改测试断言。**

**情况一：会话是「完全访问」** —— 什么都不用做，`pytest -q` 直接跑全量
（批次 2 的执行会话就是这种，报 `408 passed / 2 skipped`）。

**情况二：workspace-write 且 `memory/` 不可写** —— 用下面的 stub 把默认项目库重定向到可写目录
（**放在 `.tmp/` 下，不要提交**）：
```python
# .tmp/run_tests_local.py
import os, pathlib, sqlite3, sys
os.environ.setdefault("TEMP", r"D:\Mosaic\.tmp")
os.environ.setdefault("TMP", r"D:\Mosaic\.tmp")
sys.path.insert(0, r"D:\Mosaic")
import app.memory.storage as storage
ROOT = pathlib.Path(r"D:\Mosaic")
def factory(path):
    p = pathlib.Path(path)
    if p.parent.name == "memory":        # 默认项目库 -> 重定向到可写目录
        p = ROOT / ".tmp" / "__ci_memory.db"
    conn = sqlite3.Connection(str(p), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
storage._sqlite_connection_factory = factory
import pytest
sys.exit(pytest.main(sys.argv[1:] or ["-q", "tests/"]))
```
```bash
python .tmp/run_tests_local.py -q                                # 全量
python .tmp/run_tests_local.py tests/test_graph_nodes.py -q      # 单文件
```
> 已知副作用（验收时已确认，属正常）：重定向后这几条必然失败，因为它们的语义要求"两条连接打开同一个文件"
> 或断言文件路径，而 `tmp_path` 在沙箱里也不可写：
> `test_writes_are_visible_to_a_second_connection`、`test_writes_survive_a_restart`、
> `test_conversation_history_round_trips_across_instances`、`test_db_file_created_on_init`、
> `TestDatabaseSetup` 两条、`test_persistence.py` 两条。
> 主 Agent 复核时这一类共 36 error + 12 failed，全部归因于此；**判红之前先确认失败是不是这一类**。

**关于 gateway 模式（复核时的重要经验）**：`.env` 里是 `MARKET_GATEWAY_MODE=mcp`，
而沙箱**禁止 spawn 子进程**（实测 `MCPConnectionError: MCP server startup failed: [WinError 5] 拒绝访问`）。
所以只想验证 analyst/图逻辑时，可以临时 `$env:MARKET_GATEWAY_MODE="http"` 跑（HTTP 客户端进入时不连网）。
**但最终验收必须在 `mcp` 模式下也全绿**——这正是 §1.2 P1 的教训。`ruff` 同理，用 `--no-cache`。

---

## 附录 B — 复现命令速查

```bash
# ── §2 修红 ────────────────────────────────────────────────
# T18b：内部工具不该连 gateway（期望 opened = 0）
# 把 §2 T18b 的复现块原文存成 .tmp/verify_gateway_lazy.py 再跑（主 Agent 已跑过，输出 opened = 1）
python .tmp/verify_gateway_lazy.py
# T18b 的连带证据：mcp 模式下这 6 条红，http 模式下全绿
$env:MARKET_GATEWAY_MODE="mcp";  pytest tests/test_news_no_symbol.py tests/test_graph_topology.py -q
$env:MARKET_GATEWAY_MODE="http"; pytest tests/test_news_no_symbol.py tests/test_graph_topology.py -q

# T17T：patch 目标错误（真打网络）
pytest tests/test_tool_runtime_ttl.py -q
grep -n "_search_news" app/graph/tool_runtime.py

# T18T：stub 缺 gateway_session
pytest tests/test_graph_nodes.py -q -k "no_matching_tools or skips_non_whitelist"

# ── 批次 3 ────────────────────────────────────────────────
# T19 缓存
python -c "from app.cache import Cache; c=Cache(); c.set('k',1,ttl=0); print(c.get('k')); [c.set(f'k{i}',i) for i in range(2000)]; print(c.stats)"
# T20 必填字段
python -c "from app.models.response import MarketIntelligence as M; M.model_validate({'title':'t','state_label':'Neutral'})"
# T22 anomaly 阈值单位（期望不再误报 critical）
python -c "from app.detector.anomaly import detect_anomalies as d; from app.models.market import NormalizedDatum as D, ToolResult as T; tr=T(tool='derivatives',arguments={},status='success',normalized=[D(metric='openInterest',value=3.2e9,tool='derivatives')]); print([a.to_dict() for a in d([tr],domain='crypto')])"
# T24 注册表域过滤
python -c "from app.gateway.tool_registry import registry_text as r, ALL_TOOLS; print(len(r(domains=['us_stock']).splitlines()), len(ALL_TOOLS)); print([l for l in r().splitlines() if 'unknown' in l])"

# ── 历史修复的回归复现（已完成任务的契约，改回必红）─────────
# T1 空载荷
python -c "from app.gateway.normalizer import normalize_tool_result as n; [print(repr(r), (lambda x:(x.status,len(x.normalized)))(n('quote_tencent_quote_get',{},r))) for r in ({},[],None,'Server busy',0)]"
# T2 F10 折叠
python -c "from app.gateway.normalizer import normalize_tool_result as n; r=n('finance_eastmoney_f10_finance_get',{'symbol':'601398'},{'data':{'indicators':{'ROE':{'value':12.5,'unit':'%'},'PE':{'value':15.2,'unit':'x'}}}}); print([(d.metric,d.value) for d in r.normalized])"
# T4 evidence id 前缀
python -c "from app.research.evidence import build_evidence as b; from app.models.market import NormalizedDatum as D, ToolResult as T; mk=lambda t,m,v:T(tool=t,arguments={},status='success',normalized=[D(metric=m,value=v,tool=t)]); print([e.id for e in b([mk('a','x',1)],id_prefix='technical')],[e.id for e in b([mk('b','y',2)],id_prefix='fundamental')])"
# T7 candle_summary
python -c "from app.research.evidence import build_evidence as b; from app.models.market import NormalizedDatum as D, ToolResult as T; kn=[('candles[0].o',1.0),('candles[0].c',2.0),('candles[1].o',2.0),('candles[1].c',4.0)]; tr=T(tool='klines_market_klines_post',arguments={},status='success',normalized=[D(metric=m,value=v,tool='k') for m,v in kn]+[D(metric='openInterest',value=8.5,tool='k')]); [print(e.metric,e.value) for e in b([tr])]"
# T11 verdict 归一化
python -c "from app.graph.builder import critic_route_decision as d; from app.config import Settings; s=Settings(); [print(repr(v), d({'critique':{'verdict':v},'revision_count':0,'report':{'what_happened':'x'}}, s)) for v in ('pass','revise','research_more','PASS','fail','',None)]"
# T16 reducer（把 state.py 的三个 reducer 改回 operator.add 后必红）
pytest tests/test_graph_state_reducers.py -q
# T17 TTL
pytest tests/test_tool_runtime_ttl.py -q
# T14b 过点不补
pytest tests/test_brief_scheduler.py -q
```

---

## 附录 C — 修复顺序速览（可直接当 checklist）

```
批次 1（P0）        ✅ 735aeaa / b8e114c / f86df07
批次 2（P1）        ✅ dc75971 589cff7 a293e44 164c920 8c7c4ab c471dec d9f60fe 432b7ab 50aff98 ff03a2d f4bee89 3454af5
批次 2 遗留         ✅ 7e87c6c(T14b+T5b 文案) fca2025(T13b) a9732dc(文档) df757a5(T16) 2bf11b7(T17) 44c307f(T18)
                       —— T18b/T17T/T18T 已作为 S1 修红并验收（见下）

批次 3 前置：修红（S1）— ✅ 完成并验收（5647a4a / b1b06fa / f2e4da1+e2513eb / 0965da6）
[x] T18b gateway 按需连接（产品，修 T18 的急切连接回归）
[x] T17T 修正 T17 测试 patch 目标（测试）
[x] T18T 修 2 条 stub 测试（测试，不改产品）
[x] T12 pyproject 收成 mcp>=2,<3（D1）
    验收已过：mcp + http 双模式全绿；反向验证必红；记录见 §0.4 "S1 验收复核记录"

S2 运行时 / 缓存 / 成本 — ✅ 完成并双模式全绿（f3b76e4 / bc4849d / e6a04e5 / f119f51+7dce255）
[x] T18c 会话状态改每请求一份（P1-b 并发串台，ContextVar）
[x] T19 缓存上限 + ttl=0
[x] T27 called_signatures 真去重
[x] T23 重试感知预算

S2 之后（批次 3 其余）
[ ] T12b requirements.txt 同步 mcp>=2,<3（S7，一行）
[ ] T20 必填字段默认值（+ D4 的 _ensure_list）
[ ] T21 sqlite 线程跨线程            [ ] T22 anomaly 阈值/单位（D3 前置）
[ ] T24 注册表域过滤 / HK 漂移
[ ] T25 前端并发与渲染健壮性        [ ] T26 SSE 错误分支可达 + 任务 await
[ ] T15 Evidence Gate 降级（D2=B）  [ ] T20 后半：anomalies 用 detect_anomalies 填（D3，须在 T22 后）

批次 4（测试有效性）  T28 T29 T30(+D4 budget) T31 T32 T33 T34 + T35 机制性防线

收尾（S8）
[ ] T37 打包/CI flat-layout（pyproject 显式声明 app 包；CI 的 pip install -e ".[dev]" 才能过）  ← 验收者新发现
[ ] T36 文档与实现同步（architecture / README / prompts / tool_registry）
[ ] §0.4 更新（每完成一批就更新：任务表打 ✅ + 提交号 + 测试数字）

已拍板、不要再问：D1 mcp>=2,<3 ｜ D2 Gate 降级不短路 ｜ D3 anomalies 代码填 ｜ D4 budget/max_tool_calls 接通
已拍板、不要改回：Critic error 语义 ｜ 简报过点不补 ｜ evidence id 前缀 ｜ truncate 保留末尾 + partial ｜ 不要重建 PROJECT_STATUS.md
```

---

## 附录 D — 已完成任务的契约备忘（T1–T18 + S1 修红）

> 细节用 `git show <提交号>` 查回。这里只保留**不许改回**的语义与对应的回归测试位置。
> S1 修红新增/更新的契约已并入下表（T17 / T18 / T12 行）。

| 任务 | 提交 | 不许改回的契约 | 回归测试 |
|---|---|---|---|
| T1 空载荷判失败 | `735aeaa` | 空容器 / 裸标量 / `None` 一律 `status="error"`、`normalized=[]`；`normalized` 为空时不许是 `success` | `tests/test_normalizer_empty.py` 等 |
| T2 F10 多指标 + unit | `b8e114c` | 多子键不折叠（不取第一个）；`metric` 保留可辨识路径；同级 `unit` 写进 datum | `tests/test_normalizer_f10.py` |
| T3 持久化字段层级 | `f86df07` | 同步 / SSE 共用一套 `persist_research`；一律读 `result.report`；`report=None` 时只存研究记录 | `tests/test_persistence.py` |
| T4 evidence id 唯一化 | `c471dec` | id 形如 `{category}-NNN`（`build_evidence(..., id_prefix=...)`） | `tests/test_evidence.py` |
| T5 truncate 保留末尾 | `d9f60fe` | 保留**末尾** 200；切片前取原始条数；置 `partial`；缓存存深拷贝 | `tests/test_truncate.py` |
| T6 证据条数上限 | `50aff98` | `_MAX_EVIDENCE_ITEMS` 真的生效（按来源保留最新 + note） | `tests/test_evidence.py` |
| T7 candle_summary 取值 | `432b7ab` | 每根 K 线只取一个收盘价；`price_last` = 最后一根收盘；混合载荷不丢指标 | `tests/test_evidence.py` |
| T8 symbol 守卫白名单 | `ff03a2d` | `news_search` / `search` / `longhu` / `internal_hk_*` 无 symbol 也必须执行；跳过记 `warning` | `tests/test_news_no_symbol.py` |
| T9 CLI report=None | `164c920` | dump 前判 `None`，把 `result.errors` 打到 stderr + 非 0 退出码 | — |
| T10 日志命名空间 | `8c7c4ab` | handler 覆盖 `app.*`；不用 `propagate=True` 掩盖 | `tests/test_ask_endpoint.py` |
| T11 Critic verdict | `589cff7` | 审计失败 → `verdict="error"` → 安全 END + 保留 report + `errors`；**不得**回 `research_more`；`/api/ask` 仍 200 | `tests/test_critic_verdict.py`（20 项） |
| T12 MCP 握手 + 清理 | `a293e44` | `connect()` 先 `initialize()`（带超时）；失败先 `close()`；`close()` 无条件 `stack.aclose()` | `tests/test_data_integrity.py`（注入 `FakeSession`，仍绕开 `connect()`） |
| T13 evaluation 真验收 | `f4bee89` | 空集不得为真；读 `report.used_tools` / `tool_results`；错误按 `case_id` 查表 | `tests/test_evaluation.py` |
| T14 简报精确触发 | `3454af5` | 精确 sleep 到目标时刻 + 每日去重 + 跨日重置；固定 UTC+8 | `tests/test_brief_scheduler.py` |
| **T14b 过点不补** | `7e87c6c` | 只在 `[目标时刻, +GRACE(5min)]` 内触发；**不 catch-up**；`GRACE=0` 不可用 | 同文件 3 条 |
| T13b evaluation 兜底 | `fca2025` | 未知 `case_id` → 按 FAIL，不 KeyError、不跳过 | `tests/test_evaluation.py` |
| T5b note 文案 | `7e87c6c` | note 写明"保留最新 200 条（另加本说明条）"，与实际 201 条一致 | `tests/test_truncate.py`（追加断言） |
| **T16 回边 reducer** | `df757a5` | `results` 按 `(tool, arguments)`、`evidence` 按 `id`、`findings` 按 `analyst` 去重（同键保留最新，不同键追加以支持并行 analyst）；`errors` 仍追加；**`state.tool_results` 已删，不要再加回** | `tests/test_graph_state_reducers.py` |
| **T17 news_search TTL** | `2bf11b7` + `b1b06fa`（T17T） | `_resolve_ttl(tool, settings=None)`；`news_search` → `settings.news_search_ttl_seconds`（21600），`execute()` 不得把它覆盖成 30s；测试必须 patch **模块全局** `app.graph.tool_runtime._search_news`（实例属性无效） | `tests/test_tool_runtime_ttl.py` |
| **T18 analyst 级复用 Gateway** | `44c307f` + `5647a4a`（T18b）+ `f2e4da1`（T18T） | 一次 analyst 运行复用同一个 Gateway 客户端，异常时保证关闭；**连接必须按需**（进入会话不建连）；stub 必须提供 `gateway_session`；**会话状态还要改成每请求一份（见 T18c）** | `tests/test_gateway_reuse.py` |
