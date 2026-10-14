"""Official report/news search: keep excerpts and publisher provenance.

Search hits may be paragraphs from one document. No rating/target price is
inferred from prose, and a news mention is not a verified stock quote.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.iwencai_contracts import SEARCH_TOOLS
from app.models.market import NormalizedDatum, ToolResult
from app.research.iwencai import SOURCE, _call_iwencai, _error, _instruments, _published_at


def normalize_search(raw: str, tool: str, arguments: dict, *, text_limit: int = 800) -> ToolResult:
    key, _skill, channel = SEARCH_TOOLS[tool]
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return _error(arguments, "同花顺搜索返回非 JSON 响应", "gateway_invalid_response", tool=tool)
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int or payload["status_code"] != 0:
        return _error(arguments, "同花顺搜索业务状态非成功或缺少 status_code", "gateway_business_error", tool=tool)
    if payload.get("error") or payload.get("success") is False:
        return _error(arguments, "同花顺搜索返回业务错误", "gateway_business_error", tool=tool)
    rows = payload.get("data")
    if not isinstance(rows, list):
        return _error(arguments, "同花顺搜索缺少官方 data 列表", "gateway_invalid_response", tool=tool)
    entries = []
    rejected = 0
    retrieved = datetime.now(UTC).isoformat()
    documents = set()
    for row in rows[: arguments["size"]]:
        if not isinstance(row, dict) or row.get("channel") not in (None, channel):
            rejected += 1
            continue
        title = row.get("title")
        if not isinstance(title, str) or not title.strip():
            rejected += 1
            continue
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        stamp = _published_at(row)
        instruments = _instruments(row)
        excerpt = row.get("summary") or row.get("source_original") or ""
        excerpt = excerpt if isinstance(excerpt, str) else ""
        clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", excerpt)
        url = row.get("url") if isinstance(row.get("url"), str) else ""
        document = row.get("uid") or url
        if isinstance(document, str) and document:
            documents.add(document)
        notes = ["正文检索片段；未读取完整原文"]
        if len(clean) > text_limit:
            notes.append("摘要已截断，完整返回保留于 raw")
        if clean != excerpt:
            notes.append("摘要展示已清理控制字符")
        if not stamp:
            notes.append("上游未提供发布日期")
        if len(instruments) != 1:
            notes.append("未提供唯一证券代码；不据标题或查询猜测标的身份")
        publisher = extra.get("real_publish_source") or extra.get("publish_source")
        value = {
            "title": title.strip()[:500],
            "summary": clean[:text_limit],
            "url": url,
            "published_at": stamp,
            "stock_infos": instruments,
            "source": SOURCE,
            "publisher": publisher if isinstance(publisher, str) else None,
            "document_id": document if isinstance(document, str) else None,
            "chunk_index": row.get("para_index") if type(row.get("para_index")) is int else None,
        }
        if channel == "report":
            for field in ("organization", "author", "rating", "target_price", "currency"):
                v = extra.get(field)
                value[field] = v[:500] if isinstance(v, str) else v if type(v) in (int, float) else None
            notes.append("评级/目标价与盈利预测是机构观点，非实际行情或已实现业绩；同文档片段不代表独立研报")
        entries.append(
            NormalizedDatum(
                tool=tool,
                domain="media",
                instrument=instruments[0]["code"] if len(instruments) == 1 else None,
                instrument_name=instruments[0]["name"] or None if len(instruments) == 1 else None,
                metric=f"{channel}_{len(entries) + 1}",
                value=value,
                timestamp=stamp,
                as_of_date=stamp[:10] if stamp else None,
                retrieved_at=retrieved,
                source=SOURCE,
                authority=SOURCE,
                completeness="search_results",
                instrument_scope="instrument" if len(instruments) == 1 else "search_results",
                note="；".join(notes),
            )
        )
    if not entries:
        return _error(arguments, "未返回有效检索结果，不能据此断言没有相关研报或新闻", "empty_normalized", tool=tool)
    partial = rejected > 0 or len(rows) > arguments["size"] or any(d.timestamp is None for d in entries)
    note = f"数据来源：{SOURCE}；最终查询：{arguments['query']}；{len(entries)}条正文片段命中，按相关性返回，非完整清单；自然语言日期条件不保证严格过滤"
    if channel == "report":
        note += f"；可识别的不同文档{len(documents)}份，同文档片段不可算作多份研报或多机构交叉确认"
    return ToolResult(
        tool=tool,
        operation_id=tool,
        tool_key=key,
        arguments=arguments,
        raw=raw,
        status="partial" if partial else "success",
        partial=partial,
        normalized=entries,
        retrieved_at=retrieved,
        authority=SOURCE,
        completeness="search_results",
        note=note,
    )


async def search_content(
    settings: Settings, tool: str, arguments: dict, *, deadline: float | None = None
) -> ToolResult:
    _key, skill, channel = SEARCH_TOOLS[tool]
    arguments = canonicalize_tool_arguments(tool, arguments)
    return await _call_iwencai(
        settings,
        tool,
        arguments,
        skill_id=skill,
        endpoint="/v1/comprehensive/search",
        body={"query": arguments["query"], "channels": [channel], "app_id": "AIME_SKILL", "size": arguments["size"]},
        normalizer=lambda raw: normalize_search(raw, tool, arguments, text_limit=settings.news_max_text_chars),
        deadline=deadline,
    )
