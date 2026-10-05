# Mosaic 代码审计修复计划（交接给执行 Agent）

> **本次重写日期**：2026-10-04，由主 Agent 在独立验收批次 2 遗留（T14b/T13b/T5b）与批次 3 前半（T16/T17/T18）之后重写。
> **最近一次更新**：2026-10-05 —— **S1 / S2 / S3 / S4 / S5 / S6 均已由执行方完成、主 Agent 已独立验收通过；S7 已由执行方完成（待主 Agent 验收）**
> （见 §0.4 的 "S1–S6 验收复核记录"）。
> S1 验收追加：**T18c**（并发串台，P1；已在 S2 修完）、**T37**（打包/CI flat-layout）、**T12b**（依赖清单漂移）。
> S2 验收追加：**T19b**（假阳性用例）、**T23b**（预算起算点）—— 已在 S3 修完。
> S3 验收追加：**T22b**（点分 metric 漏报）、**T22c**（前值跨 symbol 污染）—— 已在 S4 修完。
> S4 验收追加：**T24b**（`.gitignore` 一行）—— 已在 S5 修完。
> **S5 验收结论：无新立任务**（4 个任务全部通过；T25 的**浏览器手工验证已由仓库主人执行并通过**，2026-10-05，详见 §0.4 S5 一节）。
> **S6 验收结论：无新立任务**（4 个任务全部通过；T30 的独立反向验证退回旧 `base.py` 复得 4 红 2 绿，与执行方声明精确一致；**全量从此 0 skipped**，详见 §0.4 S6 一节）。
> **S7 执行结论（执行方自记，待验收）**：T32 / T33 / T34（不含注册表条目，已并入 S4）/ T35 / T12b 全部完成，双模式 **506 passed + 0 skipped**，无新立任务；T35 的 conftest 约定与 `tests/test_conventions.py` 机械检查自 S7 起对所有新增/修改的测试文件生效。
> 另把四条"测试假阳性/反向验证"教训汇总进了 **§4 的 T35**（做 conftest 约定时一并落地）。
> **行号基准**：`44c307f`（S1/S2 的改动未影响未完成任务的锚点；`tool_runtime.py` 的
> `execute`/`_do_execute`/`_call_gateway` 已带 `deadline` 参数、会话状态在模块级 ContextVar `_session`，
> 动手时以锚点片段 grep 为准）。若行号漂移，以「定位锚点」里的代码片段为准。
> **当前 HEAD**：`55b9973`（S7 五个任务提交的最后一个；S7 的 §0.4 更新在其后的单独 chore 提交，见 §0.4 "S7 执行记录"）。
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

> **当前 HEAD：`1840f6b`（S6 收尾）。** S1（修红）+ S2（T18c/T19/T27/T23）+ S3（T19b/T23b/T22/T20/T15）
> + S4（T22b/T22c/T24）+ S5（T24b/T21/T26/T25）+ S6（T28/T29/T30+D4/T31）均已完成：
> **499 passed + 0 skipped（全量再无 skip），`mcp` 与 `http` 两种 gateway 模式下全量测试均全绿**（2026-10-05，S6 会话实测）。
> ruff check / ruff format --check 全绿。T28–T31 每条都先探针证实假阳性、改后做「改坏必红」反向验证（详见 §0.4 表格与 S6 执行记录）。
> 行号基准 = `44c307f`（S1–S6 的改动未影响 §3 以后任务的锚点；但 `tool_runtime.py` 的
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
| **T19 缓存** | ✅ 完成并验收（假阳性已由 T19b 修掉） | `bc4849d` / `61c48e9` | `ttl=0` 立即过期（`ttl or` 反向验证必红）；`max_entries=512` + LRU 淘汰 + set 顺带清理过期 + stats 加 `max_entries`/`evictions` |
| **T27 去重** | ✅ 完成 | `e6a04e5` | 同签名 3 次调用真实网关 2→1 次；重复调用返回 `partial` + note；route 层 seen 集合提前跳过；stash 旧实现 3 条全红 |
| **T23 重试预算** | ✅ 完成并验收（起算点已由 T23b 修为整次调查） | `f119f51` + `7dce255` / `661be64` | deadline 全链路下发（base 均分 → execute → gateway.call）；忽略 deadline 反向验证 3/4 条红；测试 stub 的 call/execute 签名已补 `deadline=None` |
| **T19b 假阳性** | ✅ 完成 | `61c48e9` | 断言前不再先 get；读路径覆盖拆独立用例；停用 purge 必红（已反向验证）；其余 4 条自检过 |
| **T23b 调查级预算** | ✅ 完成 | `661be64` | orchestrator/SSE 写入 state["budget_deadline"]，analyst 按剩余调查预算均分；缺失时退回旧行为 + warning；反向验证（回退本节点起算）2 条必红 |
| **T22 异常检测** | ✅ 完成并验收（T22b/T22c 缺陷已由 S4 修掉，见下两行） | `17b0782` / `e7c2272` / `08c3656` | ✅ 语义与阈值（`value_semantics`）、同 datum 同类型去重；✅ 基名归一化后嵌套载荷（`data.openInterest` / `data[0].openInterest`）不再漏报；✅ 前值按 (metric 基名, symbol) 隔离，跨 symbol 不再互相污染 |
| **T20 必填字段 + D3** | ✅ 完成并**验收通过**（前提是 T22b/T22c 修好才真正生效） | `a9d6a89` | market_state/what_happened 置空 + data_caveats；anomalies 归一化 list[dict]；build_response_from_state 用 detect_anomalies 代码填充；旧实现下 4 failed |
| **T15 Evidence Gate（D2）** | ✅ 完成并**验收通过** | `7d66ad2` | Reasoning 消费 state["gate"]：has_evidence=False → 强制 confidence=low + caveats + errors，报告仍产出；文档同步（evidence_gate/architecture/README）；停用降级 2 条必红（我复跑确认） |
| **T22b metric 名归一化** | ✅ 完成并验收（S4） | `e7c2272` | `_metric_basename`：去掉数组下标、取末段基名后与别名表精确比对（保住 T22 的"不做子串匹配"裁决，noise/openInterestRate 仍不误命中）；12 条新用例**全部经过 `normalize_tool_result`**（三种载荷形状 + A 股 `data.涨停家数` + 反向对照）；反向验证：恒等函数 → 7 条必红 |
| **T22c 前值按 symbol 隔离** | ✅ 完成并验收（S4） | `08c3656` | 前值 key = `(metric_basename, symbol)`（symbol 取 `arguments.symbol` → domain → "unknown"）；同一次调用内混入多 symbol 也隔离；market_cache 存储取舍已写入注释（TTL/LRU 淘汰与"前值过期不可比"一致，丢前值=少报不误报）+ 拿不到前值留 debug；6 条新用例；反向验证：停用 symbol 作用域 → 5 条必红（含"三次独立调查"复现用例——初版 parametrize 写法被反向验证抓出是假阳性，已改为单用例内顺序执行） |
| **T24 注册表** | ✅ 完成并验收（S4，**并入 T34 的注册表断言**） | `3bef4f4` | ①`registry_text` 删 `or ALL_TOOLS` 静默回退、health 始终附加、空域 warning；②hk_quote/hk_search domain→`hk_stock`、hk_search category 对齐 technical；③BY_NAME 重复 operationId：cross 优先→先注册者为规范条目（保住 F10 钉死的 quote/search→a_share 与 snapshot→cross），占位条目显式进 `SHARED_BY_NAME`；T34 的 `assert A or B` 恒真改 `and`；7 条新用例，三处改动逐项反向验证必红 |
| **T24b** `.gitignore` 临时目录写错 | ✅ 完成（S5） | `6c045cd` | `.temp` → `.tmp/` + 补行尾换行；删除 .tmp/ 里 4 个回退备份（3 个 .py.bak + tool_registry.t24.py，源码已确认干净全量绿）；`git check-ignore` 已生效、`git status` 不再出现 `.tmp/` |
| **T21 sqlite 跨线程** | ✅ 完成并**验收通过**（S5） | `e026f0b` | 连接改 `threading.local()` 每线程一条 + 连接登记表（`close()` 覆盖所有线程）+ 代数计数（close 后旧线程句柄按代数重建）；WAL/autocommit 工厂与落库 warning 日志未动（T3/规则 4）；复现脚本实测 ProgrammingError → 修后 4 线程并发落库全成功；新增 `test_memory_thread_safety.py` 4 条 + `test_persistence` 落库失败 WARNING 防回归 1 条；反向验证（退回共享单连接 / warning 降 debug）**3+1 条必红**（3 条我复跑确认） |
| **T26 SSE 错误分支** | ✅ 完成并验收（S5） | `7f635ff` | ①ask_stream 在返回 StreamingResponse 前 eager `_get_orchestrator()`+`_ensure_graph()` → 图构建失败 HTTP 层 500（死分支 except ValueError 删除；_parse_ask_payload 留 try 外以免 400 被改写 500）；②finally 在 cancel 后 `await gather(task, return_exceptions=True)` 收尸；运行期错误走 200+SSE 事件的契约保留；新增 2 条用例旧实现下均必红（**教训**：预算超时路径的 task.cancel() 会清掉 Future 未检索标记、掩盖 warning——测试必须用客户端断开 aclose 场景才暴露） |
| **T25 前端健壮化** | ✅ 完成并验收（S5） | `fe837b1` | ①计时器/AbortController 按请求持有（局部变量），finally 只清自己的；②ask() 防重入：追问 → superseded+abort 静默接管旧请求，不并发两条，接管期不解锁按钮；③addAILoader 返回行、removeLoader(row) 只删自己的；④severity 白名单渲染（low/medium/high/critical，未知不显示徽章），异常卡逐条 try/catch 降级；补 .critical 样式；新建 tests/test_web_frontend_contract.py 6 条文本契约，退回旧 index.html 全红；node --check 语法校验过 |
| **T12b** requirements 同步 | ✅ 完成（S7） | `55b9973` | `requirements.txt:8` `mcp>=1.12` → `mcp>=2,<3`；脚本抽取两份清单逐条 diff **完全一致**（其余 9 行本就无漂移）；纯清单同步无必补测试（按 §3 规格） |
| **T37** 打包/CI flat-layout | ⬜ 未做（S8） | — | 已被验收者直接复现：flat-layout 发现 `app` + `memory` 两个顶层包 |
| **T36 文档同步** | ⬜ 未做 | — | §5，S8 |
| **T28 finding 结构用例** | ✅ 完成（S6） | `efef499` | 旧 fake 是非 awaitable 的 lambda → 走异常降级分支 failed=True，isinstance 断言照过（探针证实）；改 MethodType 绑定 async fake，断言 `failed is False` / `tools_used == []` / `errors == []`，去 skip；反向验证（fake 改回 lambda）必红 |
| **T29 reasoning 副本** | ✅ 完成（S6） | `5495702` | 删 `_ensure_lists` 副本（把非法 confidence 置 medium、与生产置 low 相反），复用 `_stub_reasoning_engine` 真调 `reason()`，6 条用例全部走生产后处理；`defaults_to_low` 断言 low；反向验证（生产兜底改回 medium）必红 |
| **T30 budget 守卫 + D4** | ✅ 完成（S6） | `3a890a1` | ①显式 budget 优先（None 才回退 len），budget=0 = 不执行；②`max_tool_calls` 与 budget 取 min 成为真实上限（此前只有 /health 消费点）；③**语义裁决：budget 计真实执行次数**——`budget -= 1` 移到 execute 之前，跳过的调用（unknown key/缺 symbol/T27 重复）不消耗预算（探针实测旧实现 budget=n 可能只执行 1 次）；删恒真 co_varnames 断言，新增 6 条走真实 `_execute_tools` 的用例（实例级 RecordingRuntime），旧实现下 4 条必红 |
| **T31 e2e 解封** | ✅ 完成（S6） | `1840f6b` | `test_full_flow_produces_report` 去掉过期 skip（"P0 图为 supervisor→kernel"），真跑且绿（mock LLM + mock tools，断言 report 非空 + 三 analyst finding）；反向验证：builder 退回不注册 analyst 的旧拓扑 → 用例报错。全量从此 **0 skipped** |
| **T32 唯一约束假阳性** | ✅ 完成（S7） | `327ba41` | 探针证实旧用例从不触发约束（删掉 UNIQUE 它照样绿）；旧用例改名 `test_turn_indices_increment_per_turn` + 新增 2 条（直接 INSERT 重复行必抛 IntegrityError；datetime 门闩强制两条连接 TOCTOU 交错——赢家拿索引、输家大声抛、无重复行、撞车后正常续号）；反向验证：删 UNIQUE → 恰好 2 条新用例红 |
| **T33 MarketDomain no-op 循环** | ✅ 完成（S7） | `6c723f9` | 探针：`_: MarketDomain = val` 塞 bogus/123/None 全通过；改为真构造 ResearchIntent（合法值来源 `get_args`，`len>=7` 守卫防类型退化成非 Literal 时空转通过）+ 七域集合断言 + bogus 必抛 ValidationError；反向验证两方向：放宽成 str → 1 红，Literal 删 macro → 3 红 |
| **T34 剩余假阳性（三条）** | ✅ 完成（S7） | `06be493` | ①graph_nodes 空转 fixture（缺 self 静默错位 + "一触即炸"探针坐实两用例都不触类补丁）：补 self/补属性集/补 truncate，skips 用例改真实消费 fixture 记录；②routing 恒抛 mock（探针：跳过 planner 旧用例照样绿）：raise_on_prompt + last_prompt 断言 planner 真被调用 + 非空对照；③news_search 真网络+恒真断言（status 三结局恒存在）：密封 monkeypatch DDGS + **空 query 分支首覆盖**（分支自 99aad46 存在、从未有测试）。注册表条目**已并入 S4 的 T24（`3bef4f4`）**，本提交不含 |
| **T35 机制性防线** | ✅ 完成（S7） | `46d8a1b` | conftest 落 T35 约定 + S1–S6 四条假阳性教训 + 兜底降级统一标记清单；新建 `test_conventions.py` 两条机械检查（模块 docstring 强制声明 mock 范围——4 个缺声明文件补齐；整体替换 `__call__` 须 `# T35-OK:` 豁免注释——AST 扫描，10 处既有合法点已加注）；e2e 两条全链路用例补 `_fallback_leaks` 兜底降级泄漏断言（流式按节点检查）。⚠️ 过程修掉防线自身缺陷：行级扫描被 ruff format 折行绕过，改 AST 后闭合；反向验证：删 docstring/删豁免注释/GateNode 注入炸点 → 三处各自必红 |

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

