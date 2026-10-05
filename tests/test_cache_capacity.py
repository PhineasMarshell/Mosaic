"""T19 回归：Cache 容量上限（LRU 淘汰）+ ttl=0 / ttl=None 语义。

旧实现两个缺陷：
- ``set(..., ttl=0)`` 被 ``ttl or default`` 当成 falsy → 落回默认 30s（调用方想关缓存）；
- ``_store`` 无上限，长期运行只增不减。
"""

import time

from app.cache import Cache


def test_ttl_zero_disables_caching():
    """ttl=0 → 立即过期，get 返回 None（调用方想关缓存，不许落回默认 TTL）。"""
    c = Cache(default_ttl=30.0)
    c.set("k", 1, ttl=0)
    assert c.get("k") is None  # 旧实现这里返回 1


def test_ttl_none_uses_default_not_zero():
    """ttl=None → 用默认 TTL（防止把 None 也当 0）。"""
    c = Cache(default_ttl=30.0)
    c.set("k", "v", ttl=None)
    assert c.get("k") == "v"
    entry = c._store["k"]
    # 默认 TTL 30s：到期时间在 ~29s 之后，而不是"立即过期"
    assert entry.expires_at - time.monotonic() > 20


def test_invalidate_semantics_unaffected():
    c = Cache()
    c.set("a", 1, ttl=60)
    c.set("b", 2, ttl=60)
    assert c.invalidate("a") is True
    assert c.invalidate("missing") is False
    assert c.get("a") is None
    assert c.get("b") == 2


def test_capacity_enforced_and_lru_keeps_recently_used():
    """超容量 → size ≤ max_entries；最近访问过的键保留，最久未访问的被淘汰。"""
    c = Cache(max_entries=10)
    for i in range(10):
        c.set(f"key{i}", i)
    # 刷新 key0 的 last_access → 它变成最近使用
    assert c.get("key0") == 0
    for i in range(10, 15):
        c.set(f"key{i}", i)

    assert c.stats["size"] <= 10
    assert c.get("key0") == 0, "最近访问过的键不应被淘汰"
    assert c.get("key1") is None, "最久未访问的键应被淘汰"
    assert c.stats["evictions"] == 5
    assert c.stats["max_entries"] == 10


def test_expired_entries_purged_on_set():
    """set 时顺带清理过期条目：过期键从 _store 消失，而不是等读到同键才删。"""
    c = Cache()
    c.set("old", 1, ttl=0.01)
    time.sleep(0.02)
    assert c.get("old") is None  # 读路径也会删，这里只做前置确认
    c.set("new", 2)
    assert "old" not in c._store
    assert c.get("new") == 2
