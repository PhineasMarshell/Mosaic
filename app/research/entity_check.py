"""实体校验 —— 报告里的公司/指数名是否有校验源（方案 §4 阶段 4）。

阶段 4 的验收要求：报告在**没有校验源**时不得断言某个实体「不存在 / 未上市 /
查无此股」，只能表达为「未验证」。这里提供三件事：

1. :func:`local_entity_index` —— 本地 code/name 映射（``stock_codes``）；
2. :func:`evidence_entity_names` —— 证据里出现过的实体（``instrument`` 与文本）；
3. :func:`check_report_entities` —— 把报告文字里的实体分成 known / evidenced /
   unverified 三类，并给出可直接贴进 Critic prompt 的说明行。

判定顺序刻意是「本地映射 → 证据 → 未验证」：任一校验源命中即为有据，
两者都没有才落到 ``unverified``。**未验证不是运行错误**（不写 ``errors``）：
它只要求措辞降级为「未验证 / 无法确认」，由 Critic 按规则判定，
是"结论不可达"而不是"程序失败"。
"""

import json
import re
from functools import lru_cache
from typing import Any

from app.gateway.stock_codes import get_all_mapping_names, resolve_stock_code

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
    """本地 code/name 映射（名称 → 代码）。名字在这里命中即"有校验源"。"""
    index: dict[str, str] = {}
    for name in get_all_mapping_names():
        code = resolve_stock_code(name)
        if code:
            index[name] = code
    return index


def evidence_entity_names(evidence: Any) -> set[str]:
    """证据里出现过的实体名：``instrument`` + 证据文本中提到的任何已知名称。"""
    names: set[str] = set()
    known = list(local_entity_index())
    for item in evidence or []:
        instrument = _field(item, "instrument")
        if instrument:
            names.add(str(instrument))
        blob = json.dumps(_dump(item), ensure_ascii=False, default=str)
        for name in known:
            if name in blob:
                names.add(name)
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
            found.append((cursor, text[cursor : match.end()]))
    return found


def extract_entity_mentions(text: str, *, known_index: dict[str, str] | None = None) -> list[str]:
    """从文本里切出实体名（本地已知名 + 公司后缀模式），按出现位置排序、去重。

    排序保证同一份报告每次得到同样的列表（便于断言与日志比对）；
    同一位置取更长的名字，并丢弃被更长名字包含的短名
    （"行云科技" ⊂ "行云科技股份"）。
    """
    if not text:
        return []
    index = local_entity_index() if known_index is None else known_index
    positions: dict[str, int] = {}
    for name in index:
        if len(name) < 2:
            continue
        pos = text.find(name)
        if pos >= 0:
            positions.setdefault(name, pos)
    for pos, name in _entity_names_with_positions(text):
        positions.setdefault(name, pos)
    ordered = [name for name, _ in sorted(positions.items(), key=lambda kv: (kv[1], -len(kv[0])))]
    return [name for name in ordered if not any(name != other and name in other for other in ordered)]


def _is_evidenced(name: str, evidenced: set[str]) -> bool:
    """证据是否支持这个名字（含"证据里的标的被报告写全称"这种包含关系）。"""
    return any(name in entry or entry in name for entry in evidenced)


def check_report_entities(
    text: str,
    evidence: Any = (),
    *,
    known_index: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """校验报告文本里的实体，返回 ``(unverified_entities, review_lines)``。

    ``review_lines`` 直接贴进 Critic prompt —— 让 Critic 拿到的是**已分类的
    事实**（谁有校验源、谁没有），而不是让它自己去猜某个名字是不是真的存在。
    """
    index = local_entity_index() if known_index is None else known_index
    evidenced = evidence_entity_names(evidence)
    unverified: list[str] = []
    lines: list[str] = []
    for name in extract_entity_mentions(text, known_index=index):
        if name in index:
            lines.append(f"  - {name} → 本地映射 {index[name]}（有校验源）")
            continue
        if _is_evidenced(name, evidenced):
            lines.append(f"  - {name} → 证据中出现（有校验源）")
            continue
        unverified.append(name)
        lines.append(f"  - {name} → **无校验源**：不在本地 code/name 映射，证据里也没有")
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
