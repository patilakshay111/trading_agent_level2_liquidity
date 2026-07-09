from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QualityResult:
    ok: bool
    reason: str


def validate_book_features(features: dict[str, Any], max_spread_bps: float) -> QualityResult:
    required = ["best_bid", "best_ask", "mid", "spread_bps", "bid_depth", "ask_depth"]
    missing = [name for name in required if name not in features]
    if missing:
        return QualityResult(False, f"Missing features: {', '.join(missing)}")
    if features["best_bid"] <= 0 or features["best_ask"] <= 0 or features["mid"] <= 0:
        return QualityResult(False, "Invalid non-positive price.")
    if features["best_bid"] >= features["best_ask"]:
        return QualityResult(False, "Crossed or locked book.")
    if features["spread_bps"] > max_spread_bps:
        return QualityResult(False, f"Spread too wide: {features['spread_bps']:.2f} bps.")
    if features["bid_depth"] <= 0 or features["ask_depth"] <= 0:
        return QualityResult(False, "Empty book depth.")
    return QualityResult(True, "OK")