**S2 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- **通过**：逐条读了 5 个提交的 diff；测试数增量对得上（423 → 438 收集 = +15：T18c 3 + T19 5 + T27 3 + T23 4）；
  历史里没有混进 `.tmp/`、`*.db`、`.bak`、`.env`（`git log --name-only` 已核）；两个文档提交是独立的 `chore:`/`docs:`。
- **全量测试**：完全访问会话下 `pytest -q` → **`436 passed, 2 skipped`**，`mcp` 与 `http` 两种模式
  各跑一次都是这个数字，与报告**逐字相符**；`ruff check` + `ruff format --check` 全绿。
- **反向验证 T18c（我自己做的，最高风险项）**：用**外科式**回退——只把会话状态从模块级 `ContextVar`
  换回实例属性（保留 T23 的 `deadline` 参数，避免因签名不匹配而"红得不是地方"）→
  `test_gateway_reuse.py` **2 failed**：`test_concurrent_requests_get_isolated_sessions`（`assert 1 == 2` 实例数）
  与 `test_session_close_does_not_kill_concurrent_inflight`（日志正是
  `Analyst technical failed: gateway closed while quote_tencent_quote_get in flight`）；
  串行会话那条如期保持绿；还原后 `git diff` 为空、8 passed。
  → **执行方关于"并发 stub 必须有真实挂起点"的发现成立且必要**：`_SlowGateway.call` 里的 `await asyncio.sleep(0)`
  是让两个任务真正交错的关键，没有它旧实现可能整段跑完、测试就不会红。
- 代码复核要点：`gateway_session` 的 `finally` 顺序正确（先取 session → `reset(token)` → 再关闭）；
  `_ensure_gateway` 失败复位并抛出不吞异常；`_do_execute` 优先读 `_session.get()`；
  T27 的签名在真实成功后登记、重复调用返回 `partial` + note（不静默成功，符合规则 6）；
  T23 的 `normalize_tool_result(..., error=...)` 关键字参数确实存在。

- **⚠️ 未完全采信的两点（已立任务）**：
  1. **`test_expired_entries_purged_on_set` 是假阳性（T19 的 5 条用例之一）**。
     我实测：把 `set()` 里的 `self._purge_expired()` 换成 `pass`（停用该行为）→ `tests/test_cache_capacity.py`
     **仍然 5 passed**。原因是该用例在断言前先调了一次 `c.get("old")`，读路径本来就会删掉过期键，
     所以"set 时顺带清理"这个行为**没有任何测试覆盖**。正确写法（不先 get）在停用 purge 时会红：
     `old in c._store` → `True`（应断言 `False`）。→ **T19b**（必须在下一段最前面修掉，2026-10-05 追加）。
  2. **T23 的 deadline 只按"节点内开始时刻"计算（T23b）**。`base._execute_tools` 用 `run_started = time.monotonic()`
     在本节点开始时起算、并把 `research_budget_seconds` 当成本节点的全额预算 → `research_more` 回环的第二轮
     会重新获得一整份预算，而 `main.py` 的 `wait_for(research_budget_seconds)` 才是整次调查的硬上限。
     单轮内的降级目标已达成，但"第二轮烧完再 504"这条路径仍然存在。→ **T23b**（下一段）。

**S3 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- **通过**：逐条读了 7 个提交的 diff；测试数增量对得上（438 → 456 收集 = **+18**，逐文件核对：
  T19b +1、T23b +4（新文件 `test_budget_deadline.py`）、T22 +7（`test_anomaly_detector.py` 重写后 38 条）、
  T20 +4、T15 +2；`test_evidence_gate.py` 现 9 条）；历史干净（无 `.tmp/`/`*.db`/`.bak`/`.env`）；两个文档提交独立。
- **全量测试**：`pytest -q` → **`454 passed, 2 skipped`**（`mcp` 与 `http` 双模式），与报告逐字相符；
  `ruff check` + `ruff format --check` 全绿。
- **反向验证（我自己重跑的 3 条）**：
  1. **T19b**：把 `set()` 的 `_purge_expired()` 换回 `pass` → `test_expired_entries_purged_on_set`
     **变红**（`assert 'old' not in {...}`），其余 5 条（含新的读路径用例）仍绿 → 假阳性已真正修好。
  2. **T15**：把 `if gate_has_evidence is False:` 改成 `if False and ...`（停用降级）→
     `test_evidence_gate.py` **2 failed**（`assert 'high' == 'low'`），`has_evidence=True` 的对照用例保持绿。
  3. **T23b / T20**：读用例 + 复核断言判据（T23b 用 `issued < now + research_budget_seconds/2` 区分
     "调查级"与"节点级"；T20 的 4 条含 `anomalies` 标量不再炸 schema 的变体）。
- 代码复核要点：`budget_deadline` 是标量字段（无 reducer → 回环覆盖，正是要的语义），
  orchestrator 与 SSE 两条路径都写入（`main.py:263` 的 `budget` 就是 `research_budget_seconds`）；
  `_execute_tools` 缺失该字段时退回旧行为并 **warning**（没把"没有预算信息"静默当成"预算无限"）；
  T20 的 `market_state`/`what_happened` 双保险（模型层默认值 + reasoning 归一化写 `data_caveats`）；
  `build_response_from_state` 用 `detect_anomalies` 覆盖模型条目、失败时写 `errors`（规则 6）；
  `anomalies` 归一化成 `list[dict]` 确实多挡了一种崩溃；T15 的降级不短路、文档三处同步。

- **⚠️ 未采信：T22 有两个独立缺陷（已实测复现，均另立任务）**

  1. **精确别名匹配把"嵌套载荷产生的点分 metric"全部漏掉 → 异常检测在生产里等于关闭（T22b）**。
     normalizer 对嵌套载荷产出的 metric 是**路径**：我实测
     ```python
     normalize_tool_result("derivatives_history_market_derivatives_history_post", {}, {"data": {"openInterest": 3.2e9}})
     # -> metric = 'data.openInterest'（数组载荷则是 'data[0].openInterest' / 'result.list[0].openInterest'）
     ```
     而新 `_matches_metric` 是**精确**匹配：`_matches_metric(rule, "data.openInterest")` → **False**
     （旧子串实现是 True）。端到端后果：
     ```
     嵌套载荷 OI 1e9 → 1.2e9（+20%，应报 critical/high）→ detect_anomalies 返回 []
     扁平载荷同样 +20%                                       → 返回 ['oi_spike'] ✓
     ```
     A 股同理：`涨停家数` → True，`data.涨停家数` → **False**（`limitUpCount` / `data.limitUpCount` 同）。
     **这正是本项目反复出现的假阳性模式**：新用例用 `NormalizedDatum(metric="openInterest")` 手工构造，
     没走 normalizer，所以看不到真实 metric 名。→ **T22b**，并且新测试必须**经过 `normalize_tool_result`**。
  2. **前值只按 metric 名存放 → 不同 symbol 互相污染，报出错误异常（T22c）**。
     `_prev_key()` 只用 metric，`result.arguments` 里的 `symbol` 完全没用上。实测三次独立调查：
     ```
     BTCUSDT OI=1,000,000,000  -> []                                        （无前值，正确）
     ETHUSDT OI=3,000,000,000  -> [('oi_spike','critical','OI 剧烈增长 200.0%')]   ← 拿 BTC 当前值
     SOLUSDT OI=3,300,000,000  -> [('oi_spike','high','OI 短时间内快速增加 10.0%')] ← 拿 ETH 当前值
     ```
     后两条是**用户可见的错误结论**（D3 已把 anomalies 写进报告与 `daily_states`）。→ **T22c**。

