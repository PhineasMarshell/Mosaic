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

import time
from typing import Any, Generic, TypeVar


T = TypeVar("T")


class _Entry(Generic[T]):
    """带 TTL 的单条缓存条目。"""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: T, ttl_seconds: float) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class Cache:
    """简单内存 TTL 缓存。线程安全按需扩展。"""

    def __init__(self, default_ttl: float = 30.0) -> None:
        self._default_ttl = default_ttl
        self._store: dict[str, _Entry[Any]] = {}
        self._hits = 0
        self._misses = 0

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
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._store[key] = _Entry(value, ttl or self._default_ttl)

    def invalidate(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0

    @property
    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        rate = round(self._hits / total * 100, 1) if total else 0
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": rate,
        }


# ---------------------------------------------------------
# 预定义缓存策略 — A 股
# ---------------------------------------------------------

QUOTE_TTL = 10.0          # 实时行情 10 秒
SENTIMENT_TTL = 60.0      # 市场情绪 60 秒
LIMIT_UP_TTL = 60.0       # 涨停生态 60 秒
OVERVIEW_TTL = 120.0      # 全市场概览 120 秒
LONGHU_TTL = 300.0        # 龙虎榜 300 秒


# ---------------------------------------------------------
# 预定义缓存策略 — Crypto
# ---------------------------------------------------------

SNAPSHOT_TTL = 15.0            # Crypto 快照 15 秒（价格变化快）
KLINE_TTL = 60.0               # K 线 60 秒（7x24 市场，较短）
DERIVATIVES_TTL = 120.0        # 衍生品数据 120 秒（OI/Funding 更新较慢）
FUNDING_RATE_TTL = 180.0       # 资金费率 180 秒
LIQUIDATION_TTL = 30.0         # 今日爆仓 30 秒（事件驱动型）
LIQMAP_TTL = 300.0             # 清算地图 300 秒（大户仓位变化慢）
TOP_POSITION_TTL = 300.0       # 头部持仓 300 秒
EXCHANGES_TTL = 3600.0         # 交易所列表小时级（几乎不变）


# ---------------------------------------------------------
# 预定义缓存策略 — US Stock / Macro（占位）
# ---------------------------------------------------------

US_QUOTE_TTL = 10.0          # US Stock 实时行情 10 秒
US_OVERVIEW_TTL = 60.0       # US Stock 概览 60 秒
MACRO_TTL = 3600.0           # 宏观数据小时级（低频变化）


def _make_cache_key(tool: str, arguments: dict) -> str:
    """生成缓存键。"""
    sorted_args = "|".join(f"{k}={v}" for k, v in sorted(arguments.items()))
    return f"{tool}:{sorted_args}"


def _resolve_ttl(tool: str) -> float:
    """根据工具名选择 TTL。"""
    mapping = {
        # A 股
        "quote_tencent_quote_get": QUOTE_TTL,
        "public_sentiment_ashare_master_sentiment_get": SENTIMENT_TTL,
        "public_limit_up_count_ashare_master_limit_up_count_get": LIMIT_UP_TTL,
        "public_limit_up_sectors_ashare_master_limit_up_sectors_get": LIMIT_UP_TTL,
        "public_limit_up_pool_ashare_master_limit_up_pool_get": LIMIT_UP_TTL,
        "overview_eastmoney_overview_get": OVERVIEW_TTL,
        "longhu_xueqiu_longhu_get": LONGHU_TTL,
        # Crypto — K 线 & 快照
        "klines_market_klines_post": KLINE_TTL,
        "snapshot_market_snapshot_post": SNAPSHOT_TTL,
        # Crypto — 衍生品
        "derivatives_history_market_derivatives_history_post": DERIVATIVES_TTL,
        "funding_rate_coinglass_funding_rate_get": FUNDING_RATE_TTL,
        "liquidation_today_coinglass_liquidation_today_get": LIQUIDATION_TTL,
        "hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get": LIQMAP_TTL,
        "hyperliquid_top_position_coinglass_hyperliquid_top_position_get": TOP_POSITION_TTL,
        "exchanges_market_exchanges_get": EXCHANGES_TTL,
        # Health checks — 较长缓存（不频繁调用）
        "health_health_get": 600.0,
        "health_market_health_get": 300.0,
    }
    return mapping.get(tool, 30.0)


# 全局缓存实例
market_cache = Cache()
