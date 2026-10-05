"""工具执行层 — 通用工具运行时。

职责：
- 打开 / 关闭 Gateway 连接（MCP / HTTP）
- 单次工具调用（含内部工具直连、不可用检测）
- 结果缓存写入（成功/部分成功才缓存）
- 结果截断（K 线防 prompt 失控）
- HK 北向内部工具执行
- DDGS 新闻舆情内部工具执行

analyst 节点通过此层执行分配给自己的工具。
"""

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.cache import _make_cache_key, _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.tool_registry import resolve_tool_by_name
from app.models.market import STATUS_ERROR, STATUS_PARTIAL, STATUS_SUCCESS, NormalizedDatum, ToolResult
from app.research.hk_northbound import fetch_all_hk_context as fetch_hk_context_data
from app.research.news_search import extract_news_entries as _extract_news_entries
from app.research.news_search import search_news as _search_news

logger = logging.getLogger(__name__)

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

    async def execute(self, tool_name: str, arguments: dict, called_signatures: set[str]) -> ToolResult:
        """执行单个工具调用。

        Args:
            tool_name: gateway 侧操作名（operationId），如 ``snapshot_get``
            arguments: 参数字典
            called_signatures: 已执行签名的集合（去重用）

        Returns:
            ToolResult — 包含 normalized 数据、状态、错误信息
        """
        # 检查缓存命中
        cached = self._check_cache(tool_name, arguments, called_signatures)
        if cached is not None:
            logger.info("Cache hit for %s", tool_name)
            return cached

        # 执行真实调用
        result = await self._do_execute(tool_name, arguments)

        # 只缓存成功/部分成功的结果。T5：缓存里存**深拷贝**，使后续 truncate
        # 对工作对象的原地改写不会污染缓存（旧实现存同一实例，truncate 会改到缓存）。
        if result.status in (STATUS_SUCCESS, STATUS_PARTIAL):
            cache_key = _make_cache_key(tool_name, arguments)
            ttl = _resolve_ttl(tool_name, self.settings)
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

    async def _do_execute(self, tool_name: str, arguments: dict) -> ToolResult:
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
            return await self._call_gateway(gateway, session["available"], tool_name, arguments)

        gateway_cls = self._gateway_class()  # 会话外：保持旧的逐次新建路径
        async with gateway_cls(self.settings) as gateway:
            return await self._call_gateway(gateway, None, tool_name, arguments)

    async def _call_gateway(self, gateway, available: set[str] | None, tool_name: str, arguments: dict) -> ToolResult:
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
                arguments=arguments,
                status=STATUS_ERROR,
                normalized=[],
                error=f"Tool not available: {tool_name}",
            )

        return await gateway.call(tool_name, arguments)

    def _check_cache(
        self,
        tool_name: str,
        arguments: dict,
        called_signatures: set[str],
    ) -> ToolResult | None:
        """检查缓存是否命中。"""
        signature = f"{tool_name}:{sorted(arguments.items())}"
        if signature in called_signatures:
            return None

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
                    market_cache.set(cache_key, result, ttl=self.settings.news_search_ttl_seconds)
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

        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status=STATUS_ERROR,
            normalized=[],
            error=f"Unknown internal tool: {tool_name}",
        )
