"""
Risk Calculator — Position sizing, pip value, R:R computations.
Bloomberg equivalent: MARS
"""
from __future__ import annotations
from typing import Any
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import FOREX_PAIRS

def calculate_risk(
    account_balance: float = 10000,
    risk_pct: float = 1.0,
    entry_price: float = 1.0,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    symbol: str = "EURUSD",
) -> dict[str, Any]:
    """Compute position size, pip value, and risk/reward metrics."""
    
    risk_amount = account_balance * (risk_pct / 100)
    
    # Pip calculations
    is_jpy = symbol.endswith("JPY")
    pip_size = 0.01 if is_jpy else 0.0001
    
    if stop_loss and entry_price:
        sl_pips = abs(entry_price - stop_loss) / pip_size
    else:
        sl_pips = 0
    
    if take_profit and entry_price:
        tp_pips = abs(take_profit - entry_price) / pip_size
    else:
        tp_pips = 0
    
    # R:R
    rr = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0
    
    # Position size (standard lot = 100,000 units)
    if sl_pips > 0:
        pip_value = risk_amount / sl_pips
        lot_size = round(pip_value / (pip_size * 100000), 2)
    else:
        pip_value = 0
        lot_size = 0
    
    return {
        "symbol": symbol,
        "account_balance": account_balance,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2),
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "sl_pips": round(sl_pips, 1),
        "tp_pips": round(tp_pips, 1),
        "rr_ratio": rr,
        "pip_value": round(pip_value, 4),
        "lot_size": lot_size,
        "units": round(lot_size * 100000),
        "is_real": True,
    }

def get_risk_presets() -> dict[str, Any]:
    """Return common risk presets for the calculator."""
    return {
        "presets": [
            {"name": "Conservative", "risk_pct": 0.5, "description": "Low risk per trade"},
            {"name": "Standard", "risk_pct": 1.0, "description": "Industry standard"},
            {"name": "Moderate", "risk_pct": 1.5, "description": "Moderate risk"},
            {"name": "Aggressive", "risk_pct": 2.0, "description": "Higher risk tolerance"},
            {"name": "Prop Firm", "risk_pct": 0.5, "description": "Prop firm safe mode"},
        ],
        "lot_sizes": {
            "Standard": 100000,
            "Mini": 10000,
            "Micro": 1000,
            "Nano": 100,
        },
        "is_real": True,
    }