**S4 执行记录（S4 会话自记，2026-10-05；已由主 Agent 验收 —— 见下方的"S4 验收复核记录"）**

- **T22b（`e7c2272`）**：`_metric_basename` 去数组下标、取末段基名，`_matches_metric` 用基名比对别名表；
  只改"匹配哪条规则"，`value_semantics` 比对值不动。12 条新用例全部经过 `normalize_tool_result`
  （`{"openInterest":...}` / `{"data":{"openInterest":...}}` / `{"data":[{"openInterest":...}]}` 三种形状
  → 都报 `oi_spike`；A 股 `data.涨停家数` → 报 `limit_up_surge`；`data.noise` / `data.openInterestRate`
  反向对照 → 不触发）。反向验证：`_metric_basename` 换恒等函数 → **7 failed**（嵌套/数组/A 股形状全红，扁平对照仍绿）。
- **T22c（`08c3656`）**：前值 key 从 metric 改为 `(metric_basename, symbol)`；`_prev_scope` 取
  `arguments.symbol` → domain → `"unknown"`；`prev_in_call` 同步按 (基名, symbol) 隔离（同一次调用
  混入多 symbol 也不互相当前值）。前值仍存 `market_cache`（取舍已写注释：TTL/LRU 淘汰与
  "前值过期不可比"语义一致，丢前值=少报不误报）+ 拿不到前值留 `logger.debug`。
  6 条新用例（三次独立调查 `[]/[]/[]`、同 symbol 1e9→1.2e9 仍报 critical、同调用多 symbol 隔离、
  缺 symbol 退回 domain 的三种变体）。反向验证：symbol 作用域停用 → **5 failed**。
  ⚠️ 过程发现：初版把"三次调查"写成 `@parametrize` 三条独立用例——autouse fixture 每条清缓存，
  污染根本不会发生，**反向验证时那 3 条没红**，据此改成了单用例内顺序执行后才红。
  又是一次"假阳性测试"教训，已写进用例 docstring。
- **T24（`3bef4f4`）**：①`registry_text` 按"始终附加 unknown/health"实现，删 `or ALL_TOOLS`
  （us_stock 空域旧实现返回全量 40 个工具），空域 `logger.warning`；②`hk_quote`/`hk_search`
  domain→`hk_stock`，`hk_search` category→`technical`（与 A 股 search 对齐）；
  ③`BY_NAME` 重复 operationId 不再"后注册者静默胜出"：显式规则 = cross 条目优先
  （klines/snapshot），否则先注册者为规范条目（quote/search）——保住了
  `test_normalizer_f10.py` 钉死的 `quote/search → a_share` 与既有 `snapshot → cross` 语义，
  占位条目进新暴露的 `SHARED_BY_NAME`（规范 35 + 共享 5 = 40，不丢不重）。
  T34 里注册表那条恒真 `assert A or B` 改 `and`，并按其要求补了
  `registry_text(domains=["us_stock"])` 用例。7 条新用例；反向验证：三处改动逐项改坏 → **5 failed**。
- **三道门禁（每个提交前均独立跑过）**：`ruff check` + `ruff format --check` 全绿；
  `pytest -q` → `466`（T22b 后）→ `472`（T22c 后）→ **`479 passed + 2 skipped`**（T24 后），
  `mcp` 与 `http` 双模式各跑一次全量，数字一致。
- 测试数增量：456 → 481 收集 = +25（T22b +12、T22c +6、T24 +7）；`test_anomaly_detector.py` 现 56 条、
  `test_tool_registry_multi_domain.py` 现 27 条。
- 遗留：`.tmp/` 下的两个**旧**复现脚本（`verify_t22_metric_names.py` / `verify_t22_prev_scope.py`）未提交。
  ⚠️ **更正（验收者核实）**：这两个脚本是**手工往 `market_cache` 塞前值**、键格式还是 T22c 之前的
  `__anomaly_prev__:<metric>`；T22c 改键后它们连扁平载荷都输出 `[]`，**不能作为"已符合预期"的证据**。
  结论以验收者的端到端探针 `.tmp/verify_s4_anomaly.py` 为准（连续调用 `detect_anomalies`，
  不手工写内部状态）：嵌套载荷 +20% 报 `oi_spike`、三次不同 symbol 全 `[]`、扁平载荷回归仍报 —— 都对。

**S4 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- **通过**：逐条读了 3 个提交 + 2 个 chore/docs 的 diff；测试数增量对得上（456 → 481 收集 = **+25**：
  T22b +12、T22c +6、T24 +7）；历史干净。
- **全量 + 门禁**：`pytest -q` → **`479 passed, 2 skipped`**，与报告逐字相符；`ruff check` + `format --check` 全绿。
- **反向验证（4 项，我自己重跑）**：
  1. **T22b**：把 `_metric_basename` 换成恒等 → `test_anomaly_detector.py` **7 failed**（`nested`/`list` 载荷形状 + A 股
     `data.涨停家数`），扁平载荷对照仍绿 → 与报告一致。
  2. **T22c**：**忠实**回退（把 `_prev_key` 改回 `metric`-only，而不是让 `_prev_scope` 返回常量）→ **5 failed**
     （`test_three_investigations_different_symbols_no_pollution`、`test_multiple_symbols_in_one_call_are_isolated`、
     `test_missing_symbol_falls_back_to_domain[0/1/2]`）→ 与报告一致。
     *方法学提醒*：我第一次的弱回退只红了 2 条 —— **反向验证必须忠实复现旧行为**，只是"把新代码弄坏"会得出错误结论。
  3. **T24①**：恢复 `or ALL_TOOLS` → **3 failed**（`us_stock` 排除他域+含 health、`unknown` 域只回 health+warning、
     `hk_stock` 含 HK 工具+health）。
  4. **T24③**：恢复"后注册者静默胜出" → **6 failed**，其中 4 条在 `test_normalizer_f10.py`
     （`quote/search → a_share` 的域推断被打破）—— 证明这条钉死语义真的受保护。
- **我自己写的端到端探针（`.tmp/verify_s4_anomaly.py`）**：嵌套载荷同 symbol 1e9 → 1.2e9 → **报 `oi_spike` critical
  「OI 剧烈增长 20.0%」**（修复前是 `[]`）；三次不同 symbol → **全 `[]`**（修复前 ETH 被报 +200%）；
  扁平载荷 +20% 仍报（回归对照）。T22b/T22c 都真的生效了。
- **注册表独立探针（`.tmp/verify_s4_registry.py`）**：`ALL_TOOLS=40 / BY_NAME=35 / SHARED=5`（不丢不重）、`BY_KEY=40`；
  **4 组复用同一 operationId 的条目，其 `http_method`/`http_path` 完全一致** → 规范条目的选择**不改变真实调用**（这是我最担心的一点，已排除）；
  `registry_text` 行数：`us_stock` 2（health）、`hk_stock` 6（含 HK 工具）、`a_share` 20（不再混入 hk 工具）、
  `bogus`/`unknown` 2（health + warning）、`None` 40。
- **⚠️ 需要更正执行方 §0.4 里的一句（我已就地改）**：报告写"S4 修完后两个遗留脚本输出已符合预期
  （嵌套载荷 `['oi_spike']`）"——**不成立**。那两个脚本是**手工往 `market_cache` 里塞前值**、键格式是 T22c 之前的
  `__anomaly_prev__:<metric>`；T22c 把键改成 `<basename>|<symbol>` 后，手工塞的键再也读不到，
  所以它们现在**连扁平载荷都输出 `[]`**（我实测）。这不是产品问题，而是**探针过期**：
  教训与 T22c 那条同源 —— **探针要通过公开路径驱动行为（连续调用 `detect_anomalies`），不要手工写内部状态**。
  已用新探针 `.tmp/verify_s4_anomaly.py` 取代它们。

**⚠️ 轻微遗留（不影响 S4 任务本身，已立 T24b）**：`ceea711` 给 `.gitignore` 加的是 **`.temp`**（且丢了行尾换行），
不是本项目实际用的 **`.tmp/`** —— `git check-ignore -v .tmp/...` 返回"未忽略"，`git status` 仍显示 `?? .tmp/`。
`.tmp/` 里现在有 8 个文件（含 3 个 `.py.bak` 生产源码备份），正好是规则 3"不要 `git add -A`"要防的东西。→ **T24b**。

**S5 执行记录（S5 会话自记，2026-10-05；已由主 Agent 验收 —— 见下方的"S5 验收复核记录"）**

- **T24b（`6c045cd`）**：`.gitignore` 的 `.temp` → `.tmp/` 并补行尾换行；删除 `.tmp/` 里 4 个回退备份
  （`base_t23b.py.bak` / `cache_t19.py.bak` / `reasoning_node_t15.py.bak` / `tool_registry.t24.py`，
  源码已确认干净：`git diff -- app/ tests/` 为空、全量绿）。验收：`git check-ignore -v` 命中、
  `git status --porcelain` 不再出现 `.tmp/`。纯仓库卫生，无测试。
- **T21（`e026f0b`）**：复现脚本实测跨线程 `save_turn` 抛 `ProgrammingError`（计划里的复现块）。
  修法取方案 1：`threading.local()` 每线程一条连接 + **连接登记表**（`close()` 借此关闭所有线程的连接）
  + 代数计数（close 后其它线程手里缓存的旧句柄在下一次 `conn` 访问时按代数检测重建，不悬空）。
  WAL/autocommit 工厂、落库失败 warning 日志都未动（T3 / 规则 4）。
  新增 `tests/test_memory_thread_safety.py` 4 条（双线程落库、同线程连接复用不回退、
  close 覆盖其它线程的连接、close 后重建）+ `test_persistence.py` 落库失败 WARNING 防回归 1 条。
  反向验证：①`conn` 退回共享单连接 → 3 条必红；②persistence 的 warning 降 debug → 1 条必红。
  `test_persistence` / `test_data_integrity` / `test_market_memory` / `test_conversation` 全部保持绿。
