"""同花顺问财 announcement-search 1.0.0 官方协议适配。

协议来源：SkillHub 官方下载包中的 SKILL.md / references/api.md。
网络层保留原始响应文本；业务层只解析明确的 data 公告列表，不递归猜字段。
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.iwencai_contracts import IWENCAI_TOOL_KEYS
from app.gateway.policy import observed_call
from app.models.market import NormalizedDatum, ToolResult

OPERATION_ID = "internal_iwencai_announcement_search"
SOURCE = "同花顺问财"
SKILL_VERSION = "1.0.0"
_CHINA_TZ = timezone(timedelta(hours=8))
_SEARCH_NOTE = "公告检索按相关性返回，非完整公告清单；摘要不等于公告全文。数据来源：同花顺问财"
_AUTH_NOTICE = "IWENCAI_API_KEY 未配置或认证失败；请登录 https://www.iwencai.com/skillhub 获取 API Key 并配置到 .env"


def _auth_reason_codes(status_code: int | None = None, *, missing: bool = False) -> list[str]:
    """Return stable, secret-free authentication diagnostics.

    ``gateway_auth_error`` is retained for compatibility with existing audit
    consumers.  The second code identifies the actionable class without
    copying provider response text or credentials into logs.
    """
    if missing:
        return ["gateway_auth_error", "ths_missing_credentials"]
    if status_code == 401:
        return ["gateway_auth_error", "ths_invalid_credentials"]
    if status_code == 403:
        return ["gateway_auth_error", "ths_permission_denied"]
    return ["gateway_auth_error"]


def _error(arguments: dict, message: str, code: str, *, tool: str = OPERATION_ID) -> ToolResult:
    return ToolResult(
        tool=tool,
        operation_id=tool,
        tool_key=IWENCAI_TOOL_KEYS[tool],
        arguments=arguments,
        status="error",
        error=message,
        reason_codes=[code],
    )


def _published_at(row: dict) -> str | None:
    # Prefer the provider's local publication date. Never substitute retrieval time.
    value = row.get("publish_date")
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).isoformat(sep=" ")
        except ValueError:
            pass
    value = row.get("publish_time")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            seconds = value / 1000 if value >= 100_000_000_000 else value
            return datetime.fromtimestamp(seconds, _CHINA_TZ).isoformat(sep=" ")
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _instruments(row: dict) -> list[dict[str, str]]:
    result = []
    for info in row.get("stock_infos", []) if isinstance(row.get("stock_infos"), list) else []:
        if not isinstance(info, dict):
            continue
        code = str(info.get("code") or "").strip().upper()
        # The Skill returns both prefix-first (SH600519) and suffix-first
        # (600519.SH) forms. Normalize only recognized exchange-qualified
        # codes; internal aliases such as MOUTAI80 must remain excluded.
        suffix_match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", code)
        if suffix_match:
            code = suffix_match.group(2) + suffix_match.group(1)
        if not re.fullmatch(r"(?:(?:SH|SZ|BJ)?\d{6}|(?:HK)?\d{5})", code):
            continue
        if len(code) == 5:
            code = "HK" + code
        result.append({"code": code, "name": str(info.get("name") or "")})
    return result


def normalize_announcements(raw: str, arguments: dict, *, text_limit: int = 800) -> ToolResult:
    """Derive bounded evidence while retaining the gateway body unchanged in raw."""
    try:
        payload: Any = json.loads(raw)
    except (ValueError, TypeError):
        return _error(arguments, "同花顺公告接口返回非 JSON 响应", "gateway_invalid_response")
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int or payload["status_code"] != 0:
        return _error(arguments, "同花顺公告接口业务状态非成功或缺少 status_code", "gateway_business_error")
    rows = payload.get("data")
    if not isinstance(rows, list):
        return _error(arguments, "同花顺公告接口 data 结构不符合官方列表协议", "gateway_invalid_response")
    retrieved = datetime.now(UTC).isoformat()
    query = str(arguments.get("query") or "")
    requested_code_match = re.search(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", query, re.IGNORECASE)
    requested_code = requested_code_match.group(0).upper() if requested_code_match else None
    requested_month_match = re.search(r"(20[0-9]{2})[^0-9]*(1[0-2]|0?[1-9])(?![0-9])", query)
    requested_month = (
        f"{requested_month_match.group(1)}-{int(requested_month_match.group(2)):02d}"
        if requested_month_match else None
    )
    entries = []
    rejected = 0
    for row in rows[: arguments["size"]]:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip():
            rejected += 1
            continue
        if row.get("channel") not in (None, "announcement"):
            rejected += 1
            continue
        stamp = _published_at(row)
        instruments = _instruments(row)
        if requested_code:
            requested_suffix = requested_code[-6:]
            row_codes = {item["code"] for item in instruments}
            # P13 §四.3：上游没有给出可验证证券代码的公告，不能算作
            # "目标标的的公告"。旧实现用 ``if row_codes and ...`` 把无代码的
            # 行放行，于是 instrument=None 的历史公告会以"匹配"的姿态进入
            # 账本，再去支撑一个它根本不支持的标的结论。
            if not any(code.endswith(requested_suffix) for code in row_codes):
                rejected += 1
                continue
        if requested_month:
            # 只接受上游真实返回的发布日期：拿不到日期就无法核对年月，
            # 不允许用检索时间或请求参数补造。
            if not stamp or not stamp.startswith(requested_month):
                rejected += 1
                continue
        summary = row.get("summary") or row.get("source_original") or ""
        summary = summary if isinstance(summary, str) else ""
        clipped = len(summary) > text_limit
        notes = []
        if clipped:
            notes.append("摘要已截断，完整返回保留于 raw")
        if len(instruments) != 1:
            notes.append("上游未提供唯一可确认的证券代码，须核对标的是否匹配")
        if stamp is None:
            notes.append("公告发布日期未知")
        value = {
            "title": row["title"].strip()[:500],
            "url": row.get("url") if isinstance(row.get("url"), str) else "",
            "summary": summary[:text_limit],
            "published_at": stamp,
            "stock_infos": instruments,
            "source": SOURCE,
        }
        entries.append(
            NormalizedDatum(
                tool=OPERATION_ID,
                domain="media",
                metric=f"announcement_{len(entries) + 1}",
                value=value,
                timestamp=stamp,
                as_of_date=stamp[:10] if stamp else None,
                retrieved_at=retrieved,
                source=SOURCE,
                authority=SOURCE,
                instrument=instruments[0]["code"] if len(instruments) == 1 else None,
                instrument_name=instruments[0]["name"] or None if len(instruments) == 1 else None,
                instrument_scope="instrument" if len(instruments) == 1 else "search_results",
                # P13 §四：单条公告的字段完整性取决于它自己是否带有可核对的
                # 证券代码与发布日期；"search_results" 描述的是检索信封，不是
                # 这条公告的数据完整性。
                completeness="complete" if stamp and len(instruments) == 1 else "partial",
                note="；".join(notes) or None,
            )
        )
    if not entries:
        return _error(arguments, "未检索到同时匹配目标标的与日期范围的公告；不能据此判定没有公告", "empty_normalized")
    # Partial tracks dropped rows or missing provenance; successful searches
    # still cover only retrieved hits, never the entire announcement corpus.
    partial = rejected > 0 or len(rows) > arguments["size"]
    unverified = any(d.instrument is None or d.timestamp is None for d in entries)
    note = _SEARCH_NOTE
    if partial:
        note += "；部分返回记录无效或超出本次条数限制"
    if unverified:
        note += "；部分公告缺少唯一证券代码或发布日期，须核对标的与时间匹配"
        partial = True
    return ToolResult(
        tool=OPERATION_ID,
        operation_id=OPERATION_ID,
        tool_key="announcement_search",
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


async def search_announcements(
    settings: Settings,
    arguments: dict,
    *,
    deadline: float | None = None,
) -> ToolResult:
    """Fixed official endpoint/body/headers, with Mosaic's finite retry budget."""
    arguments = canonicalize_tool_arguments(OPERATION_ID, arguments)
    return await _call_iwencai(
        settings,
        OPERATION_ID,
        arguments,
        skill_id="announcement-search",
        endpoint="/v1/comprehensive/search",
        body={
            "query": arguments["query"],
            "channels": ["announcement"],
            "app_id": "AIME_SKILL",
            "size": arguments["size"],
        },
        normalizer=lambda raw: normalize_announcements(raw, arguments, text_limit=settings.news_max_text_chars),
        deadline=deadline,
    )


