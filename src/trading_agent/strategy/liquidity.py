from __future__ import annotations

from statistics import mean
from time import time
from typing import Any


def _float_levels(levels: list[list[str]], limit: int) -> list[tuple[float, float]]:
    return [(float(price), float(qty)) for price, qty in levels[:limit]]


def _ladder(levels: list[tuple[float, float]], max_qty: float, mid: float) -> list[dict[str, float]]:
    return [
        {
            "price": round(price, 4),
            "qty": round(qty, 8),
            "notional": round(price * qty, 4),
            "distance_bps": round(abs(price - mid) / mid * 10000.0, 2) if mid else 0.0,
            "strength": round((qty / max_qty) * 100.0, 2) if max_qty else 0.0,
        }
        for price, qty in levels
    ]


def _far_liquidity_bands(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    mid: float,
) -> list[dict[str, float | str]]:
    bands = [(0, 5), (5, 10), (10, 25), (25, 50), (50, 100), (100, 200), (200, 500)]
    output = []
    for low, high in bands:
        bid_notional = sum(price * qty for price, qty in bids if low <= ((mid - price) / mid * 10000.0) < high)
        ask_notional = sum(price * qty for price, qty in asks if low <= ((price - mid) / mid * 10000.0) < high)
        total = bid_notional + ask_notional
        bias = ((ask_notional - bid_notional) / total) if total else 0.0
        output.append(
            {
                "band": f"{low}-{high} bps",
                "low_bps": low,
                "high_bps": high,
                "bid_notional": round(bid_notional, 4),
                "ask_notional": round(ask_notional, 4),
                "bias": round(bias, 4),
            }
        )
    max_total = max((row["bid_notional"] + row["ask_notional"] for row in output), default=0) or 1.0
    for row in output:
        row["strength"] = round(((row["bid_notional"] + row["ask_notional"]) / max_total) * 100.0, 2)
    return output