- **T26（`7f635ff`）**：①`ask_stream` 在返回 `StreamingResponse` **之前** eager 调
  `_get_orchestrator()` + `_ensure_graph()`——图构建失败现在 HTTP 层就是 500；
  死分支 `except ValueError` 删除；`_parse_ask_payload` 留在 try 外（它抛 HTTPException(400)，
  放进 try 会被 except Exception 改写成 500）。②`finally` 在 cancel 后补
  `await asyncio.gather(task, return_exceptions=True)` 收尸。
  新增 2 条用例：`_ensure_graph` 抛异常 → 500（旧实现 200）；pump 带异常结束 → 不触发
  "Task exception was never retrieved"。旧实现下两条均红。
  **教训（写用例时实测两次）**：a) TestClient 的 portal 循环常开，task 何时被 GC 不可控——
  warning 断言必须自己在 `asyncio.run` 里把 generator 消费到结束、**关闭循环**后再 gc；
  b) 预算超时全量消费路径**测不出**这个缺陷：deadline 分支的 `task.cancel()` 恰好清掉 Future
  的未检索标记，把问题掩盖掉——必须用"客户端断开（`aclose()`）且 pump 已带异常结束"的场景。
- **T25（`fe837b1`）**：①兜底计时器/AbortController 改为 ask() 局部变量，finally 只 clearTimeout 自己的；
  ②`ask()` 防重入：`activeRequest` 单飞——追问时 superseded+abort 静默接管旧请求
  （旧请求收场不弹错误气泡、不留 loader），接管期间不解锁按钮；
  ③`addAILoader` 返回行元素、`removeLoader(row)` 只删自己的行；④`severityBadge` 白名单渲染
  （low/medium/high/critical，未知值不显示徽章），异常卡**逐条 try/catch** 降级，
  补了缺失的 `.anomaly-severity.critical` 样式。
  新建 `tests/test_web_frontend_contract.py` 6 条文本契约（计划要求的三条全覆盖 + loader 行作用域 +
  防重入 + 逐条降级）；反向验证：`git checkout HEAD -- app/web/index.html` 退回旧实现 → **6 条全红**。
  另用本机 node v24 `--check` 对内联 JS 做了语法校验（非提交内容，纯自检）。
- **三道门禁（每个提交前均独立跑过）**：`ruff check` + `ruff format --check` 全绿；
  `pytest -q` → 486（T21 后）→ **492 passed + 2 skipped**（T26 后 +2、T25 后 +6），
  `mcp` 与 `http` 双模式各跑一次全量，数字一致。T24b 为 .gitignore 一行，同样跑了全量才提交。
- 测试数增量：481 → 494 收集 = **+13**（T21 +5：`test_memory_thread_safety.py` 新建 4 条 +
  `test_persistence.py` 落库失败 WARNING 1 条；T26 +2；T25 +6：`test_web_frontend_contract.py` 新建）；
  passed 479 → 492 = +13，对得上。
- **T25 手工验证步骤**（前端无自动化环境，交验收者执行）：
  1. `uvicorn app.main:app`（或 `python -m app.main`）起服务，浏览器开 `http://127.0.0.1:8000`；
  2. **并发互踩**：发一个问题，趁 loader 还在时再回车追问一次 → 应看到旧请求静默消失
     （无"调查失败"气泡、无残留 loader 行）、新请求正常出报告；连发三次追问不再出现
     "先结束的请求让后到的失去中止手段"现象（旧实现：后到请求必须干等服务端超时）；
  3. **severity 健壮化**：临时把后端 `build_response_from_state` 产出的 anomalies 里塞一条
     `{"severity": 1}`（或在浏览器 console 里拦截 renderResult 改数据）→ 报告仍完整渲染，
     该条只是不显示徽章，页面不再整体变成"调查失败"；
  4. **正常路径回归**：正常提问/追问/重试按钮、loading 行的逐节点进度与消失时机与改前一致。

  **结果（2026-10-05，仓库主人实测）**：通过。S5 至此无任何遗留。
- 遗留：无新立任务。`.tmp/` 里的 `verify_*` 复现脚本未提交（已被 `.tmp/` ignore 覆盖）。

**S5 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- **通过**：逐条读了 4 个提交 + 2 个 chore/docs 的 diff；测试数增量对得上（481 → 494 收集 = **+13**）；
  `pytest -q` → **`492 passed, 2 skipped`**，与报告逐字相符；`ruff check` + `format --check` 全绿；
  **本次确认工作区首次真正干净**（`.gitignore:43:.tmp/` 生效，`git status --porcelain` 无输出）。
- **T24b ✓**：`git check-ignore -v .tmp/verify_s4_anomaly.py` → `.gitignore:43:.tmp/`；`.tmp/` 里 4 个
  `.py.bak`/备份已删（剩下的都是当前在用的探针与附录 A 的 runner）。
- **T21 ✓**：反向验证（把 `conn` 退回"整实例共享一条连接"= 旧行为）→ `test_memory_thread_safety.py`
  **3 failed**（多线程落库、close 覆盖他线程连接、close 后重建），与报告一致；
  测试本身是行为断言（含"同线程连接仍复用"这条**防过度修正**的对照）而不是自省断言。
- **T26 ✓**：两条反向验证我都复跑 —— 去掉建图预检 → `test_stream_graph_build_failure_returns_500` 红；
  去掉 `await gather` → `test_stream_pump_crash_does_not_leak_unretrieved_task_warning` 红。
  另加**行为探针**（`.tmp/verify_s5_http.py`，TestClient）：空 question → 400、非字符串 → 400、
  非法 domain → 400、`_ensure_graph()` 抛错 → **500** —— 确认删掉 `except ValueError` **没有**引入
  400→500 回归（`_parse_ask_payload` 抛的是 `HTTPException`，放进 try 才会被 `except Exception` 吞成 500）。
- **T25 ✓（逻辑层，DOM 接线未覆盖）**：
  1. 退回 T25 之前的 `index.html`（`git checkout fe837b1^ -- app/web/index.html`）→ 6 条契约测试
     **全红**；恢复后文件哈希与原文件**逐字节一致**（`FB532DDA…`）。
  2. `node --check` 校验**正确抽取**的内联 JS（用 Python 读 UTF-8 写出，避免 PowerShell 重编码把中文/emoji
     弄坏导致假报错）→ **exit 0**，确认执行方"语法校验通过"的说法成立。
  3. **从 index.html 原文抽取** `SEVERITY_STYLES`/`severityBadge` 与 anomalies 的 map 体，在 Node 里真跑
     （`.tmp/verify_t25_logic.js`）：13 组 severity 输入（含 `1`/`0`/`true`/`false`/`null`/`undefined`/`{}`/`[]`）
     **全部不抛错**（旧实现 `a.severity.toUpperCase()` 对数字必抛）；喂一条 `get description()` 抛错的坏卡片
     → 三张卡照常渲染、坏的那张降级为占位、数字 severity 不产生徽章、critical 徽章正常 ✓。
- **未由我完成的部分（如实报告）**：**T25 的浏览器级手工验证没做** —— 本会话没有浏览器自动化手段。
  §1.3 的四个手工步骤（并发追问静默接管、`severity: 1` 仍完整渲染、正常路径回归）仍请仓库主人在浏览器里
  走一遍；我覆盖的是其中的**纯逻辑**与**文本接线**：DOM 事件/SSE 流的实际联调属于这四步的范畴。
- **两条可选改进（不立任务，记录备查）**：
  1. `MarketMemory.close()` 目前**没有任何生产调用点**（只有测试用）。若在 FastAPI shutdown 里挂一个
     `get_memory().close()`，可在退出时确定性关闭连接/收尾 WAL（现在靠进程退出兜底，功能上无碍）。
  2. 既有测试仍用 `mem.conn.close()` 直连句柄关闭；新引入的 `close()` 才会按代数重建。
     将来若有测试"关掉 `mem.conn` 后继续用同一个 `mem`"，会拿到已关闭的句柄 —— 记得用 `memory.close()`。

**S6 执行记录（S6 会话自记，2026-10-05；已由主 Agent 验收 —— 见下方的"S6 验收复核记录"）**

- 每条都先按要求证明"现有测试在（被改坏的）旧行为下不会红"，再改，再反向验证。
- **T28（`efef499`）**：探针实测——非 awaitable 的 lambda fake 使 `await` 抛 TypeError、
  被 base.py 兜底 except 吞掉，finding 落在**异常降级分支**（failed=True、
  digest="分析失败: …"），而 4 条 isinstance 断言全部通过 → 假阳性坐实。
  改法：`types.MethodType` 绑定的 async fake（返回 []），断言 `failed is False`、
  `tools_used == []`、`errors == []`（防再次静默滑进降级分支），去 skip。
  反向验证：fake 改回 lambda → 必红。
- **T29（`5495702`）**：探针实测——真 `reason()` 对非法 confidence 返回 **low**，
  而副本断言 medium（与生产相反）且从不调 reason()。删副本，复用本文件
  `_stub_reasoning_engine`（照 test_data_integrity._stub_reasoning 模式），
  6 条用例全部真调 `reason()`；`test_validate_invalid_confidence_defaults_to_medium`
  改名 `…_defaults_to_low` 并断言 low。反向验证：生产兜底改回 medium → 必红。
  顺带清了 ruff F401/F811（顶层 MarketIntelligence 导入已无消费点）。
- **T30 + D4（`3a890a1`）**：探针实测三个缺陷——①显式 `budget=0` 被 `or` 吞成"全部执行"
  （2 个调用执行了 2 个）；②`max_tool_calls=2`、3 个不同工具执行了 3 个（无消费点）；
  ③budget=3、[未知,未知,有效,有效] 只执行 1 个（顶部扣减，被跳过的调用照样扣）。
  修复：`raw_budget is None` 才回退 len(tool_calls)；`max_tool_calls` 与 budget 取 min
  （生效时打 info 日志）；`budget -= 1` 移到**真正 execute 之前**——语义裁决为
  **"budget 计真实执行次数"**（方案①），已写进 base.py 注释与测试 docstring。
  删掉恒真 `co_varnames` 断言，新增 6 条用例（`_make_budget_node` 用实例级
  `_RecordingRuntime` 记录真实执行——`mock_runtime` 打的类补丁在实例 runtime 被替换后
  根本不会触到，这也是旧用例恒真的原因之一）。反向验证：base.py 整体退回旧实现
  → 4 条必红（budget_limits / defaults_to_all 两条两种实现都过，属预期）。
  连带确认：supervisor 的 route 一直带 `budget=len(steps)`，D4 的 None 分支与
  max_tool_calls 上限不影响现有 planner 流程。
- **T31（`1840f6b`）**：`test_full_flow_produces_report` 去掉过期 skip，真跑且绿
  （该文件 mock LLM + mock tools，断言 report 非空 + technical/fundamental/moneyflow 三个 finding）。
  反向验证：builder 临时退回"不注册 analyst"→ 用例**报错**（对图拓扑不再失明）；还原后 3 passed。
  全量测试从此 **0 skipped**（T28/T31 解封了仅剩的两个 skip）。
- **三道门禁（每个提交前均独立跑过）**：`ruff check` + `ruff format --check` 全绿；
  `pytest -q` → 493+1skip（T28 后）→ 493+1skip（T29 后）→ 498+1skip（T30 后）→
  **499 passed + 0 skipped**（T31 后），`mcp` 与 `http` 双模式各跑一次全量，数字一致。
