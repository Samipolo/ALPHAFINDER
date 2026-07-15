"""
Investopedia Service — Windows-compatible.
Provides indicator definitions for dashboard tooltips.
News removed per user request.
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR

CACHE_TERMS = os.path.join(CACHE_DIR, "invp_terms.json")
TTL_TERMS   = 86400  # 24 hours

# Indicator definitions sourced from Investopedia
INDICATOR_DEFS = {
    "GDP": {
        "full": "Gross Domestic Product",
        "desc": "Total value of goods/services produced. Beat = +2, Miss = -2.",
        "source": "Investopedia"
    },
    "mPMI": {
        "full": "Manufacturing PMI",
        "desc": "PMI >50 = expansion. >53=+2, >51=+1, 49-51=0, <49=-1, <47=-2",
        "source": "Investopedia"
    },
    "sPMI": {
        "full": "Services PMI",
        "desc": "PMI for services sector. Same scale as manufacturing PMI.",
        "source": "Investopedia"
    },
    "Retail Sales": {
        "full": "Retail Sales MoM",
        "desc": "Month-over-month retail spending change. Strong = economic health.",
        "source": "Investopedia"
    },
    "Consumer Conf": {
        "full": "Consumer Confidence",
        "desc": "Survey of consumer economic optimism. Rising = more spending = bullish.",
        "source": "Investopedia"
    },
    "CPI": {
        "full": "Consumer Price Index",
        "desc": "Inflation measure. Above 2% CB target = hawkish = currency bullish.",
        "source": "Investopedia"
    },
    "PPI": {
        "full": "Producer Price Index",
        "desc": "Leading inflation indicator — producer prices feed into consumer prices.",
        "source": "Investopedia"
    },
    "PCE": {
        "full": "Personal Consumption Expenditures",
        "desc": "Fed preferred inflation gauge. Core PCE above 2% = hawkish Fed.",
        "source": "Investopedia"
    },
    "Interest Rates": {
        "full": "Central Bank Interest Rate",
        "desc": "Policy rate direction. Hiking=+2, Hold=0, Cutting=-2.",
        "source": "Investopedia"
    },
    "NFP": {
        "full": "Nonfarm Payrolls",
        "desc": "Monthly US jobs. Above 200k = strong. Released first Friday monthly.",
        "source": "Investopedia"
    },
    "Unemployment Rate": {
        "full": "Unemployment Rate",
        "desc": "% of labor force without jobs. Falling = bullish for currency.",
        "source": "Investopedia"
    },
    "Unemployment Claims": {
        "full": "Initial Jobless Claims",
        "desc": "Weekly new unemployment filings. Below 220k = healthy labor market.",
        "source": "Investopedia"
    },
    "ADP": {
        "full": "ADP Employment Report",
        "desc": "Private payrolls preview 2 days before NFP. Good leading indicator.",
        "source": "Investopedia"
    },
}

def _load_cache() -> Optional[dict]:
    try:
        if os.path.exists(CACHE_TERMS):
            if time.time() - os.path.getmtime(CACHE_TERMS) < TTL_TERMS:
                with open(CACHE_TERMS, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None

def _save_cache(data: dict) -> None:
    try:
        with open(CACHE_TERMS, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception:
        pass

def fetch_indicator_definitions() -> dict:
    cached = _load_cache()
    if cached is not None:
        return cached
    _save_cache(INDICATOR_DEFS)
    return INDICATOR_DEFS

def fetch_news() -> list:
    return []  # News tab removed

def get_market_context() -> dict:
    return {}