def _price_bucket_floor(price: float, bucket_size: float) -> float:
    if bucket_size <= 0:
        return price
    return (price // bucket_size) * bucket_size


def _price_bucket_label(low: float, high: float) -> str:
    return f"{low:.0f}-{high:.0f}" if high >= 100 else f"{low:.2f}-{high:.2f}"


def _book_price_buckets(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    mid: float,
    bucket_size: float,
    heatmap_range: float | None = None,
) -> list[dict[str, float | str]]:
    buckets: dict[float, dict[str, float | str]] = {}
    if heatmap_range and heatmap_range > 0 and bucket_size > 0:
        start = _price_bucket_floor(mid - heatmap_range, bucket_size)
        end = _price_bucket_floor(mid + heatmap_range, bucket_size)
        current = start
        while current <= end:
            buckets[current] = {
                "price_low": current,
                "price_high": current + bucket_size,
                "label": _price_bucket_label(current, current + bucket_size),
                "bid_qty": 0.0,
                "ask_qty": 0.0,
                "bid_notional": 0.0,
                "ask_notional": 0.0,
                "distance_bps": round(abs((current + (bucket_size / 2.0)) - mid) / mid * 10000.0, 2) if mid else 0.0,
            }
            current += bucket_size

    for side_name, levels in (("bid", bids), ("ask", asks)):
        for price, qty in levels:
            low = _price_bucket_floor(price, bucket_size)
            if heatmap_range and heatmap_range > 0 and abs(price - mid) > heatmap_range:
                continue
            high = low + bucket_size
            bucket = buckets.setdefault(
                low,
                {
                    "price_low": low,
                    "price_high": high,
                    "label": _price_bucket_label(low, high),
                    "bid_qty": 0.0,
                    "ask_qty": 0.0,
                    "bid_notional": 0.0,
                    "ask_notional": 0.0,
                    "distance_bps": round(abs(((low + high) / 2.0) - mid) / mid * 10000.0, 2) if mid else 0.0,
                },
            )
            bucket[f"{side_name}_qty"] = float(bucket[f"{side_name}_qty"]) + qty
            bucket[f"{side_name}_notional"] = float(bucket[f"{side_name}_notional"]) + (price * qty)

    rows = sorted(buckets.values(), key=lambda row: float(row["price_low"]), reverse=True)
    max_notional = max((float(row["bid_notional"]) + float(row["ask_notional"]) for row in rows), default=0.0) or 1.0
    for row in rows:
        bid_notional = float(row["bid_notional"])
        ask_notional = float(row["ask_notional"])
        total = bid_notional + ask_notional
        row["strength"] = round((total / max_notional) * 100.0, 2)
        row["bias"] = round(((ask_notional - bid_notional) / total), 4) if total else 0.0
    return rows


def _volume_profile(trades: list[dict[str, Any]], bins: int = 12) -> list[dict[str, float]]:
    parsed = [(float(trade.get("price", 0)), float(trade.get("qty", 0))) for trade in trades]
    parsed = [(price, qty) for price, qty in parsed if price > 0 and qty > 0]
    if not parsed:
        return []
    low = min(price for price, _ in parsed)
    high = max(price for price, _ in parsed)
    if low == high:
        volume = sum(qty for _, qty in parsed)
        return [{"price_low": round(low, 4), "price_high": round(high, 4), "volume": round(volume, 8), "strength": 100.0}]

    width = (high - low) / bins
    buckets = [{"price_low": low + (index * width), "price_high": low + ((index + 1) * width), "volume": 0.0} for index in range(bins)]
    for price, qty in parsed:
        index = min(bins - 1, int((price - low) / width))
        buckets[index]["volume"] += qty
    max_volume = max(bucket["volume"] for bucket in buckets) or 1.0
    return [
        {
            "price_low": round(bucket["price_low"], 4),
            "price_high": round(bucket["price_high"], 4),
            "volume": round(bucket["volume"], 8),
            "strength": round((bucket["volume"] / max_volume) * 100.0, 2),
        }
        for bucket in buckets
        if bucket["volume"] > 0
    ]


def _delta_divergence(trade_prices: list[float], signed_deltas: list[float]) -> str:
    if len(trade_prices) < 6 or len(signed_deltas) < 6:
        return "none"
    midpoint = len(trade_prices) // 2
    price_change = trade_prices[-1] - trade_prices[midpoint]
    delta_change = sum(signed_deltas[midpoint:]) - sum(signed_deltas[:midpoint])
    if price_change > 0 and delta_change < 0:
        return "bearish_delta_divergence"
    if price_change < 0 and delta_change > 0:
        return "bullish_delta_divergence"
    return "none"


def _realized_trade_range_bps(prices: list[float], mid: float, window: int) -> float:
    recent = [price for price in prices[-window:] if price > 0]
    if len(recent) < 2 or mid <= 0:
        return 0.0
    return ((max(recent) - min(recent)) / mid) * 10000.0


def _trade_time_ms(trade: dict[str, Any]) -> int | None:
    value = trade.get("time") or trade.get("T") or trade.get("timestamp")
    if value is None:
        return None
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric * 1000 if numeric < 10_000_000_000 else numeric


def _m1_trade_candles(trades: list[dict[str, Any]], max_candles: int = 6) -> list[dict[str, float | int]]:
    buckets: dict[int, dict[str, float | int]] = {}
    ordered: list[tuple[int, float, float, bool]] = []
    for trade in trades:
        ts = _trade_time_ms(trade)
        if ts is None:
            continue
        price = float(trade.get("price", 0) or 0)
        qty = float(trade.get("qty", 0) or 0)
        if price <= 0 or qty <= 0:
            continue
        ordered.append((ts, price, qty, bool(trade.get("isBuyerMaker"))))

    for ts, price, qty, is_buyer_maker in sorted(ordered, key=lambda item: item[0]):
        bucket_ts = (ts // 60_000) * 60_000
        candle = buckets.setdefault(
            bucket_ts,
            {
                "open_time": bucket_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
                "delta": 0.0,
                "trade_count": 0,
            },
        )
        candle["high"] = max(float(candle["high"]), price)
        candle["low"] = min(float(candle["low"]), price)
        candle["close"] = price
        candle["volume"] = float(candle["volume"]) + qty
        candle["delta"] = float(candle["delta"]) + (-qty if is_buyer_maker else qty)
        candle["trade_count"] = int(candle["trade_count"]) + 1

    now_ms = int(time() * 1000)
    rows = sorted(buckets.values(), key=lambda candle: int(candle["open_time"]))[-max_candles:]
    for candle in rows:
        open_time = int(candle["open_time"])
        candle["is_closed"] = open_time + 60_000 <= now_ms
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close = float(candle["close"])
        candle_range = high - low
        if candle_range > 0:
            candle["lower_wick_pct"] = round((min(open_price, close) - low) / candle_range, 4)
            candle["upper_wick_pct"] = round((high - max(open_price, close)) / candle_range, 4)
            candle["close_position"] = round((close - low) / candle_range, 4)
        else:
            candle["lower_wick_pct"] = 0.0
            candle["upper_wick_pct"] = 0.0
            candle["close_position"] = 0.5
        candle["body_pct"] = round(abs(close - open_price) / candle_range, 4) if candle_range > 0 else 0.0
    return rows


def compute_features(
    symbol: str,
    depth: dict[str, Any],
    trades: list[dict[str, Any]],
    levels: int = 20,
    volatility_window: int = 80,
    price_bucket_size: float = 100.0,
    heatmap_range: float | None = None,
) -> dict[str, Any]:
    bids = _float_levels(depth.get("bids", []), levels)
    asks = _float_levels(depth.get("asks", []), levels)
    if not bids or not asks:
        raise ValueError(f"{symbol} has empty book.")

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    spread_bps = (spread / mid) * 10000 if mid else 0

    bid_depth = sum(price * qty for price, qty in bids)
    ask_depth = sum(price * qty for price, qty in asks)
    depth_total = bid_depth + ask_depth
    book_imbalance = ((bid_depth - ask_depth) / depth_total) if depth_total else 0

    buy_volume = 0.0
    sell_volume = 0.0
    trade_prices = []
    signed_deltas = []
    vwap_notional = 0.0
    vwap_volume = 0.0
    for trade in trades:
        price = float(trade.get("price", 0))
        qty = float(trade.get("qty", 0))
        if price <= 0 or qty <= 0:
            continue
        trade_prices.append(price)
        vwap_notional += price * qty
        vwap_volume += qty
        if trade.get("isBuyerMaker"):
            sell_volume += qty
            signed_deltas.append(-qty)
        else:
            buy_volume += qty
            signed_deltas.append(qty)
    flow_total = buy_volume + sell_volume
    cumulative_delta = buy_volume - sell_volume
    flow_imbalance = ((buy_volume - sell_volume) / flow_total) if flow_total else 0
    last_trade = trade_prices[-1] if trade_prices else mid
    trade_drift_bps = ((last_trade - mean(trade_prices)) / mid) * 10000 if trade_prices and mid else 0
    vwap = (vwap_notional / vwap_volume) if vwap_volume else mid
    vwap_distance_bps = ((mid - vwap) / mid) * 10000 if mid else 0
    realized_range_bps = _realized_trade_range_bps(trade_prices, mid, min(volatility_window, len(trade_prices)))
    recent_high = max(trade_prices) if trade_prices else best_ask
    recent_low = min(trade_prices) if trade_prices else best_bid
    strongest_bid = max(bids, key=lambda level: level[1])[0]
    strongest_ask = max(asks, key=lambda level: level[1])[0]
    bid_wall_qty = max(qty for _, qty in bids)
    ask_wall_qty = max(qty for _, qty in asks)
    max_book_qty = max(bid_wall_qty, ask_wall_qty)
    bid_ladder = _ladder(bids, max_book_qty, mid)
    ask_ladder = _ladder(asks, max_book_qty, mid)
    far_liquidity_bands = _far_liquidity_bands(bids, asks, mid)
    book_price_buckets = _book_price_buckets(bids, asks, mid, price_bucket_size, heatmap_range)
    bid_wall_notional = strongest_bid * bid_wall_qty
    ask_wall_notional = strongest_ask * ask_wall_qty
    magnet_raw = ask_wall_notional - bid_wall_notional
    magnet_direction = "up" if magnet_raw > 0 else "down" if magnet_raw < 0 else "neutral"
    magnet_target = strongest_ask if magnet_direction == "up" else strongest_bid if magnet_direction == "down" else mid
    liquidity_total = bid_wall_notional + ask_wall_notional
    liquidity_bias = abs(magnet_raw) / liquidity_total if liquidity_total else 0.0
    delta_alignment = 1.0
    if magnet_direction == "up" and flow_imbalance < 0:
        delta_alignment = 0.45
    elif magnet_direction == "down" and flow_imbalance > 0:
        delta_alignment = 0.45
    elif magnet_direction == "neutral":
        delta_alignment = 0.25
    magnet_confidence = round(min(100.0, (liquidity_bias * 100.0 * 0.65) + (abs(flow_imbalance) * 100.0 * 0.35)) * delta_alignment, 2)
    divergence = _delta_divergence(trade_prices, signed_deltas)
    profile = _volume_profile(trades)
    poc = max(profile, key=lambda bucket: bucket["volume"]) if profile else None
    m1_candles = _m1_trade_candles(trades)

    return {
        "symbol": symbol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "spread_bps": spread_bps,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "book_imbalance": book_imbalance,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "delta": cumulative_delta,
        "cumulative_delta": cumulative_delta,
        "delta_imbalance": flow_imbalance,
        "flow_imbalance": flow_imbalance,
        "trade_drift_bps": trade_drift_bps,
        "recent_vwap": vwap,
        "recent_vwap_distance_bps": vwap_distance_bps,
        "vwap": vwap,
        "vwap_distance_bps": vwap_distance_bps,
        "realized_range_bps": realized_range_bps,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "strongest_bid": strongest_bid,
        "strongest_ask": strongest_ask,
        "bid_wall_qty": bid_wall_qty,
        "ask_wall_qty": ask_wall_qty,
        "bid_ladder": bid_ladder,
        "ask_ladder": ask_ladder,
        "far_liquidity_bands": far_liquidity_bands,
        "book_price_buckets": book_price_buckets,
        "price_bucket_size": price_bucket_size,
        "heatmap_range": heatmap_range,
        "volume_profile": profile,
        "volume_poc": poc,
        "trade_m1_candles": m1_candles,
        "liquidity_magnet": {
            "direction": magnet_direction,
            "target": round(magnet_target, 4),
            "confidence": magnet_confidence,
            "reason": (
                f"{magnet_direction} magnet from strongest ask {strongest_ask:.4f}/{ask_wall_qty:.4f} "
                f"vs strongest bid {strongest_bid:.4f}/{bid_wall_qty:.4f}; delta imbalance {flow_imbalance:.3f}."
            ),
        },
        "delta_divergence": divergence,
    }


def score_liquidity_setup(features: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    symbol = features["symbol"]
    strategy_cfg = config.get("strategy", {})
    max_spread = strategy_cfg.get("max_spread_bps", {}).get(symbol, 6.0)
    imbalance_threshold = float(strategy_cfg.get("imbalance_threshold", 0.18))
    flow_threshold = float(strategy_cfg.get("flow_threshold", 0.12))
    tp_rr = float(strategy_cfg.get("tp_rr", 2.5))
    stop_bps = float(strategy_cfg.get("stop_bps", {}).get(symbol, 12.0))
    entry_tolerance_bps = float(strategy_cfg.get("entry_tolerance_bps", {}).get(symbol, 2.0))
    sl_buffer_bps = float(strategy_cfg.get("sl_buffer_bps", {}).get(symbol, 3.0))
    min_stop_bps = float(strategy_cfg.get("min_structural_stop_bps", {}).get(symbol, 4.0))
    max_stop_bps = float(strategy_cfg.get("max_structural_stop_bps", {}).get(symbol, 20.0))
    volatility_window = int(strategy_cfg.get("volatility_window_trades", 80))
    volatility_mult = float(strategy_cfg.get("min_stop_volatility_mult", 1.5))
    volatility_stop_bps = float(features.get("realized_range_bps", 0.0)) * volatility_mult
    effective_min_stop_bps = max(min_stop_bps, volatility_stop_bps)

    spread_bps = abs(features["spread_bps"])
    if spread_bps > max_spread:
        return None

    book_imbalance = float(features["book_imbalance"])
    flow_imbalance = float(features["flow_imbalance"])
    aligned_long = book_imbalance > imbalance_threshold and flow_imbalance > flow_threshold
    aligned_short = book_imbalance < -imbalance_threshold and flow_imbalance < -flow_threshold
    reversal_long = book_imbalance > imbalance_threshold and flow_imbalance < -flow_threshold
    reversal_short = book_imbalance < -imbalance_threshold and flow_imbalance > flow_threshold

    if aligned_long:
        side = "BUY"
        setup_type = "liquidity_void_continuation"
    elif aligned_short:
        side = "SELL"
        setup_type = "liquidity_void_continuation"
    elif reversal_long:
        side = "BUY"
        setup_type = "liquidity_sweep_reversal"
    elif reversal_short:
        side = "SELL"
        setup_type = "liquidity_sweep_reversal"
    else:
        return None

    entry = float(features["mid"])
    entry_tolerance = entry * (entry_tolerance_bps / 10000.0)
    sl_buffer = entry * (sl_buffer_bps / 10000.0)
    fallback_stop_distance = entry * (stop_bps / 10000.0)
    if side == "BUY":
        protected_extreme = min(float(features["recent_low"]), float(features["strongest_bid"]))
        stop_loss = min(protected_extreme - sl_buffer, entry - fallback_stop_distance)
        stop_distance = entry - stop_loss
        structural_target = max(float(features["recent_high"]), float(features["strongest_ask"]))
        rr_target = entry + (stop_distance * tp_rr)
        take_profit = max(structural_target, rr_target)
        entry_zone_low = entry - entry_tolerance
        entry_zone_high = entry + entry_tolerance
        entry_reason = (
            "Buy only inside the retest zone after bid-side liquidity supports price "
            "and taker flow confirms the reclaim/continuation."
        )
        stop_reason = (
            f"SL is below recent low {features['recent_low']:.4f} and strongest bid liquidity "
            f"{features['strongest_bid']:.4f}, plus buffer, so it is not sitting directly on the sweep level."
        )
        tp_reason = (
            f"TP targets opposing liquidity above price near {structural_target:.4f} or minimum "
            f"{tp_rr:.2f}R, whichever is farther."
        )
    else:
        protected_extreme = max(float(features["recent_high"]), float(features["strongest_ask"]))
        stop_loss = max(protected_extreme + sl_buffer, entry + fallback_stop_distance)
        stop_distance = stop_loss - entry
        structural_target = min(float(features["recent_low"]), float(features["strongest_bid"]))
        rr_target = entry - (stop_distance * tp_rr)
        take_profit = min(structural_target, rr_target)
        entry_zone_low = entry - entry_tolerance
        entry_zone_high = entry + entry_tolerance
        entry_reason = (
            "Sell only inside the retest zone after ask-side liquidity caps price "
            "and taker flow confirms the rejection/continuation."
        )
        stop_reason = (
            f"SL is above recent high {features['recent_high']:.4f} and strongest ask liquidity "
            f"{features['strongest_ask']:.4f}, plus buffer, so it is not sitting directly on the sweep level."
        )
        tp_reason = (
            f"TP targets opposing liquidity below price near {structural_target:.4f} or minimum "
            f"{tp_rr:.2f}R, whichever is farther."
        )

    stop_bps_actual = (stop_distance / entry) * 10000 if entry else 0
    tolerance = 1e-9
    if stop_bps_actual + tolerance < effective_min_stop_bps:
        return None
    if stop_bps_actual - tolerance > max_stop_bps:
        return None

    reward_distance = abs(take_profit - entry)
    rr = reward_distance / stop_distance if stop_distance else 0
    if rr + tolerance < tp_rr:
        return None

    liquidity_score = min(100.0, 100.0 * abs(book_imbalance) / max(imbalance_threshold, 0.001))
    flow_score = min(100.0, 100.0 * abs(flow_imbalance) / max(flow_threshold, 0.001))
    spread_score = max(0.0, 100.0 * (1.0 - (spread_bps / max_spread)))
    regime_score = 70.0
    oi_support = str(features.get("open_interest_support", "neutral"))
    oi_signal = str(features.get("open_interest_signal", "unknown"))
    if side == "BUY":
        oi_alignment = "supporting" if oi_support.startswith("supports_up") else "diverging" if oi_support.startswith("supports_down") else "neutral"
    else:
        oi_alignment = "supporting" if oi_support.startswith("supports_down") else "diverging" if oi_support.startswith("supports_up") else "neutral"
    oi_adjustment = 5.0 if oi_alignment == "supporting" else -8.0 if oi_alignment == "diverging" else 0.0
    total_score = round(
        max(0.0, min(100.0, (liquidity_score * 0.35) + (flow_score * 0.35) + (spread_score * 0.15) + (regime_score * 0.15) + oi_adjustment)),
        2,
    )

    return {
        "symbol": symbol,
        "side": side,
        "strategy": setup_type,
        "entry": round(entry, 4),
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "rr": round(rr, 2),
        "score": total_score,
        "liquidity_score": round(liquidity_score, 2),
        "flow_score": round(flow_score, 2),
        "regime_score": regime_score,
        "entry_zone_low": round(entry_zone_low, 4),
        "entry_zone_high": round(entry_zone_high, 4),
        "stop_distance_bps": round(stop_bps_actual, 2),
        "min_required_stop_bps": round(effective_min_stop_bps, 2),
        "entry_reason": entry_reason,
        "stop_reason": stop_reason,
        "tp_reason": tp_reason,
        "open_interest_signal": oi_signal,
        "open_interest_alignment": oi_alignment,
        "invalidation": "Setup invalid if price leaves the entry zone before confirmation or liquidity/flow flips.",
        "reason": (
            f"{setup_type}: book_imbalance={book_imbalance:.3f}, flow_imbalance={flow_imbalance:.3f}, "
            f"spread_bps={spread_bps:.2f}, stop_bps={stop_bps_actual:.2f}, "
            f"required_stop_bps={effective_min_stop_bps:.2f} using recent volatility window {volatility_window}. "
            f"OI={oi_signal}/{oi_alignment}. "
            f"{entry_reason} {stop_reason} {tp_reason}"
        ),
    }
