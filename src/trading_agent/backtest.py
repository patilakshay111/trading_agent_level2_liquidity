from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float


def candle_from_kline(kline: list[Any]) -> Candle:
    return Candle(
        open_time=int(kline[0]),
        open=float(kline[1]),
        high=float(kline[2]),
        low=float(kline[3]),
        close=float(kline[4]),
    )


def candles_from_klines(klines: list[list[Any]]) -> list[Candle]:
    candles = []
    for kline in klines:
        try:
            candle = candle_from_kline(kline)
        except (TypeError, ValueError, IndexError):
            continue
        if candle.high > candle.low and candle.open > 0 and candle.close > 0:
            candles.append(candle)
    return candles


def _wick_stats(candle: Candle) -> dict[str, float]:
    candle_range = candle.high - candle.low
    if candle_range <= 0:
        return {"upper_wick_pct": 0.0, "lower_wick_pct": 0.0, "close_position": 0.5}
    body_high = max(candle.open, candle.close)
    body_low = min(candle.open, candle.close)
    return {
        "upper_wick_pct": max(0.0, (candle.high - body_high) / candle_range),
        "lower_wick_pct": max(0.0, (body_low - candle.low) / candle_range),
        "close_position": (candle.close - candle.low) / candle_range,
    }


def _equal_extreme(current: float, previous: list[float], price: float, tolerance_bps: float) -> bool:
    tolerance = abs(price) * (tolerance_bps / 10000.0)
    return any(abs(current - level) <= tolerance for level in previous)


def _pivot_extremes(candles: list[Candle], kind: str, span: int = 2) -> list[dict[str, Any]]:
    extremes: list[dict[str, Any]] = []
    if len(candles) < (span * 2) + 1:
        return extremes
    for index in range(span, len(candles) - span):
        window = candles[index - span : index + span + 1]
        candle = candles[index]
        if kind == "support" and candle.low <= min(item.low for item in window):
            extremes.append({"price": candle.low, "time": candle.open_time})
        if kind == "resistance" and candle.high >= max(item.high for item in window):
            extremes.append({"price": candle.high, "time": candle.open_time})
    return extremes


def _zones_from_extremes(
    extremes: list[dict[str, Any]],
    *,
    kind: str,
    price: float,
    tolerance_bps: float,
    min_touches: int,
) -> list[dict[str, Any]]:
    tolerance = abs(price) * (tolerance_bps / 10000.0)
    zones: list[dict[str, Any]] = []
    for extreme in sorted(extremes, key=lambda item: float(item["price"])):
        level = float(extreme["price"])
        match = None
        for zone in zones:
            if abs(level - float(zone["center"])) <= tolerance:
                match = zone
                break
        if match is None:
            zones.append(
                {
                    "kind": kind,
                    "low": level,
                    "high": level,
                    "center": level,
                    "touches": 1,
                    "first_seen": extreme["time"],
                    "last_seen": extreme["time"],
                }
            )
            continue
        match["low"] = min(float(match["low"]), level)
        match["high"] = max(float(match["high"]), level)
        match["touches"] = int(match["touches"]) + 1
        match["last_seen"] = extreme["time"]
        match["center"] = (float(match["low"]) + float(match["high"])) / 2.0

    return [zone for zone in zones if int(zone["touches"]) >= min_touches]


def _nearest_zone(candle: Candle, zones: list[dict[str, Any]], side: str, tolerance_bps: float) -> dict[str, Any] | None:
    tolerance = abs(candle.close) * (tolerance_bps / 10000.0)
    if side == "BUY":
        touched = [
            zone for zone in zones
            if zone["kind"] == "support"
            and candle.low <= float(zone["high"]) + tolerance
            and candle.close >= float(zone["high"])
        ]
        return max(touched, key=lambda zone: float(zone["center"]), default=None)
    touched = [
        zone for zone in zones
        if zone["kind"] == "resistance"
        and candle.high >= float(zone["low"]) - tolerance
        and candle.close <= float(zone["low"])
    ]
    return min(touched, key=lambda zone: float(zone["center"]), default=None)


