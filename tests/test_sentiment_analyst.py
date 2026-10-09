"""舆情情绪分析员（Sentiment Analyst）测试套件 — 计划 docs/sentiment-analyst-plan.md Step 6。

9 个用例：拓扑开关两态、注册表条目、digest 格式、Evidence 格式、帖子数截断、
LLM JSON 解析、路由候选含 sentiment、市场级最低证据集回归（不含 xq_discussions）。

全部离线：fake LLM client + fake ToolRuntime.execute，不碰网络与真实网关。
"""

import json
import types

import pytest

from app.config import Settings
from app.gateway.tool_registry import ALL_TOOLS, by_category, resolve_tool
from app.graph.builder import build_graph
from app.graph.market_plan import MARKET_SUMMARY_MINIMUM
from app.graph.nodes.analysts.sentiment import SentimentAnalystNode
from app.graph.nodes.supervisor import route_candidate_categories
from app.models.market import NormalizedDatum, ToolResult


def _make_fake_openai(text: str):
    """构造返回固定 JSON 的 fake OpenAI client（对齐 test_graph_topology 的 helper）。"""

    class _FakeMessage:
        content = text

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResp:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, *args, **kwargs):
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return _FakeClient()


_SENTIMENT_LLM_JSON = json.dumps(
    {
        "sentiment_score": 0.62,
        "bullish_ratio": 0.45,
        "bearish_ratio": 0.30,
        "neutral_ratio": 0.25,
        "key_themes": ["财报预期", "政策利好"],
        "summary": "整体偏乐观",
    },
    ensure_ascii=False,
)


def _discussion_result(n_posts: int = 2, symbol: str = "SH600519") -> ToolResult:
    """构造一个携带 n_posts 条雪球评论的 list_stock_discussions 成功结果。"""
    raw = {
        "discussions": [
            {"text": f"评论样本第{i}条：看涨，业绩超预期", "created_at": f"2026-07-2{i}T10:00:00"}
            for i in range(n_posts)
        ]
    }
    normalized = [
        NormalizedDatum(domain="a_share", tool="list_stock_discussions", metric=f"discussions[{i}].text", value=p["text"])
        for i, p in enumerate(raw["discussions"])
    ]
    return ToolResult(
        tool="list_stock_discussions",
        tool_key="xq_discussions",
        arguments={"symbol": symbol},
        raw=raw,
        status="success",
        normalized=normalized,
    )


# ------------------------------------------------------------------ #
# 1-2. 拓扑：开关两态                                                  #
# ------------------------------------------------------------------ #


def test_sentiment_node_skips_when_disabled():
    """sentiment_enabled=false（默认）→ 图不含 sentiment 节点，扇出/路由候选也不含它。"""
    settings = Settings(sentiment_enabled=False)
    graph = build_graph(settings)
    nodes = graph.get_graph().nodes
    assert "sentiment" not in nodes
    assert "sentiment" not in route_candidate_categories(settings)
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("sentiment", "gate") not in edges
    assert ("supervisor", "sentiment") not in edges


def test_sentiment_node_registered_when_enabled():
    """sentiment_enabled=true → 图含 sentiment 节点 + supervisor 扇出边 + gate 汇入边。"""
    settings = Settings(sentiment_enabled=True)
    graph = build_graph(settings)
    nodes = graph.get_graph().nodes
    assert "sentiment" in nodes
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("supervisor", "sentiment") in edges
    assert ("sentiment", "gate") in edges


# ------------------------------------------------------------------ #
# 3. 注册表                                                           #
# ------------------------------------------------------------------ #


def test_sentiment_tool_in_registry():
    """xq_discussions 在 by_category["sentiment"]，元数据按计划声明；timeline 不受影响。"""
    keys = {meta.key for meta in by_category["sentiment"]}
    assert "xq_discussions" in keys
    meta = resolve_tool("xq_discussions")
    assert meta.tool_name == "list_stock_discussions"
    assert meta.category == "sentiment"
    assert meta.priority == "high"
    assert meta.requires_symbol is True
    assert meta.http_method == "GET"
    assert meta.http_path == "/market/discussions"
    # timeline 仍指向同一 gateway 工具但归属 technical，描述已修正
    timeline = resolve_tool("timeline")
    assert timeline.tool_name == "list_stock_discussions"
    assert timeline.category == "technical"
    assert "雪球" in timeline.purpose


