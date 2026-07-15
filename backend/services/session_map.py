"""
Session Map — Real-time trading session tracker.
Computes Tokyo/London/NYC session status from UTC time.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TRADING_SESSIONS

def fetch_session_map() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    
    sessions = {}
    active_sessions = []
    
    for name, info in TRADING_SESSIONS.items():
        open_h = info["open"]
        close_h = info["close"]
        
        # Handle sessions that wrap around midnight (e.g., Tokyo)
        if open_h < close_h:
            is_active = open_h <= current_hour < close_h
        else:
            is_active = current_hour >= open_h or current_hour < close_h
        
        if is_active:
            elapsed = (current_hour - open_h) % 24
            remaining = (close_h - current_hour) % 24
            progress = round((elapsed / ((close_h - open_h) % 24)) * 100, 1) if (close_h - open_h) % 24 > 0 else 0
            active_sessions.append(info["label"])
        else:
            hours_until = (open_h - current_hour) % 24
            remaining = 0
            elapsed = 0
            progress = 0
        
        sessions[name] = {
            "label": info["label"],
            "open_utc": f"{open_h:02d}:00",
            "close_utc": f"{close_h:02d}:00",
            "is_active": is_active,
            "progress": progress,
            "hours_remaining": remaining if is_active else 0,
            "hours_until_open": 0 if is_active else (open_h - current_hour) % 24,
        }
    
    # Session overlap detection
    overlaps = []
    if sessions.get("Tokyo", {}).get("is_active") and sessions.get("London", {}).get("is_active"):
        overlaps.append("Tokyo-London Overlap")
    if sessions.get("London", {}).get("is_active") and sessions.get("NYC", {}).get("is_active"):
        overlaps.append("London-NYC Overlap (Peak Liquidity)")
    
    # Market regime
    if len(active_sessions) >= 2:
        regime = "High Liquidity"
    elif len(active_sessions) == 1:
        regime = f"{active_sessions[0]} Session"
    else:
        regime = "Low Liquidity (Off-Hours)"
    
    return {
        "sessions": sessions,
        "active": active_sessions,
        "overlaps": overlaps,
        "regime": regime,
        "current_utc": now.strftime("%H:%M UTC"),
        "is_real": True,
    }
