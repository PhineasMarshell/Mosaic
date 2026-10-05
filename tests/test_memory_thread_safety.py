"""T21 回归：MarketMemory 的 SQLite 连接必须按线程各取一条。

MarketMemory 是进程级单例（``get_memory()``），而 async 端点可能运行在
另一个线程——旧实现整个实例只懒建并缓存**一条**连接，跨线程复用直接抛
``ProgrammingError: SQLite objects created in a thread can only be used
in that same thread``（save_turn 等全部落库路径因此失败）。

覆盖：
- 两个线程各写一次 → 无异常、两条都落库；
- 同线程连接仍被复用（缓存语义不回退）；
- ``close()`` 能关掉**其它线程**创建的连接（threading.local 看不到它们，
  靠连接登记表）；
- ``close()`` 之后同线程再访问 conn 拿到的是新连接（代数失效重建）。
"""

import sqlite3
import threading

import pytest

from app.memory.storage import MarketMemory


@pytest.fixture
def memory(tmp_path) -> MarketMemory:
    return MarketMemory(db_path=tmp_path / "m.db")


def test_save_turn_from_multiple_threads_persists(memory):
    """两个线程各跑一次 save_turn：都不抛 ProgrammingError、两条都落库。"""
    errors: list[Exception] = []
    results: list[int] = []

    def worker(conversation_id: str) -> None:
        try:
            results.append(memory.save_turn(conversation_id, "q", "a"))
        except Exception as exc:  # noqa: BLE001 - 断言用
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"conv-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"跨线程写入抛错：{errors}"
    assert sorted(results) == [1, 1]
    rows = memory.conn.execute("SELECT conversation_id FROM conversations ORDER BY conversation_id").fetchall()
    assert [r[0] for r in rows] == ["conv-0", "conv-1"]


def test_same_thread_connection_is_reused(memory):
    """同一线程内多次访问 conn 必须是同一条（连接复用语义不回退）。"""
    assert memory.conn is memory.conn


def test_close_covers_connections_from_other_threads(memory):
    """close() 必须能关掉**其它线程**创建的连接。"""
    created: list[sqlite3.Connection] = []
    ready = threading.Event()

    def worker() -> None:
        created.append(memory.conn)  # 在工作线程里建连接
        ready.set()

    t = threading.Thread(target=worker)
    t.start()
    assert ready.wait(timeout=10)
    t.join(timeout=10)
    assert memory.conn is not created[0]  # 主线程用的是另一条连接

    memory.close()
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")  # 工作线程的连接已被 close() 关闭


def test_connections_recreated_after_close(memory):
    """close() 之后同线程再访问 conn 拿到新连接（不是已关闭的旧句柄）。"""
    old = memory.conn
    memory.close()
    assert memory.conn is not old
    memory.save_turn("conv-post-close", "q", "a")  # 新连接可正常写入
