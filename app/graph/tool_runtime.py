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
            ttl = _resolve_ttl(tool_name)
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
        # 内部工具直连（不走 Gateway）
        meta = resolve_tool_by_name(tool_name)
        if getattr(meta, "http_method", None) == "INTERNAL":
            return await self._execute_internal(tool_name, arguments)

        gateway_cls = self._gateway_class()
        async with gateway_cls(self.settings) as gateway:
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

            result = await gateway.call(tool_name, arguments)
            return result

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
