"""
AlphaFinder Pro v3 — Central Configuration
Windows-compatible: no f-string walrus operators, no 3.10+ syntax
"""
import os
import tempfile

# ── Cache directory (Windows-safe temp path) ──────────────────────────
# Uses system temp dir so it works on all platforms
CACHE_DIR = os.path.join(tempfile.gettempdir(), "alphafinder_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── FRED API (free, no key for basic usage) ───────────────────────────
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_KEY  = "d60ac5ca6a15e39571aa62bb4bb6ef19"

FRED_SERIES = {
    "VIX": "VIXCLS",
    "INDPRO": "INDPRO",
    "HOUST": "HOUST",
    "PERMIT": "PERMIT"
}

GEX_PROXY_MAPPING = {
    "SPY": "SPX500",
    "QQQ": "NAS100",
    "DIA": "US30",
    "GLD": "XAUUSD",
    "USO": "USOIL",
    "BITO": "BTCUSD"
}

# ── Forex pair definitions ────────────────────────────────────────────
FOREX_PAIRS = {
    "EURUSD": ("EUR","USD"), "GBPUSD": ("GBP","USD"),
    "USDJPY": ("USD","JPY"), "AUDUSD": ("AUD","USD"),
    "NZDUSD": ("NZD","USD"), "USDCAD": ("USD","CAD"),
    "USDCHF": ("USD","CHF"), "EURJPY": ("EUR","JPY"),
    "GBPJPY": ("GBP","JPY"), "AUDJPY": ("AUD","JPY"),
    "NZDJPY": ("NZD","JPY"), "EURAUD": ("EUR","AUD"),
    "GBPAUD": ("GBP","AUD"), "EURGBP": ("EUR","GBP"),
    "AUDCAD": ("AUD","CAD"), "NZDCAD": ("NZD","CAD"),
    "CADJPY": ("CAD","JPY"), "GBPCAD": ("GBP","CAD"),
    "AUDNZD": ("AUD","NZD"), "EURNZD": ("EUR","NZD"),
    "GBPNZD": ("GBP","NZD"), "CHFJPY": ("CHF","JPY"),
}

INDICES = {
    "SPX500": ("^GSPC", "USD"),
    "NAS100": ("^NDX",  "USD"),
    "US30":   ("^DJI",  "USD"),
    "UK100":  ("^FTSE", "GBP"),
    "GER40":  ("^GDAXI","EUR"),
    "JP225":  ("^N225", "JPY"),
    "JP225US": ("^N225", "JPY"),
    "US2000L": ("^RUT", "USD"),
    "US10Y": ("^TNX", "USD"),
    "VIX": ("^VIX", "USD"),
}

COMMODITIES = {
    "XAUUSD": ("GC=F", "USD"),
    "XAGUSD": ("SI=F", "USD"),
    "USOIL":  ("CL=F", "USD"),
    "WTICOU": ("CL=F", "USD"),
}

CRYPTO = {
    "BTCUSD": ("BTC-USD", "USD"),
    "ETHUSD": ("ETH-USD", "USD"),
}

DXY = {
    "DXY": ("DX-Y.NYB", "USD"),
}

# Forex yfinance tickers
FOREX_TICKERS = {
    "EURUSD": "EURUSD=X",  "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",     "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",  "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",     "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",  "AUDJPY": "AUDJPY=X",
    "NZDJPY": "NZDJPY=X",  "EURAUD": "EURAUD=X",
    "GBPAUD": "GBPAUD=X",  "EURGBP": "EURGBP=X",
    "AUDCAD": "AUDCAD=X",  "NZDCAD": "NZDCAD=X",
    "CADJPY": "CADJPY=X",  "GBPCAD": "GBPCAD=X",
    "AUDNZD": "AUDNZD=X",  "EURNZD": "EURNZD=X",
    "GBPNZD": "GBPNZD=X",  "CHFJPY": "CHFJPY=X",
}

# All tickers combined
ALL_TICKERS = dict(FOREX_TICKERS)
for sym, (tkr, _) in list(INDICES.items()) + list(COMMODITIES.items()) + list(CRYPTO.items()) + list(DXY.items()):
    ALL_TICKERS[sym] = tkr

# COT market name matching
COT_KEYS = {
    "NIKKEI": ["NIKKEI STOCK AVERAGE YEN DENOM"],
    "PLATINUM": ["PLATINUM - NEW YORK MERCANTILE EXCHANGE", "PLATINUM"],
    "USD": ["U.S. DOLLAR INDEX", "USD INDEX - ICE FUTURES U.S.", "US DOLLAR INDEX"],
    "RUSSELL": ["RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE", "RUSSELL 2000"],
    "CHF": ["SWISS FRANC", "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE"],
    "BTC": ["BITCOIN", "BITCOIN - CHICAGO MERCANTILE EXCHANGE"],
    "SILVER": ["SILVER - COMMODITY EXCHANGE INC.", "SILVER", "SILVER - COMEX"],
    "COPPER": ["COPPER- #1", "COPPER"],
    "SPX": ["E-MINI S&P 500", "S&P 500 CONSOLIDATED", "S&P 500 STOCK INDEX"],
    "NZD": ["NEW ZEALAND DOLLAR", "NEW ZEALAND DOLLAR - CHICAGO MERCANTILE EXCHANGE", "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE", "NZ DOLLAR"],
    "EUR": ["EURO FX - CHICAGO MERCANTILE EXCHANGE", "EURO FX"],
    "USOil": ["CRUDE OIL, LIGHT SWEET", "WTI CRUDE OIL"],
    "Gold": ["GOLD - COMMODITY EXCHANGE INC.", "GOLD", "GOLD - COMEX"],
    "GBP": ["BRITISH POUND STERLING", "BRITISH POUND"],
    "US10T": ["UST 10Y NOTE", "10-YEAR U.S. TREASURY NOTES"],
    "NASDAQ": ["NASDAQ-100 CONSOLIDATED", "NASDAQ-100 MINI", "NASDAQ-100 STOCK INDEX"],
    "AUD": ["AUSTRALIAN DOLLAR", "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "JPY": ["JAPANESE YEN", "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE"],
    "ZAR": ["SO AFRICAN RAND", "SOUTH AFRICAN RAND"],
    "CAD": ["CANADIAN DOLLAR", "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "DOW": ["DOW JONES INDUSTRIAL AVERAGE", "MINI DOW JONES"],
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]

ECON_FIELDS = [
    "GDP", "mPMI", "sPMI", "Retail Sales", "Consumer Conf",
    "CPI", "PPI", "PCE", "Interest Rates",
    "NFP", "Unemployment Rate", "Unemployment Claims", "ADP"
]

# ── Bloomberg-Grade Extensions (v4) ──────────────────────────────────

# FRED Yield Curve series (real Treasury yields)
FRED_YIELD_CURVE = {
    "1M": "DGS1MO", "3M": "DGS3MO", "6M": "DGS6MO",
    "1Y": "DGS1",   "2Y": "DGS2",   "3Y": "DGS3",
    "5Y": "DGS5",   "7Y": "DGS7",   "10Y": "DGS10",
    "20Y": "DGS20", "30Y": "DGS30",
}

# FRED Global Macro series
FRED_MACRO = {
    "GDP":          "GDPC1",       # Real GDP
    "CPI":          "CPIAUCSL",    # CPI All Items
    "Core_CPI":     "CPILFESL",    # Core CPI
    "Unemployment": "UNRATE",      # Unemployment Rate
    "NFP":          "PAYEMS",      # Nonfarm Payrolls
    "ISM_Mfg":      "MANEMP",      # ISM Manufacturing Employment
    "Retail_Sales": "RSAFS",       # Retail Sales
    "Ind_Prod":     "INDPRO",      # Industrial Production
    "M2":           "M2SL",        # Money Supply M2
    "Fed_Balance":  "WALCL",       # Fed Balance Sheet
    "Bank_Reserves":"TOTRESNS",    # Total Reserves
    "Fed_Funds":    "DFF",         # Fed Funds Rate
    "Mortgage_30":  "MORTGAGE30US",# 30Y Mortgage Rate
    "Housing":      "HOUST",       # Housing Starts
    "Permits":      "PERMIT",      # Building Permits
    "Case_Shiller": "CSUSHPISA",   # Case-Shiller Home Price
    "PCE":          "PCEPI",       # PCE Price Index
    "VIX":          "VIXCLS",      # VIX
}

# FRED Fund Flows / Monetary
FRED_FUND_FLOWS = {
    "M2":          "M2SL",
    "Fed_Balance": "WALCL",
    "Reserves":    "TOTRESNS",
    "TIC_Total":   "SMTOTL",      # Treasury Intl Capital
    "Velocity":    "M2V",         # Velocity of M2
}

# Central Bank Policy Rate Proxies (FRED + yfinance)
CB_RATES = {
    "USD": {"fred": "DFF",   "name": "Federal Reserve"},
    "EUR": {"fred": None,    "yf": "^TNX", "name": "ECB"},
    "GBP": {"fred": None,    "yf": "^TNX", "name": "Bank of England"},
    "JPY": {"fred": None,    "yf": "^TNX", "name": "Bank of Japan"},
    "AUD": {"fred": None,    "yf": "^TNX", "name": "RBA"},
    "NZD": {"fred": None,    "yf": "^TNX", "name": "RBNZ"},
    "CAD": {"fred": None,    "yf": "^TNX", "name": "Bank of Canada"},
    "CHF": {"fred": None,    "yf": "^TNX", "name": "SNB"},
}

# Approximate policy rates for rate differential calc
CB_POLICY_RATES = {
    "USD": 5.25, "EUR": 4.50, "GBP": 5.25, "JPY": 0.25,
    "AUD": 4.35, "NZD": 5.50, "CAD": 5.00, "CHF": 1.75,
}

# Sector ETFs (S&P 500 GICS sectors)
SECTOR_ETFS = {
    "XLF": "Financials",    "XLK": "Technology",
    "XLE": "Energy",        "XLV": "Health Care",
    "XLI": "Industrials",   "XLU": "Utilities",
    "XLP": "Cons. Staples", "XLY": "Cons. Disc.",
    "XLC": "Communication", "XLRE": "Real Estate",
    "XLB": "Materials",
}

# Extended Commodities
COMMODITIES_EXT = {
    "Gold":      "GC=F",   "Silver":    "SI=F",
    "WTI Oil":   "CL=F",   "Brent Oil": "BZ=F",
    "Copper":    "HG=F",   "Platinum":  "PL=F",
    "Palladium": "PA=F",   "Nat Gas":   "NG=F",
}

# Extended Crypto
CRYPTO_EXT = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
}

# VIX Term Structure
VIX_TERM = {
    "VIX":   "^VIX",
    "VIX3M": "^VIX3M",
    "VIX9D": "^VIX9D",
}

# Trading Sessions (UTC hours)
TRADING_SESSIONS = {
    "Tokyo":  {"open": 0, "close": 9,  "label": "Tokyo/Sydney"},
    "London": {"open": 7, "close": 16, "label": "London/EU"},
    "NYC":    {"open": 13, "close": 22, "label": "New York"},
}

# FRED Housing series
FRED_HOUSING = {
    "Starts":      "HOUST",
    "Permits":     "PERMIT",
    "Case_Shiller":"CSUSHPISA",
    "Mortgage_30": "MORTGAGE30US",
}
