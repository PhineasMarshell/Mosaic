"""T5 回归：truncate 必须保留最新 200、置 partial，且不污染缓存。"""

import pytest

from app.cache import _make_cache_key, market_cache
from app.config import Settings
from app.graph.tool_runtime import ToolRuntime
from app.models.market import STATUS_SUCCESS, NormalizedDatum, ToolResult


def _full_result(n=250) -> ToolResult:
    data = [NormalizedDatum(metric=f"candles[{i}].c", value=i, tool="k") for i in range(n)]
    return ToolResult(tool="k", arguments={}, status=STATUS_SUCCESS, normalized=data)


@pytest.fixture(autouse=True)
def _clear_market_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def test_truncate_keeps_newest_and_reports_partial():
    result = _full_result()
    ToolRuntime(Settings()).truncate(result)

    values = [d.value for d in result.normalized if d.metric.endswith(".c")]
    assert values[0] == 50  # 保留末尾：首条 == 原第 51 条
    assert values[-1] == 249  # 最新一条保留
    note = next(d for d in result.normalized if d.metric == "_truncated_count")
    assert "原始 250" in note.value
    # T5b：note 文案须与实际条数一致——200 条数据 + 本说明条，共 201；
    # 旧文案"已截断至 200"让读者以为最终只剩 200 条。
    assert "保留最新 200 条" in note.value
    assert len(result.normalized) == 201
    assert result.status == "partial"
    assert result.partial is True


def test_truncate_under_cap_unchanged():
    result = _full_result(n=100)
    ToolRuntime(Settings()).truncate(result)
    assert len(result.normalized) == 100
    assert result.status == "success"
    assert not any(d.metric == "_truncated_count" for d in result.normalized)


async def test_cache_not_polluted_by_truncate():
    runtime = ToolRuntime(Settings())

    async def fake_do_execute(tool_name, arguments, deadline=None):
        return _full_result()

    runtime._do_execute = fake_do_execute

    # 第一个消费者：cache miss → 截断工作对象
    r1 = await runtime.execute("k", {}, set())
    runtime.truncate(r1)

    # 第二个消费者：cache hit → 应拿到完整 250 条再截断
    r2 = await runtime.execute("k", {}, set())
    runtime.truncate(r2)

    for consumed in (r1, r2):
        values = [d.value for d in consumed.normalized if d.metric.endswith(".c")]
        assert values[0] == 50 and values[-1] == 249
        note = next(d for d in consumed.normalized if d.metric == "_truncated_count")
        assert "原始 250" in note.value

    # 缓存内部实例保持完整、无截断 note
    internal = market_cache.get(_make_cache_key("k", {}))
    assert len(internal.normalized) == 250
    assert not any(d.metric == "_truncated_count" for d in internal.normalized)
