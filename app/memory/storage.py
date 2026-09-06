"""Market Memory — 市场记忆持久化。

保存每日 Market State、主题强弱、异常记录、重要变化和用户研究记录，
支持跨日历史比较（PRD §38-39）。

存储方案：JSON 文件（轻量 MVP，后续可换 SQLite/PostgreSQL）
存储路径：~/.mosaic/memory/ 或项目内 .mosaic_memory/

核心功能：
- save_daily_state: 保存每日 Market State 快照
- get_recent_states: 获取最近 N 天的状态（用于对比变化）
- record_anomaly: 记录异常事件
- get_anomalies: 查询历史异常
- save_research: 保存用户研究记录
- search_context: 根据关键词搜索记忆内容（给 Reasoning 注入历史上下文）
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认存储位置
DEFAULT_MEMORY_DIR = Path.home() / ".mosaic" / "memory"


class MarketMemory:
    """市场记忆系统。

    使用目录结构组织数据：
    ~/.mosaic/memory/
    ├── daily/         # 每日 Market State
    │   └── 2025-09-05.json
    ├── anomalies/     # 异常记录
    │   └── 2025-09-05.json
    └── research/      # 用户研究记录
        └── 2025-09-05_163247.json
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or DEFAULT_MEMORY_DIR
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for sub in ("daily", "anomalies", "research"):
            (self.base_dir / sub).mkdir(parents=True, exist_ok=True)

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    # ── Daily State ────────────────────────

    def save_daily_state(self, date: str | None = None, data: dict[str, Any] | None = None) -> str:
        """保存每日 Market State 快照。

        Args:
            date: YYYY-MM-DD 格式日期；None → 今天
            data: {a_share_state, crypto_state, themes, strong_areas, ...}

        Returns:
            保存的文件路径
        """
        date = date or self._today()
        filename = f"{date}.json"
        filepath = self.base_dir / "daily" / filename

        if filepath.exists():
            try:
                existing = json.loads(filepath.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing.update(data)
                data = existing
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read existing state %s: %s", filepath, exc)

        data = data or {}
        data.setdefault("_saved_at", datetime.now(UTC).isoformat())
        data.setdefault("_version", "0.1")

        try:
            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            logger.info("Saved daily state for %s → %s", date, filepath)
        except OSError as exc:
            logger.error("Failed to save daily state: %s", exc)

        return str(filepath)

    def get_daily_state(self, date: str | None = None) -> dict[str, Any] | None:
        """读取指定日期的 Market State。"""
        date = date or self._today()
        filepath = self.base_dir / "daily" / f"{date}.json"
        if not filepath.exists():
            return None
        try:
            return json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read daily state %s: %s", filepath, exc)
            return None

    def get_recent_states(self, days: int = 7, domain: str | None = None) -> list[dict[str, Any]]:
        """获取最近 N 天的 Market State。

        Returns:
            [{date, a_share_state, crypto_state, ...}, ...] 按日期倒序
        """
        states: list[dict[str, Any]] = []
        daily_dir = self.base_dir / "daily"

        if not daily_dir.is_dir():
            return states

        for fpath in sorted(daily_dir.glob("*.json"), reverse=True)[:days]:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                data["_file_date"] = fpath.stem
                if domain is None or data.get(domain.replace("_", "-")):
                    states.append(data)
            except (json.JSONDecodeError, OSError):
                continue

        return states

    # ── Anomalies ──────────────────────────

    def record_anomaly(self, anomaly_data: dict[str, Any], date: str | None = None) -> str:
        """记录一条异常事件。

        Args:
            anomaly_data: AnomalyRecord.to_dict() 的内容
            date: YYYY-MM-DD 格式日期

        Returns:
            保存路径
        """
        date = date or self._today()
        filepath = self.base_dir / "anomalies" / f"{date}.json"

        records: list[dict[str, Any]] = []
        if filepath.exists():
            try:
                records = json.loads(filepath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                records = []

        records.append({**anomaly_data, "_recorded_at": datetime.now(UTC).isoformat()})
        filepath.write_text(
            json.dumps(records, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(filepath)

    def get_anomalies(self, since: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """查询历史异常。

        Args:
            since: YYYY-MM-DD 格式，包含该日期之后（含当天）的异常
            limit: 最多返回多少条

        Returns:
            [{..., _source_file: "..."} , ...] 按时间倒序
        """
        anomalies: list[dict[str, Any]] = []
        anomalies_dir = self.base_dir / "anomalies"

        if not anomalies_dir.is_dir():
            return anomalies

        for fpath in sorted(anomalies_dir.glob("*.json"), reverse=True):
            file_date = fpath.stem
            if since and file_date < since:
                break

            try:
                records = json.loads(fpath.read_text(encoding="utf-8"))
                for rec in records:
                    rec["_source_file"] = str(fpath.relative_to(self.base_dir))
                anomalies.extend(records[:limit])
            except (json.JSONDecodeError, OSError):
                continue

        return anomalies[:limit]

    # ── Research History ───────────────────

    def save_research(self, question: str, response: dict[str, Any], user_id: str | None = None) -> str:
        """保存一次用户研究的完整记录。

        用于后续回溯和趋势分析。
        """
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        uid = (user_id or "anonymous").replace("/", "_")
        filename = f"{ts}_{uid}.json"
        filepath = self.base_dir / "research" / filename

        record = {
            "question": question,
            "response": response,
            "_timestamp": datetime.now(UTC).isoformat(),
            "_version": "0.1",
        }

        try:
            filepath.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            logger.info("Saved research record for '%s' → %s", question[:40], filepath)
        except OSError as exc:
            logger.error("Failed to save research: %s", exc)

        return str(filepath)

    # ── Search / Context ───────────────────

    def get_context_for_question(self, question: str, days_back: int = 7) -> str:
        """为给定问题生成历史上下文文本（供 Reasoning Prompt 注入）。

        提取最近几天的 Market State 摘要，帮助 AI 进行跨日比较。
        """
        states = self.get_recent_states(days=days_back)
        if not states:
            return ""

        lines = ["--- 过去 {} 天 Market State 快照 ---".format(len(states))]
        for s in states[:8]:
            date = s.get("_file_date", "?")
            a_state = s.get("a_share_state", s.get("a-share", s.get("aShare")))
            c_state = s.get("crypto_state", s.get("crypto", s.get("c-state")))
            themes = s.get("strong_areas", s.get("themes", []))
            parts = []
            if a_state:
                parts.append(f"A股={a_state}")
            if c_state:
                parts.append(f"Crypto={c_state}")
            if themes:
                theme_str = ", ".join(themes[:3])
                parts.append(f"Themes=[{theme_str}]")
            lines.append(f"{date}: {' · '.join(parts)}")

        lines.append("--- 历史上下文结束 ---")
        return "\n".join(lines)
