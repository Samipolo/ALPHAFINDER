"""Quant Lab - ML / statistics / quantitative-finance / physics model battery.

All models run on real daily OHLCV history pulled from Yahoo Finance via
yfinance. Nothing here is synthetic: if history cannot be fetched the
endpoint reports an error instead of fabricating data.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE = [
    "SPY", "QQQ", "DIA", "IWM", "TLT", "GLD", "SLV", "USO",
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "BTC-USD", "ETH-USD", "^VIX",
]

SYMBOL_LABELS = {
    "SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)", "DIA": "Dow Jones (DIA)",
    "IWM": "Russell 2000 (IWM)", "TLT": "20Y+ Treasuries (TLT)", "GLD": "Gold (GLD)",
    "SLV": "Silver (SLV)", "USO": "WTI Oil (USO)", "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD", "USDJPY=X": "USD/JPY", "AUDUSD=X": "AUD/USD",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "^VIX": "VIX",
}

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # v2 payload (cone + closes)
_CACHE_LOCK = threading.Lock()


def _fetch_history(symbol: str, period: str = "2y") -> pd.DataFrame:
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    if df is None or df.empty or len(df) < 120:
        raise ValueError(f"insufficient history for {symbol}")
    return df


def _annualization(symbol: str) -> float:
    return 365.0 if "-USD" in symbol else 252.0


# ── statistics models ────────────────────────────────────────────────

def _hurst(returns: np.ndarray) -> float:
    """Rescaled-range Hurst exponent estimate."""
    n = len(returns)
    max_k = min(int(n / 4), 128)
    rs, sizes = [], []
    for k in (8, 16, 32, 64, 128):
        if k > max_k:
            break
        chunks = n // k
        vals = []
        for c in range(chunks):
            seg = returns[c * k:(c + 1) * k]
            dev = np.cumsum(seg - seg.mean())
            r = dev.max() - dev.min()
            s = seg.std(ddof=1)
            if s > 1e-12:
                vals.append(r / s)
        if vals:
            rs.append(np.mean(vals))
            sizes.append(k)
    if len(rs) < 2:
        return 0.5
    slope = np.polyfit(np.log(sizes), np.log(rs), 1)[0]
    return float(np.clip(slope, 0.0, 1.0))


def _shannon_entropy(returns: np.ndarray, bins: int = 20) -> float:
    hist, _ = np.histogram(returns, bins=bins)
    p = hist / hist.sum()
    p = p[p > 0]
    return float(-(p * np.log(p)).sum() / math.log(bins))


def _acf(returns: np.ndarray, lag: int) -> float:
    if len(returns) <= lag + 2:
        return 0.0
    a, b = returns[:-lag], returns[lag:]
    sa, sb = a.std(), b.std()
    if sa < 1e-12 or sb < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _stats_block(closes: np.ndarray, rets: np.ndarray, ann: float) -> dict:
    mu, sd = rets.mean(), rets.std(ddof=1)
    z = (rets - mu) / (sd + 1e-12)
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean() - 3.0)
    n = len(rets)
    jb = n / 6.0 * (skew ** 2 + kurt ** 2 / 4.0)
    jb_p = math.exp(-jb / 2.0)
    peak = np.maximum.accumulate(closes)
    dd = closes / peak - 1.0
    # ADF-style mean-reversion t-stat: regress dy on y_lag
    y = np.log(closes)
    dy, ylag = np.diff(y), y[:-1]
    ylag_c = ylag - ylag.mean()
    beta = float((ylag_c * dy).sum() / (ylag_c ** 2).sum())
    resid = dy - beta * ylag_c
    se = math.sqrt(resid.var(ddof=2) / (ylag_c ** 2).sum())
    return {
        "obs": int(n),
        "ann_return_pct": round(float(mu * ann * 100), 2),
        "ann_vol_pct": round(float(sd * math.sqrt(ann) * 100), 2),
        "skew": round(skew, 3),
        "excess_kurtosis": round(kurt, 3),
        "jarque_bera": round(float(jb), 1),
        "jb_p_value": round(float(jb_p), 4),
        "normal_dist": bool(jb_p > 0.05),
        "acf": {f"lag{k}": round(_acf(rets, k), 3) for k in (1, 2, 5, 10, 21)},
        "hurst": round(_hurst(rets), 3),
        "hurst_regime": ("trending" if _hurst(rets) > 0.55 else "mean-reverting" if _hurst(rets) < 0.45 else "random walk"),
        "entropy": round(_shannon_entropy(rets), 3),
        "max_drawdown_pct": round(float(dd.min() * 100), 2),
        "current_drawdown_pct": round(float(dd[-1] * 100), 2),
        "adf_tstat": round(beta / (se + 1e-12), 2),
    }


# ── quantitative finance models ──────────────────────────────────────

def _garch11(rets: np.ndarray) -> dict:
    """GARCH(1,1) via variance-targeted grid-search MLE."""
    r = rets - rets.mean()
    var = r.var(ddof=1)
    best = (None, -1e18)
    for a in np.arange(0.02, 0.22, 0.02):
        for b in np.arange(0.70, 0.99, 0.02):
            if a + b >= 0.999:
                continue
            w = var * (1 - a - b)
            h = var
            ll = 0.0
            for x in r:
                h = w + a * x * x + b * h
                ll += -0.5 * (math.log(h) + x * x / h)
            if ll > best[1]:
                best = ((w, a, b, h), ll)
    (w, a, b, h_last) = best[0]
    x_last = r[-1]
    h_next = w + a * x_last * x_last + b * h_last
    return {"alpha": round(float(a), 3), "beta": round(float(b), 3),
            "persistence": round(float(a + b), 3),
            "next_day_vol_pct": round(math.sqrt(h_next) * 100, 3),
            "long_run_vol_pct": round(math.sqrt(var) * 100, 3)}


def _quant_block(symbol: str, closes: np.ndarray, rets: np.ndarray, ann: float,
                 bench: np.ndarray | None) -> dict:
    mu, sd = rets.mean(), rets.std(ddof=1)
    downside = rets[rets < 0].std(ddof=1) if (rets < 0).any() else 1e-12
    peak = np.maximum.accumulate(closes)
    maxdd = abs((closes / peak - 1.0).min())
    srt = sorted(rets)
    var95 = -srt[int(0.05 * len(srt))]
    var99 = -srt[int(0.01 * len(srt))]
    cvar95 = -np.mean(srt[: max(1, int(0.05 * len(srt)))])
    z = (rets - mu) / (sd + 1e-12)
    s, k = float((z ** 3).mean()), float((z ** 4).mean() - 3.0)
    zq = 1.645
    zcf = zq + (zq**2 - 1) * s / 6 + (zq**3 - 3*zq) * k / 24 - (2*zq**3 - 5*zq) * s**2 / 36
    lam = 0.94
    ew = rets[0] ** 2
    for x in rets[1:]:
        ew = lam * ew + (1 - lam) * x * x
    beta_a = alpha_a = r2 = None
    if bench is not None and len(bench) == len(rets) and symbol != "SPY":
        bc = bench - bench.mean()
        beta_a = float((bc * (rets - mu)).sum() / (bc ** 2).sum())
        alpha_a = float((mu - beta_a * bench.mean()) * ann * 100)
        r2 = float(np.corrcoef(rets, bench)[0, 1] ** 2)
    sma50 = closes[-50:].mean()
    sma200 = closes[-200:].mean()
    delta = np.diff(closes[-15:])
    up = delta[delta > 0].sum()
    dn = -delta[delta < 0].sum()
    rsi = 100.0 - 100.0 / (1.0 + (up / dn)) if dn > 0 else 100.0
    mom_12_1 = (closes[-21] / closes[-min(252, len(closes))] - 1.0) * 100
    z20 = (closes[-1] - closes[-20:].mean()) / (closes[-20:].std(ddof=1) + 1e-12)
    return {
        "sharpe": round(float(mu / sd * math.sqrt(ann)), 2),
        "sortino": round(float(mu / (downside + 1e-12) * math.sqrt(ann)), 2),
        "calmar": round(float(mu * ann / (maxdd + 1e-12)), 2),
        "var95_pct": round(float(var95 * 100), 2),
        "var99_pct": round(float(var99 * 100), 2),
        "cvar95_pct": round(float(cvar95 * 100), 2),
        "cornish_fisher_var95_pct": round(float((zcf * sd - mu) * 100), 2),
        "ewma_vol_ann_pct": round(math.sqrt(ew * ann) * 100, 2),
        "garch": _garch11(rets),
        "beta_vs_spy": round(beta_a, 2) if beta_a is not None else None,
        "alpha_ann_pct": round(alpha_a, 2) if alpha_a is not None else None,
        "r2_vs_spy": round(r2, 2) if r2 is not None else None,
        "kelly_fraction": round(float(mu / (sd ** 2 + 1e-12)), 2),
        "rsi14": round(float(rsi), 1),
        "momentum_12_1_pct": round(float(mom_12_1), 2),
        "zscore_20d": round(float(z20), 2),
        "trend_regime": "golden cross" if sma50 > sma200 else "death cross",
        "price_vs_sma200_pct": round(float((closes[-1] / sma200 - 1) * 100), 2),
    }


# ── machine learning models ──────────────────────────────────────────

def _feature_matrix(rets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lags = 5
    rows = []
    ys = []
    vol20 = pd.Series(rets).rolling(20).std().to_numpy()
    for t in range(25, len(rets) - 1):
        rows.append([rets[t - i] for i in range(lags)] + [vol20[t]])
        ys.append(rets[t + 1])
    return np.array(rows), np.array(ys)


def _ml_block(rets: np.ndarray) -> dict:
    X, y = _feature_matrix(rets)
    mu_x, sd_x = X.mean(axis=0), X.std(axis=0) + 1e-12
    Xs = (X - mu_x) / sd_x
    split = int(len(Xs) * 0.8)
    Xtr, ytr, Xte, yte = Xs[:split], y[:split], Xs[split:], y[split:]
    # ridge regression (closed form)
    lam = 1.0
    A = Xtr.T @ Xtr + lam * np.eye(Xtr.shape[1])
    wr = np.linalg.solve(A, Xtr.T @ ytr)
    pred_te = Xte @ wr
    hit = float(((pred_te > 0) == (yte > 0)).mean())
    next_pred = float(Xs[-1] @ wr)
    # logistic regression (gradient descent)
    yb = (ytr > 0).astype(float)
    wl = np.zeros(Xtr.shape[1])
    b = 0.0
    for _ in range(400):
        p = 1.0 / (1.0 + np.exp(-(Xtr @ wl + b)))
        g = Xtr.T @ (p - yb) / len(yb)
        wl -= 0.5 * g
        b -= 0.5 * float((p - yb).mean())
    p_te = 1.0 / (1.0 + np.exp(-(Xte @ wl + b)))
    log_acc = float((((p_te > 0.5).astype(float)) == (yte > 0)).mean())
    p_up = float(1.0 / (1.0 + math.exp(-(Xs[-1] @ wl + b))))
    # k-means volatility regimes (k=3) on |ret|, vol20
    feats = np.column_stack([np.abs(rets[25:]), pd.Series(rets).rolling(20).std().to_numpy()[25:]])
    f = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-12)
    rng = np.random.default_rng(7)
    cents = f[rng.choice(len(f), 3, replace=False)]
    for _ in range(15):
        d = ((f[:, None, :] - cents[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        for j in range(3):
            if (lab == j).any():
                cents[j] = f[lab == j].mean(axis=0)
    order = np.argsort(cents[:, 1])
    names = {order[0]: "calm", order[1]: "normal", order[2]: "turbulent"}
    cur = names[int(lab[-1])]
    shares = {names[j]: round(float((lab == j).mean() * 100), 1) for j in range(3)}
    # kNN analog pattern forecast
    win = 10
    series = rets
    target = series[-win:]
    tgt = (target - target.mean()) / (target.std() + 1e-12)
    dists = []
    for t0 in range(0, len(series) - win - 5, 2):
        seg = series[t0:t0 + win]
        sgn = (seg - seg.mean()) / (seg.std() + 1e-12)
        dists.append((float(((sgn - tgt) ** 2).sum()), t0))
    dists.sort()
    fwd = [series[t0 + win:t0 + win + 5].sum() for _, t0 in dists[:15]]
    return {
        "ridge": {"oos_hit_rate_pct": round(hit * 100, 1),
                  "next_day_forecast_pct": round(next_pred * 100, 3),
                  "signal": "long" if next_pred > 0 else "short"},
        "logistic": {"oos_accuracy_pct": round(log_acc * 100, 1),
                     "prob_up_next_day_pct": round(p_up * 100, 1)},
        "kmeans_regime": {"current": cur, "distribution_pct": shares},
        "knn_analogs": {"neighbors": 15,
                        "avg_next_5d_return_pct": round(float(np.mean(fwd)) * 100, 2),
                        "pct_positive": round(float(np.mean([1 if v > 0 else 0 for v in fwd])) * 100, 0)},
    }


# ── physics models ───────────────────────────────────────────────────

def _physics_block(closes: np.ndarray, rets: np.ndarray, ann: float) -> dict:
    y = np.log(closes)
    t = np.arange(len(y))
    trend = np.polyval(np.polyfit(t, y, 1), t)
    x = y - trend
    dx, xlag = np.diff(x), x[:-1]
    beta = float((xlag * dx).sum() / ((xlag ** 2).sum() + 1e-12))
    kappa = -beta
    half_life = math.log(2) / kappa if kappa > 1e-6 else None
    disp = float(x[-1] / (x.std() + 1e-12))
    # GBM Monte Carlo 21-day cone
    mu, sd = rets.mean(), rets.std(ddof=1)
    rng = np.random.default_rng(11)
    paths = closes[-1] * np.exp(np.cumsum(
        (mu - 0.5 * sd * sd) + sd * rng.standard_normal((21, 2000)), axis=0))
    ends = paths[-1]
    pct = {p: round(float(np.percentile(ends, p)), 2) for p in (5, 25, 50, 75, 95)}
    prob_up = float((ends > closes[-1]).mean())
    cone = {str(p): [round(float(closes[-1]), 4)] +
            [round(float(np.percentile(paths[d], p)), 4) for d in range(21)]
            for p in (5, 25, 50, 75, 95)}
    # LPPL-lite super-exponential bubble score
    w = y[-120:]
    tw = np.arange(len(w))
    lin = np.polyfit(tw, w, 1)
    quad = np.polyfit(tw, w, 2)
    r2l = 1 - np.var(w - np.polyval(lin, tw)) / np.var(w)
    r2q = 1 - np.var(w - np.polyval(quad, tw)) / np.var(w)
    accel = quad[0]
    bubble = float(np.clip((r2q - r2l) * 400, 0, 10) * (1 if accel > 0 else 0.3))
    # market temperature: realized-vol percentile (statistical mechanics analogy)
    roll = pd.Series(rets).rolling(20).std().dropna().to_numpy()
    temp_pct = float((roll < roll[-1]).mean() * 100)
    # vol-of-vol (entropy production proxy)
    vov = float(np.std(np.diff(roll[-60:])) / (roll[-60:].mean() + 1e-12))
    return {
        "ornstein_uhlenbeck": {
            "kappa": round(kappa, 4),
            "half_life_days": round(half_life, 1) if half_life else None,
            "displacement_sigma": round(disp, 2),
            "state": "stretched" if abs(disp) > 1.5 else "near equilibrium",
        },
        "monte_carlo_21d": {"paths": 2000, "percentiles": pct, "cone": cone,
                            "prob_above_spot_pct": round(prob_up * 100, 1)},
        "lppl_bubble_score": round(bubble, 2),
        "bubble_state": "super-exponential" if bubble > 3 else "power-law normal",
        "market_temperature_pctile": round(temp_pct, 1),
        "vol_of_vol": round(vov, 3),
    }


# ── random matrix theory across the universe ─────────────────────────

def _rmt_block() -> dict:
    data = yf.download(UNIVERSE, period="1y", interval="1d",
                       auto_adjust=True, progress=False)["Close"]
    data = data.dropna(axis=1, how="all")
    if "SPY" in data.columns:
        data = data.loc[data["SPY"].notna()]
    data = data.ffill().dropna()
    rets = data.pct_change().dropna()
    T, N = rets.shape
    corr = rets.corr().to_numpy()
    eig = np.sort(np.linalg.eigvalsh(corr))[::-1]
    q = T / N
    lam_plus = (1 + 1 / math.sqrt(q)) ** 2
    lam_minus = (1 - 1 / math.sqrt(q)) ** 2
    signal = int((eig > lam_plus).sum())
    absorption = float(eig[0] / eig.sum())
    avg_corr = float(corr[np.triu_indices(N, 1)].mean())
    return {
        "assets": int(N), "days": int(T),
        "marchenko_pastur": {"lambda_plus": round(lam_plus, 3), "lambda_minus": round(lam_minus, 3)},
        "top_eigenvalues": [round(float(v), 3) for v in eig[:5]],
        "signal_modes": signal,
        "noise_modes": int(N - signal),
        "absorption_ratio_pct": round(absorption * 100, 1),
        "systemic_risk": "elevated" if absorption > 0.45 else "normal",
        "avg_pairwise_corr": round(avg_corr, 3),
    }


def fetch_quant_lab(symbol: str = "SPY") -> dict[str, Any]:
    symbol = symbol if symbol in UNIVERSE else "SPY"
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(symbol)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    df = _fetch_history(symbol)
    closes = df["Close"].to_numpy(dtype=float)
    rets = np.diff(np.log(closes))
    ann = _annualization(symbol)
    bench = None
    if symbol != "SPY":
        try:
            bdf = _fetch_history("SPY")
            sa = df["Close"].copy()
            sb = bdf["Close"].copy()
            sa.index = pd.Index([ts.date() for ts in sa.index])
            sb.index = pd.Index([ts.date() for ts in sb.index])
            sa = sa[~sa.index.duplicated(keep="last")]
            sb = sb[~sb.index.duplicated(keep="last")]
            joined = pd.concat([sa, sb], axis=1, keys=["a", "b"]).dropna()
            if len(joined) < 60:
                raise ValueError("insufficient overlap with SPY")
            ar = np.diff(np.log(joined["a"].to_numpy()))
            bench_r = np.diff(np.log(joined["b"].to_numpy()))
            rets_for_beta, bench = ar, bench_r
        except Exception:
            rets_for_beta, bench = rets, None
    else:
        rets_for_beta = rets
    try:
        rmt = _rmt_block()
    except Exception as exc:
        rmt = {"error": str(exc)}
    result = {
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "universe": [{"symbol": s, "label": SYMBOL_LABELS.get(s, s)} for s in UNIVERSE],
        "spot": round(float(closes[-1]), 4),
        "closes_60": [round(float(x), 4) for x in closes[-60:]],
        "as_of": str(df.index[-1].date()),
        "source": "Yahoo Finance daily OHLCV (real)",
        "statistics": _stats_block(closes, rets, ann),
        "quant": _quant_block(symbol, closes, rets_for_beta if bench is not None else rets, ann, bench),
        "machine_learning": _ml_block(rets),
        "physics": _physics_block(closes, rets, ann),
        "random_matrix_theory": rmt,
    }
    with _CACHE_LOCK:
        _CACHE[symbol] = (now, result)
    return result