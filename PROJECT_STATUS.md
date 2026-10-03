# Mosaic 项目概况与未来规划

> **从 Market Data 到 Market Understanding**
>
> 最后更新：2026-10-03（本文数据均为本地实测，非引用声明）

---

## 一、项目定位

Mosaic 是一个基于 **LangGraph** 的市场情报 Agent。它不做交易执行、不以"预测涨跌"为目标，而是通过自主规划研究路径、调用市场数据工具、交叉验证证据、分析市场状态与异常，把分散的 A 股、港股、Crypto、大宗商品数据转化为**可解释的市场情报**。

- **定位**：Market Research / Market Intelligence / Decision Support
- **不是**：Trading Execution / Financial Advisor

---

## 二、完成度总览（实测）

| 维度 | 状态 | 实测情况 |
|------|------|----------|
| 核心架构 | ✅ 完成 | LangGraph P0–P5 全部落地：Supervisor 路由 + 5 节点位 Analyst 并行 + Gate + Reasoning + Critic 闭环 |
| 测试覆盖 | ✅ 健康 | **338 passed, 2 skipped**，20s 跑完；26 个测试文件 / 3,932 行 |
| 代码规范 | ✅ 完成 | Ruff lint 0 问题，**79 files** formatted；app 共 53 个 py 文件 / 6,342 行 |
| 持续集成 | ✅ 完成 | GitHub Actions：装依赖 → Ruff lint → format check → pytest，全流程覆盖 |
| Web 服务 | ✅ 可用 | FastAPI + SSE 流式 + Web UI，同步/流式双端点 |
| 市场域 | 🟡 4/6 | A股/Crypto 完整，港股基础，贵金属就绪；美股/宏观仅占位 |
| 可选分析员 | 🟡 1/2 | News 已实现（默认关）；Sentiment 仅有开关、节点未实现 |
| 容器化 | 🔴 未开始 | 无 Dockerfile，只能本地运行 |
| 可观测性 | 🔴 未开始 | 无 Token 成本统计、无 Tracing、无节点耗时埋点 |

**整体评估：约 70% —— 核心产品闭环已完成且可运行，工程质量底子好；距"可部署、可观测、可运营"的生产状态还差部署运维和数据覆盖两块。**

---

## 三、核心架构

```text
User
  ↓
FastAPI (REST + SSE)
  ↓
Orchestrator → LangGraph compiled graph
  ↓
Supervisor (LLM 路由：意图解析 + analyst 分配 + 工具预算)
  ↓ Send() 扇出（并行）
┌──────────┬──────────┬──────────┬──────────┬──────────┐
│ 技术面    │ 基本面    │ 资金面    │ 新闻事件  │ 舆情情绪  │
│ analyst  │ analyst  │ analyst  │ analyst  │ analyst  │
│ (默认开) │ (默认开) │ (默认开) │ (开关)   │ (开关,未实现)│
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘
     └──────────┴──────────┴────┬─────┴──────────┘
                                ↓
                        Evidence Gate (代码级质量检查)
                                ↓
                        Reasoning (LLM 汇总证据 → MarketIntelligence)
                                ↓
                        Critic (LLM 结论-证据审计)
                          pass │ revise / research_more（≤ critic_max_revisions 轮）
                           ↓   └──────► 回 Reasoning 重写 / 回 Supervisor 补研究
                         END
```

**关键设计**：
1. **证据账本是唯一契约**：Analyst 只追加 Evidence，不写结论；结论由 Reasoning 统一产出
2. **Critic 闭环**：证据不足时打回重写（revise）或补充研究（research_more），最多 N 轮
3. **可选节点**：news / sentiment 由配置开关控制，关闭时行为与三 analyst 基线一致
4. **预算与超时双轨**：单工具 30s 超时 vs 整次调查 300s 预算，配 recursion limit 25 防死循环

---

## 四、市场域与工具覆盖

| 市场域 | 状态 | 覆盖能力 |
|--------|------|----------|
| **A 股** | ✅ 完整 | 情绪、涨停生态、题材、个股深度（F10）、龙虎榜 |
| **Crypto** | ✅ 完整 | K线、快照、衍生品(OI/Funding)、清算地图、大户持仓 |
| **港股** | ✅ 基础 | 实时行情(腾讯)、证券搜索(雪球)、北向资金(东财直连)、恒生指数 |
| **大宗商品** | 🟡 贵金属就绪 | OKX 永续：黄金(XAU)、白银(XAG)、铂金(XPT)，铜/原油待接入 |
| **美股** | 🔴 Placeholder | domain/registry 已预留，第三方 API 待接入 |
| **宏观** | 🔴 Placeholder | domain 已预留，CPI/PMI/利率数据待接入 |