def _simulate_exit(
    side: str,
    candles: list[Candle],
    start_index: int,
    entry: float,
    stop: float,
    target: float,
    max_hold_candles: int,
    *,
    trade_management: dict[str, Any] | None = None,
) -> dict[str, Any]:
    risk = abs(entry - stop)
    current_stop = stop
    trailing_events = 0
    max_favorable_r = 0.0
    end_index = min(len(candles) - 1, start_index + max(1, max_hold_candles) - 1)
    management = trade_management or {}
    management_enabled = bool(management.get("enabled", True))
    breakeven_trigger = float(management.get("breakeven_trigger_r", 1.0))
    breakeven_lock = float(management.get("breakeven_lock_r", 0.10))
    trail_trigger = float(management.get("trail_trigger_r", 1.5))
    trail_distance = float(management.get("trail_distance_r", 0.75))
    min_update_bps = float(management.get("min_stop_update_bps", 1.0))

    for index in range(start_index, end_index + 1):
        candle = candles[index]
        if side == "SELL":
            stop_hit = candle.high >= current_stop
            target_hit = candle.low <= target
        else:
            stop_hit = candle.low <= current_stop
            target_hit = candle.high >= target
        if stop_hit and target_hit:
            r_value = _r_value(side, entry, current_stop, risk)
            return {
                "exit_index": index,
                "exit_price": current_stop,
                "outcome": _outcome_from_r(r_value),
                "exit_reason": "STOP_LOSS",
                "r": r_value,
                "final_stop_loss": current_stop,
                "trailing_events": trailing_events,
                "max_favorable_r": max_favorable_r,
            }
        if stop_hit:
            r_value = _r_value(side, entry, current_stop, risk)
            return {
                "exit_index": index,
                "exit_price": current_stop,
                "outcome": _outcome_from_r(r_value),
                "exit_reason": "TRAIL_STOP" if trailing_events and r_value > -1.0 else "STOP_LOSS",
                "r": r_value,
                "final_stop_loss": current_stop,
                "trailing_events": trailing_events,
                "max_favorable_r": max_favorable_r,
            }
        if target_hit:
            reward = abs(target - entry)
            return {
                "exit_index": index,
                "exit_price": target,
                "outcome": "WIN",
                "exit_reason": "TAKE_PROFIT",
                "r": reward / risk if risk else 0.0,
                "final_stop_loss": current_stop,
                "trailing_events": trailing_events,
                "max_favorable_r": max_favorable_r,
            }

        if management_enabled and risk > 0:
            favorable_price = candle.high if side == "BUY" else candle.low
            favorable = (favorable_price - entry) if side == "BUY" else (entry - favorable_price)
            current_r = favorable / risk
            max_favorable_r = max(max_favorable_r, current_r)
            desired_stop = None
            if current_r >= breakeven_trigger:
                desired_stop = entry + (risk * breakeven_lock) if side == "BUY" else entry - (risk * breakeven_lock)
            if current_r >= trail_trigger:
                trail_stop = favorable_price - (risk * trail_distance) if side == "BUY" else favorable_price + (risk * trail_distance)
                if desired_stop is None or (side == "BUY" and trail_stop > desired_stop) or (side == "SELL" and trail_stop < desired_stop):
                    desired_stop = trail_stop

            if desired_stop is not None:
                improves = desired_stop > current_stop if side == "BUY" else desired_stop < current_stop
                meaningful = abs(desired_stop - current_stop) >= entry * (min_update_bps / 10000.0)
                if improves and meaningful:
                    current_stop = desired_stop
                    trailing_events += 1
                    trailed_stop_hit = candle.low <= current_stop if side == "BUY" else candle.high >= current_stop
                    if trailed_stop_hit:
                        r_value = _r_value(side, entry, current_stop, risk)
                        return {
                            "exit_index": index,
                            "exit_price": current_stop,
                            "outcome": _outcome_from_r(r_value),
                            "exit_reason": "TRAIL_STOP",
                            "r": r_value,
                            "final_stop_loss": current_stop,
                            "trailing_events": trailing_events,
                            "max_favorable_r": max_favorable_r,
                        }

    exit_price = candles[end_index].close
    r_value = _r_value(side, entry, exit_price, risk)
    return {
        "exit_index": end_index,
        "exit_price": exit_price,
        "outcome": "TIME",
        "exit_reason": "MAX_HOLD",
        "r": r_value,
        "final_stop_loss": current_stop,
        "trailing_events": trailing_events,
        "max_favorable_r": max_favorable_r,
    }


