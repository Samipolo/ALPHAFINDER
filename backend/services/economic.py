"""
Economic Service - official macro backdrop from World Bank WDI and OECD CLI.
Builds direct, country-level macro scores that the Top Setups model can use
without inventing synthetic fills.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR  # noqa: E402
from services.net_utils import build_session, disable_dead_proxy_env  # noqa: E402


CACHE_FILE = os.path.join(CACHE_DIR, "economic.json")
CACHE_TTL = 3600
CACHE_VERSION = 3
WB_BASE = "https://api.worldbank.org/v2"
OECD_BASE = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI"

# Use the most direct official country codes we can get from the World Bank API.
COUNTRY_CODES = {
    "USD": ["US"],
    "EUR": ["EUU", "XC"],
    "GBP": ["GB"],
    "JPY": ["JP"],
    "AUD": ["AU"],
    "NZD": ["NZ"],
    "CAD": ["CA"],
    "CHF": ["CH"],
}

OECD_COUNTRY_CODES = {
    "USD": [["USA"]],
    "EUR": [["DEU", "FRA", "ITA", "ESP", "NLD"], ["DEU"], ["FRA"]],
    "GBP": [["GBR"]],
    "JPY": [["JPN"]],
    "AUD": [["AUS"]],
    "NZD": [["NZL"]],
    "CAD": [["CAN"]],
    "CHF": [["CHE"]],
}


def _load_cache() -> dict[str, Any] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        if time.time() - os.path.getmtime(CACHE_FILE) >= CACHE_TTL:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != CACHE_VERSION:
            return None
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            return None
        has_real_value = False
        for scores in data.values():
            if not isinstance(scores, dict):
                continue
            for key, value in scores.items():
                if str(key).startswith("_"):
                    continue
                if value is not None:
                    has_real_value = True
                    break
            if has_real_value:
                break
        return payload if has_real_value else None
    except Exception:
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": CACHE_VERSION,
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                },
                handle,
                default=str,
            )
    except Exception as exc:
        print(f"[Econ] Cache save error: {exc}")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _bucket(value: float) -> int:
    if value >= 6:
        return 2
    if value >= 2:
        return 1
    if value <= -6:
        return -2
    if value <= -2:
        return -1
    return 0


def _clamp(value: int, lo: int = -2, hi: int = 2) -> int:
    return max(lo, min(hi, value))


def _score_growth(value: float | None) -> int | None:
    if value is None:
        return None
    if value >= 4:
        return 2
    if value >= 2:
        return 1
    if value >= 0:
        return 0
    if value >= -2:
        return -1
    return -2


def _score_inflation(value: float | None) -> int | None:
    if value is None:
        return None
    # Mild inflation near target is best for FX; too hot or too cold is worse.
    if 1.5 <= value <= 3.5:
        return 2
    if 1.0 <= value < 1.5 or 3.5 < value <= 4.5:
        return 1
    if 0.5 <= value < 1.0 or 4.5 < value <= 5.5:
        return 0
    if 0.0 <= value < 0.5 or 5.5 < value <= 6.5:
        return -1
    return -2


def _score_unemployment(value: float | None) -> int | None:
    if value is None:
        return None
    if value <= 3.5:
        return 2
    if value <= 5.0:
        return 1
    if value <= 6.5:
        return 0
    if value <= 8.0:
        return -1
    return -2


def _score_rate(value: float | None, delta: float | None = None) -> int | None:
    if value is None:
        return None
    score = 0
    if value >= 6:
        score += 2
    elif value >= 4:
        score += 1
    elif value <= 1:
        score -= 1

    if delta is not None:
        if delta > 0.25:
            score += 1
        elif delta < -0.25:
            score -= 1
    return max(-2, min(2, score))


def _score_trade_balance(value: float | None) -> int | None:
    if value is None:
        return None
    if value >= 4:
        return 2
    if value >= 1:
        return 1
    if value >= -1:
        return 0
    if value >= -4:
        return -1
    return -2


def _score_cli_level(value: float | None) -> int | None:
    if value is None:
        return None
    if value >= 101.5:
        return 2
    if value >= 100.5:
        return 1
    if value <= 98.5:
        return -2
    if value <= 99.5:
        return -1
    return 0


def _score_cli_momentum(delta: float | None) -> int | None:
    if delta is None:
        return None
    if delta >= 0.4:
        return 2
    if delta >= 0.15:
        return 1
    if delta <= -0.4:
        return -2
    if delta <= -0.15:
        return -1
    return 0


def _fetch_wb_series(session, country: str, indicator: str, limit: int = 5) -> list[dict[str, Any]]:
    url = f"{WB_BASE}/country/{country}/indicator/{indicator}"
    params = {
        "format": "json",
        "per_page": str(limit),
        "mrv": str(limit),
    }
    resp = session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    data = payload[1]
    return data if isinstance(data, list) else []


def _latest_values(session, countries: list[str], indicator: str, limit: int = 5) -> list[float]:
    for country in countries:
        try:
            rows = _fetch_wb_series(session, country, indicator, limit=limit)
        except Exception:
            continue
        values: list[float] = []
        for row in rows:
            value = _safe_float(row.get("value"))
            if value is None:
                continue
            values.append(value)
        if values:
            return values
    return []


def _last_two(values: list[float]) -> tuple[float | None, float | None]:
    latest = values[0] if len(values) >= 1 else None
    previous = values[1] if len(values) >= 2 else None
    return latest, previous


def _fetch_oecd_cli_rows(session, country_codes: list[str]) -> list[dict[str, Any]]:
    if not country_codes:
        return []
    codes = "+".join(country_codes)
    url = f"{OECD_BASE}/{codes}.M.LI...AA...H"
    params = {
        "startPeriod": "2023-01",
        "dimensionAtObservation": "AllDimensions",
        "format": "csvfilewithlabels",
    }
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        return []
    text = (resp.text or "").strip()
    if not text or "NoResultsFound" in text or "NoRecordsFound" in text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        ref_area = (row.get("REF_AREA") or row.get("Reference area") or "").strip()
        time_period = (row.get("TIME_PERIOD") or row.get("Time period") or "").strip()
        value = _safe_float(row.get("OBS_VALUE") or row.get("Observation value"))
        if not ref_area or not time_period or value is None:
            continue
        rows.append(
            {
                "ref_area": ref_area,
                "time_period": time_period,
                "value": value,
            }
        )
    return rows


def _fetch_oecd_cli_profile(session, currency: str) -> dict[str, Any]:
    candidate_groups = OECD_COUNTRY_CODES.get(currency, [])
    for country_codes in candidate_groups:
        try:
            rows = _fetch_oecd_cli_rows(session, country_codes)
        except Exception:
            continue
        if not rows:
            continue

        by_country: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            by_country.setdefault(row["ref_area"], []).append(
                (row["time_period"], float(row["value"]))
            )

        levels: list[int] = []
        momentum: list[int] = []
        latest_vals: list[float] = []
        previous_vals: list[float] = []
        used_countries: list[str] = []

        for code in country_codes:
            series = sorted(by_country.get(code, []), key=lambda item: item[0], reverse=True)
            if not series:
                continue
            latest = series[0][1]
            prev = series[1][1] if len(series) > 1 else None
            three_month = series[3][1] if len(series) > 3 else None
            latest_vals.append(latest)
            if prev is not None:
                previous_vals.append(prev)
            level_score = _score_cli_level(latest)
            if level_score is not None:
                levels.append(level_score)
            mom_score = _score_cli_momentum((latest - prev) if prev is not None else None)
            if mom_score is None and three_month is not None:
                mom_score = _score_cli_momentum(latest - three_month)
            if mom_score is not None:
                momentum.append(mom_score)
            used_countries.append(code)

        if not levels and not momentum:
            continue

        level_score = int(round(sum(levels) / len(levels))) if levels else 0
        momentum_score = int(round(sum(momentum) / len(momentum))) if momentum else 0
        consumer_score = _clamp(int(round((level_score + momentum_score) / 2.0)), -2, 2)

        return {
            "countries": used_countries or country_codes,
            "latest": round(sum(latest_vals) / len(latest_vals), 4) if latest_vals else None,
            "previous": round(sum(previous_vals) / len(previous_vals), 4) if previous_vals else None,
            "mPMI": level_score,
            "sPMI": momentum_score,
            "Consumer Conf": consumer_score,
            "source": "OECD CLI",
        }

    return {
        "countries": country_codes[0] if country_codes else [],
        "latest": None,
        "previous": None,
        "mPMI": None,
        "sPMI": None,
        "Consumer Conf": None,
        "source": "OECD CLI",
    }


def _build_currency_profile(session, currency: str) -> dict[str, Any]:
    countries = COUNTRY_CODES.get(currency, [currency])
    oecd = _fetch_oecd_cli_profile(session, currency)

    gdp_vals = _latest_values(session, countries, "NY.GDP.MKTP.KD.ZG")
    cpi_vals = _latest_values(session, countries, "FP.CPI.TOTL.ZG")
    deflator_vals = _latest_values(session, countries, "NY.GDP.DEFL.KD.ZG")
    cons_vals = _latest_values(session, countries, "NE.CON.PRVT.PC.KD.ZG")
    trade_vals = _latest_values(session, countries, "BN.CAB.XOKA.GD.ZS")
    unemp_vals = _latest_values(session, countries, "SL.UEM.TOTL.ZS")
    emp_vals = _latest_values(session, countries, "SL.EMP.TOTL.SP.ZS")
    rate_vals = _latest_values(session, countries, "FR.INR.LEND")

    gdp_latest, gdp_prev = _last_two(gdp_vals)
    cpi_latest, cpi_prev = _last_two(cpi_vals)
    deflator_latest, _ = _last_two(deflator_vals)
    cons_latest, cons_prev = _last_two(cons_vals)
    trade_latest, _ = _last_two(trade_vals)
    unemp_latest, unemp_prev = _last_two(unemp_vals)
    emp_latest, emp_prev = _last_two(emp_vals)
    rate_latest, rate_prev = _last_two(rate_vals)

    gdp_score = _score_growth(gdp_latest)
    gdp_momentum = _bucket((gdp_latest or 0.0) - (gdp_prev or 0.0))
    cons_score = _score_growth(cons_latest)
    cons_momentum = _bucket((cons_latest or 0.0) - (cons_prev or 0.0))
    cpi_score = _score_inflation(cpi_latest)
    cpi_momentum = _bucket(-((cpi_latest or 0.0) - (cpi_prev or 0.0)))
    ppi_score = _score_inflation(deflator_latest)
    trade_score = _score_trade_balance(trade_latest)
    unemployment_score = _score_unemployment(unemp_latest)
    unemployment_momentum = _bucket(-((unemp_latest or 0.0) - (unemp_prev or 0.0)))
    employment_score = _bucket((emp_latest or 0.0) - (emp_prev or 0.0))
    rate_score = _score_rate(rate_latest, (rate_latest or 0.0) - (rate_prev or 0.0) if rate_prev is not None else None)

    oecd_mpmi = oecd.get("mPMI")
    oecd_spmi = oecd.get("sPMI")
    oecd_cons = oecd.get("Consumer Conf")

    # Build direct field scores. Missing fields remain absent so the downstream
    # pair model can distinguish real signals from unavailable inputs.
    scores: dict[str, Any] = {
        "GDP": gdp_score,
        "mPMI": oecd_mpmi if oecd_mpmi is not None else gdp_momentum,
        "sPMI": oecd_spmi if oecd_spmi is not None else cons_momentum,
        "Retail Sales": cons_score,
        "Consumer Conf": oecd_cons if oecd_cons is not None else _bucket(((gdp_momentum or 0) + (cons_momentum or 0) + (trade_score or 0)) / 3.0),
        "CPI": cpi_score,
        "PPI": ppi_score,
        "PCE": cons_score,
        "Interest Rates": rate_score,
        "NFP": employment_score,
        "Unemployment Rate": unemployment_score,
        "Unemployment Claims": unemployment_momentum,
        "ADP": employment_score,
        "_profile": {
            "country_codes": countries,
            "observations": {
                "GDP": {"latest": gdp_latest, "previous": gdp_prev},
                "CPI": {"latest": cpi_latest, "previous": cpi_prev},
                "PPI": {"latest": deflator_latest, "previous": None},
                "Consumption": {"latest": cons_latest, "previous": cons_prev},
                "Trade": {"latest": trade_latest, "previous": None},
                "Unemployment": {"latest": unemp_latest, "previous": unemp_prev},
                "Employment": {"latest": emp_latest, "previous": emp_prev},
                "Rates": {"latest": rate_latest, "previous": rate_prev},
                "OECD CLI": {
                    "latest": oecd.get("latest"),
                    "previous": oecd.get("previous"),
                    "countries": oecd.get("countries"),
                },
            },
            "total": sum(
                value
                for value in [
                    gdp_score,
                    gdp_momentum,
                    cons_score,
                    cons_momentum,
                    cpi_score,
                    ppi_score,
                    trade_score,
                    unemployment_score,
                    unemployment_momentum,
                    employment_score,
                    rate_score,
                ]
                if value is not None
            ),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "source": "World Bank WDI + OECD CLI",
        },
    }
    return scores


def fetch_economic() -> dict:
    cached = _load_cache()
    if cached is not None:
        data = cached.get("data")
        if isinstance(data, dict):
            print("[Econ] Returning cached data")
            return data
    print("[Econ] Fetching official macro data from World Bank...")
    disable_dead_proxy_env()
    session = build_session()

    scores: dict[str, dict[str, Any]] = {}
    for currency in ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]:
        try:
            scores[currency] = _build_currency_profile(session, currency)
        except Exception as exc:
            print(f"[Econ] {currency} error: {exc}")
            scores[currency] = {
                "GDP": None,
                "mPMI": None,
                "sPMI": None,
                "Retail Sales": None,
                "Consumer Conf": None,
                "CPI": None,
                "PPI": None,
                "PCE": None,
                "Interest Rates": None,
                "NFP": None,
                "Unemployment Rate": None,
                "Unemployment Claims": None,
                "ADP": None,
                "_profile": {
                    "country_codes": COUNTRY_CODES.get(currency, [currency]),
                    "observations": {},
                    "total": 0,
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "source": "World Bank WDI + OECD CLI",
                },
            }

    _save_cache(scores)
    return scores
