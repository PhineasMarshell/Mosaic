"""常见股票/指数代码映射表。

用于 Market Gateway 的 quote_tencent_quote_get 和 klines_market_klines_post 工具。
腾讯行情 API 的代码格式：
- A 股：SH + 代码（如 SH600519）、SZ + 代码（如 SZ000001）
- 指数：纯数字（如 000300 代表沪深 300）

此映射表覆盖常见股票和指数，后续可扩展。
"""

# ------------------------------------------------------------------ #
# A 股常用股票代码映射                                                  #
# ------------------------------------------------------------------ #

#: 股票名称 → 腾讯代码映射（A 股）
STOCK_CODE_MAP_A_SHARE: dict[str, str] = {
    # 白酒
    "贵州茅台": "SH600519",
    "五粮液": "SZ000858",
    "泸州老窖": "SZ000568",
    "山西汾酒": "SH600809",
    "洋河股份": "SZ002304",
    # 新能源
    "宁德时代": "SZ300750",
    "比亚迪": "SZ002594",
    "阳光电源": "SZ300274",
    "隆基绿能": "SH601012",
    # 半导体
    "中芯国际": "SH688981",
    "韦尔股份": "SH603501",
    "北方华创": "SZ002371",
    # 金融
    "招商银行": "SH600036",
    "平安银行": "SZ000001",
    "中国平安": "SH601318",
    "中信证券": "SH600030",
    # 消费
    "伊利股份": "SH600887",
    "海天味业": "SH603288",
    "蒙牛乳业": "HK02319",  # 港股
    # 互联网
    "腾讯控股": "HK00700",
    "阿里巴巴": "HK09988",
    "美团": "HK03690",
    "京东": "HK09618",
}

#: 指数名称 → 代码映射（A 股指数，腾讯行情 API 用纯数字）
INDEX_CODE_MAP_A_SHARE: dict[str, str] = {
    # 带空格（常见中文排版）
    "沪深 300": "000300",
    "上证 50": "000016",
    "创业板指": "399006",
    "中证 500": "000905",
    "中证 1000": "000852",
    "科创 50": "000688",
    "中证 100": "000903",
    "中证 200": "000904",
    "中证红利": "000922",
    "国证 2000": "399303",
    # 无空格（搜索关键词常用）
    "沪深300": "000300",
    "上证50": "000016",
    "中证500": "000905",
    "中证1000": "000852",
    "中证100": "000903",
    "中证200": "000904",
    "中证红利": "000922",
}

# ------------------------------------------------------------------ #
# Crypto 常用币对映射                                                   #
# ------------------------------------------------------------------ #

#: 币名 → Market Gateway symbol 映射
CRYPTO_SYMBOL_MAP: dict[str, str] = {
    "比特币": "BTCUSDT",
    "以太坊": "ETHUSDT",
    "BNB": "BNBUSDT",
    "Solana": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "Cardano": "ADAUSDT",
    "Avalanche": "AVAXUSDT",
    "Polkadot": "DOTUSDT",
    "Chainlink": "LINKUSDT",
}

# ------------------------------------------------------------------ #
# 大宗商品常用代码映射                                                  #
# ------------------------------------------------------------------ #

#: 商品名 → Market Gateway symbol 映射
# ⚠️ 注意：crypto 交易所极少支持实物大宗商品。以下仅黄金(OKX永续)可用，其余需独立数据源
COMMODITY_SYMBOL_MAP: dict[str, str] = {
    "黄金": "XAU/USDT:USDT",   # OKX 黄金永续合约 — ccxt 统一写法
    # 白银、铜、原油等暂无主流交易所现货交易对；待接入 Bloomberg/LME/API
    # "白银": "SILVERUSDT",       # TODO: OKX SILVER-USDT-SWAP → "XAG/USDT:USDT"
}

# ------------------------------------------------------------------ #
# 查询工具                                                              #
# ------------------------------------------------------------------ #

def _infer_index_exchange(index_code: str) -> str:
    """根据指数代码范围推断所属交易所。

    沪深指数编码规则：
      - 000xxx → 上证指数系列 → sh
      - 399xxx → 深证指数系列 → sz
    """
    if index_code.startswith(("399", "398")):
        return "sz"
    # 默认归入上证（大多数宽基指数）
    return "sh"


def resolve_stock_code(name: str) -> str | None:
    """从股票名称解析腾讯代码。

    Args:
        name: 股票/指数/币名/商品名

    Returns:
        腾讯行情 API 代码，或 None（未找到）
    """
    # 先查 A 股股票
    if name in STOCK_CODE_MAP_A_SHARE:
        return STOCK_CODE_MAP_A_SHARE[name]

    # 再查指数
    if name in INDEX_CODE_MAP_A_SHARE:
        raw = INDEX_CODE_MAP_A_SHARE[name]
        # 指数需要带交易所前缀供雪球等接口使用
        return f"{_infer_index_exchange(raw)}{raw}"

    # 再查 Crypto
    if name in CRYPTO_SYMBOL_MAP:
        return CRYPTO_SYMBOL_MAP[name]

    # 最后查商品
    if name in COMMODITY_SYMBOL_MAP:
        return COMMODITY_SYMBOL_MAP[name]

    return None


def get_all_mapping_names() -> list[str]:
    """返回所有已映射的名称列表（用于帮助文本）。"""
    names = set()
    names.update(STOCK_CODE_MAP_A_SHARE.keys())
    names.update(INDEX_CODE_MAP_A_SHARE.keys())
    names.update(CRYPTO_SYMBOL_MAP.keys())
    names.update(COMMODITY_SYMBOL_MAP.keys())
    return sorted(names)
