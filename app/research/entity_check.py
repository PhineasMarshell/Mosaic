"""实体校验：分别记录证券存在、名称代码匹配和本次行情数据。

常见股票静态表只用作名称抽取提示。校验来源是带来源/日期的全 A 股
code/name 快照，或成功工具调用实际返回的结构化数据。新闻文本只算提及，
错误响应和请求参数不能证明实体。没有校验源时保持未验证。
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.gateway.stock_codes import STOCK_CODE_MAP_A_SHARE
from app.research.entity_registry import RegistrySnapshot, normalize_security_code

#: 中文公司/机构名后缀。用它把"行云科技"这类自由文本里的实体名切出来。
#: 纯靠后缀会产生假阳性（例如"科技创新"），因此命中后仍要走校验源判定。
_ENTITY_SUFFIXES = (
    "股份", "科技", "集团", "控股", "实业", "银行", "证券", "保险", "医药", "生物",
    "电子", "能源", "电力", "汽车", "传媒", "材料", "重工", "航空", "港口", "农业",
    "食品", "通信", "网络", "软件", "智能", "新材", "环保", "建设", "工程", "机械",
    "化工", "有色", "钢铁", "煤炭", "石油", "燃气", "水务", "交通", "铁路", "船舶",
    "军工", "航天", "半导体", "芯片", "光电", "激光", "仪器", "电源", "电气", "电机",
    "电缆", "地产", "置业", "物流", "纺织", "服装", "家电", "家具", "牧业", "渔业",
    "种业", "化肥", "农药", "酒业", "乳业", "旅游", "酒店", "商业", "贸易", "投资",
    "资本", "信托", "租赁", "矿业", "黄金", "铝业", "铜业", "水泥", "玻璃", "造纸",
    "包装", "印刷", "数据", "信息",
)

_SUFFIX_RE = re.compile("|".join(_ENTITY_SUFFIXES))
_NAME_CHAR_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")
#: 实体名里不可能出现的连接词/标点。没有它，"宁德时代与行云科技" 会被贪婪匹配成
#: 一个横跨两个公司的假实体（那样真正的两个名字反而都检测不到）。
_ENTITY_SEPARATORS = frozenset("与和及暨或在的、，。；：:;,.")
#: 后缀往前最多回看的字数（"行云科技股份" 这类名字够用）。
_MAX_NAME_TAIL = 6
_LEADING_SYNTAX = ("包括", "例如", "涉及", "诸如", "以及", "其中", "还有", "比如")
_THEMES = frozenset({"商业航天", "人工智能", "低空经济", "算力", "机器人", "半导体", "芯片", "新能源", "军工"})
# Extraction hints only. Their presence does not verify listing or code/name identity.
_NON_SUFFIX_HINTS = frozenset({"寒武纪"})
_NAME_KEYS = frozenset({"name", "stock_name", "company_name", "security_name", "sec_name"})
_CODE_KEYS = frozenset({"code", "symbol", "stock_code", "security_code", "ticker"})
_THEME_KEYS = frozenset({"sector", "industry", "theme", "concept", "sector_name", "concept_name"})
_MARKET_KEYS = frozenset({"close", "last", "price", "change", "change_pct", "pct_change", "return"})
_ENTITY_TOOLS = (
    "quote", "kline", "f10", "finance", "news", "telegraph", "search", "limit_up", "longhu",
    "abnormal", "company", "stock", "filing", "business", "shareholder", "survey",
)

#: "这个实体不存在"一类断言的措辞。
_NONEXISTENCE_RE = re.compile(r"(不存在|未上市|查无|没有这只股票|不是上市公司|已退市|无此公司)")

#: 报告里**用户可见**的单值文本字段（实体校验要完整扫一遍，不能只扫 what_happened）。
_REPORT_TEXT_FIELDS = ("title", "market_state", "state_label", "what_happened", "confidence")
#: 报告里用户可见的列表字段。
_REPORT_LIST_FIELDS = (
    "why",
    "strong_areas",
    "what_changed",
    "what_matters",
    "risks",
    "data_caveats",
)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict 或对象读取字段 —— LangGraph 可能传入任一形式。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dump(obj: Any) -> Any:
    """把 pydantic 对象 / dict 统一成可 json.dumps 的形式。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


@lru_cache(maxsize=1)
def local_entity_index() -> dict[str, str]:
    """常见名称抽取提示；这张表不能证明 A 股存在或覆盖范围。"""
    return {
        name: code
        for name, code in STOCK_CODE_MAP_A_SHARE.items()
        if code.startswith(("SH", "SZ", "BJ"))
    }


