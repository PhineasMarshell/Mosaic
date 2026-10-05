"""Market Memory — 市场记忆持久化 (SQLite)。

存储每日 Market State、主题强弱、异常记录、重要变化和用户研究记录（PRD §38-39）。
跨日快照读取由 `get_recent_states` 提供（晨报/晚报模板消费）；**交互式提问不做跨日
上下文注入**——此前用于 Reasoning 注入的 `get_context_for_question` 无生产调用方，
已按 S8 验收裁决删除，不要以"文档承诺"为由重新引入。

存储方案：SQLite WAL 模式（ACID + 并发安全）
存储路径：<项目根>/memory/memory.db
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection as SQLite3Connection
from typing import Any, final
from uuid import uuid4

logger = logging.getLogger(__name__)


def _sqlite_connection_factory(path: Path) -> SQLite3Connection:
    """创建 SQLite 连接，开启 WAL 模式和 JSON1 扩展。

    ``isolation_level=None`` = autocommit。**这不是可选的优化**：
    sqlite3 默认是隐式事务模式，每条 INSERT 会开一个事务，而本模块的四个写入
    方法（save_daily_state / record_anomaly / save_research / save_turn）过去
    都是裸 ``conn.execute(...)``，一次 commit 都没有 —— 于是：

      * 进程退出时整个未提交事务被回滚，当天写入全部丢失；
      * 其它连接（investigate() 自己 new 的 MarketMemory）永远读不到这些数据，
        多轮对话上下文和跨日历史因此一直是空的，且不报任何错。

    实测：迁移到 SQLite 之后 ~/.mosaic/memory.db 四张表长期 0 行，
    而 235 个单元测试全绿（它们只用同一条连接读写，能看见自己未提交的数据）。

    autocommit 的代价是 save_daily_state 的 SELECT→merge→UPSERT 不再是单个
    事务。当前所有调用方都跑在单线程事件循环上、方法内没有 await 点，
    所以构造不出交错；如果将来改用 asyncio.to_thread 或多 worker，
    需要把这段改成单条 UPSERT 或显式 BEGIN IMMEDIATE。
    """
    conn = SQLite3Connection(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # SQLite 3.38+ 内置 json() 函数；旧版本需要加载扩展（极少见）
    return conn


def project_root() -> Path:
    """项目根目录（仓库里 = <repo>；wheel 安装后 = site-packages 的上一级）。"""
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """运行期数据目录，落盘路径的统一入口。

    优先级：配置 `MOSAIC_DATA_DIR` > `<项目根>/memory`。
    容器里代码在 site-packages，项目根之外不可写，所以 Dockerfile 把
    `MOSAIC_DATA_DIR` 指到可写卷（/data）；本地开发不设即保持历史行为。
    """
    try:
        from app.config import get_settings

        raw = (get_settings().mosaic_data_dir or "").strip()
    except Exception:  # noqa: BLE001 — 配置不可用不应阻止默认路径生效
        raw = ""
    return Path(raw).expanduser() if raw else project_root() / "memory"


def _configured_db_path() -> Path | None:
    """从配置读 `MOSAIC_MEMORY_DB`（容器部署用可写卷路径）；未配置返回 None。

    读取失败（缺少依赖、settings 校验不过等）一律退回默认路径，不让配置问题
    变成导入期崩溃——真正的可写性检查交给 sqlite 打开时的报错。
    """
    try:
        from app.config import get_settings

        raw = (get_settings().mosaic_memory_db or "").strip()
    except Exception:  # noqa: BLE001 — 配置不可用不应阻止默认路径生效
        return None
    return Path(raw).expanduser() if raw else None


# ── Schema ────────────────────────────────────────────────────────────────

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS daily_states (
    date        TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    saved_at    TEXT,
    version     TEXT DEFAULT '0.1'
);

CREATE TABLE IF NOT EXISTS anomalies (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    data        TEXT NOT NULL,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS research_records (
    id          TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    user_id     TEXT DEFAULT 'anonymous'
);

CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_index      INTEGER NOT NULL,
    question        TEXT NOT NULL,
    answer_summary  TEXT,
    created_at      TEXT,
    UNIQUE(conversation_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_anomalies_date ON anomalies(date DESC);
CREATE INDEX IF NOT EXISTS idx_research_created ON research_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_conv ON conversations(conversation_id, turn_index);
"""


