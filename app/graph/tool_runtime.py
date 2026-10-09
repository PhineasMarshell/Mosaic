"""工具执行层 — 通用工具运行时。

职责：
- 打开 / 关闭 Gateway 连接（MCP / HTTP）
- 单次工具调用（含内部工具直连、不可用检测）
- 结果缓存写入（成功/部分成功才缓存）
- 结果截断（K 线防 prompt 失控）
- HK 北向内部工具执行
- DDGS 新闻舆情内部工具执行
- 新闻面多源聚合内部工具执行（symbol_news / telegraph / news_digest）
- SEC EDGAR 美股基本面内部工具执行（us_fundamentals / us_filings_recent）

analyst 节点通过此层执行分配给自己的工具。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.cache import _make_cache_key, _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments, semantic_signature
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.tool_registry import resolve_tool_by_name
from app.models.market import STATUS_ERROR, STATUS_PARTIAL, STATUS_SUCCESS, NormalizedDatum, ToolResult
from app.research.hk_northbound import fetch_all_hk_context as fetch_hk_context_data
from app.research.news_search import extract_news_entries as _extract_news_entries
from app.research.news_search import search_news as _search_news
from app.research.news_sources import (
    NewsItem,
    clip,
    dedup_news,
    fetch_code_name_map,
    fetch_ddgs,
    fetch_google_rss,
    fetch_stock_news,
    fetch_telegraph,
    multi_source_count,
    rank_news,
    symbol_to_akshare6,
)
from app.research.news_sources.types import DdgsSourceError
from app.research.sec_edgar import fetch_us_fundamentals as _fetch_us_fundamentals
from app.research.sec_edgar import fetch_us_recent_filings as _fetch_us_filings_recent

logger = logging.getLogger(__name__)

#: SEC EDGAR XBRL 数据日级更新 → 基本面缓存 12h
_US_FUNDAMENTALS_TTL_SECONDS = 12 * 3600
#: 8-K 可能日内新增 → 申报文件缓存 1h
_US_FILINGS_TTL_SECONDS = 3600

#: SEC_EDGAR_CONTACT 未配置时的错误文案（SEC 公平访问政策要求声明访问身份）
_SEC_EDGAR_CONTACT_MISSING = "SEC_EDGAR_CONTACT 未配置（SEC 公平访问政策要求声明访问身份，见 .env.example）"

#: code↔name 名称表在 market_cache 的键（TTL 由 settings.news_code_name_ttl_seconds 控制）
_CODE_NAME_CACHE_KEY = "internal:code_name_map"


def _news_published_str(item: NewsItem) -> str:
    """NewsItem.published_at（naive datetime | None）→ 'YYYY-MM-DD HH:MM:SS' 字符串。"""
    return item.published_at.strftime("%Y-%m-%d %H:%M:%S") if item.published_at else ""


def _news_window(items: list[NewsItem]) -> tuple[str, str]:
    """deduped 池的时间窗，返回 ``(最新时刻, 最早时刻)``，无时间数据时为空串。

    命名陷阱（已踩过一次）：调用方把这两个值分别写进 meta 的 ``window_start`` /
    ``window_end``，但**语义是反的**——``window_start`` 装的是最新、``window_end``
    装的是最早。渲染「截止/最新」一律取第一个返回值，不要按 start/end 字面理解。
    """
    stamps = [i.published_at for i in items if i.published_at]
    if not stamps:
        return "", ""
    return max(stamps).strftime("%Y-%m-%d %H:%M"), min(stamps).strftime("%Y-%m-%d %H:%M")


def _build_news_datums(
    *,
    tool: str,
    meta: dict,
    items: list[NewsItem],
    text_limit: int,
    item_metric: str,
    meta_metric: str = "news_meta",
    include_stats: bool = True,
    instrument: str | None = None,
    include_url: bool = True,
    multi_source_titles: int | None = None,
) -> list[NormalizedDatum]:
    """NewsItem 列表 → NormalizedDatum（meta + item 条目 + stats），domain 统一 'media'。

    item 条目结构对齐 extract_news_entries 现口径：url 允许为空（电报无链接字段）。
    stats 的 per_source 在传入的 items（保留池）上统计；multi_source_titles 必须由
    调用方传**去重前合池**的统计结果（传 None 时退化在 items 上算，恒为 0）。
    """
    entries: list[NormalizedDatum] = [NormalizedDatum(domain="media", tool=tool, metric=meta_metric, value=meta)]
    for i, item in enumerate(items, 1):
        value: dict = {
            "title": item.title,
            "source": item.source,
            "published_at": _news_published_str(item),
            "text": clip(item.text, text_limit),
        }
        if include_url:
            value["url"] = item.url
        entries.append(
            NormalizedDatum(
                domain="media",
                tool=tool,
                metric=f"{item_metric}_{i}",
                value=value,
                timestamp=_news_published_str(item),
                source=item.source or None,
                instrument=instrument,
            )
        )
    per_source: dict[str, int] = {}
    for item in items:
        key = item.source or "未知来源"
        per_source[key] = per_source.get(key, 0) + 1
    if include_stats:
        multi = multi_source_count(items) if multi_source_titles is None else multi_source_titles
        entries.append(
            NormalizedDatum(
                domain="media",
                tool=tool,
                metric="news_stats",
                value={"per_source": per_source, "multi_source_titles": multi},
            )
        )
    return entries


def _news_status(sources_ok: list[str], sources_failed: list[str], kept_count: int) -> tuple[str, str | None]:
    """新闻聚合工具的状态裁决。

    全部已尝试源失败或聚合 0 条 → error（对齐「0 条 datum 判 error」的既有纪律）；
    恰一源失败 → partial（note 写明哪路挂了）；否则 success。
    """
    failed_note = "、".join(sources_failed)
    if not kept_count:
        if not sources_ok:
            return STATUS_ERROR, f"全部新闻源失败: {failed_note}"
        if sources_failed:
            return STATUS_ERROR, f"新闻源可用但 0 条结果（失败源: {failed_note}）"
        return STATUS_ERROR, "新闻源可用但 0 条结果"
    if sources_failed:
        return STATUS_PARTIAL, f"部分新闻源失败: {failed_note}"
    return STATUS_SUCCESS, None


def _normalize_us_fundamentals(data: dict) -> list[NormalizedDatum]:
    """将 sec_edgar.fetch_us_fundamentals 返回的 dict 转换为 NormalizedDatum。"""
    symbol = str(data.get("symbol") or "")
    entries: list[NormalizedDatum] = []
    for metric in ("revenue", "net_income", "eps", "gross_profit"):
        value = data.get(metric)
        if value is None:
            continue
        entries.append(
            NormalizedDatum(
                source="sec_edgar_xbrl",
                tool="us_fundamentals",
                metric=metric,
                value=value,
                domain="us_stock",
                instrument=symbol,
            )
        )
    caveats = data.get("caveats") or []
    if caveats:
        entries.append(
            NormalizedDatum(
                source="sec_edgar_xbrl",
                tool="us_fundamentals",
                metric="_caveats",
                value=caveats,
                domain="us_stock",
                instrument=symbol,
            )
        )
    entries.append(
        NormalizedDatum(
            source="sec_edgar_xbrl",
            tool="us_fundamentals",
            metric="_meta",
            value={"cik": data.get("cik"), "source_urls": data.get("source_urls") or []},
            domain="us_stock",
            instrument=symbol,
        )
    )
    return entries


def _normalize_us_filings(filings: list[dict]) -> list[NormalizedDatum]:
    """将 sec_edgar.fetch_us_recent_filings 返回的 list 转换为 NormalizedDatum。"""
    return [
        NormalizedDatum(
            source="sec_edgar_submissions",
            tool="us_filings_recent",
            metric=f"sec_filing_{f.get('form', 'unknown')}",
            value=f,
            domain="us_stock",
            instrument=None,
        )
        for f in filings
    ]


# T18c：会话状态必须"每请求一份"。单例图的节点实例（及其 ToolRuntime）被并发请求共享，
# 会话状态放实例属性上会串台——第二个请求进入 gateway_session 被当成"嵌套会话"复用
# 第一个请求的客户端，先结束者关闭客户端后，后到者在途的调用随即失败。
# ContextVar 语义天然"每个 asyncio task 一份"：asyncio.create_task 复制当前 context，
# 子任务看到会话、兄弟任务看不到。gateway/available 成组放 dict，一次 set 成组读写。
_session: ContextVar[dict | None] = ContextVar("gateway_session", default=None)


def _normalize_hk_entries(hk_data: dict) -> list[NormalizedDatum]:
    """将 eastmoney_kline_api 返回的 dict 转换为 NormalizedDatum。"""
    entries: list[NormalizedDatum] = []
    for k, v in hk_data.items():
        if k.startswith("_"):
            continue
        entries.append(
            NormalizedDatum(
                source="eastmoney_kline_api",
                tool=k,
                metric=k,
                value=v,
                domain="hk_stock",
            )
        )
    return entries


class ToolRuntime:
    """通用工具运行时 —— 各 analyst 节点共享此层。

    各 analyst 节点通过此层执行分配给自己的工具，支持并发不覆盖。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # T18：analyst 运行级别复用的 Gateway 客户端（gateway_session 打开期间按需建连）。
        # 旧实现每个工具调用都 async with 新建客户端——MCP 模式下等于每次 spawn
        # 子进程 + 握手 + list_tools，一轮 12 个工具就是 12 次。
        # T18c：会话状态放在模块级 ContextVar `_session`（每 asyncio task 一份），
        # 不要再往 self.* 上放会话状态——单例图的节点实例被并发请求共享。

    @asynccontextmanager
    async def gateway_session(self):
        """在 analyst 一次运行（一个节点调用）内复用同一个 Gateway 客户端。

        用法：``async with runtime.gateway_session(): ... execute(...) ...``。
        进入时**只标记会话**、不连接；首个需要 Gateway 的工具调用触发
        ``_ensure_gateway`` 建连并解析可用工具集。只执行内部工具的 analyst
        因此完全不碰 Gateway。退出（含异常）时若已建连则关闭。可重入
        （同一 task 内嵌套开两次会话 → 复用，由外层负责关闭）。
        未在会话内直接调 execute() 仍走旧的逐次新建路径（兼容单工具调用方）。
        """
        if _session.get() is not None:  # 同一 task 内嵌套 → 复用
            yield
            return
        token = _session.set({"gateway": None, "available": None})
        try:
            yield
        finally:
            session = _session.get()
            _session.reset(token)
            if session is not None and session["gateway"] is not None:
                await self._close_gateway(session)

    async def _ensure_gateway(self, session: dict):
        """按需创建并连接 Gateway 客户端，解析可用工具集（状态存入 session dict）。

        ``__aenter__`` 失败时把 ``session["gateway"]`` 复位为 None 并把异常抛出去，
        不吞成默认值；失败路径由 T12 的 ``mcp_client.close()`` 自清理。
        """
        if session["gateway"] is None:
            gateway = self._gateway_class()(self.settings)
            session["gateway"] = gateway
            try:
                await gateway.__aenter__()
            except Exception:
                session["gateway"] = None
                raise
            if self.settings.market_gateway_mode.lower() == "mcp":
                session["available"] = {tool.name for tool in gateway.tools}
            else:
                session["available"] = {t.tool_name for t in self._http_allowed_tools()}
        return session["gateway"]

    async def _close_gateway(self, session: dict):
        """关闭本请求会话内按需建立的 Gateway 客户端（未建连则无事发生）。"""
        gateway = session["gateway"]
        session["gateway"] = None
        session["available"] = None
        if gateway is None:
            return
        try:
            await gateway.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning("Gateway client close failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 公共 API                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _signature(tool_name: str, arguments: dict) -> str:
        """同一次运行内的稳定语义签名（支持嵌套参数）。"""
        return semantic_signature(tool_name, arguments)

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        called_signatures: set[str],
        deadline: float | None = None,
    ) -> ToolResult:
        """执行单个工具调用。

        Args:
            tool_name: gateway 侧操作名（operationId），如 ``snapshot_get``
            arguments: 参数字典
            called_signatures: 已执行签名的集合（去重用）
            deadline: T23 — 本工具的预算截止时刻（monotonic 秒），下发到 gateway
                客户端做重试预算感知；None 表示无预算约束

        Returns:
            ToolResult — 包含 normalized 数据、状态、错误信息
        """
        try:
            arguments = canonicalize_tool_arguments(tool_name, arguments)
        except Exception as exc:
            try:
                logical_key = resolve_tool_by_name(tool_name).key
            except Exception:
                logical_key = None
            return ToolResult(
                tool=tool_name,
                operation_id=tool_name,
                tool_key=logical_key,
                arguments=dict(arguments or {}),
                status=STATUS_ERROR,
                normalized=[],
                error=f"Invalid arguments: {exc}",
            )
        signature = self._signature(tool_name, arguments)

        # T27：同一次运行里同签名已真实执行过 → 明确跳过（partial + note）。
        # 旧实现这里 return None 被调用方当成"无缓存"，同签名第 3 次调用
        # 会**绕过缓存**再打一次真实网关——与"去重"意图相反。
        if signature in called_signatures:
            logger.info("Skipping duplicate call %s (already executed this run)", tool_name)
            return ToolResult(
                tool=tool_name,
                operation_id=tool_name,
                tool_key=resolve_tool_by_name(tool_name).key,
                arguments=arguments,
                status=STATUS_PARTIAL,
                partial=True,
                note="重复调用已跳过：同签名工具本次运行已执行过，结果已在 state 中",
                normalized=[],
            )

        # 检查缓存命中
        cached = self._check_cache(tool_name, arguments, called_signatures, signature)
        if cached is not None:
            logger.info("Cache hit for %s", tool_name)
            cached.operation_id = cached.operation_id or tool_name
            cached.arguments = arguments
            return cached

        # 执行真实调用
        result = await self._do_execute(tool_name, arguments, deadline=deadline)
        # Keep trace fields present even when a Gateway/mock implementation
        # constructs an older ToolResult shape.
        result.operation_id = result.operation_id or tool_name
        result.arguments = arguments

        # 只缓存成功/部分成功的结果。T5：缓存里存**深拷贝**，使后续 truncate
        # 对工作对象的原地改写不会污染缓存（旧实现存同一实例，truncate 会改到缓存）。
        if result.status in (STATUS_SUCCESS, STATUS_PARTIAL):
            # T27：真实执行成功后**立即**登记签名（旧实现只在缓存命中时登记，
            # 导致 called_signatures 阻止不了重复调用）。
            called_signatures.add(signature)
            cache_key = _make_cache_key(tool_name, arguments)
            ttl = _resolve_ttl(tool_name, self.settings, arguments)
            market_cache.set(cache_key, result.model_copy(deep=True), ttl=ttl)

        return result

    def truncate(self, result: ToolResult) -> None:
        """截断 result.normalized，防止下游 evaluator/reasoning prompt 失控。"""
        if len(result.normalized) <= 200:
            return
        # T5：原始条数必须在切片 / 追加 note **之前**取，note 自身不算进计数。
        original_count = len(result.normalized)
        # 保留**末尾** 200：K 线按时间升序，末尾才是最新数据；
        # 旧实现 [:200] 保留最旧，note 却写"保留最近 200"。
        kept = result.normalized[-200:]
        kept.append(
            NormalizedDatum(
                source="truncation_note",
                tool=result.tool,
                metric="_truncated_count",
                value=f"原始 {original_count} 项，保留最新 200 条（另加本说明条，不计入 200）",
                status=STATUS_PARTIAL,
                partial=True,
            )
        )
        result.normalized = kept
        # 截断=数据不完整：必须置 partial 并让状态反映出来；
        # 旧实现不置标志，静默保持 success。
        result.status = STATUS_PARTIAL
        result.partial = True

    async def inject_hk_context(self, results: list[ToolResult]) -> None:
        """HK 域专属：注入北向资金 + 恒生指数行情（内部直连，不走 Gateway）。"""
        try:
            hk_data = await fetch_hk_context_data()
            filtered = {
                k: v
                for k, v in hk_data.items()
                if not k.startswith("_")
                and k in ("northbound", "sh_connect", "sz_connect", "hs_index", "hs_tech_index")
            }
            normalized = _normalize_hk_entries(filtered)
            results.append(
                ToolResult(
                    tool="hk_northbound_daily",
                    arguments={},
                    status=STATUS_SUCCESS if normalized else STATUS_ERROR,
                    normalized=normalized,
                    error=None if normalized else "All HK context sources failed",
                )
            )
            logger.info("HK context injected: %d entries from eastmoney", len(normalized))
        except Exception as exc:
            logger.warning("HK context injection failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------ #
    # 内部实现                                                             #
    # ------------------------------------------------------------------ #

    async def _do_execute(self, tool_name: str, arguments: dict, deadline: float | None = None) -> ToolResult:
        """单次工具调用的核心逻辑。"""
        # 内部工具直连（不走 Gateway）—— 必须在会话分支之前 return，
        # 只用内部工具的 analyst 不得触发建连（T18b）
        meta = resolve_tool_by_name(tool_name)
        if getattr(meta, "http_method", None) == "INTERNAL":
            return await self._execute_internal(tool_name, arguments)

        # T18：gateway_session 打开期间复用同一个客户端；T18b：按需建连；
        # T18c：会话状态在 ContextVar 里，每请求一份
        session = _session.get()
        if session is not None:
            gateway = await self._ensure_gateway(session)
            return await self._call_gateway(gateway, session["available"], tool_name, arguments, deadline=deadline)

        gateway_cls = self._gateway_class()  # 会话外：保持旧的逐次新建路径
        async with gateway_cls(self.settings) as gateway:
            return await self._call_gateway(gateway, None, tool_name, arguments, deadline=deadline)

    async def _call_gateway(
        self,
        gateway,
        available: set[str] | None,
        tool_name: str,
        arguments: dict,
        deadline: float | None = None,
    ) -> ToolResult:
        """通过已连接的 gateway 执行一次调用；available 为 None 时按模式现场解析。"""
        if available is None:
            available = (
                {tool.name for tool in gateway.tools}
                if self.settings.market_gateway_mode.lower() == "mcp"
                else {t.tool_name for t in self._http_allowed_tools()}
            )

        if tool_name not in available:
            return ToolResult(
                tool=tool_name,
                operation_id=tool_name,
                arguments=arguments,
                status=STATUS_ERROR,
                normalized=[],
                error=f"Tool not available: {tool_name}",
            )

        return await gateway.call(tool_name, arguments, deadline=deadline)

    def _check_cache(
        self,
        tool_name: str,
        arguments: dict,
        called_signatures: set[str],
        signature: str,
    ) -> ToolResult | None:
        """检查缓存是否命中；命中时登记签名（本次运行内不再重复执行）。"""
        cache_key = _make_cache_key(tool_name, arguments)
        cached = market_cache.get(cache_key)
        if cached is not None:
            called_signatures.add(signature)
            # T5：返回深拷贝，调用方 truncate 的原地改写不影响缓存内实例。
            return cached.model_copy(deep=True)
        return None

    def _gateway_class(self):
        mode = self.settings.market_gateway_mode.lower().strip()
        if mode == "mcp":
            return MarketGatewayClient
        if mode == "http":
            return MarketGatewayHttpClient
        raise ValueError(
            f"Unsupported MARKET_GATEWAY_MODE={self.settings.market_gateway_mode!r}; expected 'mcp' or 'http'"
        )

    def _http_allowed_tools(self):
        from app.gateway.tool_registry import ALL_TOOLS

        return ALL_TOOLS

    async def _execute_internal(self, tool_name: str, arguments: dict) -> ToolResult:
        """执行内部工具（不走 Gateway，直连外部 API）。"""
        if tool_name == "internal_hk_northbound":
            try:
                data = await fetch_hk_context_data()
                normalized = _normalize_hk_entries(data)
                logger.info("Internal tool hk_northbound: fetched %d entries", len(normalized))
                return ToolResult(
                    tool="hk_northbound_daily",
                    arguments=arguments,
                    status=STATUS_SUCCESS,
                    normalized=normalized,
                )
            except Exception as exc:
                logger.warning("Internal tool hk_northbound failed: %s", exc)
                return ToolResult(
                    tool="hk_northbound_daily",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=f"Internal fetch failed: {exc}",
                )

        if tool_name == "internal_hk_index":
            try:
                data = await fetch_hk_context_data()
                hs_data = {
                    "hs_index": data.get("hs_index", {}),
                    "hs_tech_index": data.get("hs_tech_index", {}),
                }
                normalized = _normalize_hk_entries({k: v for k, v in hs_data.items() if v})
                logger.info("Internal tool hk_index: fetched %d entries", len(normalized))
                return ToolResult(
                    tool="hk_index_snapshot",
                    arguments=arguments,
                    status=STATUS_SUCCESS,
                    normalized=normalized,
                )
            except Exception as exc:
                logger.warning("Internal tool hk_index failed: %s", exc)
                return ToolResult(
                    tool="hk_index_snapshot",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=f"Internal fetch failed: {exc}",
                )

        if tool_name == "news_search":
            cache_key = _make_cache_key(tool_name, arguments)
            cached = market_cache.get(cache_key)
            if cached is not None:
                logger.info("Cache hit for news_search")
                return cached
            try:
                query = arguments.get("query", "")
                max_results = int(arguments.get("max_results", 5))
                time_limit = arguments.get("time_limit", "d")
                raw = await _search_news(query, max_results=max_results, time_limit=time_limit)
                # DDGS 返回每个 item 有 {date, title, body, url, image, source}
                normalized = _extract_news_entries(raw)
                meta = raw.get("meta", {})
                logger.info(
                    "Internal tool news_search: fetched %d entries (status=%s)",
                    len(normalized),
                    meta.get("status", ""),
                )
                status = STATUS_SUCCESS if raw.get("news") else STATUS_ERROR
                result = ToolResult(
                    tool="news_search",
                    arguments=arguments,
                    status=status,
                    normalized=normalized,
                    error=meta.get("error"),
                )
                if status == STATUS_SUCCESS:
                    # TTL 分档：time_limit=d 是「今天的新闻」，缓存 6 小时是错误语义
                    # （A2.4 实测发现并修正）—— d/w/m 三档各自配 TTL。
                    ttl_tier = {
                        "d": self.settings.news_ttl_day_seconds,
                        "w": self.settings.news_ttl_week_seconds,
                        "m": self.settings.news_ttl_month_seconds,
                    }
                    ttl = ttl_tier.get(str(time_limit), self.settings.news_ttl_week_seconds)
                    market_cache.set(cache_key, result, ttl=ttl)
                return result
            except Exception as exc:
                logger.warning("Internal tool news_search failed: %s", exc)
                return ToolResult(
                    tool="news_search",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=f"Internal fetch failed: {exc}",
                )

        if tool_name == "internal_symbol_news":
            return await self._run_internal_symbol_news(arguments)

        if tool_name == "internal_market_telegraph":
            return await self._run_internal_market_telegraph(arguments)

        if tool_name == "internal_news_digest":
            return await self._run_internal_news_digest(arguments)

        if tool_name == "internal_us_fundamentals":
            # SEC 公平访问政策门闩：未声明访问身份不得发起任何网络请求
            if not self.settings.sec_edgar_contact:
                return ToolResult(
                    tool="us_fundamentals",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=_SEC_EDGAR_CONTACT_MISSING,
                )
            cache_key = _make_cache_key(tool_name, arguments)
            cached = market_cache.get(cache_key)
            if cached is not None:
                logger.info("Cache hit for us_fundamentals")
                return cached
            try:
                symbol = str(arguments.get("symbol", "")).strip()
                if not symbol:
                    return ToolResult(
                        tool="us_fundamentals",
                        arguments=arguments,
                        status=STATUS_ERROR,
                        normalized=[],
                        error="缺少必填参数 symbol（美股裸代码，如 AAPL）",
                    )
                data = await _fetch_us_fundamentals(symbol)
                normalized = _normalize_us_fundamentals(data)
                # 有至少一个财务指标才算成功（单指标缺失走 caveat，不整体失败）
                has_metric = any(e.metric in ("revenue", "net_income", "eps", "gross_profit") for e in normalized)
                logger.info("Internal tool us_fundamentals: %s → %d entries", symbol, len(normalized))
                result = ToolResult(
                    tool="us_fundamentals",
                    arguments=arguments,
                    status=STATUS_SUCCESS if has_metric else STATUS_ERROR,
                    normalized=normalized,
                    error=None if has_metric else "SEC EDGAR 无该公司的有效 10-K/10-Q 财务数据（见 caveats）",
                )
                if result.status == STATUS_SUCCESS:
                    market_cache.set(cache_key, result, ttl=_US_FUNDAMENTALS_TTL_SECONDS)
                return result
            except Exception as exc:
                logger.warning("Internal tool us_fundamentals failed: %s", exc)
                return ToolResult(
                    tool="us_fundamentals",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=f"Internal fetch failed: {exc}",
                )

        if tool_name == "internal_us_filings_recent":
            if not self.settings.sec_edgar_contact:
                return ToolResult(
                    tool="us_filings_recent",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=_SEC_EDGAR_CONTACT_MISSING,
                )
            cache_key = _make_cache_key(tool_name, arguments)
            cached = market_cache.get(cache_key)
            if cached is not None:
                logger.info("Cache hit for us_filings_recent")
                return cached
            try:
                symbol = str(arguments.get("symbol", "")).strip()
                if not symbol:
                    return ToolResult(
                        tool="us_filings_recent",
                        arguments=arguments,
                        status=STATUS_ERROR,
                        normalized=[],
                        error="缺少必填参数 symbol（美股裸代码，如 AAPL）",
                    )
                filings = await _fetch_us_filings_recent(symbol, limit=10)
                normalized = _normalize_us_filings(filings)
                logger.info("Internal tool us_filings_recent: %s → %d filings", symbol, len(normalized))
                result = ToolResult(
                    tool="us_filings_recent",
                    arguments=arguments,
                    status=STATUS_SUCCESS if normalized else STATUS_ERROR,
                    normalized=normalized,
                    error=None if normalized else "SEC EDGAR 未返回符合条件的申报文件",
                )
                if result.status == STATUS_SUCCESS:
                    market_cache.set(cache_key, result, ttl=_US_FILINGS_TTL_SECONDS)
                return result
            except Exception as exc:
                logger.warning("Internal tool us_filings_recent failed: %s", exc)
                return ToolResult(
                    tool="us_filings_recent",
                    arguments=arguments,
                    status=STATUS_ERROR,
                    normalized=[],
                    error=f"Internal fetch failed: {exc}",
                )

        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status=STATUS_ERROR,
            normalized=[],
            error=f"Unknown internal tool: {tool_name}",
        )

    # ------------------------------------------------------------------ #
    # 新闻面多源聚合工具（A2）                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clamp_int(value, default: int, low: int, high: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, n))

    async def _fetch_code_name(self, symbol6: str) -> str:
        """code↔name 名称表（market_cache 长 TTL；表取不到不阻塞，用 6 位码兜底）。"""
        cached = market_cache.get(_CODE_NAME_CACHE_KEY)
        if isinstance(cached, dict):
            return str(cached.get(symbol6, ""))
        try:
            code_map = await fetch_code_name_map(timeout=60.0)
            market_cache.set(_CODE_NAME_CACHE_KEY, code_map, ttl=self.settings.news_code_name_ttl_seconds)
            return str(code_map.get(symbol6, ""))
        except Exception as exc:  # noqa: BLE001 名称表失败不阻塞主路
            logger.warning("Internal tool news: code_name map fetch failed (non-fatal): %s", exc)
            return ""

    async def _run_internal_symbol_news(self, arguments: dict) -> ToolResult:
        """internal_symbol_news — A 股个股新闻聚合（东财个股新闻 + Google 资讯）。"""
        tool = "symbol_news"
        cache_key = _make_cache_key("internal_symbol_news", arguments)
        cached = market_cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for internal_symbol_news")
            return cached

        symbol_raw = str(arguments.get("symbol", "")).strip()
        symbol6 = symbol_to_akshare6(symbol_raw)
        if not symbol6:
            return ToolResult(
                tool=tool,
                arguments=arguments,
                status=STATUS_ERROR,
                normalized=[],
                error=f"无法识别的 A 股代码: {symbol_raw!r}（需 SH600519/SZ000001/600519 等 6 位代码，不猜）",
            )
        top_k = self._clamp_int(arguments.get("top_k", 8), default=8, low=1, high=15)
        include_google = bool(arguments.get("include_google", True))
        timeout = float(self.settings.news_source_timeout_seconds)

        name = await self._fetch_code_name(symbol6)
        google_query = name or symbol6

        sources_ok: list[str] = []
        sources_failed: list[str] = []
        east_items: list[NewsItem] = []
        google_items: list[NewsItem] = []

        tasks = [fetch_stock_news(symbol6, timeout=timeout)]
        if include_google:
            tasks.append(fetch_google_rss(google_query, timeout=timeout))
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        east_out = outcomes[0]
        if isinstance(east_out, BaseException):
            sources_failed.append("eastmoney")
            logger.warning("Internal tool internal_symbol_news: eastmoney failed: %s", east_out)
        else:
            sources_ok.append("eastmoney")
            east_items = east_out
        if include_google:
            google_out = outcomes[1]
            if isinstance(google_out, BaseException):
                sources_failed.append("google")
                logger.warning("Internal tool internal_symbol_news: google rss failed: %s", google_out)
            else:
                sources_ok.append("google")
                google_items = google_out

        pool = east_items + google_items
        total_before_dedup = len(pool)
        # multi_source_titles 必须在**去重前**算：dedup 会把同标题跨源条目折叠成一条
        multi_source = multi_source_count(pool)
        deduped, dup_count = dedup_news(pool)
        ranked = rank_news(deduped)
        kept = ranked[:top_k]
        window_start, window_end = _news_window(deduped)
        meta = {
            "symbol": symbol_raw,
            "symbol6": symbol6,
            "name": name,
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "total_before_dedup": total_before_dedup,
            "duplicates_removed": dup_count,
            "kept": len(kept),
            "window_start": window_start,
            "window_end": window_end,
            "multi_source_titles": multi_source,
        }
        status, note = _news_status(sources_ok, sources_failed, len(kept))
        normalized = (
            _build_news_datums(
                tool=tool,
                meta=meta,
                items=kept,
                text_limit=int(self.settings.news_max_text_chars),
                item_metric="news_top",
                instrument=symbol6,
                multi_source_titles=multi_source,
            )
            if status != STATUS_ERROR
            else []
        )
        result = ToolResult(
            tool=tool,
            arguments=arguments,
            status=status,
            partial=status == STATUS_PARTIAL,
            normalized=normalized,
            error=note,
            note=note,
        )
        logger.info(
            "Internal tool internal_symbol_news: %s → kept %d/%d (status=%s)",
            symbol6,
            len(kept),
            total_before_dedup,
            status,
        )
        if status != STATUS_ERROR:
            market_cache.set(cache_key, result, ttl=self.settings.news_symbol_ttl_seconds)
        return result

    async def _run_internal_market_telegraph(self, arguments: dict) -> ToolResult:
        """internal_market_telegraph — 财联社电报快讯（全市场最新电报流）。"""
        tool = "telegraph"
        cache_key = _make_cache_key("internal_market_telegraph", arguments)
        cached = market_cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for %s", "internal_market_telegraph")
            return cached

        top_k = self._clamp_int(arguments.get("top_k", 15), default=15, low=1, high=30)
        keyword = str(arguments.get("keyword", "")).strip()
        try:
            items = await fetch_telegraph(timeout=float(self.settings.news_source_timeout_seconds))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Internal tool telegraph failed: %s", exc)
            return ToolResult(
                tool=tool,
                arguments=arguments,
                status=STATUS_ERROR,
                normalized=[],
                error=f"财联社电报抓取失败: {exc}",
            )
        total = len(items)
        if keyword:
            items = [i for i in items if keyword in i.title or keyword in i.text]
        ranked = rank_news(items)[:top_k]
        window_start, window_end = _news_window(ranked)
        meta = {
            "total": total,
            "kept": len(ranked),
            "keyword": keyword,
            "window_start": window_start,
            "window_end": window_end,
        }
        status = STATUS_SUCCESS if ranked else STATUS_ERROR
        normalized = (
            _build_news_datums(
                tool=tool,
                meta=meta,
                items=ranked,
                text_limit=400,
                item_metric="telegraph",
                meta_metric="telegraph_meta",
                include_stats=False,
                include_url=False,
            )
            if status == STATUS_SUCCESS
            else []
        )
        result = ToolResult(
            tool=tool,
            arguments=arguments,
            status=status,
            normalized=normalized,
            error=None if status == STATUS_SUCCESS else "财联社电报 0 条结果",
        )
        logger.info("Internal tool telegraph: total %d → kept %d", total, len(ranked))
        if status == STATUS_SUCCESS:
            market_cache.set(cache_key, result, ttl=self.settings.news_telegraph_ttl_seconds)
        return result

    async def _run_internal_news_digest(self, arguments: dict) -> ToolResult:
        """internal_news_digest — 跨市场主题/事件聚合（Google 资讯 + DDGS）。"""
        tool = "news_digest"
        cache_key = _make_cache_key("internal_news_digest", arguments)
        cached = market_cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for %s", "internal_news_digest")
            return cached

        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(
                tool=tool,
                arguments=arguments,
                status=STATUS_ERROR,
                normalized=[],
                error="缺少必填参数 query（搜索关键词）",
            )
        top_k = self._clamp_int(arguments.get("top_k", 8), default=8, low=1, high=15)
        time_limit = str(arguments.get("time_limit", "d")).strip().lower()
        if time_limit not in ("d", "w", "m"):
            time_limit = "d"
        timeout = float(self.settings.news_source_timeout_seconds)

        sources_ok: list[str] = []
        sources_failed: list[str] = []
        google_items: list[NewsItem] = []
        ddgs_items: list[NewsItem] = []

        google_out, ddgs_out = await asyncio.gather(
            self._fetch_google_for_digest(query, timeout),
            self._fetch_ddgs_with_retry(query, time_limit, timeout),
            return_exceptions=True,
        )
        if isinstance(google_out, BaseException):
            sources_failed.append("google")
            logger.warning("Internal tool news_digest: google rss failed: %s", google_out)
        else:
            sources_ok.append("google")
            google_items = google_out
        if isinstance(ddgs_out, BaseException):
            sources_failed.append("ddgs")
            logger.warning("Internal tool news_digest: ddgs failed: %s", ddgs_out)
        else:
            sources_ok.append("ddgs")
            ddgs_items = ddgs_out

        pool = google_items + ddgs_items
        total_before_dedup = len(pool)
        multi_source = multi_source_count(pool)
        deduped, dup_count = dedup_news(pool)
        ranked = rank_news(deduped)
        kept = ranked[:top_k]
        window_start, window_end = _news_window(deduped)
        meta = {
            "query": query,
            "time_limit": time_limit,
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "total_before_dedup": total_before_dedup,
            "duplicates_removed": dup_count,
            "kept": len(kept),
            "window_start": window_start,
            "window_end": window_end,
            "multi_source_titles": multi_source,
        }
        status, note = _news_status(sources_ok, sources_failed, len(kept))
        normalized = (
            _build_news_datums(
                tool=tool,
                meta=meta,
                items=kept,
                text_limit=int(self.settings.news_max_text_chars),
                item_metric="news_top",
                multi_source_titles=multi_source,
            )
            if status != STATUS_ERROR
            else []
        )
        result = ToolResult(
            tool=tool,
            arguments=arguments,
            status=status,
            partial=status == STATUS_PARTIAL,
            normalized=normalized,
            error=note,
            note=note,
        )
        logger.info(
            "Internal tool news_digest: %r → kept %d/%d (status=%s)", query, len(kept), total_before_dedup, status
        )
        if status != STATUS_ERROR:
            market_cache.set(cache_key, result, ttl=self.settings.news_digest_ttl_seconds)
        return result

    @staticmethod
    async def _fetch_google_for_digest(query: str, timeout: float) -> list[NewsItem]:
        return await fetch_google_rss(query, timeout=timeout)

    @staticmethod
    async def _fetch_ddgs_with_retry(query: str, time_limit: str, timeout: float) -> list[NewsItem]:
        """DDGS 限流实证：首败自动重试 1 次（只许一次，不许更多）。"""
        try:
            return await fetch_ddgs(query, time_limit=time_limit, timeout=timeout)
        except DdgsSourceError:
            logger.info("Internal tool news_digest: ddgs first attempt failed, retrying once")
            return await fetch_ddgs(query, time_limit=time_limit, timeout=timeout)
