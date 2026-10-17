"""Read-only live inventory and differential probes of Mosaic data interfaces.

Uses the project's httpx/MCP clients and normalizers. No LLM, research history,
configuration, or production code is changed. Artifacts contain synthetic probe
arguments and bounded diagnostics, never credentials or complete upstream data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.gateway.arguments import canonicalize_tool_arguments, gateway_transport_arguments, quote_transport_symbols
from app.gateway.controlled_fallback import primary_trigger_reason
from app.gateway.mcp_client import MarketGatewayClient, _mcp_result_to_json, _mcp_result_to_text
from app.gateway.normalizer import normalize_tool_result
from app.gateway.policy import response_diagnostics
from app.gateway.source_routing import skill_route_for
from app.gateway.ths_skill_adapter import skill_adapter_for
from app.gateway.ths_skill_registry import THS_SKILL_SPECS
from app.gateway.tool_registry import BY_NAME, declared_provider_for_operation
from app.graph.tool_runtime import ToolRuntime
from app.models.market import ToolResult


def nonempty(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, dict):
        return any(nonempty(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(nonempty(v) for v in value)
    return True


def field_paths(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    paths = []
    if isinstance(value, dict):
        for key, item in list(value.items())[:24]:
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(field_paths(item, path, depth + 1))
    elif isinstance(value, list) and value:
        paths.extend(field_paths(value[0], prefix + "[0]", depth + 1))
    return paths[:100]


def cases(today: str) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    end = datetime.fromisoformat(today).date() - timedelta(days=1)
    start = end - timedelta(days=7)
    kline = {
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "interval": "1d",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cache": False,
    }
    args = {name: {} for name in BY_NAME}
    for name in (
        "get_company_detail",
        "get_company_business",
        "get_company_concepts",
        "get_company_finance",
        "get_company_shareholders",
        "get_company_survey",
        "get_market_orderbook",
        "list_market_trades",
        "list_stock_discussions",
        "get_stock_longhu",
        "get_stock_abnormal_reasons",
    ):
        args[name] = {"symbol": "SH600519"}
    args.update(
        {
            "get_market_quotes": {"symbols": ["SH000001", "SH000300", "SZ399006"]},
            "search_stocks": {"q": "600519"},
            "get_market_klines": kline,
            "get_market_snapshot": {"symbol": "BTC/USDT", "exchange": "binance"},
            "get_market_window": {
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "interval": "1d",
                "anchor": (end - timedelta(days=2)).isoformat(),
                "days_before": 2,
                "days_after": 0,
                "cache": False,
            },
            "get_derivatives_history": {
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "datasets": ["funding"],
                "include_data": True,
                "refresh": False,
            },
            "get_hyperliquid_liquidation_map": {"symbol": "ETH"},
            "get_hyperliquid_top_position": {"symbol": "ETH"},
            "get_crypto_liquidation_today": {"symbol": "BTC"},
            "list_crypto_funding_rates": {"page_num": 1, "page_size": 5},
            "internal_us_fundamentals": {"symbol": "AAPL"},
            "internal_us_filings_recent": {"symbol": "AAPL"},
            "internal_symbol_news": {"symbol": "SH600519", "limit": 5},
            "internal_news_digest": {"query": "A股 市场", "max_results": 5, "time_limit": "w"},
            "news_search": {"query": "A股 市场", "max_results": 5, "time_limit": "w"},
        }
    )
    queries = {
        "finance": "贵州茅台 2025年年报 营业收入 净利润 ROE",
        "events": "贵州茅台 最近一年 业绩预告 分红",
        "basicinfo": "贵州茅台 股票代码 股票简称 所属行业",
        "market": f"同花顺全A {today} 涨跌幅 成交额 上涨家数 下跌家数 涨停家数",
        "macro": "中国 2026年9月 CPI 同比",
        "industry": f"行业板块 {today} 涨跌幅 涨停家数",
        "management": "贵州茅台 2025年年报 前十大股东 持股比例",
        "announcement": "贵州茅台 最近一年 年度报告",
        "report": "贵州茅台 最近一年 研报",
        "news": f"A股 {today} 市场 新闻",
        "index": f"上证指数 深证成指 创业板指 {today} 涨跌幅 成交额",
        "astock_selector": f"{today} 涨停的A股 股票代码 股票简称",
        "sector_selector": f"{today} 涨幅大于0的行业板块 涨跌幅",
        "business": "贵州茅台 2025年年报 主营业务构成",
        "insresearch": "贵州茅台 最近一年 研报评级 机构名称 评级日期",
    }
    skills = {}
    for spec in THS_SKILL_SPECS:
        a = {"query": queries[spec.name]}
        a.update(
            {"size": 5}
            if spec.operation_id.endswith("_search")
            else {"page": 1, "limit": 5, "is_cache": False, "expand_index": True}
        )
        skills[spec.operation_id] = a
        args[spec.operation_id] = a
    variants = [
        {"label": "quote_stock", "tool": "get_market_quotes", "arguments": {"symbols": ["SH600519"]}},
        {"label": "quote_hk", "tool": "get_market_quotes", "arguments": {"symbols": ["HK00700"]}},
        {"label": "search_hk", "tool": "search_stocks", "arguments": {"q": "00700"}},
        {
            "label": "klines_ashare",
            "tool": "get_market_klines",
            "arguments": {**kline, "symbol": "SH600519", "exchange": "xueqiu"},
        },
        {
            "label": "klines_us",
            "tool": "get_market_klines",
            "arguments": {**kline, "symbol": "AAPL", "exchange": "xueqiu"},
        },
        {
            "label": "window_us",
            "tool": "get_market_window",
            "arguments": {**args["get_market_window"], "symbol": "AAPL", "exchange": "xueqiu"},
        },
        {
            "label": "gold",
            "tool": "get_market_klines",
            "arguments": {**kline, "symbol": "XAU/USDT:USDT", "exchange": "binance"},
        },
        {
            "label": "silver",
            "tool": "get_market_snapshot",
            "arguments": {"symbol": "XAG/USDT:USDT", "exchange": "binance"},
        },
        {
            "label": "platinum",
            "tool": "get_market_snapshot",
            "arguments": {"symbol": "XPT/USDT:USDT", "exchange": "binance"},
        },
    ]
    return args, skills, variants


class Audit:
    def __init__(self, options):
        self.options = options
        self.settings = get_settings()
        self.today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.args, self.skills, self.variants = cases(self.today)
        self.root = Path(options.output).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.rows: list[dict] = []
        if options.resume:
            previous = Path(options.resume) / "results.jsonl"
            self.rows = [json.loads(line) for line in previous.read_text(encoding="utf-8").splitlines() if line.strip()]
            (self.root / "results.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8"
            )
        self.sem = asyncio.Semaphore(options.concurrency)
        self.secrets = [
            v
            for k, v in self.settings.model_dump().items()
            if isinstance(v, str) and v and any(x in k.lower() for x in ("api_key", "contact", "password", "token"))
        ]
        self.mcp = MarketGatewayClient(self.settings)
        self.http = httpx.AsyncClient(
            base_url=self.settings.market_gateway_http_url.rstrip("/"),
            headers={"X-API-Key": self.settings.market_gateway_api_key},
            timeout=options.timeout,
        )
        self.runtime = ToolRuntime(self.settings)
        self.internal = ToolRuntime(self.settings.model_copy(update={"market_source_routing_enabled": False}))
        self.started = time.monotonic()
        self.discussion_url = None

    def clean(self, value: Any) -> str:
        text = str(value or "")
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1<redacted>", text)
        return text[:700]

    def record(self, row: dict) -> None:
        row["at"] = datetime.now(UTC).isoformat()
        self.rows.append(row)
        with (self.root / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        print(
            json.dumps(
                {k: row.get(k) for k in ("attempt", "layer", "case", "status", "classification", "ms", "reason_codes")},
                ensure_ascii=False,
            ),
            flush=True,
        )

    def result_summary(self, r: ToolResult, job: dict) -> dict:
        business = [
            d
            for d in r.normalized
            if not d.structural_metadata
            and not d.metric.startswith("_")
            and d.metric not in {"news_meta", "news_stats"}
            and not d.metric.startswith("meta.")
            and nonempty(d.value)
        ]
        dates = [d for d in business if d.as_of_date and d.as_of_date != "unknown" or d.timestamp]
        sources = [d for d in business if d.source or d.authority]
        codes = list(r.reason_codes)
        status = r.status
        if status == "error":
            classification = "timeout" if any("timeout" in c or c == "deadline_exhausted" for c in codes) else "error"
        elif not business:
            classification = "no_business_data"
        elif r.partial or status == "partial" or len(dates) < len(business) or len(sources) < len(business):
            classification = "partial_or_missing_provenance"
        else:
            classification = "data_returned"
        route = skill_route_for(job["tool"], job["arguments"])
        trigger = None
        if route and job["layer"] in {"mcp", "http", "runtime", "primary_route"}:
            trigger = primary_trigger_reason(r, route[1], job["arguments"])
            if trigger and business and status != "error":
                classification = "capability_contract_gap"
        return {
            "status": status,
            "classification": classification,
            "reason_codes": codes,
            "note": self.clean(r.note),
            "error": self.clean(r.error),
            "datum_count": len(r.normalized),
            "business_datums": len(business),
            "dated_business_datums": len(dates),
            "sourced_business_datums": len(sources),
            "metrics": [d.metric for d in business[:18]],
            "dates": sorted({str(d.as_of_date or d.timestamp) for d in business if d.as_of_date or d.timestamp})[:15],
            "instruments": sorted({d.instrument for d in business if d.instrument})[:20],
            "sources": sorted({d.source or d.authority for d in business if d.source or d.authority})[:12],
            "capability_gap": trigger,
            "source_switch": r.source_switch,
            "primary_source": r.primary_source,
            "fallback_source": r.fallback_source,
            "fallback_reason": r.fallback_reason,
            "date_annotation": r.date_annotation,
            "raw_fields": field_paths(r.raw),
            "declared_coverage": self.coverage(r.raw),
        }

    @staticmethod
    def coverage(raw: Any) -> list[str]:
        found = []

        def walk(v, depth=0):
            if depth > 3:
                return
            if isinstance(v, dict):
                for k, item in v.items():
                    if k == "coverage_status":
                        found.append(str(item))
                    elif isinstance(item, (dict, list)):
                        walk(item, depth + 1)
            elif isinstance(v, list):
                for item in v[:3]:
                    walk(item, depth + 1)

        walk(raw)
        return list(dict.fromkeys(found))

    async def probe(self, job: dict, attempt: int) -> None:
        async with self.sem:
            started = time.monotonic()
            row = {
                "layer": job["layer"],
                "case": job["case"],
                "tool": job["tool"],
                "arguments": job["arguments"],
                "attempt": attempt,
                "observed": False,
            }
            try:
                async with asyncio.timeout(self.options.timeout + 1):
                    await self.call(job, row)
            except Exception as exc:
                row.update(
                    status="error",
                    classification="timeout" if isinstance(exc, TimeoutError) else "exception",
                    exception_type=type(exc).__name__,
                    error=self.clean(exc),
                    reason_codes=["probe_timeout" if isinstance(exc, TimeoutError) else "probe_exception"],
                )
            row["ms"] = round((time.monotonic() - started) * 1000, 1)
            if "not_collected" in row.get("declared_coverage", []):
                row["classification"] = "declared_not_collected"
            self.record(row)

    async def call(self, job: dict, row: dict) -> None:
        layer, tool, args = job["layer"], job["tool"], dict(job["arguments"])
        deadline = time.monotonic() + self.options.timeout
        if layer == "mcp":
            # A canceled call must not leave later probes queued behind it.
            # Each business probe owns its own initialize/list/call/close cycle.
            async with MarketGatewayClient(self.settings) as gateway:
                await self.call_mcp(gateway, job, row)
            return
        if layer == "http":
            meta = BY_NAME[tool]
            canonical = canonicalize_tool_arguments(tool, args)
            wire = gateway_transport_arguments(tool, canonical)
            if tool == "get_market_quotes":
                wire["symbols"] = ",".join(quote_transport_symbols(canonical["symbols"]))
            row.update(wire_arguments=wire, observed=True, endpoint=meta.http_path, method=meta.http_method)
            resp = await self.http.request(
                meta.http_method,
                meta.http_path,
                params=wire if meta.http_method == "GET" else None,
                json=wire if meta.http_method != "GET" else None,
            )
            row.update(response_diagnostics(resp.text, resp.status_code))
            try:
                raw = resp.json()
            except ValueError:
                raw = None
            if tool == "list_stock_discussions":
                self.find_post_url(raw)
            r = normalize_tool_result(
                tool,
                canonical,
                raw if resp.status_code < 400 else None,
                error=f"HTTP {resp.status_code}" if resp.status_code >= 400 else None,
                declared_provider=declared_provider_for_operation(tool),
                requested_as_of_date=self.today,
            )
            row.update(self.result_summary(r, job))
            if resp.status_code >= 400:
                row["upstream_detail"] = self.clean(resp.text)
            return
        row["observed"] = True
        if layer == "skill":
            r = await skill_adapter_for(self.settings).call(
                tool, args, deadline=deadline, refresh=True, run_id="interface-audit"
            )
        elif layer == "internal_native":
            r = await self.internal.execute(tool, args, set(), deadline=deadline, refresh=True)
        elif layer == "primary_route":
            route = skill_route_for(tool, args)
            if route is None:
                raise ValueError("no registered primary route")
            op, capability, skill_args = route
            row.update(routed_operation=op, routed_arguments=skill_args, capability=capability)
            r = await skill_adapter_for(self.settings).call(
                op, skill_args, deadline=deadline, refresh=True, run_id="interface-audit"
            )
        else:
            r = await self.runtime.execute(tool, args, set(), deadline=deadline, refresh=True)
        row.update(self.result_summary(r, job))

    async def call_mcp(self, gateway: MarketGatewayClient, job: dict, row: dict) -> None:
        tool, args = job["tool"], dict(job["arguments"])
        canonical = canonicalize_tool_arguments(tool, args) if tool in BY_NAME else args
        wire = gateway._encode_transport_arguments(tool, canonical) if tool in BY_NAME else args
        row.update(wire_arguments=wire, observed=True, session_mode="isolated")
        raw_result = await asyncio.wait_for(gateway.session.call_tool(tool, arguments=wire), self.options.timeout)
        raw = _mcp_result_to_json(raw_result)
        text = _mcp_result_to_text(raw_result)
        is_error = bool(getattr(raw_result, "is_error", False) or getattr(raw_result, "isError", False))
        row.update(response_diagnostics(text))
        if tool == "list_stock_discussions":
            self.find_post_url(raw)
        if tool in BY_NAME:
            r = normalize_tool_result(
                tool,
                canonical,
                raw,
                error=text if is_error else None,
                declared_provider=declared_provider_for_operation(tool),
                requested_as_of_date=self.today,
            )
            row.update(self.result_summary(r, job))
        else:
            row.update(
                status="error" if is_error else "success",
                classification="error" if is_error else "unregistered_response",
                reason_codes=[],
                raw_fields=field_paths(raw),
                declared_coverage=self.coverage(raw),
                error=self.clean(text) if is_error else "",
                payload_nonempty=nonempty(raw),
            )

    def find_post_url(self, raw: Any) -> None:
        if self.discussion_url:
            return
        if isinstance(raw, dict):
            for value in raw.values():
                self.find_post_url(value)
        elif isinstance(raw, list):
            for value in raw[:20]:
                self.find_post_url(value)
        elif isinstance(raw, str):
            match = re.search(r"https?://xueqiu\.com/\d+/\d+", raw)
            if match:
                self.discussion_url = match.group(0)

    def job(self, layer: str, tool: str, arguments: dict | None = None, label: str | None = None) -> dict:
        return {
            "layer": layer,
            "tool": tool,
            "arguments": self.args.get(tool, {}) if arguments is None else arguments,
            "case": label or tool,
        }

    async def run(self) -> None:
        print(
            json.dumps(
                {
                    "output": str(self.root),
                    "date": self.today,
                    "timeout": self.options.timeout,
                    "concurrency": self.options.concurrency,
                }
            ),
            flush=True,
        )
        try:
            await self.mcp.connect()
            inventory = [t.model_dump(by_alias=True) for t in self.mcp.tools]
            (self.root / "mcp_inventory.json").write_text(
                json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.record(
                {
                    "layer": "inventory",
                    "case": "mcp_initialize_list_tools",
                    "status": "success",
                    "classification": "connected",
                    "tool_count": len(inventory),
                    "observed": True,
                    "registered_tools": len(BY_NAME),
                    "attempt": 1,
                }
            )
        except Exception as exc:
            self.record(
                {
                    "layer": "inventory",
                    "case": "mcp_initialize_list_tools",
                    "status": "error",
                    "classification": "connection_failed",
                    "error": self.clean(exc),
                    "attempt": 1,
                }
            )
            inventory = []
        try:
            names = {t["name"] for t in inventory}
            gateway = [name for name, meta in BY_NAME.items() if meta.http_method != "INTERNAL"]
            jobs = [self.job("mcp", name) for name in gateway if name in names]
            jobs += [self.job("http", name) for name in gateway]
            for name in sorted(names - set(BY_NAME)):
                a = {"symbol": "SH600519"} if "ashare" in name else {}
                if name in {
                    "get_ashare_capital_flow",
                    "list_ashare_holders",
                    "list_ashare_reductions",
                    "list_ashare_unlocks",
                }:
                    a["as_of"] = f"{self.today}T00:00:00+08:00"
                if name == "list_stock_post_comments":
                    continue
                jobs.append(self.job("mcp", name, a))
            for name in gateway:
                if name not in names:
                    self.record(
                        {
                            "layer": "mcp",
                            "case": name,
                            "tool": name,
                            "status": "not_exposed",
                            "classification": "not_in_mcp_catalog",
                            "attempt": 1,
                            "observed": False,
                        }
                    )
            jobs += [self.job("skill", name, a) for name, a in self.skills.items()]
            jobs += [
                self.job("internal_native", name)
                for name, meta in BY_NAME.items()
                if meta.http_method == "INTERNAL" and name not in self.skills
            ]
            jobs += [
                self.job("primary_route", name)
                for name in BY_NAME
                if name not in self.skills and skill_route_for(name, self.args.get(name, {}))
            ]
            jobs += [
                self.job("runtime", name)
                for name in (
                    "get_market_quotes",
                    "get_ashare_sentiment",
                    "get_limit_up_count",
                    "list_limit_up_sectors",
                    "list_limit_up_stocks",
                    "get_stock_abnormal_reasons",
                    "internal_market_telegraph",
                    "internal_symbol_news",
                    "internal_news_digest",
                    "news_search",
                )
            ]
            for v in self.variants:
                for layer in ("mcp", "http"):
                    jobs.append(self.job(layer, v["tool"], v["arguments"], v["label"]))
            # Alternate layers so slow upstreams cannot delay all internal probes.
            queues = defaultdict(list)
            for job in jobs:
                queues[job["layer"]].append(job)
            mixed = []
            while any(queues.values()):
                for queue in queues.values():
                    if queue:
                        mixed.append(queue.pop(0))
            completed = {(row["layer"], row["case"]) for row in self.rows if row.get("attempt") == 1}
            await asyncio.gather(*(self.probe(job, 1) for job in mixed if (job["layer"], job["case"]) not in completed))
            if "list_stock_post_comments" in names:
                if self.discussion_url:
                    comment = self.job(
                        "mcp", "list_stock_post_comments", {"post_url": self.discussion_url, "max_pages": 1}
                    )
                    jobs.append(comment)
                    await self.probe(comment, 1)
                else:
                    self.record(
                        {
                            "layer": "mcp",
                            "case": "list_stock_post_comments",
                            "tool": "list_stock_post_comments",
                            "status": "not_tested",
                            "classification": "no_valid_post_url",
                            "attempt": 1,
                            "observed": False,
                        }
                    )
            first = {(r["layer"], r["case"]): r for r in self.rows if r.get("attempt") == 1}
            retry = [
                job
                for job in jobs
                if (
                    first[(job["layer"], job["case"])].get("classification")
                    not in {"data_returned", "declared_not_collected"}
                    or job["layer"] == "mcp"
                    and first[(job["layer"], job["case"])].get("session_mode") != "isolated"
                )
            ]
            print(json.dumps({"phase": "repeat_abnormal", "count": len(retry)}), flush=True)
            await asyncio.gather(*(self.probe(job, 2) for job in retry))
        finally:
            await self.http.aclose()
            await self.mcp.close()
            self.finish()

    def finish(self) -> None:
        groups = defaultdict(list)
        for row in self.rows:
            if row["layer"] != "inventory":
                groups[(row["layer"], row["case"])].append(row)
        items = []
        for (layer, case), rows in groups.items():
            last = rows[-1]
            clean = [r for r in rows if r.get("classification") == "data_returned"]
            items.append(
                {
                    "layer": layer,
                    "case": case,
                    "tool": last.get("tool"),
                    "attempts": len(rows),
                    "statuses": "/".join(str(r.get("status")) for r in rows),
                    "classification": last.get("classification"),
                    "repeat_outcome": "recovered"
                    if clean and len(rows) > 1
                    else "repeated_issue"
                    if len(rows) > 1
                    else "single_probe",
                    "ms": "/".join(str(r.get("ms", "")) for r in rows),
                    "business_datums": last.get("business_datums"),
                    "dated_business_datums": last.get("dated_business_datums"),
                    "reason_codes": sorted({c for r in rows for c in r.get("reason_codes", [])}),
                    "capability_gap": last.get("capability_gap"),
                    "error": last.get("error"),
                    "observed": last.get("observed", False),
                }
            )
        summary = {
            "date": self.today,
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
            "registered_operations": len(BY_NAME),
            "call_rows": len(self.rows),
            "classification_counts": dict(Counter(r["classification"] for r in items)),
            "interfaces": items,
        }
        (self.root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if items:
            with (self.root / "summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(items[0]))
                writer.writeheader()
                for item in items:
                    writer.writerow({**item, "reason_codes": ", ".join(item["reason_codes"])})
        print(json.dumps({k: v for k, v in summary.items() if k != "interfaces"}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume", help="Reuse completed probes from an earlier artifact directory")
    parser.add_argument("--timeout", type=float, default=32)
    parser.add_argument("--concurrency", type=int, default=3)
    options = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(Audit(options).run())


if __name__ == "__main__":
    main()
