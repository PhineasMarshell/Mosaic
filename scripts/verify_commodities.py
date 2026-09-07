"""验证 CCXT 连接的交易所中哪些商品永续合约可用。

用法：
    python -c "from scripts.verify_commodities import main; main()"
"""

import asyncio
import logging

import ccxt

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# 要验证的商品交易对（ccxt 格式）
COMMODITY_SYMBOLS = ["XAU/USDT:USDT", "XAG/USDT:USDT", "XPT/USDT:USDT"]

EXCHANGES_TO_CHECK = ["okx", "binance", "bybit"]


async def verify_symbols() -> dict[str, dict[str, bool]]:
    """检查每个交易所是否支持这些商品交易对。

    Returns:
        {exchange: {symbol: True/False}}
    """
    results: dict[str, dict[str, bool]] = {}

    for exchange_name in EXCHANGES_TO_CHECK:
        try:
            exchange_class = getattr(ccxt, exchange_name)
            exchange = exchange_class({"enableRateLimit": True})
        except AttributeError:
            logger.info("%s: ccxt class not available", exchange_name)
            continue

        results[exchange_name] = {}

        # Fetch markets once (cached by ccxt)
        try:
            markets = exchange.load_markets()
        except Exception as exc:
            logger.info("%s: failed to load markets: %s", exchange_name, exc)
            continue

        for symbol in COMMODITY_SYMBOLS:
            is_future = symbol.endswith(":USDT")
            if is_future and symbol in markets:
                market = markets[symbol]
                is_perpetual = market.get("swap", {}).get("future", False) or \
                               market.get("type") == "swap"
                results[exchange_name][symbol] = True
                status = f"✅ swap={market.get('type')}"
            elif symbol in markets:
                results[exchange_name][symbol] = True
                status = f"✅ spot={markets[symbol].get('type')}"
            else:
                results[exchange_name][symbol] = False
                status = "❌ not found"

            logger.info("%-10s %-25s → %s", exchange_name, symbol, status)

    return results


def main():
    print("=" * 70)
    print("OKX / Binance / Bybit 商品永续合约可用性验证")
    print("=" * 70)
    print()

    loop = asyncio.new_event_loop()
    try:
        results = loop.run_until_complete(verify_symbols())
    finally:
        loop.close()

    print()
    print("总结:")
    for exchange, syms in results.items():
        supported = [s for s, ok in syms.items() if ok]
        unsupported = [s for s, ok in syms.items() if not ok]
        if supported:
            print(f"  {exchange}: ✅ {', '.join(supported)}")
        if unsupported:
            print(f"  {exchange}: ❌ {', '.join(unsupported)}")


if __name__ == "__main__":
    main()
