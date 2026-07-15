"""Institutional Desk - factor models, portfolio construction and
hedge-fund-style positioning composites, all computed from real ETF /
futures / FX prices via Yahoo Finance.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

FACTORS = {
    "MTUM": "Momentum", "VLUE": "Value", "QUAL": "Quality", "USMV": "Min Vol",
    "IWM": "Small Size", "IWF": "Growth", "IWD": "Value (LC)", "SPY": "Market",
}
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
MACRO = ["SPY", "TLT", "GLD", "DBC", "BTC-USD", "HYG", "LQD", "UUP", "^VIX"]
TREND_ASSETS = ["SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "SLV", "USO", "DBC",
                "UUP", "EURUSD=X", "USDJPY=X", "BTC-USD", "ETH-USD"]
RP_ASSETS = ["SPY", "TLT", "GLD", "DBC", "BTC-USD"]

_CACHE: tuple[float, dict] | None = None
_CACHE_TTL = 900
_CACHE_LOCK = threading.Lock()


def _download(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    data = yf.download(sorted(set(tickers)), period=period, interval="1d",
                       auto_adjust=True, progress=False)["Close"]
    if isinstance(data, pd.Series):
        data = data.to_frame()
    return data.ffill()


def _period_return(s: pd.Series, days: int) -> float | None:
    s = s.dropna()
    if len(s) <= days:
        return None
    return float((s.iloc[-1] / s.iloc[-1 - days] - 1) * 100)


def _factor_dashboard(px: pd.DataFrame) -> list[dict]:
    out = []
    for tkr, name in FACTORS.items():
        if tkr not in px.columns:
            continue
        s = px[tkr]
        row = {"ticker": tkr, "factor": name,
               "r_1w": _period_return(s, 5), "r_1m": _period_return(s, 21),
               "r_3m": _period_return(s, 63), "r_6m": _period_return(s, 126),
               "r_12m": _period_return(s, 252)}
        spy = px["SPY"]
        rel = _period_return(s, 63)
        rel_spy = _period_return(spy, 63)
        row["excess_3m"] = round(rel - rel_spy, 2) if rel is not None and rel_spy is not None else None
        out.append({k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()})
    ranked = [r for r in out if r["r_3m"] is not None]
    ranked.sort(key=lambda r: -r["r_3m"])
    for i, r in enumerate(ranked):
        r["rank_3m"] = i + 1
    return out


def _portfolios(px: pd.DataFrame) -> dict:
    cols = [c for c in RP_ASSETS if c in px.columns]
    rets = px[cols].pct_change().dropna()
    ann = 252
    mu = rets.mean().to_numpy() * ann
    cov = rets.cov().to_numpy() * ann
    vol = np.sqrt(np.diag(cov))
    inv_vol = 1 / vol
    rp = inv_vol / inv_vol.sum()
    ones = np.ones(len(cols))
    cov_inv = np.linalg.pinv(cov)
    w_minvar = cov_inv @ ones
    w_minvar = w_minvar / w_minvar.sum()
    w_maxsh = cov_inv @ mu
    if w_maxsh.sum() != 0:
        w_maxsh = w_maxsh / np.abs(w_maxsh).sum()
    def _stats(w):
        r = float(w @ mu) * 100
        v = float(np.sqrt(max(w @ cov @ w, 1e-12))) * 100
        return {"ret_pct": round(r, 1), "vol_pct": round(v, 1),
                "sharpe": round(r / v, 2) if v > 0 else None}
    corr = rets.corr()
    dist = np.sqrt(0.5 * (1 - corr.to_numpy()))
    avg_dist = dist[np.triu_indices(len(cols), 1)].mean()
    return {
        "assets": cols,
        "ann_vol_pct": [round(float(v) * 100, 1) for v in vol],
        "risk_parity_w": [round(float(w), 3) for w in rp],
        "risk_parity": _stats(rp),
        "min_variance_w": [round(float(w), 3) for w in w_minvar],
        "min_variance": _stats(w_minvar),
        "max_sharpe_w": [round(float(w), 3) for w in w_maxsh],
        "max_sharpe": _stats(w_maxsh),
        "equal_weight": _stats(ones / len(cols)),
        "avg_corr_distance": round(float(avg_dist), 3),
    }


def _breadth(px: pd.DataFrame) -> dict:
    above50 = above200 = counted = 0
    rows = []
    for t in SECTORS:
        if t not in px.columns:
            continue
        s = px[t].dropna()
        if len(s) < 200:
            continue
        counted += 1
        a50 = s.iloc[-1] > s.rolling(50).mean().iloc[-1]
        a200 = s.iloc[-1] > s.rolling(200).mean().iloc[-1]
        above50 += a50
        above200 += a200
        rows.append({"sector": t, "above_50dma": bool(a50), "above_200dma": bool(a200),
                     "r_1m": _period_return(s, 21)})
    rows.sort(key=lambda r: -(r["r_1m"] or -999))
    sec_1m = [r["r_1m"] for r in rows if r["r_1m"] is not None]
    return {
        "pct_above_50dma": round(above50 / counted * 100, 0) if counted else None,
        "pct_above_200dma": round(above200 / counted * 100, 0) if counted else None,
        "sector_dispersion_1m": round(float(np.std(sec_1m)), 2) if sec_1m else None,
        "sectors": [{**r, "r_1m": round(r["r_1m"], 2) if r["r_1m"] is not None else None} for r in rows],
    }


def _risk_regime(px: pd.DataFrame) -> dict:
    def ratio_trend(a, b, days=63):
        if a not in px.columns or b not in px.columns:
            return None
        r = (px[a] / px[b]).dropna()
        if len(r) <= days:
            return None
        return float((r.iloc[-1] / r.iloc[-1 - days] - 1) * 100)
    signals = {
        "stocks_vs_bonds_3m": ratio_trend("SPY", "TLT"),
        "credit_hyg_lqd_3m": ratio_trend("HYG", "LQD"),
        "discretionary_vs_staples_3m": ratio_trend("XLY", "XLP"),
        "gold_vs_spx_3m": ratio_trend("GLD", "SPY"),
    }
    score = 0.0
    weights = {"stocks_vs_bonds_3m": 1, "credit_hyg_lqd_3m": 1.5,
               "discretionary_vs_staples_3m": 1, "gold_vs_spx_3m": -0.5}
    total_w = 0.0
    for k, v in signals.items():
        if v is not None:
            score += weights[k] * np.tanh(v / 10)
            total_w += abs(weights[k])
    composite = score / total_w * 100 if total_w else 0.0
    vix = float(px["^VIX"].dropna().iloc[-1]) if "^VIX" in px.columns else None
    return {
        "signals": {k: (round(v, 2) if v is not None else None) for k, v in signals.items()},
        "risk_on_off_score": round(composite, 1),
        "regime": "RISK-ON" if composite > 15 else "RISK-OFF" if composite < -15 else "NEUTRAL",
        "vix": round(vix, 2) if vix else None,
    }


def _cta_positioning(px: pd.DataFrame) -> dict:
    rows = []
    longs = shorts = 0
    for t in TREND_ASSETS:
        if t not in px.columns:
            continue
        s = px[t].dropna()
        if len(s) < 200:
            continue
        p = float(s.iloc[-1])
        sma50 = float(s.rolling(50).mean().iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1])
        mom = float((p / s.iloc[-63] - 1) * 100) if len(s) > 63 else 0.0
        sig = (1 if p > sma200 else -1) + (1 if sma50 > sma200 else -1) + (1 if mom > 0 else -1)
        pos = "LONG" if sig >= 2 else "SHORT" if sig <= -2 else "FLAT"
        longs += pos == "LONG"
        shorts += pos == "SHORT"
        vol = float(s.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100)
        rows.append({"asset": t, "position": pos, "trend_score": sig,
                     "mom_3m_pct": round(mom, 2), "ann_vol_pct": round(vol, 1),
                     "vol_target_lev": round(min(15.0 / max(vol, 1e-6), 3.0), 2)})
    return {"assets": rows, "net_exposure": longs - shorts,
            "stance": "net long" if longs > shorts else "net short" if shorts > longs else "balanced"}


def fetch_institutional() -> dict[str, Any]:
    global _CACHE
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE and now - _CACHE[0] < _CACHE_TTL:
            return _CACHE[1]
    tickers = list(FACTORS) + SECTORS + MACRO + TREND_ASSETS
    px = _download(tickers)
    result = {
        "as_of": str(px.index[-1].date()),
        "source": "Yahoo Finance ETF/FX/futures closes (real)",
        "factor_dashboard": _factor_dashboard(px),
        "portfolio_lab": _portfolios(px),
        "breadth": _breadth(px),
        "risk_regime": _risk_regime(px),
        "cta_trend": _cta_positioning(px),
    }
    with _CACHE_LOCK:
        _CACHE = (now, result)
    return result