- 测试数增量：494 → 499 收集（净 +5：T30 净 +5——删 1 恒真、加 6 条；T28/T31 是 skip→run，
  不改收集数）；passed 492 → 499，skipped 2 → 0。
- 遗留：无新立任务。探针脚本在 `.tmp/`（`probe_t28.py` / `probe_t30.py`），未提交。

**S6 验收复核记录（主 Agent 独立重跑，2026-10-05，非采信执行方报告）**

- **通过**：逐条读了 4 个任务提交（`efef499` / `5495702` / `3a890a1` / `1840f6b`）+ 1 个 §0.4
  chore（`ed7e830`）的 diff；测试数增量对得上（494 → 499 收集 = 净 +5，全部来自 T30 的
  −1 恒真 +6 新用例；T28/T31 是 skip→run 不改收集数；492+2 → 499+0 逐项吻合：+5 新增 +2 解封）。
  双模式独立重跑三道门禁：`ruff check --no-cache` 全绿、`ruff format --check` 103 files、
  `pytest -q` → **499 passed + 0 skipped**，`market_gateway_mode=http` 下同样 **499 + 0**，
  与报告逐字相符；`git status --porcelain` 干净。
- **T30 ✓（S6 唯一动生产代码的提交，重点复核 + 独立反向验证）**：把 `base.py` 整体退回
  `3a890a1^` 的旧实现 → 6 条 budget 用例 **4 failed, 2 passed**，与执行方声明"4 条必红"精确一致
  （红：budget=0 / max_tool_calls 上限 / 跳过不耗预算 / 重复不耗预算；绿的两条
  budget_limits / defaults_to_all 在新旧语义下行为相同——执行记录已如实标注"属预期"，非隐瞒）。
  D4 三条语义在 diff 里逐条落实：`raw_budget is None` 才回退（budget=0 保留）、`max_tool_calls`
  取 min（带 `>0` 守卫，0/未配置 = 不限制）、`budget -= 1` 位于全部 continue 之后、execute 之前。
  grep 确认 `max_tool_calls` 此前确实只有 `main.py:109`（/health 展示）一个消费点；
  `supervisor.py:136` 确认 route 总带 `budget=len(steps)`——回退分支与上限不影响现有 planner
  流程，连带确认属实。
- **T29 ✓（方向性核实——防"测试改向错误的生产行为"）**：`reasoning.py:271-274` 确认生产把
  非法 confidence 置 **low**（`response.py:39` 的 schema 默认值同为 low），测试断言 low 是与
  生产对齐的正确方向。新用例真调 `reason()`（`_stub_reasoning_engine` 只 mock OpenAI 客户端，
  `_ensure_list`/`_parse_evidence`/confidence 兜底全走生产代码），跑绿即自证，无需改坏验证。
- **T28 ✓（逻辑复核，未改坏复跑）**：`lambda *a, **k: []` 语法上即非 awaitable，`await` 必抛
  TypeError 落进兜底 except；新断言 `failed is False` + `errors == []` 本身就是"走成功分支"的
  证明（异常降级分支必红），假阳性链条闭合。
- **T31 ✓（断言强度核实，未改坏复跑）**：断言 `finding_analysts` 必含 technical / fundamental /
  moneyflow 三者——builder 退回不注册 analyst 的拓扑则三条断言全红，对图拓扑不再失明；
  499 收集 + 0 skip 证明该用例真跑且绿。
- **S6 验收结论：无新立任务**（4 个任务全部通过）。D4 的"budget 计真实执行次数"裁决已写进
  `base.py` 注释与测试 docstring；S7 的 T35 若落地 conftest 约定，无需再动 budget 语义。

**S7 执行记录（S7 会话自记，2026-10-05；待主 Agent 验收）**

- 开工检查：`git status --porcelain` 为空；`git log --oneline -5` 与 §0.4 记录一致
  （2c80152 → 731b15d → ed7e830 → 1840f6b → 3a890a1）。本会话 `memory/` 可写（探针实测），
  直接用 `pytest -q`，未用附录 A runner。
- 每条都先按要求证明"现有测试在（被改坏的）旧行为下不会红"，再改，再做「改坏必红」反向验证。
  探针脚本在 `.tmp/`（`probe_t32.py` / `probe_t33.py` / `probe_t34_nodes.py` / `probe_t34_routing.py` /
  `probe_t34_news.py`），未提交。
- **T32（`327ba41`）**：探针实测——①把 `_INIT_SQL` 的 `UNIQUE(conversation_id, turn_index)` 删掉，
  旧用例体（顺序三条 `save_turn`、断言 `idx3 == 3`）**照样 PASS**，坐实它从不触发约束；
  ②真实 schema 直接 INSERT 重复行 → `sqlite3.IntegrityError`（约束真实存在、可触发）；
  ③两条连接并发 `save_turn`，用 `datetime.now` 门闩把两条执行**确定性**拦在
  SELECT MAX 之后、INSERT 之前（MAX+1 的 TOCTOU 窗口），5/5 轮撞车、约束兜底一方
  IntegrityError、无重复行。改法：旧用例改名 `test_turn_indices_increment_per_turn`
  （docstring 写明"本用例不触发约束"）；新增 `test_duplicate_turn_index_raises_integrity_error`
  （直接 INSERT 重复行必抛）；新增 `test_concurrent_save_turn_from_two_connections_no_silent_duplicate`
  （断言：赢家拿索引、输家**大声**抛 IntegrityError 不许静默成功、表里不落重复行、
  撞车后下一条正常续号 = 2；两条连接来自 T21 的 threading.local）。
  反向验证：storage.py 临时删掉 UNIQUE → **恰好 2 条新用例红**（DID NOT RAISE / 撞车未复现），
  旧用例与改名用例照旧绿——与假阳性声明精确一致；还原后 27 passed。
- **T33（`6c723f9`）**：探针实测——旧循环体 `_: MarketDomain = val` 塞 "bogus"/123/None 全通过
  （注解求值后即丢弃，运行时 no-op）；生产模型真拒 "bogus"（ValidationError: domain）。
  改法：`test_market_domain_type_accepted_values` 改为真构造 `ResearchIntent`，合法值来源换成
  `get_args(MarketDomain)`（类型本身，防手工清单漂移）并加 **`len >= 7` 守卫**
  （防"类型退化成非 Literal → get_args 为空 → 循环空转通过"的残余假阳性）；
  新增 `test_market_domain_type_expected_members`（七域集合断言：允许新增、不许悄悄删减）；
  新增 `test_market_domain_rejects_bogus_value`。反向验证两方向：A) `domain: MarketDomain`
  放宽成 `str` → 恰好 rejects_bogus 红（DID NOT RAISE），其余绿；B) Literal 删 "macro" →
  expected_members + accepted_values 的 >=7 守卫 + 既有 macro 用例共 **3 红**。
- **T34（`06be493`，三条独立成段；注册表条目已并入 S4 的 T24（`3bef4f4`），本提交不含）**：
  - ① `test_graph_nodes.py`：探针实测——mock_runtime 的 fake_execute **缺 self**：类补丁真触发一次，
    记录到的 tool 是 ToolRuntime 实例、"sentiment" 落到 arguments（静默错位成垃圾，不报错）；
    把类补丁换成"一触即炸"的假 execute、复刻两个消费用例的公开路径 → 全程不炸，
    **fixture 完全空转坐实**（一个用例按构造不执行工具、另一个用本地 FakeRuntime 整体替换 `_runtime`）。
    修复：fixture 补 self + 补齐生产消费的属性集（build_evidence/_make_digest/_cache_info）+ 补 truncate
    打桩；`test_execute_skips_non_whitelist_no_stock` 不再替换实例、断言直接消费 fixture 记录
    （白名单回归或打桩失联都会红）；`test_analyst_with_no_matching_tools_returns_empty` 移除空转参数。
    反向验证：a) sentiment 移出 WHITELIST_NO_SYMBOL → **7 failed**（含目标用例）；
    b) fake_execute 退回缺 self → 目标用例红（TypeError 被兜底 except 吞成 failed finding → KeyError 'results'）。
  - ② `test_graph_routing.py`：探针实测——`FakeOpenAI(raise_on=...)` 对任何输入都抛；
    把生产改成"空 question 跳过 planner 直接写 errors"（planner 调用次数 0），旧用例照样绿 →
    它对"planner 是否被调用"失明。修复：FakeOpenAI 增加 `raise_on_prompt`（仅当问题真进入 prompt
    才抛）与 `last_prompt` 记录；用例断言空 question 也真的流经 planner，并补**非空 question 对照**
    （同一个 fake 成功产出 intent、问题进入 prompt、无 errors）。反向验证：supervisor 对空问题短路
    → 恰好该用例红（last_prompt 断言），其余 7 条绿。
  - ③ `test_news_search.py`：探针实测——旧断言表达式
    （`if count==0: isinstance(meta.get("error",""),str) or meta.get("status")`）套在
    "成功有结果 / 成功零结果 / 抛异常"三种结局上**恒为 True**（status 键恒存在）；
    且旧用例不打桩、直接打真实 DDGS（非密封、慢、依赖网络）。修复：TestSyncSearch 全部
    monkeypatch **模块全局** DDGS（密封）——异常 → 断言 `meta.error == str(exc)`、`status == "搜索失败"`；
    零结果 → "成功获取 0 条" 且无 error 键；成功 → count 与 news 对齐；
    **新增空 query 分支首覆盖**（该分支自初始提交 `99aad46` 就存在于 news_search.py、
    从未有测试；打"一触即炸"的假 DDGS 证明短路）。反向验证：a) except 改 raise →
    error-path 用例红（RuntimeError 传播）；b) 空 query 分支禁用 → empty-query 用例红
    （booby-trap 异常被生产兜底 except 吞掉后 status 变"搜索失败"，仍被抓）。