def _structured_entity_rows(evidence: Any) -> tuple[list[dict[str, Any]], set[str]]:
    """Read only successful returned datums; request arguments are never proof."""
    rows: list[dict[str, Any]] = []
    themes: set[str] = set()
    for item in evidence or []:
        if _field(item, "status", "success") not in ("success", "partial"):
            continue
        tool = str(_field(item, "source_tool", _field(item, "tool", "")) or "").lower()
        if not any(token in tool for token in _ENTITY_TOOLS):
            continue
        datums = _field(item, "normalized")
        if datums is None:
            datums = [item]
        grouped: dict[str, dict[str, Any]] = {}
        for datum in datums:
            if _field(datum, "status", "success") not in ("success", "partial"):
                continue
            metric = str(_field(datum, "metric", "") or "")
            value = _field(datum, "value")
            if value is None or value == "":
                continue
            key = metric.rsplit(".", 1)[-1].lower()
            if key in _THEME_KEYS or ("sector" in tool and key == "name"):
                themes.add(str(value).strip())
                continue
            row_key = metric.rsplit(".", 1)[0] if "." in metric else "root"
            row = grouped.setdefault(row_key, {
                "tool": tool,
                "source": _field(datum, "source", _field(item, "source")) or tool,
                "as_of": _field(datum, "timestamp", _field(item, "timestamp")),
            })
            if key in _NAME_KEYS and not any(token in tool for token in ("news", "telegraph")):
                row["name"] = str(value).strip()
            elif key in _CODE_KEYS:
                row["code"] = normalize_security_code(value)
            elif key not in {"tool_status", "error", "query", "q"}:
                row["datum"] = True
                if key in _MARKET_KEYS and any(token in tool for token in ("quote", "kline")):
                    row["market_datum"] = True
            instrument = _field(datum, "instrument", _field(item, "instrument"))
            if instrument:
                row["instrument_code"] = normalize_security_code(instrument)
        rows.extend(grouped.values())
    return rows, themes


def evidence_entity_names(evidence: Any, registry: RegistrySnapshot | None = None) -> set[str]:
    rows, _ = _structured_entity_rows(evidence)
    names = {row["name"] for row in rows if row.get("name")}
    if registry and registry.available:
        for row in rows:
            code = row.get("code") or row.get("instrument_code")
            if code in registry.mapping and (row.get("name") or row.get("datum")):
                names.add(registry.mapping[code])
    return names


def _entity_names_with_positions(text: str) -> list[tuple[int, str]]:
    """找出所有"X 公司后缀"形式的实体名（返回 ``(起始位置, 名称)``）。

    做法是**先定位后缀、再往前回看**，而不是用一个贪婪正则去吞字符：
    "宁德时代与行云科技同时上涨" 里的连接词会中止回看，因此切出来的是
    "行云科技"，而不是横跨两个公司的 "宁德时代与行云科技"。
    """
    found: list[tuple[int, str]] = []
    for match in _SUFFIX_RE.finditer(text):
        cursor = match.start()
        taken = 0
        while cursor > 0 and taken < _MAX_NAME_TAIL:
            char = text[cursor - 1]
            if char in _ENTITY_SEPARATORS or not _NAME_CHAR_RE.match(char):
                break
            cursor -= 1
            taken += 1
        if taken >= 2:
            name = text[cursor : match.end()]
            for lead in _LEADING_SYNTAX:
                if name.startswith(lead):
                    cursor += len(lead)
                    name = name[len(lead):]
                    break
            if len(name) >= 3:
                found.append((cursor, name))
    return found


def extract_entity_mentions(text: str, *, known_index: dict[str, str] | None = None) -> list[str]:
    """从文本里切出候选实体名，按出现位置排序、去重。

    排序保证同一份报告每次得到同样的列表（便于断言与日志比对）；
    同一位置取更长的名字，并丢弃被更长名字包含的短名
    （"行云科技" ⊂ "行云科技股份"）。
    """
    if not text:
        return []
    index = local_entity_index() if known_index is None else known_index
    positions: dict[str, int] = {}
    for name in set(index) | _NON_SUFFIX_HINTS:
        if len(name) < 2:
            continue
        pos = text.find(name)
        if pos >= 0:
            positions.setdefault(name, pos)
    for pos, name in _entity_names_with_positions(text):
        positions.setdefault(name, pos)
    ordered = [name for name, _ in sorted(positions.items(), key=lambda kv: (kv[1], -len(kv[0])))]
    return [
        name for name in ordered
        if name not in _THEMES and not any(name != other and name in other for other in ordered)
    ]


@dataclass(frozen=True)
class EntityAssessment:
    name: str
    existence: str
    code_match: str
    market_evidence: bool
    source: str | None = None
    as_of: str | None = None
    code: str | None = None
    conflict: str | None = None
    authority: str | None = None