# ------------------------------------------------------------------ #
# 4. digest 格式                                                      #
# ------------------------------------------------------------------ #


def test_sentiment_digest_format():
    """_make_digest 产出计划 §4 规定格式、≤200 字；空评论走降级文案。"""
    node = SentimentAnalystNode(Settings())
    results = [_discussion_result()]
    posts = node._extract_posts(results)
    analysis = {
        "sentiment_score": 0.62,
        "bullish_ratio": 0.45,
        "bearish_ratio": 0.30,
        "neutral_ratio": 0.25,
        "key_themes": ["财报预期", "政策利好", "行业竞争"],
        "summary": "整体偏乐观",
    }
    digest = node._make_digest(["xq_discussions"], results, posts, analysis, False)
    assert len(digest) <= 200
    assert digest.startswith("sentiment: ")
    assert "雪球评论2条" in digest
    assert "情绪偏乐观" in digest
    assert "sentiment_score=0.62" in digest
    assert "看涨45%/看跌30%/中性25%" in digest
    assert "热门主题: 财报预期、政策利好、行业竞争" in digest

    # 无评论 → 固定文案
    empty = ToolResult(
        tool="list_stock_discussions", tool_key="xq_discussions", arguments={}, raw={"discussions": []}, status="success", normalized=[]
    )
    assert node._make_digest(["xq_discussions"], [empty], [], None, False) == "sentiment: 无评论数据"

    # LLM 不可用 → 规则统计降级文案
    degraded_text = node._make_digest(["xq_discussions"], results, posts, None, True)
    assert "LLM 不可用，降级为规则统计" in degraded_text


# ------------------------------------------------------------------ #
# 5. Evidence 格式                                                    #
# ------------------------------------------------------------------ #


def test_sentiment_evidence_format():
    """情感分析结果正确转为 4 条 Evidence：id 续排、metric、value、note。"""
    node = SentimentAnalystNode(Settings())
    results = [_discussion_result()]
    analysis = {
        "sentiment_score": 0.62,
        "bullish_ratio": 0.45,
        "bearish_ratio": 0.30,
        "neutral_ratio": 0.25,
        "key_themes": ["财报预期"],
        "summary": "整体偏乐观",
    }
    evidence = node._build_sentiment_evidence(analysis, results, n_posts=2)
    metrics = [e.metric for e in evidence]
    assert metrics == ["sentiment_bullish", "sentiment_bearish", "sentiment_neutral", "sentiment_score"]
    values = {e.metric: e.value for e in evidence}
    assert values["sentiment_bullish"] == pytest.approx(0.45)
    assert values["sentiment_bearish"] == pytest.approx(0.30)
    assert values["sentiment_neutral"] == pytest.approx(0.25)
    assert values["sentiment_score"] == pytest.approx(0.62)
    assert all(e.status == "success" for e in evidence)
    assert all(e.id.startswith("sentiment-") for e in evidence)
    assert all(e.instrument == "SH600519" for e in evidence)
    assert all(e.source_tool == "xq_discussions" for e in evidence)
    # id 与基类 build_evidence 的编号账本续排、不冲突
    from app.research.evidence import build_evidence

    base_ids = {e.id for e in build_evidence(results, id_prefix="sentiment")}
    assert base_ids.isdisjoint({e.id for e in evidence})
    # composite 条目 note 带主题与 summary
    score_note = next(e.note for e in evidence if e.metric == "sentiment_score")
    assert "关键主题: 财报预期" in score_note


# ------------------------------------------------------------------ #
# 6. SENTIMENT_MAX_COMMENTS 截断                                      #
# ------------------------------------------------------------------ #


