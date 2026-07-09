from __future__ import annotations

from typing import Any


def options_positioning_context(rows: list[dict[str, Any]], price: float) -> dict[str, Any]:
    if not rows:
        return {
            "enabled": True,
            "available": False,
            "reason": "No options positioning rows loaded.",
            "bias": "unknown",
            "levels": [],
        }

    parsed = []
    for row in rows:
        try:
            strike = float(row.get("strike") or 0.0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        call_oi = float(row.get("call_oi") or 0.0)
        put_oi = float(row.get("put_oi") or 0.0)
        total_oi = call_oi + put_oi
        distance = abs(strike - price)
        distance_bps = (distance / price) * 10000.0 if price > 0 else 0.0
        parsed.append(
            {
                "as_of_date": row.get("as_of_date"),
                "underlying": row.get("underlying"),
                "expiry": row.get("expiry"),
                "strike": strike,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "call_volume": float(row.get("call_volume") or 0.0),
                "put_volume": float(row.get("put_volume") or 0.0),
                "total_oi": total_oi,
                "put_call_ratio": round(put_oi / call_oi, 4) if call_oi else None,
                "distance_bps": round(distance_bps, 2),
            }
        )

    if not parsed:
        return {
            "enabled": True,
            "available": False,
            "reason": "Options positioning rows did not contain valid strikes.",
            "bias": "unknown",
            "levels": [],
        }

    max_call = max(parsed, key=lambda item: (item["call_oi"], item["total_oi"]))
    max_put = max(parsed, key=lambda item: (item["put_oi"], item["total_oi"]))
    max_total = max(parsed, key=lambda item: item["total_oi"])
    nearest = min(parsed, key=lambda item: item["distance_bps"])
    nearby = sorted(parsed, key=lambda item: item["distance_bps"])[:8]
    call_total = sum(item["call_oi"] for item in parsed)
    put_total = sum(item["put_oi"] for item in parsed)
    ratio = put_total / call_total if call_total else None
    if ratio is None:
        bias = "unknown"
    elif ratio >= 1.25:
        bias = "put_heavy"
    elif ratio <= 0.8:
        bias = "call_heavy"
    else:
        bias = "balanced"

    return {
        "enabled": True,
        "available": True,
        "as_of_date": parsed[0].get("as_of_date"),
        "underlying": parsed[0].get("underlying"),
        "bias": bias,
        "put_call_ratio": round(ratio, 4) if ratio is not None else None,
        "call_oi_total": round(call_total, 2),
        "put_oi_total": round(put_total, 2),
        "nearest_strike": nearest,
        "call_wall": max_call,
        "put_wall": max_put,
        "largest_magnet": max_total,
        "levels": nearby,
    }