- **T35（`46d8a1b`）**：
  - `tests/conftest.py`：T35 约定（禁整体替换被测节点 / 每文件 docstring 声明 mock 范围 /
    关键用例断言成功分支 / "改坏必红"自问）+ S1–S6 验收沉淀的**四条假阳性教训**（模式①②③ +
    反向验证姿势）+ 评审清单；节点兜底降级的统一标记清单落档
    （analyst/gate/critic/reasoning/supervisor → "… failed: …"；**"Evidence gate: …" 是 T15
    合法降级、不是失败标记**）。
  - 新建 `tests/test_conventions.py` 两条机械检查：a) 每个测试模块必须有 docstring——
    4 个缺声明的文件补齐（test_evidence / test_http_client / test_normalizer / test_tool_registry，
    均为不打桩的纯单元测试，docstring 如实声明"不 mock 任何东西"+ 未覆盖归属）；
    b) 整体替换节点 `__call__`（setattr / 字符串路径 / 属性赋值）必须带 `# T35-OK:` 行内豁免注释——
    10 处既有合法替换点（topology 9 + e2e 1）已全部加注理由。
    ⚠️ **过程发现并修掉防线自身缺陷**：b) 初版是行级扫描，反向验证第一次删注释**没红**——
    ruff format 把长 setattr 调用折成多行，`setattr(` 与 `"__call__"` 分家后扫不到；
    改为 AST 扫描（Call/Assign 节点 + 语句行范围内找豁免注释）后闭合。
  - 兜底降级泄漏断言推广：`test_full_flow_produces_report` 与 `test_stream_emits_node_events`
    （流式路径**按节点逐一**检查）新增 `_fallback_leaks` 断言——任何节点的 errors 出现兜底标记即红；
    topology 既有 3 处 `not errors` 断言保持。
  - 反向验证三项：删 docstring → 检查 a 红；删豁免注释 → 检查 b 红（AST 版；
    第一次没红即上述缺陷，修复后重做通过）；GateNode 注入 `RuntimeError("t35 drill")` →
    full flow 与 stream 两条用例**均红**（"Gate failed: t35 drill" 泄漏被抓，流式路径连节点名都指出）。
- **T12b（`55b9973`）**：`requirements.txt:8` `mcp>=1.12` → `mcp>=2,<3`（T12 收口 `0965da6` 漏改的镜像清单）。
  核对方式：脚本从两份文件各抽取依赖清单逐条 diff → **完全一致**（其余 9 行
  fastapi/uvicorn[standard]/openai/pydantic/pydantic-settings/python-dotenv/httpx/ddgs/langgraph
  本就无漂移）。纯清单同步，无必补测试（按 §3 规格）。
- **数字演进（全量 `pytest -q`，mcp 与 http 双模式每步各跑一次、数字一致）**：
  - S6 收官：499 passed + 0 skipped
  - T32 后：**501**（`test_market_memory.py` 25 → 27：旧用例改名净 0，+2 新用例）
  - T33 后：**503**（`test_models_multi_domain.py` 11 → 13：accepted_values 原名重写净 0，+2 新用例）
  - T34 后：**504**（`test_news_search.py` 9 → 10：TestSyncSearch 旧 3 条 → 新 4 条——
    `test_empty_result_structure` 改名重写为 `test_empty_results_structure`、
    `test_error_path_no_crash` 重写为 `test_ddgs_error_path_returns_structured_error`、
    `test_meta_has_required_fields` 原名重写、新增 `test_empty_query_skipped_without_touching_ddgs`；
    `test_graph_nodes.py` 21 → 21、`test_graph_routing.py` 8 → 8，只动签名/fixture/用例体，不改收集数）
  - T35 后：**506**（+`test_conventions.py` 2 条；其余文件只加断言/注释/docstring，不改收集数）
  - T12b 后：**506**（清单一行，0 变化）
  - 净 **+7** = 499 → 506，每步在提交前都独立跑过双模式全量。
- **三道门禁（每个提交前独立跑过）**：`ruff check --no-cache app/ tests/` +
  `ruff format --check --no-cache app/ tests/` 全绿（104 files）。
- **遗留：无新立任务。** 对 S8 的生效项：**新增/修改测试文件必须过 `tests/test_conventions.py`
  两条检查**（docstring 声明 mock 范围；整体替换 `__call__` 须 `# T35-OK:` 豁免注释），
  conftest.py 的四条教训即评审清单。`.tmp/` 探针未提交（已被 ignore 覆盖）。

---

### 0.5 执行分段与会话交接（**每个子 agent 只做一段**）

> **为什么分段**：§2 + §3 + §4 共 20 个任务，一个会话跑完必然上下文溢出。
> 每个任务都要"读代码 → 复现 → 改 → 写测试 → 跑三道门禁"，批次 4 还要逐个读测试文件。
> **分段机制**：每段是一个**全新子 agent**，本文档是唯一交接物，`§0.4` + `git log --oneline` 是状态载体。
> 因此——**每段结束必须更新 `§0.4` 并单独 `chore:` 提交**，否则下一段无法判断哪些已完成。

| 段 | 任务（按顺序） | 读本文档哪些部分 | 为什么这么切 | 结束门槛 |
|---|---|---|---|---|
| **S1 修红**（独占一段，阻塞一切） | T18b → T17T → T18T → T12 收尾 | §0 全部 + §1 全部 + §2 全部 + 附录 A | T18b（产品）与 T18T（测试）互相牵制，必须在同一会话里把基线跑绿；T12 只是一行 `pyproject`，顺手 | **`mcp` 与 `http` 双模式全绿**；4 个提交 |
| **S2 运行时 / 缓存 / 成本** | ✅ **已完成并验收**：T18c → T19 → T27 → T23 | — | 见 §0.4 "S2 验收复核记录" | `436 passed + 2 skipped`（双模式）；6 个提交；但 S3 需先补 **T19b / T23b** |
| **S3 检测与报告契约** | ✅ **已完成并验收**：T19b → T23b → T22 → T20 → T15 | — | 见 §0.4 "S3 验收复核记录" | `454 passed + 2 skipped`（双模式）；7 个提交；但 S4 需先补 **T22b / T22c** |
| **S4 注册表 + 异常检测尾巴** | ✅ **已完成并验收**：T22b → T22c → T24（T34 的注册表断言已并入） | — | 见 §0.4 "S4 验收复核记录" | `479 passed + 2 skipped`（双模式）；4 个提交；但 S5 需先补 **T24b** |
| **S5 请求生命周期** | ✅ **已完成并验收**：T24b（一行） → T21 → T26 → T25 | — | 见 §0.4 "S5 验收复核记录"（T25 的**浏览器手工验证已通过**，2026-10-05） | `492 passed + 2 skipped`（双模式）；5 个提交；**无新立任务** |
| **S6 测试有效性（上）** | ✅ **已完成并验收**：T28 → T29 → T30（含 D4 budget）→ T31 | — | 见 §0.4 "S6 验收复核记录" | `499 passed + 0 skipped`（双模式）；4 个提交；**无新立任务** |
| **S7 测试有效性（下）** | ✅ **已完成待验收**：T32 → T33 → T34（不含注册表条目，已并入 S4 的 T24）→ T35 → T12b | — | 见 §0.4 "S7 执行记录"（待主 Agent 验收） | `506 passed + 0 skipped`（双模式）；5 个任务提交 + 1 个 §0.4 chore；**无新立任务** |
| **S8 打包与收尾** | T37 → T36 → 最终全量验收 | §0 + §5 + §3 的 T37 | T37（打包/CI）会动 `pyproject.toml`，必须在所有代码改动之后；T36 文档同步放最后 | 全绿；`§0.4` 定稿；CI 安装步骤可通过 |

**硬性顺序约束（不要打乱）**：
1. **S1 必须最先**（✅ 已完成并验收）；S2 ✅、S3 ✅、S4 ✅、S5 ✅。
2. S6/S7 建议在 S1 之后：T31 解 skip 后会真跑到 analyst 节点，依赖 T18b 已修。
3. **S8 必须最后**：T37 会改 `pyproject.toml` 的打包配置，改完要重跑一次全量。
4. 段的**内部**仍然遵守规则 1：一个任务一次提交。

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

**P1-b — 并发请求共享 `ToolRuntime`，会话状态串台（T18 引入）→ ✅ 已由 `f3b76e4`（T18c）修复并验收**

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

**尚未复现（执行时自己确认）**：T25（前端并发/渲染，需浏览器或文本契约）、T26（SSE 错误分支，需 TestClient + monkeypatch）。
（T23 与 T27 原在此列，S2 已完成并由验收者复核；T27 的根因是：`_check_cache` 只在**缓存命中**时
`called_signatures.add(...)`，而"签名已存在"时 `return None` 被调用方当成"无缓存"→ 真的再打一次网关。）

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

> **已完成（保留原文作回归依据）**：T12 收尾（`0965da6`）、T18c（`f3b76e4`）、T19（`bc4849d`）、
> T23（`f119f51`+`7dce255`）、T27（`e6a04e5`）、T19b（`61c48e9`）、T23b（`661be64`）、T22 主体（`17b0782`）、
> T20（`a9d6a89`）、T15（`7d66ad2`）——各自小节里都标了 ✅。
> **未完成**：T12b、T21、T24–T26，以及验收追加的 **T22b / T22c**（异常检测的两个正确性缺陷，物理位置
> 在 T22 小节里，务必先做）与 **T19b / T23b 已完成**（小节末尾那两段已标 ✅）。
> 顺序以 **§0.5 的分段表**为准：S4 = **T22b → T22c** → T24。
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
> `logger.warning(...)`（`:77-78` / `:92-93` / `:97-98`），且 `report is None` 时也有 warning。
> （写这句时全仓 `app/` 已无 `logger.debug`；T19 之后 `app/cache.py` 的 LRU 淘汰**有意**加了一条
> `logger.debug`，那是内存缓存的观测点，与"落库失败被吞"无关。**本任务只需要处理跨线程连接。**）
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

### T22 — 异常检测阈值按百分比设定，但输入是绝对值 → ✅ 主体完成（`17b0782`），**但有两个缺陷：T22b / T22c**
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

**✅ 已完成（`17b0782`）**：`value_semantics`（absolute / pct_change / ratio_to_percent）+ 精确别名表 +
同 datum 同 rule_type 只留最高严重级 + 前值经 `market_cache` 跨调查。**但验收发现两个缺陷 → T22b / T22c。**

### T22b — 精确匹配漏掉"点分 metric"，异常检测在生产里等于关闭（**S4 最前面做；验收者实测复现**）

**定位锚点**
- `app/detector/anomaly.py` 的 `_matches_metric`（`metric.strip().lower() in aliases`）
- 别名表 `_CRYPTO_RULES` / `_ASHARE_RULES` 的 `metric_pattern`
- metric 的来源：`app/gateway/normalizer.py` 的 `_make_datum`（metric 是 **JSON 路径**，不是裸键名）

**现象**：normalizer 对**嵌套载荷**产出的 metric 是点分路径；精确匹配全部漏掉 → 该 metric 一条规则都不命中。
D3 刚把 anomalies 接进报告，于是这个功能在生产里大概率**永远输出空列表**（而且没有任何报错）。

**复现（验收者已跑）**
```python
from app.gateway.normalizer import normalize_tool_result
from app.detector.anomaly import ALL_RULES, _matches_metric, detect_anomalies
from app.cache import market_cache

# metric 名随载荷形状变化
normalize_tool_result("derivatives_history_market_derivatives_history_post", {}, {"openInterest": 3.2e9})
#   -> metric = 'openInterest'
normalize_tool_result("derivatives_history_market_derivatives_history_post", {}, {"data": {"openInterest": 3.2e9}})
#   -> metric = 'data.openInterest'      （数组载荷：'data[0].openInterest'、'result.list[0].openInterest'）

rule = next(r for r in ALL_RULES if r.rule_id == "oi_spike_high")
_matches_metric(rule, "openInterest")        # True
_matches_metric(rule, "data.openInterest")   # False ← 旧子串实现本来是 True

# 端到端：嵌套载荷 OI 1e9 → 1.2e9（+20%，应报 high/critical）
#   detect_anomalies([...]) -> []            ← 什么都不报
# 扁平载荷同样 +20% -> ['oi_spike']           ← 正常
```
A 股同理：`涨停家数` → True，`data.涨停家数` → **False**；`limitUpCount` → True，`data.limitUpCount` → **False**。