def test_sentiment_max_comments_truncation():
    """帖子数超过 settings.sentiment_max_comments 时硬截断到上限。"""
    settings = Settings(sentiment_max_comments=3)
    node = SentimentAnalystNode(settings)
    results = [_discussion_result(n_posts=10)]
    posts = node._extract_posts(results)
    assert len(posts) == 3
    assert [p["text"] for p in posts] == [f"评论样本第{i}条：看涨，业绩超预期" for i in range(3)]


# ------------------------------------------------------------------ #
# 7. LLM JSON 解析                                                    #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_sentiment_llm_parse():
    """LLM 返回的 JSON 正确解析为情感指标；越界值 clamp、非法类型回默认。"""
    node = SentimentAnalystNode(Settings())
    node.client = _make_fake_openai(_SENTIMENT_LLM_JSON)
    posts = [{"text": "看涨", "created_at": "2026-07-20T10:00:00"}]
    data = await node._analyze_sentiment(posts)
    assert data["sentiment_score"] == pytest.approx(0.62)
    assert data["bullish_ratio"] == pytest.approx(0.45)
    assert data["key_themes"] == ["财报预期", "政策利好"]

    node.client = _make_fake_openai('{"sentiment_score": 1.7, "bullish_ratio": "bad", "bearish_ratio": 0.1, "neutral_ratio": 0.2}')
    data = await node._analyze_sentiment(posts)
    assert data["sentiment_score"] == 1.0  # clamp 上界
    assert data["bullish_ratio"] == 0.5  # 非法类型 → 默认
    assert data["key_themes"] == []


# ------------------------------------------------------------------ #
# 8. 路由候选                                                          #
# ------------------------------------------------------------------ #


def test_sentiment_route_includes_sentiment_category():
    """route_candidate_categories 开关注入顺序：news 在前、sentiment 在后；关闭时无。"""
    assert route_candidate_categories(Settings(sentiment_enabled=True)) == (
        "technical",
        "fundamental",
        "moneyflow",
        "sentiment",
    )
    both = Settings(news_enabled=True, sentiment_enabled=True)
    assert route_candidate_categories(both) == ("technical", "fundamental", "moneyflow", "news", "sentiment")
    assert "sentiment" not in route_candidate_categories(Settings())


# ------------------------------------------------------------------ #
# 9. 市场级最低证据集回归                                              #
# ------------------------------------------------------------------ #


def test_market_summary_minimum_no_sentiment():
    """市场级最低证据集**不**引入 sentiment 分析师的工具（xq_discussions）。

    注意：最低证据集中已有的 ``sentiment`` key 是市场情绪聚合工具
    （get_ashare_sentiment），与本分析师无关，保持原样。
    """
    minimum_keys = {tool_key for tool_key, _args, _purpose in MARKET_SUMMARY_MINIMUM}
    assert "xq_discussions" not in minimum_keys
    assert "timeline" not in minimum_keys


# ------------------------------------------------------------------ #
# 附加：端到端节点执行（fanout 分配给 sentiment 时全链路跑通）          #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_sentiment_node_end_to_end(monkeypatch):
    """route 分配 xq_discussions → 节点执行工具、跑 fake LLM、产出 sentiment_* 证据。"""
    node = SentimentAnalystNode(Settings(sentiment_enabled=True))
    node.client = _make_fake_openai(_SENTIMENT_LLM_JSON)

    result_holder = _discussion_result()

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        r = result_holder.model_copy(deep=True)
        r.tool_key = "xq_discussions"
        return r

    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", fake_execute)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, r: None)

    state = {
        "question": "贵州茅台600519散户情绪如何",
        "domain": "a_share",
        "route": [
            {
                "analyst": "sentiment",
                "tool_calls": [{"tool_key": "xq_discussions", "arguments": {"symbol": "SH600519"}, "purpose": "舆情"}],
                "budget": 1,
            }
        ],
    }
    out = await node(state)

    assert not out["errors"]
    finding = out["findings"][0]
    assert finding["analyst"] == "sentiment"
    assert finding["failed"] is False
    assert "雪球评论2条" in finding["digest"]
    metrics = [e.metric for e in out["evidence"]]
    assert "sentiment_score" in metrics
    assert "sentiment_bullish" in metrics
