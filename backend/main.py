"""
AlphaFinder Pro v4 — Bloomberg-Grade FastAPI backend.
Run locally with: python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

Auto-refresh: All data sources are refreshed every 5 minutes in the background.
"""
from __future__ import annotations

import json
import math
import os
import sys
import re
import hashlib
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query, Request, Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse as _StarletteJSONResponse
from fastapi.staticfiles import StaticFiles


def _sanitize_json(obj):
    """Recursively replace NaN/Infinity floats with None so Starlette's strict
    json.dumps(allow_nan=False) never raises on a stray value from live data
    (e.g. a 0/0 division inside a pandas/numpy computation)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


class JSONResponse(_StarletteJSONResponse):
    """Drop-in replacement for fastapi.responses.JSONResponse that sanitizes
    NaN/Infinity out of the payload before encoding, applied everywhere in
    this file since every endpoint constructs JSONResponse directly."""

    def render(self, content) -> bytes:
        return super().render(_sanitize_json(content))

# Ensure the backend directory is importable when the app is launched from the repo root.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from services.cot import fetch_cot, invalidate_cache as _invalidate_cot_cache
from services.analytics import (
    build_currency_strength,
    build_surprise_meter,
    fetch_bond_yields,
)
from services.economic import fetch_economic
from services.forex_factory import fetch_forexfactory_calendar
from services.investing_com import (
    fetch_economic_calendar,
    fetch_quotes,
    get_calendar_scores,
    get_latest_calendar_scores,
)
from services.newsletter import fetch_daily_newsletter
from services.investopedia import fetch_indicator_definitions
from services.atr_levels import fetch_daily_atr
from services.options_gex import fetch_options_gex
from services.retail import fetch_retail
from services.scoring import calculate_setups
from services.technical import fetch_technical
from services.trade_ideas import fetch_daily_trade_ideas

# ── Bloomberg-Grade v4 Services ──
from services.yield_curve import fetch_yield_curve
from services.cross_rates import fetch_cross_rates
from services.volatility_surface import fetch_volatility_surface
from services.correlation_matrix import fetch_correlation_matrix
from services.sector_rotation import fetch_sector_rotation
from services.fund_flows import fetch_fund_flows
from services.global_macro import fetch_global_macro
from services.rate_differentials import fetch_rate_differentials
from services.commodities_board import fetch_commodities_board
from services.crypto_dashboard import fetch_crypto_dashboard
from services.housing import fetch_housing
from services.session_map import fetch_session_map
from services.central_bank import fetch_central_bank_watch
from services.signal_convergence import compute_signal_convergence
from services.risk_calculator import calculate_risk, get_risk_presets
from services.market_pulse import fetch_market_pulse
from services.credit_monitor import fetch_credit_monitor
from services.liquidity_monitor import fetch_liquidity_monitor
from services.real_rates_monitor import fetch_real_rates_monitor
from services.lse_provider import fetch_lse_provider

from db import User, UserSession, get_db, init_db
from auth import (
    ADMIN_EMAIL,
    AuthGateMiddleware,
    MIN_PASSWORD_LEN,
    SESSION_COOKIE,
    create_session,
    get_current_user,
    hash_password,
    is_valid_email,
    require_admin,
    verify_password,
)
from sqlalchemy.orm import Session as OrmSession

# -- Quant Lab / Options Flow / Institutional Desk --
from services.quant_lab import fetch_quant_lab
from services.options_flow import fetch_options_flow
from services.institutional import fetch_institutional
from services.chart_data import fetch_chart, fetch_depth

from config import CACHE_DIR, COMMODITIES, CRYPTO, DXY, ECON_FIELDS, FOREX_PAIRS, INDICES

REGION_BY_CCY = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
    "AUD": "AU",
    "NZD": "NZ",
    "CAD": "CA",
    "CHF": "CH",
}

APP_VERSION = "4.3.0"
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend"))

# ── Background Auto-Refresh Configuration ─────────────────────────────
BACKGROUND_REFRESH_INTERVAL = 5 * 60  # 5 minutes in seconds
_data_cache_lock = threading.Lock()
_cached_data: dict[str, Any] | None = None
_cache_timestamp: float = 0
_logger = logging.getLogger("alphafinder.refresh")

# ── Master payload cache: serve instantly, rebuild in background ──
MASTER_PAYLOAD_FILE = os.path.join(CACHE_DIR, "master_payload.json")
_PAYLOAD_LOCK = threading.Lock()
_payload_cache: dict | None = None
_payload_ts: float = 0.0


def _persist_master_payload(payload: dict) -> None:
    try:
        with open(MASTER_PAYLOAD_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
    except Exception as exc:
        _logger.warning(f"[PAYLOAD] persist failed: {exc}")


def _load_persisted_payload() -> dict | None:
    try:
        if os.path.exists(MASTER_PAYLOAD_FILE):
            with open(MASTER_PAYLOAD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        _logger.warning(f"[PAYLOAD] load failed: {exc}")
    return None



def _rebuild_master_payload() -> dict | None:
    global _payload_cache, _payload_ts
    try:
        payload = _build_master_payload()
        payload = _sanitize_json(payload)
    except Exception as exc:
        _logger.error(f"[PAYLOAD] rebuild failed: {exc}")
        return None
    with _PAYLOAD_LOCK:
        _payload_cache = payload
        _payload_ts = _time.time()
    _persist_master_payload(payload)
    return payload


def _background_refresh_all_sources() -> None:
    """Fetch all data sources and cache the results."""
    global _cached_data, _cache_timestamp
    try:
        _logger.info("[REFRESH] Starting background refresh of all data sources...")
        # Import fetch functions here to avoid circular imports in the thread
        all_tasks = [
            ("cot", fetch_cot, list),
            ("economic", fetch_economic, dict),
            ("retail", fetch_retail, list),
            ("technical", fetch_technical, dict),
            ("calendar", fetch_economic_calendar, list),
            ("ff_calendar", fetch_forexfactory_calendar, list),
            ("definitions", fetch_indicator_definitions, dict),
            ("quotes", fetch_quotes, dict),
            ("bonds", fetch_bond_yields, dict),
            ("options_gex", fetch_options_gex, list),
            ("newsletter", fetch_daily_newsletter, list),
            ("trade_ideas", fetch_daily_trade_ideas, list),
            ("yield_curve", fetch_yield_curve, dict),
            ("cross_rates", fetch_cross_rates, dict),
            ("vol_surface", fetch_volatility_surface, dict),
            ("correlation", fetch_correlation_matrix, dict),
            ("sectors", fetch_sector_rotation, list),
            ("fund_flows", fetch_fund_flows, dict),
            ("global_macro", fetch_global_macro, dict),
            ("rate_diffs", fetch_rate_differentials, dict),
            ("commodities_board", fetch_commodities_board, list),
            ("crypto_dash", fetch_crypto_dashboard, dict),
            ("housing", fetch_housing, dict),
            ("central_bank", fetch_central_bank_watch, dict),
            ("market_pulse", fetch_market_pulse, dict),
            ("credit_monitor", fetch_credit_monitor, dict),
            ("liquidity_monitor", fetch_liquidity_monitor, dict),
            ("real_rates", fetch_real_rates_monitor, dict),
            ("lse", fetch_lse_provider, dict),
        ]
        results, errors = _run_tasks(all_tasks)
        with _data_cache_lock:
            _cached_data = results
            _cache_timestamp = _time.time()
        ok_count = len(results) - len(errors)
        _rebuild_master_payload()
        _logger.info("[REFRESH] Master payload rebuilt and persisted")
        _logger.info(
            f"[REFRESH] Background refresh complete: {ok_count}/{len(all_tasks)} sources OK"
            f" | {len(errors)} errors | cache timestamp: {datetime.now(timezone.utc).isoformat()}"
        )
        if errors:
            for name, err in errors.items():
                _logger.warning(f"[REFRESH]   ⚠ {name}: {err.get('message', 'unknown error')}")
    except Exception as exc:
        _logger.error(f"[REFRESH] Background refresh failed: {exc}")


def _background_refresh_loop(stop_event: threading.Event) -> None:
    """Continuously refresh data sources every 5 minutes."""
    _logger.info(
        f"[REFRESH] Background refresh thread started "
        f"(interval={BACKGROUND_REFRESH_INTERVAL}s / {BACKGROUND_REFRESH_INTERVAL // 60}min)"
    )
    # Initial refresh immediately on startup
    _background_refresh_all_sources()
    while not stop_event.is_set():
        stop_event.wait(timeout=BACKGROUND_REFRESH_INTERVAL)
        if not stop_event.is_set():
            _background_refresh_all_sources()
    _logger.info("[REFRESH] Background refresh thread stopped")


_refresh_stop_event = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: clear COT cache + start background refresh. Shutdown: stop refresh thread."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    _logger.info("[STARTUP] AlphaFinder Pro v%s starting up...", APP_VERSION)

    init_db()
    _logger.info("[STARTUP] Auth database ready")

    # Invalidate stale COT cache on startup so we always get fresh data
    global _payload_cache, _payload_ts
    persisted = _load_persisted_payload()
    if persisted:
        with _PAYLOAD_LOCK:
            _payload_cache = persisted
            _payload_ts = os.path.getmtime(MASTER_PAYLOAD_FILE)
        _logger.info("[STARTUP] Served-from-disk master payload loaded — UI will paint instantly")
    _invalidate_cot_cache()
    _logger.info("[STARTUP] COT cache cleared — will fetch fresh data from CFTC")

    # Start background refresh thread
    _refresh_stop_event.clear()
    refresh_thread = threading.Thread(
        target=_background_refresh_loop,
        args=(_refresh_stop_event,),
        daemon=True,
        name="alphafinder-refresh",
    )
    refresh_thread.start()
    _logger.info("[STARTUP] Background auto-refresh started (every %d min)", BACKGROUND_REFRESH_INTERVAL // 60)

    yield

    # Shutdown: signal the refresh thread to stop
    _refresh_stop_event.set()
    _logger.info("[SHUTDOWN] Background refresh thread signaled to stop")


app = FastAPI(title="AlphaFinder Pro API", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(AuthGateMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc) or exc.__class__.__name__,
    }


def _run_tasks(
    tasks: list[tuple[str, Callable[[], Any], Callable[[], Any]]]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    results: dict[str, Any] = {}
    errors: dict[str, dict[str, str]] = {}

    with ThreadPoolExecutor(max_workers=max(1, min(16, len(tasks)))) as executor:
        future_map = {
            executor.submit(func): (name, default_factory)
            for name, func, default_factory in tasks
        }

        for future in as_completed(future_map):
            name, default_factory = future_map[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = default_factory()
                errors[name] = _error_payload(exc)

    return results, errors


def _source_status(value: Any) -> str:
    if isinstance(value, dict):
        return "ok" if bool(value) else "empty"
    if isinstance(value, (list, tuple, set)):
        return "ok" if len(value) > 0 else "empty"
    return "ok" if value is not None else "empty"


def _clamp_score(value: int) -> int:
    return max(-2, min(2, int(value)))


def _merge_score_values(values: list[Any]) -> int:
    clean: list[int] = []
    for value in values:
        try:
            clean.append(_clamp_score(int(value)))
        except (TypeError, ValueError):
            continue

    if not clean:
        return 0

    avg = sum(clean) / len(clean)
    if avg >= 1.4:
        return 2
    if avg >= 0.2:
        return 1
    if avg <= -1.4:
        return -2
    if avg <= -0.2:
        return -1

    positives = sum(1 for item in clean if item > 0)
    negatives = sum(1 for item in clean if item < 0)
    if positives > negatives:
        return 1
    if negatives > positives:
        return -1
    return 0


def _merge_score_maps(
    base: dict[str, dict[str, Any]], *sources: dict[str, dict[str, Any]]
) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    currencies = set(base.keys())
    for source in sources:
        currencies.update(source.keys())

    for currency in currencies:
        base_fields = base.get(currency, {})
        field_names = {
            key for key in base_fields.keys() if not str(key).startswith("_")
        }
        for source in sources:
            field_names.update(source.get(currency, {}).keys())

        merged[currency] = {}
        for field in field_names:
            values: list[Any] = []
            if field in base_fields:
                values.append(base_fields[field])
            for source in sources:
                if field in source.get(currency, {}):
                    values.append(source[currency][field])
            merged[currency][field] = _merge_score_values(values)

    return merged


def _dedupe_calendar_events(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for group in groups:
        for event in group or []:
            key = (
                event.get("currency"),
                event.get("field") or (event.get("name") or "").strip().lower(),
                (event.get("name") or "").strip().lower(),
                event.get("actual"),
                event.get("forecast"),
                event.get("previous"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)

    return merged


def _parse_event_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _calendar_alert_payload(
    calendar: list[dict[str, Any]], ff_calendar: list[dict[str, Any]]
) -> dict[str, Any]:
    combined = _dedupe_calendar_events(calendar, ff_calendar)
    high_impact: list[dict[str, Any]] = []
    for event in combined:
        try:
            impact = int(event.get("impact") or 0)
        except (TypeError, ValueError):
            impact = 0
        if impact < 3:
            continue
        enriched = dict(event)
        enriched["_ts"] = _parse_event_datetime(str(event.get("datetime") or ""))
        high_impact.append(enriched)

    high_impact.sort(
        key=lambda item: item.get("_ts") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    released = [item for item in high_impact if item.get("actual") is not None]
    upcoming = [item for item in high_impact if item.get("actual") is None]
    fingerprint_input = "|".join(
        f"{item.get('currency')}|{item.get('field')}|{item.get('name')}|{item.get('actual')}|"
        f"{item.get('forecast')}|{item.get('previous')}|{item.get('datetime')}|{item.get('source')}"
        for item in high_impact[:32]
    )
    fingerprint = hashlib.sha1(fingerprint_input.encode("utf-8")).hexdigest()[:16] if fingerprint_input else ""
    latest_release = next(
        (
            item.get("datetime")
            for item in released
            if item.get("datetime")
        ),
        "",
    )
    return {
        "fingerprint": fingerprint,
        "red_folder_count": len(high_impact),
        "released_count": len(released),
        "upcoming_count": len(upcoming),
        "latest_release": latest_release,
        "events": [
            {key: value for key, value in item.items() if key != "_ts"}
            for item in high_impact[:8]
        ],
        "source_status": {
            "investing": _source_status(calendar),
            "forex_factory": _source_status(ff_calendar),
        },
        "poll_interval_sec": 60,
    }


def _prefer_real_economic_scores(
    base: dict[str, dict[str, Any]], *real_sources: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    currencies = set(base.keys())
    for source in real_sources:
        currencies.update(source.keys())

    for currency in currencies:
        merged[currency] = {}
        fields = set(base.get(currency, {}).keys())
        for source in real_sources:
            fields.update(source.get(currency, {}).keys())

        for field in fields:
            if str(field).startswith("_"):
                continue
            real_values = [
                source[currency][field]
                for source in real_sources
                if field in source.get(currency, {})
            ]
            if real_values:
                merged[currency][field] = _merge_score_values(real_values)
            else:
                merged[currency][field] = base.get(currency, {}).get(field)

    return merged


def _build_effective_economic_scores(
    monthly_base: dict[str, dict[str, Any]],
    weekly_latest: dict[str, dict[str, Any]],
    weekly_blended: dict[str, dict[str, Any]],
    ff_blended: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    effective: dict[str, dict[str, Any]] = {}
    currencies = set(monthly_base.keys()) | set(weekly_latest.keys()) | set(weekly_blended.keys()) | set(ff_blended.keys())

    for currency in currencies:
        monthly_row = monthly_base.get(currency, {})
        row: dict[str, Any] = {
            "_profile": monthly_row.get("_profile", {}),
            "_field_sources": {},
            "_field_layers": {},
        }
        for field in ECON_FIELDS:
            monthly_value = monthly_row.get(field)
            weekly_values = []
            for name, source in (
                ("latest", weekly_latest),
                ("calendar", weekly_blended),
                ("forex_factory", ff_blended),
            ):
                value = source.get(currency, {}).get(field)
                if value is None:
                    continue
                weekly_values.append((name, value))

            if weekly_values:
                row[field] = _merge_score_values([value for _, value in weekly_values])
                row["_field_sources"][field] = "weekly"
                row["_field_layers"][field] = {
                    "weekly_sources": [name for name, _ in weekly_values],
                    "monthly_available": monthly_value is not None,
                }
            elif monthly_value is not None:
                row[field] = monthly_value
                row["_field_sources"][field] = "monthly"
                row["_field_layers"][field] = {
                    "weekly_sources": [],
                    "monthly_available": True,
                }
            else:
                row[field] = None
                row["_field_sources"][field] = "missing"
                row["_field_layers"][field] = {
                    "weekly_sources": [],
                    "monthly_available": False,
                }

        effective[currency] = row

    return effective


def _pair_surprise_signal(currency_scores: dict[str, dict[str, int]], symbol: str) -> int:
    def total_for(currency: str) -> int:
        return sum(int(v) for v in currency_scores.get(currency, {}).values())

    if symbol in FOREX_PAIRS:
        base, quote = FOREX_PAIRS[symbol]
        diff = total_for(base) - total_for(quote)
    elif symbol in INDICES:
        _, currency = INDICES[symbol]
        diff = total_for(currency)
    elif symbol in COMMODITIES or symbol in CRYPTO:
        diff = -total_for("USD")
    elif symbol in DXY:
        diff = total_for("USD")
    else:
        diff = 0

    if diff >= 6:
        return 2
    if diff >= 2:
        return 1
    if diff <= -7:
        return -2
    if diff <= -2:
        return -1
    if diff > 0:
        return 1
    if diff < 0:
        return -1
    return 0


def _pair_bond_signal(bonds: dict[str, dict[str, Any]], symbol: str) -> int:
    if symbol not in FOREX_PAIRS:
        # Non-FX assets: rate environment as tailwind/headwind.
        def _cur(currency: str) -> int:
            item = bonds.get(currency) or {}
            if not item:
                return 0
            s = 0
            if float(item.get("chg10") or 0) > 0.05: s += 1
            elif float(item.get("chg10") or 0) < -0.05: s -= 1
            if float(item.get("spread") or 0) < 0: s -= 1
            regime = str(item.get("regime") or "")
            if regime in ("Steepening", "Hawkish Repricing"): s += 1
            elif regime in ("Curve Inversion", "Risk-Off Bid"): s -= 1
            return max(-2, min(2, s))
        if symbol == "US10Y":
            return _cur("USD")
        if symbol in DXY:
            return _cur("USD")
        if symbol in INDICES:
            _, currency = INDICES[symbol]
            return -_cur(currency)
        if symbol in COMMODITIES or symbol in CRYPTO:
            return -_cur("USD")
        return 0

    def currency_score(currency: str) -> int:
        item = bonds.get(currency) or {}
        if not item:
            return 0
        score = 0
        spread = float(item.get("spread") or 0.0)
        chg10 = float(item.get("chg10") or 0.0)
        chg2 = float(item.get("chg2") or 0.0)
        regime = str(item.get("regime") or "")

        if spread >= 1.5:
            score += 2
        elif spread >= 0.75:
            score += 1
        elif spread < 0:
            score -= 2

        if regime == "Steepening":
            score += 1
        elif regime == "Hawkish Repricing":
            score += 1
        elif regime == "Curve Inversion":
            score -= 2
        elif regime == "Risk-Off Bid":
            score -= 1

        if chg10 > 0.05:
            score += 1
        elif chg10 < -0.05:
            score -= 1
        if chg2 > 0.05 and chg10 > 0.05:
            score += 1

        if score >= 3:
            return 2
        if score >= 1:
            return 1
        if score <= -3:
            return -2
        if score <= -1:
            return -1
        return 0

    base, quote = FOREX_PAIRS[symbol]
    return max(-2, min(2, currency_score(base) - currency_score(quote)))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _trend_momentum(tech_row: dict[str, Any]) -> int:
    score = 0
    roc20 = _safe_float(tech_row.get("roc20"))
    chg1d = _safe_float(tech_row.get("chg1d"))
    price = _safe_float(tech_row.get("price"))
    sma20 = _safe_float(tech_row.get("sma20"))
    sma50 = _safe_float(tech_row.get("sma50"))
    sma200 = _safe_float(tech_row.get("sma200"))

    if roc20 is not None:
        if roc20 >= 2.5:
            score += 2
        elif roc20 >= 0.8:
            score += 1
        elif roc20 <= -2.5:
            score -= 2
        elif roc20 <= -0.8:
            score -= 1

    if chg1d is not None:
        if chg1d >= 0.5:
            score += 1
        elif chg1d <= -0.5:
            score -= 1

    if None not in (price, sma20, sma50, sma200):
        if price > sma20 and sma20 >= sma50 and sma50 >= sma200:
            score += 1
        elif price < sma20 and sma20 <= sma50 and sma50 <= sma200:
            score -= 1

    return _clamp_score(score)


def _compose_trend_bundle(
    setup: dict[str, Any], tech_row: dict[str, Any]
) -> dict[str, Any]:
    technical = _clamp_score(int(setup.get("trend") or 0))
    momentum = _trend_momentum(tech_row or {})
    weekly = _clamp_score(int(setup.get("surprise") or 0) + int(setup.get("bond") or 0))
    monthly = _clamp_score(int(setup.get("macro") or 0))
    sentiment = _clamp_score(int(setup.get("cot") or 0) + int(setup.get("retail") or 0))

    weighted = (
        (technical * 1.05)
        + (momentum * 0.55)
        + (weekly * 1.25)
        + (monthly * 1.15)
        + (sentiment * 0.80)
    )

    if weighted >= 3.0:
        trend = 2
    elif weighted >= 0.75:
        trend = 1
    elif weighted <= -3.0:
        trend = -2
    elif weighted <= -0.75:
        trend = -1
    else:
        trend = 0

    source_scores = {
        "weekly": weekly,
        "monthly": monthly,
        "price": _clamp_score(technical + momentum),
        "sentiment": sentiment,
    }
    source_priority = {
        "weekly": 4,
        "monthly": 3,
        "price": 2,
        "sentiment": 1,
    }
    source, source_score = max(
        source_scores.items(),
        key=lambda item: (abs(item[1]), source_priority[item[0]]),
    )
    if source_score == 0 and trend == 0:
        source = "composite"
    elif trend == 0 and source_score != 0:
        trend = 1 if source_score > 0 else -1

    contributors = sum(1 for value in source_scores.values() if value != 0)
    magnitude = abs(source_score)
    if contributors >= 3 and magnitude >= 2:
        confidence = "high"
    elif contributors >= 2 and magnitude >= 1:
        confidence = "medium"
    elif magnitude >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    note = (
        f"tech {technical:+d} · mom {momentum:+d} · weekly {weekly:+d} · "
        f"monthly {monthly:+d} · sent {sentiment:+d}"
    )

    return {
        "trend": trend,
        "source": source,
        "confidence": confidence,
        "note": note,
        "components": {
            "technical": technical,
            "momentum": momentum,
            "weekly": weekly,
            "monthly": monthly,
            "sentiment": sentiment,
        },
    }


def _normalize_symbol_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _symbol_region_hints(symbol: str) -> list[str]:
    token = _normalize_symbol_token(symbol)
    if token in FOREX_PAIRS:
        base, quote = FOREX_PAIRS[token]
        regions = [REGION_BY_CCY.get(base, ""), REGION_BY_CCY.get(quote, "")]
        return [region for region in regions if region]
    if token in INDICES:
        return ["US"]
    if token in COMMODITIES:
        return ["US"]
    if token in CRYPTO:
        return ["US"]
    if token in DXY:
        return ["US"]
    if token in {"SPY", "QQQ", "AAPL", "NVDA", "TSLA"}:
        return ["US"]
    return ["US"]


def _build_lse_overlay(lse: dict[str, Any]) -> dict[str, dict[str, Any]]:
    watchlist = lse.get("watchlist") if isinstance(lse, dict) else []
    if not isinstance(watchlist, list):
        return {}
    overlay: dict[str, dict[str, Any]] = {}
    for row in watchlist:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        label = str(row.get("label") or "")
        keys = {_normalize_symbol_token(symbol), _normalize_symbol_token(label)}
        for key in keys:
            if not key:
                continue
            overlay[key] = row
    return overlay


def _build_lse_event_gate(lse: dict[str, Any], symbol: str) -> dict[str, Any]:
    calendar = lse.get("calendar") if isinstance(lse, dict) else {}
    events = calendar.get("next_high_impact") if isinstance(calendar, dict) else []
    regions = set(_symbol_region_hints(symbol))
    matched: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_region = str(event.get("region") or "").upper()
        if event_region and event_region in regions:
            matched.append(event)
            continue
        if not event_region and regions:
            matched.append(event)

    level = "Clear"
    if len(matched) >= 3:
        level = "High"
    elif len(matched) == 2:
        level = "Elevated"
    elif len(matched) == 1:
        level = "Watch"

    return {
        "level": level,
        "count": len(matched),
        "regions": sorted(regions),
        "events": matched[:3],
    }


def _attach_lse_overlay(
    setups: list[dict[str, Any]],
    trade_ideas: list[dict[str, Any]],
    lse: dict[str, Any],
) -> None:
    overlay = _build_lse_overlay(lse)
    for row in setups:
        symbol = str(row.get("symbol") or "")
        key = _normalize_symbol_token(symbol)
        lse_row = overlay.get(key, {})
        event_gate = _build_lse_event_gate(lse, symbol)
        lse_bias = str(lse_row.get("bias") or "Neutral")
        live_change = _safe_float(lse_row.get("live_change_pct"))
        opportunity = _safe_float(lse_row.get("opportunity_score"))
        if opportunity is None:
            opportunity = 0.0
        validated = bool(lse_row) and lse_bias == row.get("bias") and event_gate["level"] in {"Clear", "Watch"}
        row["lse"] = {
            "matched": bool(lse_row),
            "symbol": lse_row.get("symbol") if lse_row else None,
            "label": lse_row.get("label") if lse_row else None,
            "bias": lse_bias,
            "trend_score": lse_row.get("trend_score") if lse_row else None,
            "live_price": lse_row.get("live_price") if lse_row else None,
            "live_change_pct": live_change,
            "rsi14": lse_row.get("rsi14") if lse_row else None,
            "vol_ratio": lse_row.get("vol_ratio") if lse_row else None,
            "opportunity_score": opportunity,
            "event_gate": event_gate,
            "confirmed": validated,
        }

    for row in trade_ideas:
        symbol = str(row.get("symbol") or "")
        key = _normalize_symbol_token(symbol)
        lse_row = overlay.get(key, {})
        event_gate = _build_lse_event_gate(lse, symbol)
        row["lse"] = {
            "matched": bool(lse_row),
            "bias": lse_row.get("bias") if lse_row else None,
            "live_price": lse_row.get("live_price") if lse_row else None,
            "live_change_pct": lse_row.get("live_change_pct") if lse_row else None,
            "trend_score": lse_row.get("trend_score") if lse_row else None,
            "opportunity_score": lse_row.get("opportunity_score") if lse_row else None,
            "event_gate": event_gate,
            "confirmed": bool(lse_row) and event_gate["level"] in {"Clear", "Watch"},
        }


def _single_task(
    name: str, func: Callable[[], Any], default_factory: Callable[[], Any]
) -> tuple[Any, dict[str, dict[str, str]]]:
    results, errors = _run_tasks([(name, func, default_factory)])
    return results[name], errors


# ── Auth models ──────────────────────────────────────────────────────
class SignupBody(BaseModel):
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


# The session cookie only requires HTTPS ("Secure") in production. Detected via
# DATABASE_URL pointing at real Postgres (set explicitly in render.yaml) rather
# than the local sqlite fallback — over plain-HTTP local dev, Secure would make
# browsers silently refuse to store the cookie at all.
_IS_PRODUCTION = os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://"))
COOKIE_KWARGS = dict(
    httponly=True,
    secure=_IS_PRODUCTION,
    samesite="lax",
    max_age=60 * 60 * 24 * 30,  # 30 days
    path="/",
)


def _session_public(sess: "UserSession", email: str) -> dict:
    return {
        "id": sess.id,
        "email": email,
        "user_agent": sess.user_agent or "",
        "ip_address": sess.ip_address or "",
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "last_seen_at": sess.last_seen_at.isoformat() if sess.last_seen_at else None,
    }


@app.post("/api/auth/signup")
def api_auth_signup(body: SignupBody, request: Request, response: Response, db: OrmSession = Depends(get_db)):
    email = (body.email or "").strip().lower()
    password = body.password or ""
    if not is_valid_email(email):
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    if len(password) < MIN_PASSWORD_LEN:
        return JSONResponse({"error": f"Password must be at least {MIN_PASSWORD_LEN} characters."}, status_code=400)
    if db.query(User).filter(User.email == email).first():
        return JSONResponse({"error": "An account with that email already exists."}, status_code=409)
    user = User(email=email, password_hash=hash_password(password), is_admin=(email == ADMIN_EMAIL.lower()))
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_session(db, user, request)
    response.set_cookie(SESSION_COOKIE, token, **COOKIE_KWARGS)
    return {"email": user.email, "is_admin": user.is_admin}


@app.post("/api/auth/login")
def api_auth_login(body: LoginBody, request: Request, response: Response, db: OrmSession = Depends(get_db)):
    email = (body.email or "").strip().lower()
    password = body.password or ""
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return JSONResponse({"error": "Incorrect email or password."}, status_code=401)
    token = create_session(db, user, request)
    response.set_cookie(SESSION_COOKIE, token, **COOKIE_KWARGS)
    return {"email": user.email, "is_admin": user.is_admin}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request, response: Response, db: OrmSession = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = db.query(UserSession).filter(UserSession.token == token).first()
        if sess:
            sess.revoked = True
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def api_auth_me(user: User = Depends(get_current_user)):
    return {"email": user.email, "is_admin": user.is_admin}


# ── Admin: live sessions + user management ──────────────────────────
@app.get("/api/admin/sessions")
def api_admin_sessions(_: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    rows = (
        db.query(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .filter(UserSession.revoked == False)  # noqa: E712
        .order_by(UserSession.last_seen_at.desc())
        .all()
    )
    return {"sessions": [_session_public(sess, u.email) for sess, u in rows]}


@app.post("/api/admin/sessions/{session_id}/revoke")
def api_admin_revoke_session(session_id: int, _: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    sess = db.query(UserSession).filter(UserSession.id == session_id).first()
    if not sess:
        return JSONResponse({"error": "Session not found."}, status_code=404)
    sess.revoked = True
    db.commit()
    return {"ok": True}


@app.get("/api/admin/users")
def api_admin_users(_: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    users = db.query(User).order_by(User.created_at.asc()).all()
    out = []
    for u in users:
        active = db.query(UserSession).filter(UserSession.user_id == u.id, UserSession.revoked == False).count()  # noqa: E712
        out.append({
            "id": u.id, "email": u.email, "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "active_sessions": active,
        })
    return {"users": out}


@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int, admin: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return JSONResponse({"error": "User not found."}, status_code=404)
    if target.email == ADMIN_EMAIL.lower():
        return JSONResponse({"error": "The primary admin account cannot be deleted."}, status_code=403)
    db.query(UserSession).filter(UserSession.user_id == user_id).delete()
    db.delete(target)
    db.commit()
    return {"ok": True}


@app.get("/")
def root():
    index_file = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"status": "AlphaFinder Pro API running", "docs": "/docs"}


@app.get("/api/health")
def health():
    return {"status": "ok", "time": _utc_now_iso(), "version": APP_VERSION}


@app.get("/api/cot")
def api_cot():
    data, errors = _single_task("cot", fetch_cot, list)
    payload = {"data": data, "count": len(data), "source": "CFTC Socrata"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/economic")
def api_economic():
    data, errors = _single_task("economic", fetch_economic, dict)
    payload = {"data": data, "source": "World Bank WDI + OECD CLI"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/retail")
def api_retail():
    data, errors = _single_task("retail", fetch_retail, list)
    payload = {"data": data, "count": len(data), "source": "Myfxbook / FXSSI / Dukascopy"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/technical")
def api_technical():
    data, errors = _single_task("technical", fetch_technical, dict)
    payload = {"data": data, "count": len(data), "source": "yfinance"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/calendar")
def api_calendar():
    events, errors = _single_task("calendar", fetch_economic_calendar, list)
    payload = {"data": events, "count": len(events), "source": "Investing.com"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/calendar/alerts")
def api_calendar_alerts():
    tasks = [
        ("calendar", lambda: fetch_economic_calendar(2), list),
        ("ff_calendar", lambda: fetch_forexfactory_calendar(2), list),
    ]
    results, errors = _run_tasks(tasks)
    payload = _calendar_alert_payload(
        results.get("calendar") or [],
        results.get("ff_calendar") or [],
    )
    payload["last_checked"] = _utc_now_iso()
    payload["source"] = "Investing.com + Forex Factory"
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/quotes")
def api_quotes():
    data, errors = _single_task("quotes", fetch_quotes, dict)
    payload = {"data": data, "source": "Investing.com / yfinance"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


@app.get("/api/definitions")
def api_definitions():
    data, errors = _single_task("definitions", fetch_indicator_definitions, dict)
    payload = {"data": data, "source": "Investopedia"}
    if errors:
        payload["errors"] = errors
    return JSONResponse(payload)


def _build_master_payload() -> dict:
    """Assemble the full dashboard payload (runs all fetchers; slow on cold caches)."""
    # ── Phase 1: Core data (original 12 tasks) ──
    tasks = [
        ("cot", fetch_cot, list),
        ("economic", fetch_economic, dict),
        ("retail", fetch_retail, list),
        ("technical", fetch_technical, dict),
        ("calendar", fetch_economic_calendar, list),
        ("ff_calendar", fetch_forexfactory_calendar, list),
        ("definitions", fetch_indicator_definitions, dict),
        ("quotes", fetch_quotes, dict),
        ("bonds", fetch_bond_yields, dict),
        ("options_gex", fetch_options_gex, list),
        ("newsletter", fetch_daily_newsletter, list),
        ("trade_ideas", fetch_daily_trade_ideas, list),
        # ── Phase 2: Bloomberg v4 new modules ──
        ("yield_curve", fetch_yield_curve, dict),
        ("cross_rates", fetch_cross_rates, dict),
        ("vol_surface", fetch_volatility_surface, dict),
        ("correlation", fetch_correlation_matrix, dict),
        ("sectors", fetch_sector_rotation, list),
        ("fund_flows", fetch_fund_flows, dict),
        ("global_macro", fetch_global_macro, dict),
        ("rate_diffs", fetch_rate_differentials, dict),
        ("commodities_board", fetch_commodities_board, list),
        ("crypto_dash", fetch_crypto_dashboard, dict),
        ("housing", fetch_housing, dict),
        ("central_bank", fetch_central_bank_watch, dict),
        ("market_pulse", fetch_market_pulse, dict),
        ("credit_monitor", fetch_credit_monitor, dict),
        ("liquidity_monitor", fetch_liquidity_monitor, dict),
        ("real_rates", fetch_real_rates_monitor, dict),
        ("lse", fetch_lse_provider, dict),
    ]
    results, errors = _run_tasks(tasks)

    cot = results["cot"]
    econ = results["economic"]
    retail = results["retail"]
    tech = results["technical"]
    calendar = results["calendar"]
    ff_calendar = results["ff_calendar"]
    definitions = results["definitions"]
    quotes = results["quotes"]
    bonds = results["bonds"]
    options_gex = results["options_gex"]
    newsletter = results["newsletter"]
    trade_ideas = results["trade_ideas"]
    calendar_alerts = _calendar_alert_payload(calendar, ff_calendar)
    try:
        daily_atr = fetch_daily_atr()
    except Exception as exc:
        daily_atr = []
        errors["daily_atr"] = _error_payload(exc)

    # Bloomberg v4 results
    yield_curve = results.get("yield_curve", {})
    cross_rates = results.get("cross_rates", {})
    vol_surface = results.get("vol_surface", {})
    correlation = results.get("correlation", {})
    sectors = results.get("sectors", [])
    fund_flows = results.get("fund_flows", {})
    global_macro = results.get("global_macro", {})
    rate_diffs = results.get("rate_diffs", {})
    commodities_board = results.get("commodities_board", [])
    crypto_dash = results.get("crypto_dash", {})
    housing = results.get("housing", {})
    central_bank = results.get("central_bank", {})
    market_pulse = results.get("market_pulse", {})
    credit_monitor = results.get("credit_monitor", {})
    liquidity_monitor = results.get("liquidity_monitor", {})
    real_rates = results.get("real_rates", {})
    lse = results.get("lse", {})

    # Session map (instant, no I/O)
    try:
        session_map = fetch_session_map()
    except Exception:
        session_map = {}

    combined_calendar = _dedupe_calendar_events(calendar, ff_calendar)
    weekly_scores = {}
    ff_scores = {}

    try:
        weekly_scores = get_calendar_scores(calendar) if calendar else {}
        ff_scores = get_calendar_scores(ff_calendar) if ff_calendar else {}
        ff_latest_scores = get_latest_calendar_scores(ff_calendar) if ff_calendar else {}
        econ = _build_effective_economic_scores(
            econ, ff_latest_scores, weekly_scores, ff_scores
        )
    except Exception as exc:
        errors["calendar_scores"] = _error_payload(exc)

    try:
        setups = calculate_setups(tech, econ, cot, retail)
    except Exception as exc:
        setups = []
        errors["setups"] = _error_payload(exc)

    weekly_surprise_scores = _merge_score_maps(
        {},
        ff_latest_scores if "ff_latest_scores" in locals() else {},
        weekly_scores,
        ff_scores,
    )
    for setup in setups:
        symbol = setup.get("symbol", "")
        tech_row = tech.get(symbol, {}) if isinstance(tech, dict) else {}

        # Supplementary intelligence (display-only, NOT added to bias score)
        surprise_signal = _pair_surprise_signal(weekly_surprise_scores, symbol)
        bond_signal = _pair_bond_signal(bonds if isinstance(bonds, dict) else {}, symbol)
        setup["surprise"] = surprise_signal
        setup["bond"] = bond_signal

        # Build trend detail bundle for the UI breakdown
        trend_bundle = _compose_trend_bundle(setup, tech_row)
        setup["trend_source"] = trend_bundle["source"]
        setup["trend_confidence"] = trend_bundle["confidence"]
        setup["trend_note"] = trend_bundle["note"]
        setup["trend_components"] = trend_bundle["components"]

        # A1 model: trend stays as-is from scoring.py (SMA3/14 crossover, −3 to +3)
        # Surprise/bond are stored for display but do NOT modify total_score
        components = setup.setdefault("components", {})
        components["surprise"] = surprise_signal
        components["bond"] = bond_signal

        # total_score is already computed by scoring.py using the 5 A1 pillars
        # Bias thresholds: ≥9 Very Bullish, ≥5 Bullish, ≤−5 Bearish, ≤−9 Very Bearish
        total_score = int(setup.get("total_score") or 0)
        if total_score >= 9:
            setup["bias"] = "Very Bullish"
        elif total_score >= 5:
            setup["bias"] = "Bullish"
        elif total_score <= -9:
            setup["bias"] = "Very Bearish"
        elif total_score <= -5:
            setup["bias"] = "Bearish"
        else:
            setup["bias"] = "Neutral"

    surprise_meter = build_surprise_meter(combined_calendar)
    currency_strength = build_currency_strength(
        setups, tech, econ, retail, cot, surprise_meter
    )

    # Signal convergence (computed from setups)
    try:
        signal_conv = compute_signal_convergence(setups)
    except Exception:
        signal_conv = []

    _attach_lse_overlay(setups, trade_ideas, lse)

    validated_setups = [
        item for item in setups
        if item.get("lse", {}).get("confirmed")
    ]
    validated_trade_ideas = [
        item for item in trade_ideas
        if item.get("lse", {}).get("confirmed")
    ]
    if isinstance(lse, dict):
        lse["validated_setups"] = validated_setups[:20]
        lse["validated_trade_ideas"] = validated_trade_ideas[:20]
        use_cases = lse.setdefault("use_cases", {})
        use_cases["trade_validation"] = {
            "count": len(validated_setups),
            "leaders": validated_setups[:8],
        }
        use_cases["live_candle_confirmation"] = {
            "count": len([item for item in setups if item.get("lse", {}).get("matched")]),
            "leaders": validated_setups[:8],
        }
        use_cases["event_gate"] = {
            "count": len([item for item in setups if item.get("lse", {}).get("event_gate", {}).get("level") in {"Elevated", "High"}]),
            "leaders": [
                {
                    "symbol": item.get("symbol"),
                    "level": item.get("lse", {}).get("event_gate", {}).get("level"),
                    "regions": item.get("lse", {}).get("event_gate", {}).get("regions", []),
                }
                for item in setups
                if item.get("lse", {}).get("event_gate", {}).get("level") in {"Elevated", "High"}
            ][:8],
        }

    setups.sort(
        key=lambda item: (
            int(item.get("total_score") or 0),
            1 if item.get("lse", {}).get("confirmed") else 0,
            int(item.get("lse", {}).get("opportunity_score") or 0),
        ),
        reverse=True,
    )
    trade_ideas.sort(
        key=lambda item: (
            1 if item.get("lse", {}).get("confirmed") else 0,
            int(item.get("lse", {}).get("opportunity_score") or 0),
            1 if str(item.get("bias") or "") == "Bullish" else 0,
        ),
        reverse=True,
    )

    econ_summary = {}
    for currency, scores in econ.items():
        econ_summary[currency] = {
            key: value for key, value in scores.items() if not key.startswith("_")
        }

    # Risk presets (instant, no I/O)
    risk_presets = get_risk_presets()

    payload = {
        "status": "ok" if not errors else "partial",
        "setups": setups,
        "cot": cot,
        "retail": retail,
        "technical": tech,
        "econ_summary": econ_summary,
        "calendar": combined_calendar[:60],
        "tech_summaries": {},
        "news": newsletter,
        "newsletter": newsletter,
        "quotes": quotes,
        "bonds": bonds,
        "options_gex": options_gex,
        "daily_atr": daily_atr,
        "trade_ideas": trade_ideas,
        "surprise_meter": surprise_meter,
        "currency_strength": currency_strength,
        "calendar_alerts": calendar_alerts,
        "definitions": definitions,
        # ── Bloomberg v4 new data ──
        "yield_curve": yield_curve,
        "cross_rates": cross_rates,
        "vol_surface": vol_surface,
        "correlation": correlation,
        "sectors": sectors,
        "fund_flows": fund_flows,
        "global_macro": global_macro,
        "rate_diffs": rate_diffs,
        "commodities_board": commodities_board,
        "crypto_dash": crypto_dash,
        "housing": housing,
        "central_bank": central_bank,
        "market_pulse": market_pulse,
        "credit_monitor": credit_monitor,
        "liquidity_monitor": liquidity_monitor,
        "real_rates": real_rates,
        "lse": lse,
        "session_map": session_map,
        "signal_convergence": signal_conv,
        "risk_presets": risk_presets,
        "last_updated": _utc_now_iso(),
        "version": APP_VERSION,
        "refresh_policy": {
            "full_dashboard_sec": 300,
            "red_folder_poll_sec": 60,
        },
        "source_status": {
            "cot": _source_status(cot),
            "economic": _source_status(econ_summary),
            "retail": _source_status(retail),
            "technical": _source_status(tech),
            "calendar": _source_status(combined_calendar),
            "ff_calendar": _source_status(ff_calendar),
            "quotes": _source_status(quotes),
            "bonds": _source_status(bonds),
            "options_gex": _source_status(options_gex),
            "newsletter": _source_status(newsletter),
            "daily_atr": _source_status(daily_atr),
            "trade_ideas": _source_status(trade_ideas),
            "definitions": _source_status(definitions),
            # v4 sources
            "yield_curve": _source_status(yield_curve),
            "cross_rates": _source_status(cross_rates),
            "vol_surface": _source_status(vol_surface),
            "correlation": _source_status(correlation),
            "sectors": _source_status(sectors),
            "fund_flows": _source_status(fund_flows),
            "global_macro": _source_status(global_macro),
            "rate_diffs": _source_status(rate_diffs),
            "commodities_board": _source_status(commodities_board),
            "crypto_dash": _source_status(crypto_dash),
            "housing": _source_status(housing),
            "central_bank": _source_status(central_bank),
            "market_pulse": _source_status(market_pulse),
            "credit_monitor": _source_status(credit_monitor),
            "liquidity_monitor": _source_status(liquidity_monitor),
            "real_rates": _source_status(real_rates),
            "lse": _source_status(lse),
            "session_map": _source_status(session_map),
            "signal_convergence": _source_status(signal_conv),
        },
        "sources": {
            "cot": "CFTC Socrata Public API",
            "economic": "Latest releases + Investing.com + Forex Factory + World Bank WDI + OECD CLI",
            "retail": "Myfxbook / FXSSI / Dukascopy",
            "technical": "yfinance SMA/ROC + seasonality",
            "quotes": "Investing.com / yfinance",
            "bonds": "TradingEconomics",
            "options_gex": "Yahoo Finance options chain / yfinance",
            "newsletter": "Federal Reserve, ECB, BIS, CFTC, World Bank, and institutional commentary pages",
            "daily_atr": "yfinance daily OHLC",
            "trade_ideas": "yfinance daily + intraday OHLC, EMA 50/200, ATR, VWAP/session proxy, correlation basket",
            "calendar_alerts": "Investing.com + Forex Factory high-impact event pulse",
            # v4 sources
            "yield_curve": "FRED Treasury yields (DGS series)",
            "cross_rates": "yfinance FX crosses",
            "vol_surface": "CBOE VIX term structure (yfinance)",
            "correlation": "yfinance 60-day rolling correlation",
            "sectors": "yfinance S&P 500 sector ETFs",
            "fund_flows": "FRED M2, Fed Balance Sheet, Bank Reserves",
            "global_macro": "FRED multi-series (GDP, CPI, NFP, ISM)",
            "rate_diffs": "Central bank official rates",
            "commodities_board": "yfinance futures (GC, SI, CL, HG, PL, PA, NG)",
            "crypto_dash": "yfinance + alternative.me Fear/Greed",
            "housing": "FRED housing (HOUST, PERMIT, CSUSHPISA, MORTGAGE30US)",
            "central_bank": "FRED Fed Funds + CB communications",
            "market_pulse": "yfinance cross-asset monitor (indices, FX, rates, commodities, crypto)",
            "credit_monitor": "FRED / ICE BofA credit spread and yield series",
            "liquidity_monitor": "FRED Fed balance sheet, TGA, RRP, reserves, SOFR, DFF",
            "real_rates": "FRED nominal yields, TIPS real yields, and breakeven inflation",
            "lse": "London Strategic Edge live candles, calendar, insider trades, dividends and splits",
            "session_map": "UTC session computation",
            "signal_convergence": "Multi-signal alignment aggregation",
        },
    }
    if errors:
        payload["errors"] = errors
    return payload


@app.get("/api/data")
def api_all():
    """Master endpoint — serves the prebuilt payload instantly; background loop keeps it fresh."""
    with _PAYLOAD_LOCK:
        cached = _payload_cache
        age = _time.time() - _payload_ts if _payload_cache else None
    if cached is not None:
        cached = dict(cached)
        cached["cache_age_sec"] = round(age, 1) if age is not None else None
        return JSONResponse(cached)
    # No cached payload yet (fresh cold start / just-deployed instance). The background
    # refresh thread is already building it (started at app startup) — building it again
    # synchronously here would block this request past Render's proxy timeout and return
    # a bare 500. Return fast instead; the frontend already retries with backoff.
    return JSONResponse(
        {"status": "warming_up", "message": "Initial data build in progress — retry shortly."},
        status_code=503,
    )


# ── v4 Individual API Endpoints ──

@app.get("/api/chart")
def api_chart(symbol: str = Query("SPY"), interval: str = Query("1d")):
    try:
        return JSONResponse(fetch_chart(symbol, interval))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
@app.get("/api/depth")
def api_depth(symbol: str = Query("BTC-USD")):
    try:
        return JSONResponse(fetch_depth(symbol))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
@app.get("/api/quant_lab")
def api_quant_lab(symbol: str = Query("SPY")):
    try:
        return JSONResponse(fetch_quant_lab(symbol))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/options_flow")
def api_options_flow(symbol: str = Query("SPY")):
    try:
        return JSONResponse(fetch_options_flow(symbol))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/institutional")
def api_institutional():
    try:
        return JSONResponse(fetch_institutional())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
@app.get("/api/yield_curve")
def api_yield_curve():
    try:
        return JSONResponse(fetch_yield_curve())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/cross_rates")
def api_cross_rates():
    try:
        return JSONResponse(fetch_cross_rates())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/vol_surface")
def api_vol_surface():
    try:
        return JSONResponse(fetch_volatility_surface())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/correlation")
def api_correlation():
    try:
        return JSONResponse(fetch_correlation_matrix())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/sectors")
def api_sectors():
    try:
        return JSONResponse({"data": fetch_sector_rotation()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/fund_flows")
def api_fund_flows():
    try:
        return JSONResponse(fetch_fund_flows())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/global_macro")
def api_global_macro():
    try:
        return JSONResponse(fetch_global_macro())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/rate_diffs")
def api_rate_diffs():
    try:
        return JSONResponse(fetch_rate_differentials())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/commodities_board")
def api_commodities_board():
    try:
        return JSONResponse({"data": fetch_commodities_board()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/crypto_dash")
def api_crypto_dash():
    try:
        return JSONResponse(fetch_crypto_dashboard())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/housing")
def api_housing():
    try:
        return JSONResponse(fetch_housing())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/central_bank")
def api_central_bank():
    try:
        return JSONResponse(fetch_central_bank_watch())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/market_pulse")
def api_market_pulse():
    try:
        return JSONResponse(fetch_market_pulse())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/credit_monitor")
def api_credit_monitor():
    try:
        return JSONResponse(fetch_credit_monitor())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/liquidity_monitor")
def api_liquidity_monitor():
    try:
        return JSONResponse(fetch_liquidity_monitor())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/real_rates")
def api_real_rates():
    try:
        return JSONResponse(fetch_real_rates_monitor())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/lse")
def api_lse():
    try:
        return JSONResponse(fetch_lse_provider())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/session_map")
def api_session_map():
    try:
        return JSONResponse(fetch_session_map())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/risk_calc")
def api_risk_calc(
    balance: float = Query(10000),
    risk_pct: float = Query(1.0),
    entry: float = Query(1.0),
    sl: float = Query(0.0),
    tp: float = Query(0.0),
    symbol: str = Query("EURUSD"),
):
    try:
        return JSONResponse(calculate_risk(balance, risk_pct, entry, sl, tp, symbol))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/signal_convergence")
def api_signal_convergence():
    try:
        # Need to run setups first
        from services.scoring import calculate_setups as _calc
        tech_d, _ = _single_task("technical", fetch_technical, dict)
        econ_d, _ = _single_task("economic", fetch_economic, dict)
        cot_d, _ = _single_task("cot", fetch_cot, list)
        retail_d, _ = _single_task("retail", fetch_retail, list)
        setups = _calc(tech_d, econ_d, cot_d, retail_d)
        return JSONResponse({"data": compute_signal_convergence(setups)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[BASE_DIR, FRONTEND_DIR],
    )