def assess_report_entities(
    text: str,
    evidence: Any = (),
    *,
    registry: RegistrySnapshot | None = None,
    known_index: dict[str, str] | None = None,
) -> list[EntityAssessment]:
    """Keep existence, code/name match, and market datum as separate facts."""
    rows, themes = _structured_entity_rows(evidence)
    news_texts: list[str] = []
    for item in evidence or []:
        if _field(item, "status", "success") not in ("success", "partial"):
            continue
        tool = str(_field(item, "source_tool", _field(item, "tool", "")) or "").lower()
        if not any(token in tool for token in ("news", "telegraph")):
            continue
        datums = _field(item, "normalized")
        for datum in datums if datums is not None else [item]:
            if _field(datum, "status", "success") in ("success", "partial"):
                value = _field(datum, "value")
                if value is not None:
                    news_texts.append(json.dumps(value, ensure_ascii=False, default=str))
    hints = dict(local_entity_index() if known_index is None else known_index)
    if registry and registry.available:
        hints.update(registry.name_to_code)
    for row in rows:
        if row.get("name"):
            hints.setdefault(row["name"], row.get("code") or row.get("instrument_code") or "")
    assessments: list[EntityAssessment] = []
    for name in extract_entity_mentions(text, known_index=hints):
        if name in themes:
            continue
        registry_code = registry.name_to_code.get(name) if registry and registry.available else None
        matching = [row for row in rows if row.get("name") == name]
        code_rows = [
            row for row in rows
            if registry_code and (row.get("code") or row.get("instrument_code")) == registry_code
        ]
        row = (matching or code_rows or [None])[0]
        code = (row.get("code") or row.get("instrument_code")) if row else None
        conflict = None
        reference_discrepancy = False
        if row and registry and registry.available and code:
            registered_name = registry.mapping.get(code)
            if registered_name and row.get("name") and registered_name != row["name"]:
                if registry.authority == "authoritative":
                    conflict = f"{code}: {row['name']} != {registered_name}"
                else:
                    reference_discrepancy = True
        existence = "verified" if registry_code or matching or code_rows else "unverified"
        if existence == "unverified" and any(name in story for story in news_texts):
            existence = "mentioned_only"
        if conflict:
            existence = "contradicted"
        code_match = "unverified"
        if conflict:
            code_match = "contradicted"
        elif reference_discrepancy:
            code_match = "unverified"
        elif row and row.get("name") and code:
            code_match = "verified"
        elif registry_code and code == registry_code:
            code_match = "verified"
        source = (row.get("source") or row.get("tool")) if row else None
        if registry_code and not source:
            source = registry.source
        assessments.append(EntityAssessment(
            name=name,
            existence=existence,
            code_match=code_match,
            market_evidence=bool(
                row and (row.get("market_datum") or "limit_up_stocks" in row.get("tool", ""))
            ),
            source=source,
            as_of=(row.get("as_of") if row else None) or (registry.as_of if registry_code and registry else None),
            code=code or registry_code,
            conflict=conflict,
            authority=registry.authority if registry else None,
        ))
    return assessments


def check_report_entities(
    text: str,
    evidence: Any = (),
    *,
    known_index: dict[str, str] | None = None,
    registry: RegistrySnapshot | None = None,
) -> tuple[list[str], list[str]]:
    """校验报告文本里的实体，返回 ``(unverified_entities, review_lines)``。

    ``review_lines`` 包含来源和日期；未验证不代表不存在。
    """
    assessments = assess_report_entities(text, evidence, registry=registry, known_index=known_index)
    unverified = [item.name for item in assessments if item.existence in ("unverified", "mentioned_only")]
    lines: list[str] = []
    for item in assessments:
        if item.existence in ("unverified", "mentioned_only"):
            lines.append(f"  - {item.name} → **无校验源**：当前未验证，不能推断不存在")
        else:
            lines.append(
                f"  - {item.name} → 本地映射/实际数据校验；existence={item.existence}; "
                f"code/name={item.code_match}; market_evidence={item.market_evidence}; "
                f"source={item.source or 'unknown'}; as_of={item.as_of or 'unknown'}"
            )
    return unverified, lines


def nonexistence_assertions(text: str, entities: Any) -> list[str]:
    """找出报告对 ``entities`` 做出的"不存在 / 未上市"类断言涉及的实体名。

    无校验源的实体被断言"不存在"是最典型的实体幻觉：报告把"我没查到"说成了
    "这东西不存在"。这一步把它变成代码级事实，交给 Critic 强制降级措辞。
    """
    hits: list[str] = []
    for name in entities or []:
        name = str(name)
        if not name:
            continue
        for match in re.finditer(re.escape(name), text or ""):
            window = text[match.end() : match.end() + 24]
            if _NONEXISTENCE_RE.search(window):
                hits.append(name)
                break
    return hits


def report_text(report: Any) -> str:
    """把报告里所有**用户可见**的文字字段拼成一段，供实体校验扫描。"""
    parts: list[str] = []
    for key in _REPORT_TEXT_FIELDS:
        value = _field(report, key)
        if value:
            parts.append(str(value))
    for key in _REPORT_LIST_FIELDS:
        for item in _field(report, key) or []:
            if item:
                parts.append(str(item))
    for claim in _field(report, "claims") or []:
        claim_text = _field(claim, "claim")
        if claim_text:
            parts.append(str(claim_text))
    return "\n".join(parts)