**根因**：`_matches_metric` 拿**整条路径**去比别名表。

**修改（推荐 A）**
- **A（最小、够用）**：匹配前把 metric 归一到"末段基名"——去掉数组下标、取最后一个 `.` 之后的部分：
  ```python
  def _metric_basename(metric: str) -> str:
      # 'data[0].openInterest' -> 'openinterest' ; 'data.涨停家数' -> '涨停家数'
      tail = re.sub(r"\[\d+\]", "", metric.strip()).split(".")[-1]
      return tail.strip().lower()
  ```
  `_matches_metric` 用 basename 与别名表精确比对。**这样仍然不会**让 `noise` / `openInterestRate`
  误命中（`openinterestrate` ≠ `openinterest`），保住了 T22 想要的"不做子串匹配"。
- **B（更彻底，但范围大）**：让 normalizer 直接产出规范指标名——它里面那两个
  `_CRYPTO_METRICS` / `_ASHARE_METRICS` 集合目前**只被文档字符串提到、没有任何调用方**，
  看起来本来就是为此准备的。选 B 会影响 evidence / 报告里所有 metric 的展示名，
  **必须单独提交并说明影响面**，且要跑 `test_normalizer*` / `test_evidence*` / `test_candle_summary` 全部。

**必补测试（关键：必须经过 `normalize_tool_result`，不许手工造 `NormalizedDatum`）**
- 参数化载荷形状 `{"openInterest": ...}` / `{"data": {"openInterest": ...}}` / `{"data": [{"openInterest": ...}]}`
  → 归一化后喂 `detect_anomalies`，**三种形状都必须报出同一条 `oi_spike`**；
- 同形状再来一遍 A 股 `涨停家数`（含 `data.` 前缀）；
- 反向对照：`metric="noise"`、`metric="data.openInterestRate"` → 仍然**不触发**。
**验收**：上面的复现脚本在嵌套载荷下也输出 `['oi_spike']`；把 `_metric_basename` 换回恒等函数 → 新用例必红。
**风险 / 牵连**：`value_semantics` 的比对值不受影响（只改"匹配哪条规则"）；改完顺手确认
`tests/test_anomaly_detector.py` 原有的 38 条仍绿。

### T22c — 前值只按 metric 名存放，不同 symbol 互相污染（**S4；验收者实测复现**）

**定位锚点**：`app/detector/anomaly.py` 的 `_prev_key` / `_load_prev_value` / `_store_prev_value`
与 `detect_anomalies` 里的 `_store_prev_value(metric, num_value)` 调用点。
**现象**：前值的 key 只有 metric 名，**`result.arguments` 里的 `symbol` 完全没用上** → 用户的两次独立调查
（不同币种 / 不同股票）会互相当成"前值"，报出**用户可见的错误异常**。D3 之后这些错误异常会写进
报告与 `daily_states`。
**复现（验收者已跑，三次独立调查）**
```
BTCUSDT OI=1,000,000,000  -> []                                              （无前值，正确）
ETHUSDT OI=3,000,000,000  -> [('oi_spike','critical','OI 剧烈增长 200.0%')]   ← 拿 BTC 的值当前值
SOLUSDT OI=3,300,000,000  -> [('oi_spike','high','OI 短时间内快速增加 10.0%')] ← 拿 ETH 的值当前值
```
**修改**
1. 前值 key 必须带**作用域**：至少 `(metric_basename, symbol)`，其中 symbol 取
   `getattr(result, "arguments", {}).get("symbol")`（拿不到就用 `domain`，再拿不到用 `"unknown"`）。
   注意符号可能在 `arguments` 里是 `"BTCUSDT"` 这种，也可能是 `";".join(stocks)`，按原样用即可。
2. 跨**同 symbol**的调查保留"与上次快照比"的能力（这是 T22 想要的）；跨 symbol 必须隔离。
3. 顺便明确这个前值存储是**有状态的业务数据**却借用了 `market_cache`（会被 T19 的 LRU 淘汰、
   也会被任何 `market_cache.clear()` 清掉）。**若保留现方案**，在注释里写明这个权衡并
   让"拿不到前值 → 不触发"保持可见（可选：命中不到前值时 `logger.debug` 一条）；
   **若改成独立的小字典/持久化**，要说明为什么（本任务不强制，但必须选一个并写下来）。
**必补测试**：`tests/test_anomaly_detector.py` —— 三次不同 symbol 的检测，断言后两次**不产生** anomalies；
再补一条同 symbol 两次（1e9 → 1.2e9）→ 必须报 `oi_spike`（防"隔离过头把正常检测也关了"）。
**验收**：复现脚本三次输出依次为 `[]`、`[]`、`[]`；同 symbol 的对照用例仍报 anomaly。
**风险 / 牵连**：`detect_anomalies` 的签名可能需要在内部从 `tool_results` 里取 symbol——
它已经拿到 `result` 对象，不需要改签名；改完确认 T20 的 `build_response_from_state` 调用无需变动。

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

### T24b — `.gitignore` 的临时目录写法写错（S5 一行；验收者实测）
`ceea711` 给 `.gitignore` 最后加的是 **`.temp`**，而本项目实际用的是 **`.tmp/`**（附录 A 的 runner、
各段的复现脚本、执行方的 `.py.bak` 备份都放在那里），并且该行丢了行尾换行符。
**复现**
```bash
git check-ignore -v .tmp/verify_s4_anomaly.py   # 无输出（退出码 1）= 未被忽略
git status --porcelain                          # 仍显示 ?? .tmp/
```
**修改**：把 `.temp` 改成 `.tmp/`（或两者都写），补回文件末尾换行；
顺手把 `.tmp/` 里执行方留下的 `base_t23b.py.bak` / `cache_t19.py.bak` / `reasoning_node_t15.py.bak` /
`tool_registry.t24.py` 删掉（它们是回退备份，源码已确认干净：`git diff -- app/ tests/` 为空、全量绿）。
**验收**：`git check-ignore -v .tmp/run_tests_local.py` 能命中；`git status --porcelain` 里不再出现 `.tmp/`；
`.tmp/` 里只剩 `run_tests_local.py` 与当前在用的复现脚本。
**必补测试**：无（纯仓库卫生）。**风险**：无。

### T24 — 工具注册表：域过滤与注释相反、HK 工具 domain/category 漂移 → ✅ 已完成（`3bef4f4`，见 §0.4）
> 原始问题描述保留在下方作为回归依据。已实现：`registry_text` 删掉 `or ALL_TOOLS` 静默回退、
> health 始终附加、空域记 warning；`hk_quote`/`hk_search` 的 domain → `hk_stock`（category 与 A 股对齐）；
> `BY_NAME` 规范条目选取显式化（cross 优先、否则先注册者），占位条目进 `SHARED_BY_NAME`。
> 回归测试：`tests/test_tool_registry_multi_domain.py`（27 条，含并入的 T34 断言）；
> **验证结论见 §0.4 "S4 验收复核记录"**（4 组复用 operationId 的 `http_method`/`http_path` 完全一致 → 不影响真实调用）。

**原始描述（历史）**

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

### T19b — 修掉 T19 的假阳性用例（**验收者实测发现；下一段最前面做**）
**定位锚点**：`tests/test_cache_capacity.py` 的 `test_expired_entries_purged_on_set`（文件末尾那条）。
**现象 / 证据**：把 `app/cache.py` 的 `set()` 里 `self._purge_expired()` 换成 `pass`（停用"set 时清理过期条目"），
该文件 **仍然 5 passed** —— 这条用例声称验证 purge-on-set，实际什么都没验证。
**根因**：用例在断言前先调了 `c.get("old")`；读路径本身会把过期键删掉，于是断言 `"old" not in c._store` 恒真。
**修改**：让断言只可能被 `set` 里的 purge 满足（**不要先 get**）：
```python
def test_expired_entries_purged_on_set():
    c = Cache()
    c.set("old", 1, ttl=0.01)
    time.sleep(0.02)
    c.set("new", 2)                 # 只有这里的 _purge_expired 能让 old 消失
    assert "old" not in c._store    # 停用 purge → True → 必红
    assert c.get("new") == 2
```
若想同时保留"读路径也会删"的覆盖，另开一条用例断言 `c.get("old") is None`，不要混在一条里。
**验收（必须自己跑反向验证）**：把 `set()` 里的 `_purge_expired()` 换成 `pass` → 这条用例**必须变红**；
还原后全绿。顺手把 `tests/test_cache_capacity.py` 里其余 4 条也做一次"改坏必红"自检。
**风险**：纯测试改动，不碰产品代码。

### T23b — 预算要按"整次调查"起算，而不是"本节点"（**验收者发现；下一段做**）
**定位锚点**
- `app/graph/nodes/analysts/base.py:195-197` — `budget_seconds = settings.research_budget_seconds`，
  `run_started = time.monotonic()`（**每次节点调用都重置**），`:229-234` 的 deadline 计算
- `app/main.py:219` / `:353` — 整次调查的硬上限 `asyncio.wait_for(..., timeout=settings.research_budget_seconds)`
**现象**：`research_more` 回环会让 analyst 再跑一轮，而每一轮都在自己的起点重新获得一整份
`research_budget_seconds`；于是"第二轮把预算又烧满"→ 撞上 `main.py` 的 300s 硬上限 → 仍然 504。
T23 只在**单轮内**达成了"超预算就降级返回 error ToolResult"。
**修改**：把整次调查的起点（或已用时长）往下传，deadline 用**剩余调查预算**而不是节点预算：
1. 由 orchestrator / `main.py` 在图启动前把 `time.monotonic()` 写进 state（例如
   `state["budget_started_at"]`，或直接写 `state["budget_deadline"] = monotonic() + research_budget_seconds`），
   并在 `ResearchState` 里加对应字段（注意别用会跨回环累加的 reducer）。
2. `_execute_tools` 读该值算 `remaining_budget = deadline_at - now`；缺失时**退回当前行为**并记一条 warning
   （不要把"没有预算信息"静默当成"预算无限"）。
