"""
Signal Convergence — Aggregated signal alignment per asset.
Bloomberg equivalent: Composite Signal View
"""
from __future__ import annotations
from typing import Any, List

def compute_signal_convergence(setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Takes the setup results and computes how many signals align."""
    
    result = []
    for s in setups:
        signals = {
            "trend": s.get("trend", 0),
            "seasonality": s.get("seasonality", 0),
            "cot": s.get("cot", 0),
            "retail": s.get("retail", 0),
            "macro": s.get("macro", 0),
        }
        
        # Enhanced scores if available
        enhanced = s.get("enhanced", {})
        if enhanced:
            for key in ("vol", "rsi", "gex", "strength", "risk"):
                if key in enhanced:
                    signals[key] = enhanced[key]
        
        # Count aligned signals
        bullish = sum(1 for v in signals.values() if v > 0)
        bearish = sum(1 for v in signals.values() if v < 0)
        total = len(signals)
        neutral = total - bullish - bearish
        
        # Strong signals (value of 2 or -2)
        strong_bull = sum(1 for v in signals.values() if v >= 2)
        strong_bear = sum(1 for v in signals.values() if v <= -2)
        
        # Convergence score
        if bullish > bearish:
            direction = "Bullish"
            convergence = round((bullish / total) * 100, 1)
        elif bearish > bullish:
            direction = "Bearish"
            convergence = round((bearish / total) * 100, 1)
        else:
            direction = "Neutral"
            convergence = 0
        
        # Confidence level
        aligned = max(bullish, bearish)
        if aligned >= total * 0.8 and (strong_bull >= 2 or strong_bear >= 2):
            confidence = "Very High"
        elif aligned >= total * 0.6:
            confidence = "High"
        elif aligned >= total * 0.4:
            confidence = "Medium"
        else:
            confidence = "Low"
        
        result.append({
            "symbol": s.get("symbol", ""),
            "direction": direction,
            "convergence": convergence,
            "confidence": confidence,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "strong_bull": strong_bull,
            "strong_bear": strong_bear,
            "total_signals": total,
            "signals": signals,
            "total_score": s.get("total_score", 0),
            "bias": s.get("bias", "Neutral"),
        })
    
    result.sort(key=lambda x: abs(x.get("convergence", 0)), reverse=True)
    return result