def _r_value(side: str, entry: float, exit_price: float, risk: float) -> float:
    if risk <= 0:
        return 0.0
    if side == "SELL":
        return (entry - exit_price) / risk
    return (exit_price - entry) / risk


def _outcome_from_r(r_value: float) -> str:
    if r_value > 0:
        return "WIN"
    if r_value < 0:
        return "LOSS"
    return "BREAKEVEN"


def _pnl_usd(side: str, entry: float, exit_price: float, lot_size: float, contract_spec: dict[str, Any] | None) -> float:
    favorable_move = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    spec = contract_spec or {}
    tick_size = float(spec.get("trade_tick_size") or spec.get("tick_size") or 0.0)
    tick_value = float(
        spec.get("trade_tick_value")
        or spec.get("tick_value")
        or spec.get("trade_tick_value_profit")
        or spec.get("tick_value_profit")
        or spec.get("trade_tick_value_loss")
        or spec.get("tick_value_loss")
        or 0.0
    )
    if tick_size > 0 and tick_value > 0:
        return (favorable_move / tick_size) * tick_value * lot_size
    contract_size = float(spec.get("trade_contract_size") or spec.get("contract_size") or 1.0)
    return favorable_move * contract_size * lot_size


def backtest_equal_extreme_rejection(
    candles: list[Candle],
    *,
    side: str = "BOTH",
    rr: float = 2.0,
    lookback_candles: int = 20,
    equal_tolerance_bps: float = 3.0,
    min_zone_touches: int = 2,
    pivot_span: int = 2,
    min_wick_pct: float = 0.45,
    min_close_reclaim_pct: float = 0.60,
    stop_buffer_bps: float = 1.0,
    max_hold_candles: int = 120,
    trade_management: dict[str, Any] | None = None,
    lot_size: float = 0.01,
    contract_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed_sides = {"BUY", "SELL"} if side == "BOTH" else {side.upper()}
    trades: list[dict[str, Any]] = []
    index = max(1, lookback_candles, (pivot_span * 2) + 1)
    while index < len(candles) - 1:
        candle = candles[index]
        stats = _wick_stats(candle)
        previous = candles[max(0, index - lookback_candles) : index]
        support_zones = _zones_from_extremes(
            _pivot_extremes(previous, "support", pivot_span),
            kind="support",
            price=candle.close,
            tolerance_bps=equal_tolerance_bps,
            min_touches=min_zone_touches,
        )
        resistance_zones = _zones_from_extremes(
            _pivot_extremes(previous, "resistance", pivot_span),
            kind="resistance",
            price=candle.close,
            tolerance_bps=equal_tolerance_bps,
            min_touches=min_zone_touches,
        )
        zones = [*support_zones, *resistance_zones]
        signal_side = None
        reason = ""
        zone = None

        if "SELL" in allowed_sides:
            zone = _nearest_zone(candle, zones, "SELL", equal_tolerance_bps)
            rejected = stats["upper_wick_pct"] >= min_wick_pct and stats["close_position"] <= 1.0 - min_close_reclaim_pct
            if zone and rejected:
                signal_side = "SELL"
                reason = (
                    f"Resistance box {float(zone['low']):.4f}-{float(zone['high']):.4f} rejected "
                    f"after {int(zone['touches'])} touches: upper wick {stats['upper_wick_pct']:.2f}, "
                    f"close position {stats['close_position']:.2f}."
                )

        if signal_side is None and "BUY" in allowed_sides:
            zone = _nearest_zone(candle, zones, "BUY", equal_tolerance_bps)
            rejected = stats["lower_wick_pct"] >= min_wick_pct and stats["close_position"] >= min_close_reclaim_pct
            if zone and rejected:
                signal_side = "BUY"
                reason = (
                    f"Support box {float(zone['low']):.4f}-{float(zone['high']):.4f} rejected "
                    f"after {int(zone['touches'])} touches: lower wick {stats['lower_wick_pct']:.2f}, "
                    f"close position {stats['close_position']:.2f}."
                )

        if signal_side is None or zone is None:
            index += 1
            continue

        entry = candle.close
        buffer_value = abs(entry) * (stop_buffer_bps / 10000.0)
        if signal_side == "SELL":
            stop = max(candle.high, float(zone["high"])) + buffer_value
            risk = stop - entry
            target = entry - (risk * rr)
        else:
            stop = min(candle.low, float(zone["low"])) - buffer_value
            risk = entry - stop
            target = entry + (risk * rr)
        if risk <= 0:
            index += 1
            continue

        exit_result = _simulate_exit(
            signal_side,
            candles,
            index + 1,
            entry,
            stop,
            target,
            max_hold_candles,
            trade_management=trade_management,
        )
        exit_index = int(exit_result["exit_index"])
        exit_price = float(exit_result["exit_price"])
        pnl_usd = _pnl_usd(signal_side, entry, exit_price, lot_size, contract_spec)
        trades.append(
            {
                "entry_time": datetime.fromtimestamp(candle.open_time / 1000, timezone.utc).isoformat(timespec="minutes"),
                "exit_time": datetime.fromtimestamp(candles[exit_index].open_time / 1000, timezone.utc).isoformat(timespec="minutes"),
                "side": signal_side,
                "entry": round(entry, 4),
                "stop_loss": round(stop, 4),
                "final_stop_loss": round(float(exit_result["final_stop_loss"]), 4),
                "take_profit": round(target, 4),
                "exit_price": round(exit_price, 4),
                "outcome": exit_result["outcome"],
                "exit_reason": exit_result["exit_reason"],
                "r": round(float(exit_result["r"]), 4),
                "lot_size": round(lot_size, 4),
                "pnl_usd": round(pnl_usd, 2),
                "holding_minutes": max(1, exit_index - index),
                "trailing_events": int(exit_result["trailing_events"]),
                "max_favorable_r": round(float(exit_result["max_favorable_r"]), 4),
                "zone": f"{float(zone['low']):.4f}-{float(zone['high']):.4f}",
                "zone_touches": int(zone["touches"]),
                "reason": reason,
            }
        )
        index = exit_index + 1

    wins = sum(1 for trade in trades if trade["outcome"] == "WIN")
    losses = sum(1 for trade in trades if trade["outcome"] == "LOSS")
    total_r = sum(float(trade["r"]) for trade in trades)
    total_pnl_usd = sum(float(trade["pnl_usd"]) for trade in trades)
    gross_win_usd = sum(float(trade["pnl_usd"]) for trade in trades if float(trade["pnl_usd"]) > 0)
    gross_loss_usd = abs(sum(float(trade["pnl_usd"]) for trade in trades if float(trade["pnl_usd"]) < 0))
    gross_win_r = sum(float(trade["r"]) for trade in trades if float(trade["r"]) > 0)
    gross_loss_r = abs(sum(float(trade["r"]) for trade in trades if float(trade["r"]) < 0))
    equity = 0.0
    peak = 0.0
    max_drawdown_r = 0.0
    equity_usd = 0.0
    peak_usd = 0.0
    max_drawdown_usd = 0.0
    for trade in trades:
        equity += float(trade["r"])
        peak = max(peak, equity)
        max_drawdown_r = max(max_drawdown_r, peak - equity)
        equity_usd += float(trade["pnl_usd"])
        peak_usd = max(peak_usd, equity_usd)
        max_drawdown_usd = max(max_drawdown_usd, peak_usd - equity_usd)

    return {
        "ok": True,
        "summary": {
            "candles": len(candles),
            "lot_size": round(lot_size, 4),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "breakevens": sum(1 for trade in trades if trade["outcome"] == "BREAKEVEN"),
            "timeouts": sum(1 for trade in trades if trade["outcome"] == "TIME"),
            "trail_stops": sum(1 for trade in trades if trade.get("exit_reason") == "TRAIL_STOP"),
            "management_updates": sum(int(trade.get("trailing_events") or 0) for trade in trades),
            "win_rate": round((wins / (wins + losses)) * 100.0, 2) if wins + losses else 0.0,
            "total_r": round(total_r, 4),
            "average_r": round(total_r / len(trades), 4) if trades else 0.0,
            "profit_factor": round(gross_win_r / gross_loss_r, 3) if gross_loss_r > 0 else None,
            "total_pnl_usd": round(total_pnl_usd, 2),
            "average_pnl_usd": round(total_pnl_usd / len(trades), 2) if trades else 0.0,
            "profit_factor_usd": round(gross_win_usd / gross_loss_usd, 3) if gross_loss_usd > 0 else None,
            "max_drawdown_r": round(max_drawdown_r, 4),
            "max_drawdown_usd": round(max_drawdown_usd, 2),
        },
        "trades": trades,
    }
