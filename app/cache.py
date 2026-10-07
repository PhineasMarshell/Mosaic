"""Cache — Mosaic 内存 TTL 缓存。

减少重复工具调用，避免对同一数据源的冗余请求。

缓存层级：
- 短缓存（5-30s）：实时行情、Crypto 快照
- 中缓存（30s-数分钟）：市场情绪、涨停池、Crypto K线/衍生品
- 长缓存（小时级）：公司基本面资料、交易所列表

注意：缓存仅用于降低重复请求，不能把旧数据包装成"刚刚发生"。
每次推理必须标注数据来源和时间戳。

用法：

    from app.cache import market_cache

    cached = market_cache.get(key)
    if cached is None:
        data = fetch_from_gateway(...)
        market_cache.set(key, data, ttl=30)
"""

import logging
import time
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _Entry(Generic[T]):
    """带 TTL 的单条缓存条目。"""

    __slots__ = ("value", "expires_at", "last_access")

    def __init__(self, value: T, ttl_seconds: float) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds
        # T19：LRU 淘汰依据——get 命中时刷新
        self.last_access = time.monotonic()

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class Cache:
    """简单内存 TTL 缓存。

    T19：容量有上限（LRU 淘汰），长期运行的进程里 ``_store`` 不再只增不减。
    """

    def __init__(self, default_ttl: float = 30.0, max_entries: int = 512) -> None:
        self._default_ttl = default_ttl
        self._max_entries = max(1, int(max_entries))
        self._store: dict[str, _Entry[Any]] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.is_expired:
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        entry.last_access = time.monotonic()
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        # T19：ttl=0（调用方想关缓存）不得被 ``or`` 当成 falsy 落回默认 TTL；
        # ttl=None 才表示"用默认值"。
        effective_ttl = ttl if ttl is not None else self._default_ttl
        self._purge_expired()
        self._store[key] = _Entry(value, effective_ttl)
        self._evict_over_capacity()

    def invalidate(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def _purge_expired(self) -> None:
        """set 时顺带清理已过期条目（旧实现只在读到同键时才删）。"""
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if e.expires_at <= now]
        for k in expired:
            del self._store[k]

    def _evict_over_capacity(self) -> None:
        """超过 max_entries 时按 last_access 淘汰最久未访问的条目（LRU）。"""
        overflow = len(self._store) - self._max_entries
        if overflow <= 0:
            return
        by_lru = sorted(self._store.items(), key=lambda kv: kv[1].last_access)
        for k, _ in by_lru[:overflow]:
            del self._store[k]
        self._evictions += overflow
        logger.debug(
            "Cache evicted %d entries (size=%d, max_entries=%d)",
            overflow,
            len(self._store),
            self._max_entries,
        )

    @property
    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        rate = round(self._hits / total * 100, 0) if total else 0
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": int(rate),
            "max_entries": self._max_entries,
            "evictions": self._evictions,
        }


# ---------------------------------------------------------
# 预定义缓存策略 — A 股
# ---------------------------------------------------------

QUOTE_TTL = 10.0  # 实时行情 10 秒
SENTIMENT_TTL = 60.0  # 市场情绪 60 秒
LIMIT_UP_TTL = 60.0  # 涨停生态 60 秒
OVERVIEW_TTL = 120.0  # 全市场概览 120 秒
LONGHU_TTL = 300.0  # 龙虎榜 300 秒


# ---------------------------------------------------------
# 预定义缓存策略 — Crypto
# ---------------------------------------------------------

SNAPSHOT_TTL = 15.0  # Crypto 快照 15 秒（价格变化快）
KLINE_TTL = 60.0  # K 线 60 秒（7x24 市场，较短）
DERIVATIVES_TTL = 120.0  # 衍生品数据 120 秒（OI/Funding 更新较慢）
FUNDING_RATE_TTL = 180.0  # 资金费率 180 秒
LIQUIDATION_TTL = 30.0  # 今日爆仓 30 秒（事件驱动型）
LIQMAP_TTL = 300.0  # 清算地图 300 秒（大户仓位变化慢）
TOP_POSITION_TTL = 300.0  # 头部持仓 300 秒
EXCHANGES_TTL = 3600.0  # 交易所列表小时级（几乎不变）


# ---------------------------------------------------------
# 预定义缓存策略 — US Stock / Macro（占位）
# ---------------------------------------------------------

US_QUOTE_TTL = 10.0  # US Stock 实时行情 10 秒
US_OVERVIEW_TTL = 60.0  # US Stock 概览 60 秒
MACRO_TTL = 3600.0  # 宏观数据小时级（低频变化）


def _make_cache_key(tool: str, arguments: dict) -> str:
    """生成缓存键。"""
    sorted_args = "|".join(f"{k}={v}" for k, v in sorted(arguments.items()))
    return f"{tool}:{sorted_args}"


def _resolve_ttl(tool: str, settings=None) -> float:
    """根据工具名选择 TTL。

    T17：``news_search`` 的 6 小时 TTL 来自 settings（DDGS 限流保护），
    不能落进 30 秒默认值——旧实现 ``execute()`` 会用它重新 set 同一个 key，
    把内部工具写入的 21600s 覆盖掉。settings 未传时保持旧返回值（测试依赖）。
    """
    if tool == "news_search" and settings is not None:
        return float(settings.news_search_ttl_seconds)
    mapping = {
        # A 股
        "get_market_quotes": QUOTE_TTL,
        "get_ashare_sentiment": SENTIMENT_TTL,
        "get_limit_up_count": LIMIT_UP_TTL,
        "list_limit_up_sectors": LIMIT_UP_TTL,
        "list_limit_up_stocks": LIMIT_UP_TTL,
        "get_company_overview": OVERVIEW_TTL,
        "get_stock_longhu": LONGHU_TTL,
        # Crypto — K 线 & 快照
        "get_market_klines": KLINE_TTL,
        "get_market_snapshot": SNAPSHOT_TTL,
        # Crypto — 衍生品
        "get_derivatives_history": DERIVATIVES_TTL,
        "list_crypto_funding_rates": FUNDING_RATE_TTL,
        "get_crypto_liquidation_today": LIQUIDATION_TTL,
        "get_hyperliquid_liquidation_map": LIQMAP_TTL,
        "get_hyperliquid_top_position": TOP_POSITION_TTL,
        "list_exchanges": EXCHANGES_TTL,
        # Health checks — 较长缓存（不频繁调用）
        "get_service_health": 600.0,
        "get_market_health": 300.0,
    }
    return mapping.get(tool, 30.0)


# 全局缓存实例
market_cache = Cache()