3. deadline 仍然按"剩余预算 ÷ 剩余工具数"均分下发（T23 已有逻辑不变）。
**必补测试**：`tests/test_graph_topology.py` 或新建 —— 构造 `state["budget_deadline"]` 已过半的场景，
断言 analyst 下发的 deadline **明显早于**"现在 + research_budget_seconds / n"；
再补一条：state 里没有该字段时退回旧行为且有 warning。**反向验证**：把 `_execute_tools` 改回用本节点起点 → 用例必红。
**风险 / 牵连**：`ResearchState` 加字段要确认不会与 T16 的 reducer 语义冲突（标量字段默认无 reducer，回环时覆盖，正是我们要的）；
`main.py` 的硬上限**不要删**（它是最后的安全网，两者都要在）。

### T27 — `called_signatures` 的"去重"契约不成立 → ✅ 已完成（`e6a04e5`，见 §0.4）
> 下面保留原始问题描述作为回归依据。已实现的语义：签名在**真实执行成功后立即**登记；
> 同签名重复调用返回 `status="partial"` + `note`（不静默成功、不再绕过缓存打网关）；
> `base._execute_tools` 用 `(tool_name, sorted(args))` 的 `seen` 集合在 route 层提前跳过重复。
> 回归测试：`tests/test_tool_runtime_dedup.py`（3 条）。

**原始描述（历史）**

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

> **⚠️ D4 要一并决定的细节（验收者在 S2 复核时留意到的）**：`budget -= 1` 现在位于循环**顶部**
> （`base.py:199-202`，在 unknown tool_key / 缺 symbol / T27 重复签名这些 `continue` **之前**），
> 所以**被跳过的调用照样消耗预算**。T27 之后"route 里重复的 tool_call 被跳过"是常见情形，
> 于是 `budget=n` 实际可能只执行 1 次。做 D4 时请明确选一种语义并写进测试：
> ①`max_tool_calls` 计"真实执行次数"（把扣减移到真正 `execute` 之前）；或②计"处理过的 route 条目数"（保留现状，注释写明）。
> 两种都可以，**但不能再出现"文档说上限 N、实际执行数看跳过情况"的含糊状态**。

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

**S1–S4 验收中反复出现、请一并沉淀进 `conftest.py` 注释与评审清单的四条教训**：
- **假阳性模式①（整体替换被测对象）**：stub 把 `_runtime` 换成 `None`/缺方法的假对象 → 异常被节点的兜底
  `except` 吞成 "failed finding"，用例却断言通过（T18T）。
- **假阳性模式②（autouse fixture 清状态 + `@parametrize`）**：每条参数化用例都在干净状态下跑，
  **跨调用污染类**回归（前值串台、缓存复用、签名去重）根本不会发生 —— T22c 初版就是这么写的，
  被反向验证抓出后才改成**单条用例内顺序执行**。凡是"第 N 次调用受第 N-1 次影响"的行为，
  都必须放在一条用例里连续调用。
- **假阳性模式③（fixture 手工造数据、绕开真实入口）**：T22 的用例手写 `NormalizedDatum(metric="openInterest")`，
  而生产里 metric 是 normalizer 产出的 `data.openInterest`；T22b 的漏报就是这么漏过去的。
  **凡是断言"某条规则会命中"的用例，都必须经过 `normalize_tool_result`。**
- **反向验证的正确姿势**：必须**忠实复现旧行为**，而不是"随便把新代码弄坏"。T22c 我第一次只让
  `_prev_scope` 返回常量，只红了 2 条；改成把 `_prev_key` 退回 metric-only（与旧实现等价）才红了 5 条。
  同理，**探针脚本不要手工写内部状态**（如直接往 `market_cache` 塞 `__anomaly_prev__:*`）：
  T22c 改了键格式后，两个旧探针连扁平载荷都输出 `[]`，看起来像"修复失效"，其实是探针过期；
  探针要通过公开路径驱动（连续调用 `detect_anomalies`）。

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
    验收已过：436 passed + 2 skipped（双模式）；T18c 反向验证（回退实例属性）2 条必红；
    记录见 §0.4 "S2 验收复核记录"；**留下两个尾巴 T19b / T23b**

S3 检测与报告契约 — ✅ 完成并双模式全绿（61c48e9 / 661be64 / 17b0782 / a9d6a89 / 7d66ad2）
[x] T19b 修 test_expired_entries_purged_on_set 假阳性（停用 purge 必红，验收者复跑确认）
[x] T23b 预算按整次调查起算（state["budget_deadline"]；反向验证 2 条必红）
[x] T22 anomaly 阈值/单位（D3 前置）  → [x] T20 必填字段默认值（+ anomalies 归一化）
[x] T20 后半：anomalies 用 detect_anomalies 填（D3，代码填、模型条目被覆盖）
[x] T15 Evidence Gate 降级（D2=B：降级不短路）
    验收已过：454 passed + 2 skipped（双模式）；T19b / T15 反向验证验收者复跑过；
    记录见 §0.4 "S3 验收复核记录"；**T22 留下两个正确性缺陷 → T22b / T22c**

S4 注册表 + 异常检测尾巴 — ✅ 完成并验收（e7c2272 / 08c3656 / 3bef4f4）
[x] T22b metric 名归一化（点分路径取末段；新测试全部经过 normalize_tool_result）
[x] T22c 前值按 symbol 隔离
[x] T24 注册表域过滤 / HK 漂移 / BY_NAME 歧义（含并入 T34 的 multi_domain 断言）
    验收已过：479 passed + 2 skipped（双模式）；四项反向验证我复跑过（7 / 5 / 3 / 6 条必红）；
    记录见 §0.4 "S4 验收复核记录"；**留下 T24b（.gitignore 一行）**

S5 请求生命周期 — ✅ 完成并验收（6c045cd / e026f0b / 7f635ff / fe837b1）
[x] T24b .gitignore 的 .temp → .tmp/（`git check-ignore` 已命中；.tmp 里备份已清）
[x] T21 sqlite 连接按线程各取一条（反向验证：退回共享单连接 → 3 条必红）
[x] T26 SSE 错误分支可达 + cancel 后 await 收尸（两条反向验证各 1 条必红；400 未被改写成 500）
[x] T25 前端并发与渲染健壮性（退回旧 index.html → 6 条契约全红；severity 逻辑在 Node 里动态验证通过）
    验收已过：492 passed + 2 skipped（双模式）；无新立任务；
    记录见 §0.4 "S5 验收复核记录"；**T25 的浏览器手工步骤仍待仓库主人执行**

S6 测试有效性（上）  [ ] T28 → [ ] T29 → [ ] T30（含 D4 budget）→ [ ] T31

其余
[ ] T12b requirements.txt 同步 mcp>=2,<3（S7，一行）

批次 4（测试有效性）  T28 T29 T30(+D4 budget，含"跳过是否消耗预算"的语义决定) T31 T32 T33 T34 + T35 机制性防线

收尾（S8）
[ ] T37 打包/CI flat-layout（pyproject 显式声明 app 包；CI 的 pip install -e ".[dev]" 才能过）  ← 验收者新发现
[ ] T36 文档与实现同步（architecture / README / prompts / tool_registry）
[ ] §0.4 更新（每完成一批就更新：任务表打 ✅ + 提交号 + 测试数字）

已拍板、不要再问：D1 mcp>=2,<3 ｜ D2 Gate 降级不短路 ｜ D3 anomalies 代码填 ｜ D4 budget/max_tool_calls 接通
已拍板、不要改回：Critic error 语义 ｜ 简报过点不补 ｜ evidence id 前缀 ｜ truncate 保留末尾 + partial ｜ 不要重建 PROJECT_STATUS.md
```

---

## 附录 D — 已完成任务的契约备忘（T1–T18 + S1 修红 + S2 运行时）

> 细节用 `git show <提交号>` 查回。这里只保留**不许改回**的语义与对应的回归测试位置。
> S1/S2 新增或更新的契约已并入下表（T17 / T18 / T19 / T23 / T27 行）。

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
| **T18 analyst 级复用 Gateway** | `44c307f` + `5647a4a`（T18b）+ `f2e4da1`（T18T）+ `f3b76e4`（T18c） | 一次 analyst 运行复用同一个 Gateway 客户端，异常时保证关闭；**连接必须按需**（进入会话不建连）；stub 必须提供 `gateway_session`；**会话状态必须在模块级 ContextVar 里（每 asyncio task 一份），不许放回 `self.*`**（单例图的节点实例被并发请求共享） | `tests/test_gateway_reuse.py`（8 条） |
| **T19 缓存容量与 TTL** | `bc4849d` | `set` 用 `ttl if ttl is not None else default`（`ttl=0` = 立即过期，不许落回默认）；`max_entries=512` + LRU 淘汰；`set` 时清理过期条目；`stats` 含 `max_entries`/`evictions`（**该行为的测试是假阳性，须由 T19b 修好**） | `tests/test_cache_capacity.py` |
| **T23 重试感知预算** | `f119f51` + `7dce255` | `deadline` 全链路下发（analyst 均分 → `execute` → `gateway.call`）；HTTP 重试前/退避前检查剩余时间，MCP 每次尝试前检查；超预算返回 `error` ToolResult（不抛异常炸链）；**预算起算点是本节点而非整次调查（T23b 修）** | `tests/test_http_client_budget.py`（4 条） |
| **T15 Evidence Gate** | `7d66ad2` | `has_evidence=False` 时 Reasoning **降级不短路**：报告照常产出，但 confidence 强制 low、data_caveats 追加"证据链为空"、errors 写 "Evidence gate: ..."；模型置信度被覆盖；gate 为 dict（reducer 序列化后）同样要被消费 | `tests/test_evidence_gate.py`（9 条） |
| **T20 报告字段 + D3 anomalies** | `a9d6a89` | `market_state` / `what_happened` 允许为空但必须写 data_caveats（不许静默）；`anomalies` 归一化为 list[dict]（标量不炸 schema）；**anomalies 由 build_response_from_state 用 detect_anomalies 代码填充，模型条目一律被覆盖** | `tests/test_reasoning_parsing.py`（+4） |
| **T22 异常检测语义** | `17b0782` | metric 匹配是**精确别名表**（'oi' 子串不许回潮）；OI 规则比较**对前值的 % 变化**，无前值不触发；fundingRate 是小数比率（×100 后比 % 阈值）；同 datum 同 rule_type 只报最高严重级；OI 前值存 market_cache（key `__anomaly_prev__:*`，ttl 3600） | `tests/test_anomaly_detector.py`（38 条） |
| **T23b 调查级预算** | `661be64` | 预算起算点是 **state["budget_deadline"]**（orchestrator.run / SSE initial_state 写入，research_more 回环覆盖）；`_execute_tools` 缺该字段时退回本节点起算**并记 warning**，不许静默当成预算无限；main.py 的 wait_for 硬上限保留 | `tests/test_budget_deadline.py`（4 条） |
| **T27 called_signatures** | `e6a04e5` | 签名在**真实执行成功后立即**登记；同签名重复调用返回 `status="partial"` + `note`（不许静默成功、不许绕过缓存再打网关）；`_execute_tools` 用 `seen` 集合在 route 层提前跳过 | `tests/test_tool_runtime_dedup.py`（3 条） |