**工具规模**：35+ 个工具；klines / snapshot / window 为跨域复用工具；支持 `INTERNAL` 类型工具绕过 Gateway 直连外部源（北向资金、恒生指数）。

### 已落地的产品特性

- **Research Experience**：SSE 逐节点推送研究进度（progress 事件带 node 字段），用户看到"AI 正在研究"
- **Market Memory**：SQLite 持久化每日市场状态 / 异常 / 研究历史，问"今天和昨天有何不同"自动注入近 7 天上下文
- **Anomaly Radar**：OI 突变、Funding 极端值、大规模清算、涨停异常扩张/收缩、情绪骤降，分 Low→Critical 四级
- **Daily Briefs**：09:15 晨报 / 15:30 晚报，走完整图调查，LLM 失败时回退 memory 模板，简报 JSON 落盘
- **统一数据结构**：Normalizer 将所有 Tool Response 转为标准 NormalizedDatum（domain/metric/value/source/status）

---

## 五、本轮核查新发现的问题

> 以下是对照实际文件系统检查出的、既有文档未覆盖的问题，按优先级排列。

| # | 问题 | 影响 | 建议 |
|---|------|------|------|
| 1 | **Token 成本完全不可见**：多节点 + Critic 多轮，单次调查调用 LLM 5+ 次，无任何用量统计 | 不知道每次调研花多少钱，成本可能失控 | 记录每次 LLM 调用的 prompt/completion tokens，落库并在 /health 汇总 |
| 2 | **无部署手段**：无 Dockerfile / 部署文档，只能在开发机本地跑 | 无法上服务器长期运行，简报调度形同单机玩具 | Dockerfile + compose（含 memory 卷挂载） |
| 3 | **残留垃圾文件**：`app/sentiment/` 源文件已删，仅剩 `__pycache__/pipeline.cpython-314.pyc` 孤立缓存 | 误导后来者以为 sentiment 模块存在 | 删除整个 `app/sentiment/` 目录 |
| 4 | **文档引用失效**：README 项目结构仍引用 `docs/langgraph-refactor-plan.md`，该文件已在提交 f5c8bc4 中删除 | 读者按图索骥找不到文件 | 更新 README 结构树 |
| 5 | **`.ruff_cache/` 未加入 .gitignore**（当前只忽略了 .pytest_cache） | 存在误提交缓存的风险 | .gitignore 补一行 `.ruff_cache/` |
| 6 | **memory.db 已达 7.2 MB**（已被 gitignore，无泄漏风险） | 长期运行后无限膨胀，daily state 只增不删 | 增加保留期（如 90 天）与定期归档/清理 |
| 7 | **依赖全部用 `>=` 不锁定** | CI 与本地、现在与未来会拉到不同版本，langgraph 1.x 迭代快，易被 breaking change 击中 | uv / pip-tools 生成 lock 文件 |
| 8 | **CI 只测 Python 3.11 单版本**，本地实际为 3.14 | 版本差异问题（如本次 pyc 残留）无防线 | matrix 加 3.11 / 3.12 / 3.13 |
| 9 | **PROJECT_STATUS.md 尚未提交 git**（untracked） | 状态文档只在本地 | 整理后随下次提交入库 |

### 近期已修复的关键 Bug（2026-10，值得记录）

| Bug | 根因 | 修复 |
|-----|------|------|
| Critic 闭环完全失效 | `model_dump()` 后 critique 变 dict，`hasattr` 永远 False，revise/research_more 回边从未触发 | 提取 `critic_route_decision()` 兼容 dict/对象 |
| research_more 无限循环 | Reasoning 失败 → report=None → 回 supervisor → revision_count 不递增，烧到 recursion limit | research_more 时 report 为 None 直接终止 |
| 测试假阳性 | 3 个路由测试重定义简化版 `_route` + FakeCritique，掩盖真实 bug | 改调真实路由函数 + 真实图 e2e 回环测试 |
| langgraph 未声明依赖 | CI 干净环境直接 ModuleNotFoundError | 补齐 pyproject / requirements |

---

## 六、未来规划

### P0 — 可观测性与部署（建议 1 周内）