async def _call_iwencai(
    settings: Settings,
    tool: str,
    arguments: dict,
    *,
    skill_id: str,
    endpoint: str,
    body: dict,
    normalizer: Callable[[str], ToolResult],
    deadline: float | None,
    retry_header: bool = False,
) -> ToolResult:
    """Shared transport for explicitly registered official Skills only."""
    if not settings.iwencai_api_key.strip():
        result = _error(arguments, _AUTH_NOTICE, "gateway_auth_error", tool=tool)
        result.reason_codes = _auth_reason_codes(missing=True)
        return result

    attempt = 0
    async with httpx.AsyncClient(base_url=settings.iwencai_base_url.rstrip("/")) as client:

        async def request(timeout: float):
            nonlocal attempt
            attempt += 1
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": "Bearer " + settings.iwencai_api_key,
                    "Content-Type": "application/json",
                    "X-Claw-Call-Type": "retry" if retry_header and attempt > 1 else "normal",
                    "X-Claw-Skill-Id": skill_id,
                    "X-Claw-Skill-Version": SKILL_VERSION,
                    "X-Claw-Plugin-Id": "none",
                    "X-Claw-Plugin-Version": "none",
                    "X-Claw-Trace-Id": secrets.token_hex(32),
                },
                json=body,
                timeout=timeout,
            )
            # Diagnostics never include query, credentials, raw body or server error messages.
            diagnostics = {"http_status": response.status_code, "payload_chars": len(response.text)}
            if response.status_code in (401, 403):
                result = _error(arguments, _AUTH_NOTICE, "gateway_auth_error", tool=tool)
                result.reason_codes = _auth_reason_codes(response.status_code)
            elif response.status_code == 429:
                result = _error(arguments, "同花顺问财接口限流", "gateway_rate_limited", tool=tool)
                result.reason_codes.append("ths_rate_limited")
            elif response.status_code >= 500:
                result = _error(arguments, "同花顺问财接口上游服务错误", "gateway_upstream_error", tool=tool)
                result.reason_codes.append("ths_upstream_error")
            elif not response.is_success:
                result = _error(
                    arguments, f"同花顺问财接口 HTTP {response.status_code}", "gateway_http_error", tool=tool
                )
            else:
                result = normalizer(response.text)
            return result, diagnostics

        transport_settings = (
            settings.model_copy(update={"max_retry_per_tool": min(2, settings.max_retry_per_tool)})
            if retry_header
            else settings
        )
        return await observed_call(transport_settings, tool, arguments, deadline, request)