@final
class MarketMemory:
    """市场记忆系统（SQLite 后端）。

    使用单个数据库文件组织所有数据：
    <项目根>/memory/memory.db

    表结构：daily_states / anomalies / research_records / conversations
    """

    def __init__(self, db_path: Path | None = None):
        # 路径优先级：显式参数 > 配置 MOSAIC_MEMORY_DB > <项目根>/memory/memory.db
        # （P5：从 ~/.mosaic 迁入项目目录；容器部署时项目根在 site-packages 之外，
        # 必须用 MOSAIC_MEMORY_DB 指到可写卷，否则导入期就 PermissionError）
        _project_root = project_root()
        self.db_path = db_path or _configured_db_path() or (_project_root / "memory" / "memory.db")
        # T21：连接按线程各取一条。SQLite 连接默认只能在创建它的线程里使用，
        # 而本实例是进程级单例（get_memory()），async 端点可能运行在另一个
        # 线程——跨线程复用同一条连接会抛 ProgrammingError。
        self._local = threading.local()
        # 所有线程的连接登记表：threading.local 只能看到当前线程，
        # close() 靠它才能覆盖全部连接。
        self._all_conns: list[SQLite3Connection] = []
        self._conns_lock = threading.Lock()
        # close() 后自增：其它线程手里缓存的是已关闭的旧连接，
        # 下次访问 conn 时按代数检测并重建。
        self._generation = 0
        # Ensure directory exists and initialize schema eagerly (before any caller uses conn)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def conn(self) -> SQLite3Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "generation", -1) == self._generation:
            return conn
        conn = _sqlite_connection_factory(self.db_path)
        conn.row_factory = None  # return tuples, not dicts
        self._local.conn = conn
        self._local.generation = self._generation
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def close(self) -> None:
        """关闭**所有**线程的连接（T21：threading.local 只能看到当前线程，靠登记表覆盖全部）。

        close() 后任何线程再访问 conn 都会拿到新连接（按代数检测重建）。
        """
        with self._conns_lock:
            conns = self._all_conns
            self._all_conns = []
            self._generation += 1
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.debug("Ignored error closing sqlite connection: %s", exc)

    def _ensure_schema(self) -> None:
        """初始化数据库 schema（幂等）。"""
        with self.conn as c:
            c.executescript(_INIT_SQL)

    # ── Helpers ─────────────────────────────────

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    # ── Daily State ───────────────────────────

    def save_daily_state(self, date: str | None = None, data: dict[str, Any] | None = None) -> str:
        """保存每日 Market State 快照（upsert，合并字段）。

        Args:
            date: YYYY-MM-DD 格式日期；None → 今天
            data: {a_share_state, crypto_state, themes, strong_areas, ...}

        Returns:
            保存的日期字符串
        """
        date = date or self._today()
        data = dict(data) if data else {}
        ts = datetime.now(UTC).isoformat()
        data.setdefault("_saved_at", ts)
        data.setdefault("_version", "0.1")

        # Read existing to merge (single round-trip)
        existing = self.conn.execute("SELECT data FROM daily_states WHERE date = ?", (date,)).fetchone()

        if existing and existing[0]:
            try:
                merged = json.loads(existing[0])
                if isinstance(merged, dict):
                    merged.update(data)
                data = merged
            except (json.JSONDecodeError, TypeError):
                pass  # Corrupt data → use new data as-is

        self.conn.execute(
            """INSERT INTO daily_states (date, data, saved_at, version)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   data = excluded.data,
                   saved_at = excluded.saved_at,
                   version = excluded.version""",
            (date, json.dumps(data, ensure_ascii=False, default=str), ts, "0.1"),
        )
        logger.info("Saved daily state for %s", date)
        return date

    def get_daily_state(self, date: str | None = None) -> dict[str, Any] | None:
        """读取指定日期的 Market State。"""
        date = date or self._today()
        row = self.conn.execute("SELECT data FROM daily_states WHERE date = ?", (date,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Corrupt daily state for %s", date)
            return None

    def get_recent_states(self, days: int = 7, domain: str | None = None) -> list[dict[str, Any]]:
        """获取最近 N 天的 Market State。

        Returns:
            [{data, _file_date}, ...] 按日期倒序
        """
        rows = self.conn.execute(
            "SELECT date, data FROM daily_states ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()

        states: list[dict[str, Any]] = []
        for date_str, data_json in rows:
            try:
                data = json.loads(data_json)
            except json.JSONDecodeError:
                continue
            if domain is not None:
                key = domain.replace("_", "-")
                if key not in data:
                    continue
            data["_file_date"] = date_str
            states.append(data)
        return states

    # ── Anomalies ─────────────────────────────

    def record_anomaly(self, anomaly_data: dict[str, Any], date: str | None = None) -> str:
        """记录一条异常事件。

        Args:
            anomaly_data: AnomalyRecord.to_dict() 的内容
            date: YYYY-MM-DD 格式日期

        Returns:
            生成的 UUID
        """
        date = date or self._today()
        record_id = str(uuid4())
        ts = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO anomalies (id, date, data, recorded_at) VALUES (?, ?, ?, ?)",
            (record_id, date, json.dumps(anomaly_data, ensure_ascii=False, default=str), ts),
        )
        logger.info("Recorded anomaly %s for %s", record_id[:8], date)
        return record_id

    def get_anomalies(self, since: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """查询历史异常。

        Args:
            since: YYYY-MM-DD 格式，包含该日期之后（含当天）的异常
            limit: 最多返回多少条

        Returns:
            [{data, _source_date}, ...] 按时间倒序
        """
        if since:
            rows = self.conn.execute(
                "SELECT date, data FROM anomalies WHERE date >= ? ORDER BY date DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT date, data FROM anomalies ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [{"_source_date": date_str, **json.loads(data_json)} for date_str, data_json in rows]

    # ── Research History ──────────────────────

    def save_research(self, question: str, response: dict[str, Any], user_id: str | None = None) -> str:
        """保存一次用户研究的完整记录。

        Returns:
            生成的记录 ID
        """
        record_id = str(uuid4())
        ts = datetime.now(UTC).isoformat()
        uid = (user_id or "anonymous").replace("/", "_")
        self.conn.execute(
            """INSERT INTO research_records (id, question, response, created_at, user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (record_id, question, json.dumps(response, ensure_ascii=False, default=str), ts, uid),
        )
        logger.info("Saved research '%s' → %s", question[:40], record_id[:8])
        return record_id

    # ── Conversation History ──────────────────

    def save_turn(self, conversation_id: str, question: str, answer_summary: str) -> str:
        """保存一轮对话（append-store 模式）。

        Returns:
            新轮次索引
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(turn_index), 0) FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        turn_index = row[0] + 1

        ts = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO conversations (id, conversation_id, turn_index, question, answer_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), conversation_id, turn_index, question, answer_summary, ts),
        )
        return turn_index

    def get_conversation_history(self, conversation_id: str, last_n: int = 10) -> str:
        """获取最近 N 轮对话历史，返回纯文本格式供 Prompt 注入。

        Returns:
            格式化的纯文本
        """
        rows = self.conn.execute(
            """SELECT turn_index, question, answer_summary
               FROM conversations
               WHERE conversation_id = ?
               ORDER BY turn_index DESC
               LIMIT ?""",
            (conversation_id, last_n),
        ).fetchall()

        if not rows:
            return ""

        # Reverse to chronological order (oldest first, newest last)
        lines = [f"--- 对话历史 (最后 {len(rows)} 轮) ---"]
        for turn_index, question, answer_summary in reversed(rows):
            lines.append(f"Q{turn_index}: {question}")
            if answer_summary:
                lines.append(f"A{turn_index}: {answer_summary}")

        lines.append("--- 对话历史结束 ---")
        return "\n".join(lines)


# ── 进程内共享实例 ──────────────────────────

_shared_memory: MarketMemory | None = None
_shared_memory_lock = __import__("threading").Lock()


def get_memory() -> MarketMemory:
    """返回进程内共享的 MarketMemory。

    以前每个调用点都自己 ``MarketMemory()``：一次调查里 main.py 的单例、
    investigate() 的对话历史读取等各开一条连接且从不关闭（连接/FD 随请求量增长）。

    更严重的是它和未提交事务叠加后的效果：写入方那条连接能看见自己未提交的
    数据，别的连接看不见 —— 于是多轮对话上下文永远是空字符串，追问功能
    实际上是坏的，而且没有任何报错。

    新代码一律用这个函数，不要再直接构造 MarketMemory()。
    """
    global _shared_memory
    if _shared_memory is None:
        with _shared_memory_lock:
            if _shared_memory is None:
                _shared_memory = MarketMemory()
    return _shared_memory