| 事项 | 价值 | 工作量 |
|------|------|--------|
| Token 成本追踪 | 每次调查记录 tokens + 成本，回答"一次调研多少钱"，并为 Critic 轮次设成本上限 | 1–2 天 |
| Tracing 接入 | LangSmith 或自建 trace：每节点输入/输出/耗时/重试，定位慢在哪一步 | 1 天 |
| Docker 容器化 | Dockerfile + compose + memory 卷，一键部署到服务器 | 0.5–1 天 |
| 清理与文档修补 | 删 sentiment 残留、修 README 引用、补 .ruff_cache 忽略、提交本文档 | 0.5 天 |
| 错误响应兜底 | report=None 时给用户明确错误提示而非空报告 | 0.5 天 |

### P1 — 市场域与能力扩展（2–4 周）

| 事项 | 价值 | 工作量 |
|------|------|--------|
| 美股接入 | 补齐第 5 个市场域（yfinance / Finnhub / Alpha Vantage） | 2–3 天 |
| 宏观数据接入 | CPI / PMI / 利率 / 美联储日历，显著增强"为什么"的归因深度 | 2–3 天 |
| 商品扩展 | 铜 / 原油（OKX 或专用商品 API） | 1 天 |
| News 默认启用 | DDGS 已就绪，观察稳定性后打开开关 | 0.5 天 |
| Sentiment 节点落地 | 先定数据源（评论 MCP），再实现节点；或正式砍掉该开关避免死配置 | 2–3 天 |
| 依赖版本锁定 + CI 多版本 | lock 文件 + pytest matrix 3.11/3.12/3.13 | 1 天 |
| Memory 保留策略 | daily state 90 天滚动清理，DB 体积可控 | 0.5 天 |

### P2 — 体验与智能化

| 事项 | 价值 |
|------|------|
| Web UI 可视化升级 | 图表化市场状态、异常时间线、历史对比，而非纯文字报告 |
| 异常告警推送 | 触发 High/Critical 异常时推 Telegram / 企业微信 / 邮件 |
| 多轮追问优化 | 基于 conversation_id 的上下文追问（已有雏形，需打磨指代消解） |
| 缓存预热 | 开盘前预加载常用数据，降低首问延迟 |
| LLM 调用并发化 | Reasoning/Critic 之外可并行的 LLM 环节并发，缩短端到端耗时 |
| 多语言 | 英文报告 / 界面 |

### P3 — 长期方向

- **插件系统**：第三方可注册自定义 analyst / tool / 异常规则
- **情报质量回测**：记录历史结论，事后量化判断准确率，让"情报质量"本身可度量
- **多 Agent 协作**：复杂问题拆分给多个专项 Agent 协同调查
- **报告订阅**：用户自定义关注板块 / 指标，个性化简报

---

## 七、风险与技术债

| 风险 | 说明 | 缓解 |
|------|------|------|
| LLM 成本不可见 | Critic 闭环天然放大 token 消耗，无统计、无上限 | P0 Token 追踪 + 成本熔断 |
| 上游单点依赖 | Market Gateway MCP / 第三方 API 故障时降级手段有限 | 多源 fallback + 熔断 + 明确 partial 状态 |
| 版本漂移 | 依赖 `>=` 不锁，langgraph/openai SDK 迭代快 | P1 lock file |
| 死配置风险 | SENTIMENT_ENABLED 开关存在但节点不存在，配置与代码不符 | P1 要么落地要么砍掉 |
| 单机运行 | 简报调度、Memory 都绑在一台开发机上 | P0 Docker 后上服务器 |

---

## 八、推荐执行顺序

```text
1. 卫生清理（sentiment 残留 / README 引用 / gitignore / 提交本文档）  ← 半天，先做
2. Token 成本追踪 + Tracing        → 搞清楚每次调研的成本与瓶颈
3. Docker 容器化 + 错误响应兜底     → 能上服务器、异常不裸奔
4. 依赖锁定 + CI 多版本 + Memory 清理 → 稳定性加固
5. 美股 / 宏观接入                  → 扩展市场覆盖
6. News 启用 / Sentiment 取舍       → 消除死配置
7. Web UI 可视化 + 异常告警         → 产品体验升级
```

---

## 附：关键链接与实测基线

- 代码仓库：https://github.com/PhineasMarshell/Mosaic
- CI：https://github.com/PhineasMarshell/Mosaic/actions
- 本地服务：http://127.0.0.1:8000 （健康检查：/health）
- 实测基线（2026-10-03）：338 passed / 2 skipped / 1 warning，20.06s；Ruff 0 问题 / 79 files
