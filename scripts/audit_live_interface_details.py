"""Focused source and valid-argument controls for audit_live_interfaces.py."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_live_interfaces import Audit, field_paths


async def source_probe(audit, name, callback, attempt=1):
    started = time.monotonic()
    row = {"layer": "source", "case": name, "tool": name, "attempt": attempt, "observed": True}
    try:
        async with asyncio.timeout(25):
            data = await callback()
        if isinstance(data, list):
            count = len(data)
            dates = sorted({str(item.published_at) for item in data if getattr(item, "published_at", None)})
            row.update(
                item_count=count,
                dated_items=len([x for x in data if getattr(x, "published_at", None)]),
                dates=dates[-4:],
            )
        else:
            count = len(data)
            row.update(item_count=count, raw_fields=field_paths(data)[:30])
        row.update(
            status="success" if count else "empty", classification="data_returned" if count else "no_business_data"
        )
    except Exception as exc:
        row.update(
            status="error",
            classification="timeout" if isinstance(exc, TimeoutError) else "exception",
            exception_type=type(exc).__name__,
            error=audit.clean(exc),
        )
    row["ms"] = round((time.monotonic() - started) * 1000, 1)
    audit.record(row)


async def probe_news_sources(audit):
    from app.research.news_sources import (
        fetch_code_name_map,
        fetch_ddgs,
        fetch_google_rss,
        fetch_stock_news,
        fetch_telegraph,
    )

    sources = [
        ("eastmoney_stock_news", lambda: fetch_stock_news("600519", timeout=15)),
        ("cls_telegraph", lambda: fetch_telegraph(timeout=15)),
        ("google_news_rss", lambda: fetch_google_rss("贵州茅台 when:7d", max_items=5, timeout=15)),
        ("ddgs_news", lambda: fetch_ddgs("A股 市场", max_results=5, time_limit="w", timeout=15)),
        ("ashare_code_name_map", lambda: fetch_code_name_map(timeout=20)),
    ]
    for name, callback in sources:
        await source_probe(audit, name, callback)
    failed = {r["case"] for r in audit.rows if r["layer"] == "source" and r.get("status") in {"empty", "error"}}
    for name, callback in sources:
        if name in failed:
            await source_probe(audit, name, callback, 2)


async def probe_llm(audit):
    from openai import AsyncOpenAI

    async with AsyncOpenAI(
        api_key=audit.settings.openai_api_key,
        base_url=audit.settings.openai_base_url,
        timeout=audit.settings.llm_timeout_seconds,
        max_retries=0,
    ) as client:
        for attempt in (1, 2):
            started = time.monotonic()
            row = {
                "layer": "llm",
                "case": "configured_chat_json",
                "tool": "chat.completions",
                "attempt": attempt,
                "observed": True,
                "model": audit.settings.openai_model,
            }
            try:
                async with asyncio.timeout(audit.settings.llm_timeout_seconds + 1):
                    response = await client.chat.completions.create(
                        model=audit.settings.openai_model,
                        messages=[
                            {"role": "system", "content": "Return only a valid JSON object."},
                            {"role": "user", "content": f'Return {{"audit":"ok","attempt":{attempt}}}.'},
                        ],
                        response_format={"type": "json_object"},
                        temperature=0,
                        max_tokens=audit.settings.llm_max_completion_tokens,
                    )
                parsed = json.loads(response.choices[0].message.content or "")
                passed = parsed == {"audit": "ok", "attempt": attempt}
                row.update(
                    status="success" if passed else "error",
                    classification="valid_json_response" if passed else "unexpected_response",
                    finish_reason=response.choices[0].finish_reason,
                    usage=response.usage.model_dump() if response.usage else {},
                )
            except Exception as exc:
                row.update(
                    status="error",
                    classification="exception",
                    exception_type=type(exc).__name__,
                    error=audit.clean(exc),
                )
            row["ms"] = round((time.monotonic() - started) * 1000, 1)
            audit.record(row)


async def probe_routing_controls(audit):
    kline_args = next(v["arguments"] for v in audit.variants if v["label"] == "klines_ashare")
    for attempt in (1, 2):
        for tool, arguments, label in (
            ("get_market_klines", kline_args, "klines_ashare_primary"),
            ("get_company_overview", {"symbol": "SH600519"}, "overview_with_symbol_primary"),
        ):
            await audit.probe(audit.job("primary_route", tool, arguments, label), attempt)
    for tool in ("get_limit_up_count", "list_limit_up_sectors", "get_ashare_sentiment"):
        await audit.probe(audit.job("primary_route", tool, {"as_of_date": audit.today}, tool + "_dated_control"), 1)
    for symbol in ("hk00700", "00700"):
        await audit.probe(audit.job("http", "get_market_quotes", {"symbols": [symbol]}, "quote_hk_format_" + symbol), 1)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--hk-control-only", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    parser.add_argument("--llm-only", action="store_true")
    parser.add_argument("--routing-controls-only", action="store_true")
    options = parser.parse_args()
    audit = Audit(argparse.Namespace(output=options.output, resume=None, timeout=40, concurrency=2))
    from app.research.hk_northbound import _EASTMONEY_KLINE_URL, _build_params

    try:
        if options.routing_controls_only:
            await probe_routing_controls(audit)
            return
        if options.llm_only:
            await probe_llm(audit)
            return
        if options.sources_only:
            await probe_news_sources(audit)
            return
        if options.hk_control_only:
            async with httpx.AsyncClient(timeout=15) as client:
                for path, secid in (
                    ("klines", "HK.800300"),
                    ("kline", "HK.800300"),
                    ("kline", "100.HSI"),
                    ("kline", "100.HSTECH"),
                ):
                    started = time.monotonic()
                    endpoint = _EASTMONEY_KLINE_URL.replace("/klines/", f"/{path}/")
                    row = {
                        "layer": "source_control",
                        "case": f"{path}_{secid}",
                        "tool": "eastmoney_klines",
                        "attempt": 1,
                        "arguments": {"path": endpoint, "secid": secid},
                        "observed": True,
                    }
                    try:
                        response = await client.get(endpoint, params=_build_params(secid, 3))
                        row.update(
                            http_status=response.status_code,
                            content_type=response.headers.get("content-type"),
                            payload_chars=len(response.content),
                        )
                        raw = response.json() if response.content else {}
                        data = raw.get("data") or {}
                        row.update(
                            upstream_rc=raw.get("rc"),
                            data_is_null=raw.get("data") is None,
                            klines_count=len(data.get("klines") or []),
                            raw_fields=field_paths(raw)[:20],
                            status="success" if data.get("klines") else "empty",
                            classification="data_returned" if data.get("klines") else "no_business_data",
                        )
                    except Exception as exc:
                        row.update(status="error", classification="exception", error=audit.clean(exc))
                    row["ms"] = round((time.monotonic() - started) * 1000, 1)
                    audit.record(row)
            return
        for attempt in (1, 2):
            for tool, args, label in (
                (
                    "get_derivatives_history",
                    {**audit.args["get_derivatives_history"], "symbol": "BTCUSDT"},
                    "derivatives_valid_native_symbol",
                ),
                ("get_market_quotes", audit.args["get_market_quotes"], "quote_indices_isolated_control"),
            ):
                await asyncio.gather(
                    *(audit.probe(audit.job(layer, tool, args, label), attempt) for layer in ("mcp", "http"))
                )
            for tool, query, label in (
                ("internal_iwencai_event_query", "贵州茅台 最近一年 机构调研 调研日期", "events_alternate_query"),
                ("internal_iwencai_insresearch_query", "贵州茅台 研报评级", "insresearch_alternate_query"),
            ):
                await audit.probe(
                    audit.job("skill", tool, {"query": query, "limit": 5, "is_cache": False}, label), attempt
                )

        async with httpx.AsyncClient(timeout=15) as client:
            for secid in ("HK.960036", "SH.960052", "SZ.960053", "HK.800300", "HK.800505", "100.HSI", "100.HSTECH"):
                started = time.monotonic()
                row = {
                    "layer": "source",
                    "case": "eastmoney_" + secid,
                    "tool": "eastmoney_klines",
                    "attempt": 1,
                    "arguments": {"secid": secid},
                    "observed": True,
                }
                try:
                    response = await client.get(_EASTMONEY_KLINE_URL, params=_build_params(secid, 3))
                    raw = response.json()
                    data = raw.get("data") or {}
                    row.update(
                        http_status=response.status_code,
                        upstream_rc=raw.get("rc"),
                        data_is_null=raw.get("data") is None,
                        klines_count=len(data.get("klines") or []),
                        raw_fields=field_paths(raw)[:20],
                        status="success" if data.get("klines") else "empty",
                        classification="data_returned" if data.get("klines") else "no_business_data",
                    )
                except Exception as exc:
                    row.update(status="error", classification="exception", error=audit.clean(exc))
                row["ms"] = round((time.monotonic() - started) * 1000, 1)
                audit.record(row)

            # Obtain a real public post URL before testing the dependent comments API.
            meta_url = audit.settings.market_gateway_http_url.rstrip("/") + "/market/discussions"
            response = await client.get(
                meta_url,
                headers={"X-API-Key": audit.settings.market_gateway_api_key},
                params={"symbol": "SH600519", "count": 3},
            )
            if response.status_code == 200:
                audit.find_post_url(response.json())
            if audit.discussion_url:
                for attempt in (1, 2):
                    await audit.probe(
                        audit.job(
                            "mcp", "list_stock_post_comments", {"post_url": audit.discussion_url, "max_pages": 1}
                        ),
                        attempt,
                    )
            else:
                audit.record(
                    {
                        "layer": "mcp",
                        "case": "list_stock_post_comments",
                        "tool": "list_stock_post_comments",
                        "status": "not_tested",
                        "classification": "no_valid_post_url",
                        "observed": False,
                        "attempt": 1,
                    }
                )

            # Brief endpoints execute research and persist state; omit them here.
            for path in ("/health", "/"):
                started = time.monotonic()
                row = {
                    "layer": "local_http",
                    "case": path,
                    "tool": "web_endpoint",
                    "attempt": 1,
                    "observed": True,
                }
                try:
                    response = await client.get("http://127.0.0.1:8000" + path)
                    row.update(
                        http_status=response.status_code,
                        status="success" if response.status_code == 200 else "error",
                        classification="responded",
                    )
                except Exception as exc:
                    row.update(status="error", classification="exception", error=audit.clean(exc))
                row["ms"] = round((time.monotonic() - started) * 1000, 1)
                audit.record(row)

        await probe_news_sources(audit)
    finally:
        await audit.http.aclose()
        await audit.mcp.close()
        audit.finish()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
