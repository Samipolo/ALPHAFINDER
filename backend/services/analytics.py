from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from config import CURRENCIES, FOREX_PAIRS
from services.net_utils import build_session
from services.scoring import FIELD_GROUPS


BOND_MARKETS = {
    "USD": {
        "name": "United States",
        "flag": "🇺🇸",
        "url": "https://tradingeconomics.com/united-states/government-bond-yield",
    },
    "EUR": {
        "name": "Germany",
        "flag": "🇩🇪",
        "url": "https://tradingeconomics.com/germany/government-bond-yield",
    },
    "GBP": {
        "name": "United Kingdom",
        "flag": "🇬🇧",
        "url": "https://tradingeconomics.com/united-kingdom/government-bond-yield",
    },
    "JPY": {
        "name": "Japan",
        "flag": "🇯🇵",
        "url": "https://tradingeconomics.com/japan/government-bond-yield",
    },
    "AUD": {
        "name": "Australia",
        "flag": "🇦🇺",
        "url": "https://tradingeconomics.com/australia/government-bond-yield",
    },
    "NZD": {
        "name": "New Zealand",
        "flag": "🇳🇿",
        "url": "https://tradingeconomics.com/new-zealand/government-bond-yield",
    },
    "CAD": {
        "name": "Canada",
        "flag": "🇨🇦",
        "url": "https://tradingeconomics.com/canada/government-bond-yield",
    },
    "CHF": {
        "name": "Switzerland",
        "flag": "🇨🇭",
        "url": "https://tradingeconomics.com/switzerland/government-bond-yield",
    },
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_event_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _event_numeric_surprise(event: dict[str, Any]) -> float | None:
    actual = _safe_float(event.get("actual"))
    forecast = _safe_float(event.get("forecast"))
    previous = _safe_float(event.get("previous"))
    baseline = forecast if forecast not in (None, 0) else previous
    if actual is None or baseline in (None, 0):
        return None

    delta = (actual - baseline) / abs(baseline)
    field = event.get("field")
    if field in ("Unemployment Rate", "Unemployment Claims"):
        delta *= -1
    return delta


def build_surprise_meter(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest_dt = None
    for event in events or []:
        dt = _parse_event_datetime(str(event.get("datetime") or ""))
        if dt and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    result: dict[str, dict[str, Any]] = {}
    for currency in CURRENCIES:
        weighted = 0.0
        total_weight = 0.0
        beat_weight = 0.0
        miss_weight = 0.0
        high_impact = 0
        event_count = 0

        for event in events or []:
            if event.get("currency") != currency:
                continue
            delta = _event_numeric_surprise(event)
            if delta is None:
                continue
            event_count += 1
            impact = int(event.get("impact") or 2)
            if impact >= 3:
                high_impact += 1
            dt = _parse_event_datetime(str(event.get("datetime") or ""))
            days_old = max(0, (latest_dt - dt).days) if latest_dt and dt else 0
            recency = max(0.35, 1.0 - (days_old * 0.12))
            weight = (1.0 if impact <= 1 else 1.35 if impact == 2 else 1.85) * recency
            normalized = _clamp(delta * 12.0, -2.0, 2.0)
            weighted += normalized * weight
            total_weight += 2.0 * weight
            if normalized > 0:
                beat_weight += normalized * weight
            elif normalized < 0:
                miss_weight += abs(normalized) * weight

        if total_weight == 0:
            continue

        centered = weighted / total_weight
        pct = int(round(_clamp((centered + 1.0) * 50.0, 0.0, 100.0)))
        result[currency] = {
            "pct": pct,
            "score": round(weighted / max(total_weight / 2.0, 1e-6), 2),
            "beats": round(beat_weight, 2),
            "misses": round(miss_weight, 2),
            "event_count": event_count,
            "high_impact": high_impact,
        }
    return result


def _retail_signal(long_pct: float | None) -> float:
    if long_pct is None:
        return 0.0
    if long_pct >= 75:
        return -2.0
    if long_pct >= 62:
        return -1.0
    if long_pct <= 25:
        return 2.0
    if long_pct <= 38:
        return 1.0
    if long_pct > 50:
        return -0.5
    if long_pct < 50:
        return 0.5
    return 0.0


def _currency_macro_profile(scores: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, int] = {}
    counts: dict[str, int] = {}
    total = 0
    for group, fields in FIELD_GROUPS.items():
        values = []
        for field in fields:
            value = scores.get(field)
            if value is None:
                continue
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue
        if values:
            groups[group] = int(round(sum(values) / len(values)))
            counts[group] = len(values)
        else:
            groups[group] = 0
            counts[group] = 0
        total += groups[group]
    return {"groups": groups, "counts": counts, "total": total}


def build_currency_strength(
    setups: list[dict[str, Any]],
    technical: dict[str, dict[str, Any]],
    econ: dict[str, dict[str, Any]],
    retail: list[dict[str, Any]],
    cot: list[dict[str, Any]],
    surprise_meter: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    pair_sum = defaultdict(float)
    pair_count = defaultdict(int)
    momentum_sum = defaultdict(float)
    day_change_sum = defaultdict(float)
    retail_sum = defaultdict(float)
    retail_count = defaultdict(int)

    retail_map = {row.get("symbol"): row for row in retail or []}
    cot_map = {row.get("symbol"): row for row in cot or []}

    for setup in setups or []:
        symbol = setup.get("symbol")
        if symbol not in FOREX_PAIRS:
            continue
        base, quote = FOREX_PAIRS[symbol]
        total_score = float(setup.get("total_score") or 0.0)
        pair_signal = _clamp(total_score * 5.0, -100.0, 100.0)
        pair_sum[base] += pair_signal
        pair_sum[quote] -= pair_signal
        pair_count[base] += 1
        pair_count[quote] += 1

        tech = technical.get(symbol, {})
        trend = _safe_float(tech.get("trend")) or 0.0
        seas = _safe_float(tech.get("seasonality")) or 0.0
        chg1d = _safe_float(tech.get("chg1d")) or 0.0
        momentum_signal = _clamp((trend * 22.0) + (seas * 12.0) + (chg1d * 55.0), -100.0, 100.0)
        momentum_sum[base] += momentum_signal
        momentum_sum[quote] -= momentum_signal
        day_change_sum[base] += chg1d
        day_change_sum[quote] -= chg1d

        retail_row = retail_map.get(symbol)
        long_pct = _safe_float(retail_row.get("long_pct")) if retail_row else None
        retail_signal = _retail_signal(long_pct)
        retail_sum[base] += retail_signal * 35.0
        retail_sum[quote] -= retail_signal * 35.0
        retail_count[base] += 1
        retail_count[quote] += 1

    scores: dict[str, dict[str, Any]] = {}
    for currency in CURRENCIES:
        pair_avg = pair_sum[currency] / max(pair_count[currency], 1)
        momentum_avg = momentum_sum[currency] / max(pair_count[currency], 1)
        change_avg = day_change_sum[currency] / max(pair_count[currency], 1)
        retail_avg = retail_sum[currency] / max(retail_count[currency], 1) if retail_count[currency] else 0.0

        macro_profile = _currency_macro_profile(econ.get(currency) or {})
        macro_signal = _clamp(macro_profile["total"] * 18.0, -100.0, 100.0)
        surprise_pct = (surprise_meter.get(currency) or {}).get("pct", 50)
        surprise_signal = _clamp((float(surprise_pct) - 50.0) * 2.0, -100.0, 100.0)

        cot_row = cot_map.get(currency)
        cot_signal = 0.0
        if cot_row:
            cot_signal = _clamp(float(cot_row.get("net_pct_change_raw") or 0.0) * 8.0, -100.0, 100.0)

        composite = (
            pair_avg * 0.38
            + macro_signal * 0.18
            + surprise_signal * 0.16
            + cot_signal * 0.12
            + retail_avg * 0.08
            + momentum_avg * 0.08
        )
        composite = _clamp(composite, -100.0, 100.0)
        score = int(round((composite + 100.0) / 2.0))
        scores[currency] = {
            "score": score,
            "raw": round(composite, 2),
            "change": round(change_avg, 2),
            "trend": (
                "Accelerating" if composite >= 25 and change_avg > 0
                else "Strengthening" if composite > 5
                else "Breaking Down" if composite <= -25 and change_avg < 0
                else "Weakening" if composite < -5
                else "Rangebound"
            ),
            "pair_bias": round(pair_avg, 2),
            "macro_bias": round(macro_signal, 2),
            "surprise_bias": round(surprise_signal, 2),
            "cot_bias": round(cot_signal, 2),
            "retail_bias": round(retail_avg, 2),
            "macro_profile": macro_profile,
        }

    for currency, payload in scores.items():
        payload["vs"] = {}
        for ref in ("USD", "EUR", "GBP", "JPY"):
            payload["vs"][ref] = None if currency == ref else int(round(payload["score"] - scores[ref]["score"]))
    return scores


def fetch_bond_yields() -> dict[str, dict[str, Any]]:
    session = build_session(headers={"User-Agent": "Mozilla/5.0"})
    result: dict[str, dict[str, Any]] = {}

    for currency, info in BOND_MARKETS.items():
        try:
            response = session.get(info["url"], timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            rows = soup.select("tr[data-symbol]")
            ten_year = None
            two_year = None
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                label = cells[0].get_text(" ", strip=True)
                value = _safe_float(cells[1].get_text(" ", strip=True))
                daily = _safe_float(cells[3].get_text(" ", strip=True))
                monthly = _safe_float(cells[4].get_text(" ", strip=True))
                if value is None:
                    continue
                if "10Y" in label:
                    ten_year = {"label": label, "value": value, "daily": daily or 0.0, "monthly": monthly or 0.0}
                elif "2Y" in label:
                    two_year = {"label": label, "value": value, "daily": daily or 0.0, "monthly": monthly or 0.0}
                if ten_year and two_year:
                    break

            if not ten_year:
                continue

            short_leg = two_year or {
                "label": "3M",
                "value": 0.0,
                "daily": 0.0,
                "monthly": 0.0,
            }
            spread = ten_year["value"] - short_leg["value"]
            if spread < 0:
                regime = "Curve Inversion"
            elif spread >= 1.0:
                regime = "Steepening"
            elif ten_year["daily"] > 0 and short_leg["daily"] > 0:
                regime = "Hawkish Repricing"
            elif ten_year["daily"] < 0 and short_leg["daily"] < 0:
                regime = "Risk-Off Bid"
            else:
                regime = "Balanced"

            result[currency] = {
                "name": info["name"],
                "flag": info["flag"],
                "currency": currency,
                "y10": round(ten_year["value"], 2),
                "y2": round(short_leg["value"], 2),
                "short_label": short_leg["label"],
                "chg10": round(ten_year["daily"], 3),
                "chg2": round(short_leg["daily"], 3),
                "month10": round(ten_year["monthly"], 3),
                "spread": round(spread, 2),
                "regime": regime,
                "source": "TradingEconomics",
                "url": info["url"],
            }
        except Exception:
            continue

    return result
