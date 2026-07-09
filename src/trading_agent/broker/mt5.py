from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor
from typing import Any


@dataclass
class LotSizingResult:
    lots: float
    raw_lots: float
    risk_cash: float
    loss_per_lot: float
    valid: bool
    reason: str


@dataclass
class ExecutionResult:
    ok: bool
    order_id: int | None
    deal_id: int | None
    retcode: int | None
    price: float | None
    volume: float | None
    message: str
    raw: dict[str, Any] | None = None


def _import_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        return None
    return mt5


def mt5_available() -> bool:
    return _import_mt5() is not None


def scan_symbol(symbol: str) -> dict[str, Any]:
    mt5 = _import_mt5()
    if mt5 is None:
        return {"symbol": symbol, "available": False, "error": "MetaTrader5 Python package is not installed."}

    if not mt5.initialize():
        return {"symbol": symbol, "available": False, "error": f"MT5 initialize failed: {mt5.last_error()}"}

    try:
        if not mt5.symbol_select(symbol, True):
            return {"symbol": symbol, "available": False, "error": f"MT5 symbol_select failed: {mt5.last_error()}"}
        info = mt5.symbol_info(symbol)
        if info is None:
            return {"symbol": symbol, "available": False, "error": f"MT5 symbol_info failed: {mt5.last_error()}"}
        data = info._asdict()
        data["available"] = True
        return data
    finally:
        mt5.shutdown()


def symbol_tick(symbol: str) -> tuple[bool, dict[str, Any], str]:
    mt5 = _import_mt5()
    if mt5 is None:
        return False, {}, "MetaTrader5 Python package is not installed."
    if not mt5.initialize():
        return False, {}, f"MT5 initialize failed: {mt5.last_error()}"

    try:
        if not mt5.symbol_select(symbol, True):
            return False, {}, f"MT5 symbol_select failed: {mt5.last_error()}"
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, {}, f"MT5 symbol_info_tick failed: {mt5.last_error()}"
        data = tick._asdict()
        return True, data, "OK"
    finally:
        mt5.shutdown()


def normalize_volume(raw_lots: float, volume_min: float, volume_max: float, volume_step: float) -> float:
    if volume_step <= 0:
        return 0.0
    stepped = floor(raw_lots / volume_step) * volume_step
    clipped = max(volume_min, min(volume_max, stepped))
    decimals = max(0, len(str(volume_step).split(".")[-1].rstrip("0")))
    return round(clipped, decimals)


def normalize_price(price: float, spec: dict[str, Any]) -> float:
    tick_size = float(spec.get("trade_tick_size") or spec.get("tick_size") or spec.get("point") or 0)
    digits_raw = spec.get("digits")
    digits = int(digits_raw) if digits_raw is not None else None
    normalized = float(price)
    if tick_size > 0:
        normalized = round(normalized / tick_size) * tick_size
    if digits is not None and digits >= 0:
        normalized = round(normalized, digits)
    return normalized


def supported_filling_types(mt5: Any, spec: dict[str, Any]) -> list[int]:
    filling_mode = int(spec.get("filling_mode") or 0)
    modes: list[int] = []
    if filling_mode & 1:
        modes.append(mt5.ORDER_FILLING_FOK)
    if filling_mode & 2:
        modes.append(mt5.ORDER_FILLING_IOC)
    if filling_mode & 4 and hasattr(mt5, "ORDER_FILLING_BOC"):
        modes.append(mt5.ORDER_FILLING_BOC)
    if modes:
        return modes
    fallback = [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC]
    if hasattr(mt5, "ORDER_FILLING_RETURN"):
        fallback.append(mt5.ORDER_FILLING_RETURN)
    return fallback


def calculate_lot_size(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    spec: dict[str, Any],
) -> LotSizingResult:
    tick_size = float(spec.get("trade_tick_size") or spec.get("tick_size") or 0)
    tick_value_loss = float(
        spec.get("trade_tick_value_loss")
        or spec.get("tick_value_loss")
        or spec.get("trade_tick_value")
        or spec.get("tick_value")
        or 0
    )
    volume_min = float(spec.get("volume_min") or 0)
    volume_max = float(spec.get("volume_max") or 0)
    volume_step = float(spec.get("volume_step") or 0)
    stop_distance = abs(float(entry) - float(stop_loss))
    risk_cash = float(equity) * (float(risk_pct) / 100.0)

    if tick_size <= 0 or tick_value_loss <= 0:
        return LotSizingResult(0, 0, risk_cash, 0, False, "Missing MT5 tick size or tick value.")
    if stop_distance <= 0:
        return LotSizingResult(0, 0, risk_cash, 0, False, "Stop distance is zero.")
    if volume_min <= 0 or volume_max <= 0 or volume_step <= 0:
        return LotSizingResult(0, 0, risk_cash, 0, False, "Missing MT5 volume constraints.")

    loss_per_lot = (stop_distance / tick_size) * tick_value_loss
    if loss_per_lot <= 0:
        return LotSizingResult(0, 0, risk_cash, loss_per_lot, False, "Loss per lot is not positive.")

    raw_lots = risk_cash / loss_per_lot
    lots = normalize_volume(raw_lots, volume_min, volume_max, volume_step)
    if lots < volume_min:
        return LotSizingResult(lots, raw_lots, risk_cash, loss_per_lot, False, "Calculated lot size below minimum.")
    return LotSizingResult(lots, raw_lots, risk_cash, loss_per_lot, True, "OK")


def account_equity() -> tuple[bool, float, str]:
    mt5 = _import_mt5()
    if mt5 is None:
        return False, 0.0, "MetaTrader5 Python package is not installed."
    if not mt5.initialize():
        return False, 0.0, f"MT5 initialize failed: {mt5.last_error()}"
    try:
        info = mt5.account_info()
        if info is None:
            return False, 0.0, f"MT5 account_info failed: {mt5.last_error()}"
        return True, float(info.equity), "OK"
    finally:
        mt5.shutdown()


def send_market_order(
    *,
    symbol: str,
    side: str,
    lots: float,
    stop_loss: float,
    take_profit: float,
    deviation_points: int,
    magic_number: int,
    comment: str,
) -> ExecutionResult:
    mt5 = _import_mt5()
    if mt5 is None:
        return ExecutionResult(False, None, None, None, None, lots, "MetaTrader5 Python package is not installed.")
    if not mt5.initialize():
        return ExecutionResult(False, None, None, None, None, lots, f"MT5 initialize failed: {mt5.last_error()}")

    try:
        if not mt5.symbol_select(symbol, True):
            return ExecutionResult(False, None, None, None, None, lots, f"MT5 symbol_select failed: {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return ExecutionResult(False, None, None, None, None, lots, f"MT5 symbol_info_tick failed: {mt5.last_error()}")
        info = mt5.symbol_info(symbol)
        spec = info._asdict() if info is not None else {}

        side_upper = side.upper()
        if side_upper == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = normalize_price(float(tick.ask), spec)
        elif side_upper == "SELL":
            order_type = mt5.ORDER_TYPE_SELL
            price = normalize_price(float(tick.bid), spec)
        else:
            return ExecutionResult(False, None, None, None, None, lots, f"Unsupported side: {side}")
        sl = normalize_price(float(stop_loss), spec)
        tp = normalize_price(float(take_profit), spec)

        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": int(deviation_points),
            "magic": int(magic_number),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        invalid_fill_messages = []
        for filling_type in supported_filling_types(mt5, spec):
            request = {**base_request, "type_filling": filling_type}
            result = mt5.order_send(request)
            if result is None:
                return ExecutionResult(False, None, None, None, price, lots, f"MT5 order_send returned None: {mt5.last_error()}", {"request": request})

            raw = result._asdict()
            raw["request"] = request
            ok = result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                return ExecutionResult(
                    ok=True,
                    order_id=int(getattr(result, "order", 0) or 0) or None,
                    deal_id=int(getattr(result, "deal", 0) or 0) or None,
                    retcode=int(getattr(result, "retcode", 0) or 0),
                    price=float(getattr(result, "price", price) or price),
                    volume=float(getattr(result, "volume", lots) or lots),
                    message=f"OK filling={filling_type}",
                    raw=raw,
                )
            message = f"MT5 rejected order: retcode={result.retcode}, comment={getattr(result, 'comment', '')}, filling={filling_type}"
            if result.retcode == getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
                invalid_fill_messages.append(message)
                continue
            return ExecutionResult(False, None, None, int(getattr(result, "retcode", 0) or 0), price, lots, message, raw)

        return ExecutionResult(
            False,
            None,
            None,
            getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030),
            price,
            lots,
            "; ".join(invalid_fill_messages) or "MT5 rejected all supported filling modes.",
            {"request": base_request, "filling_mode": spec.get("filling_mode")},
        )
    finally:
        mt5.shutdown()


def modify_position_sl_tp(*, symbol: str, position_id: int, stop_loss: float, take_profit: float) -> ExecutionResult:
    mt5 = _import_mt5()
    if mt5 is None:
        return ExecutionResult(False, None, None, None, None, None, "MetaTrader5 Python package is not installed.")
    if not mt5.initialize():
        return ExecutionResult(False, None, None, None, None, None, f"MT5 initialize failed: {mt5.last_error()}")
    try:
        info = mt5.symbol_info(symbol)
        spec = info._asdict() if info is not None else {}
        sl = normalize_price(float(stop_loss), spec)
        tp = normalize_price(float(take_profit), spec)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(position_id),
            "sl": sl,
            "tp": tp,
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(False, None, None, None, None, None, f"MT5 SLTP returned None: {mt5.last_error()}")
        raw = result._asdict()
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return ExecutionResult(ok, None, None, int(result.retcode), None, None, "OK" if ok else str(raw), raw)
    finally:
        mt5.shutdown()


def close_position(
    *,
    symbol: str,
    position_id: int,
    side: str,
    volume: float,
    deviation_points: int,
    magic_number: int,
    comment: str,
) -> ExecutionResult:
    mt5 = _import_mt5()
    if mt5 is None:
        return ExecutionResult(False, None, None, None, None, volume, "MetaTrader5 Python package is not installed.")
    if not mt5.initialize():
        return ExecutionResult(False, None, None, None, None, volume, f"MT5 initialize failed: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(symbol, True):
            return ExecutionResult(False, None, None, None, None, volume, f"MT5 symbol_select failed: {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return ExecutionResult(False, None, None, None, None, volume, f"MT5 symbol_info_tick failed: {mt5.last_error()}")
        info = mt5.symbol_info(symbol)
        spec = info._asdict() if info is not None else {}

        side_upper = side.upper()
        if side_upper == "BUY":
            order_type = mt5.ORDER_TYPE_SELL
            price = normalize_price(float(tick.bid), spec)
        elif side_upper == "SELL":
            order_type = mt5.ORDER_TYPE_BUY
            price = normalize_price(float(tick.ask), spec)
        else:
            return ExecutionResult(False, None, None, None, None, volume, f"Unsupported side: {side}")

        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": int(position_id),
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "deviation": int(deviation_points),
            "magic": int(magic_number),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        invalid_fill_messages = []
        for filling_type in supported_filling_types(mt5, spec):
            request = {**base_request, "type_filling": filling_type}
            result = mt5.order_send(request)
            if result is None:
                return ExecutionResult(False, None, None, None, price, volume, f"MT5 close returned None: {mt5.last_error()}", {"request": request})

            raw = result._asdict()
            raw["request"] = request
            ok = result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                return ExecutionResult(
                    ok=True,
                    order_id=int(getattr(result, "order", 0) or 0) or None,
                    deal_id=int(getattr(result, "deal", 0) or 0) or None,
                    retcode=int(getattr(result, "retcode", 0) or 0),
                    price=float(getattr(result, "price", price) or price),
                    volume=float(getattr(result, "volume", volume) or volume),
                    message=f"OK filling={filling_type}",
                    raw=raw,
                )
            message = f"MT5 rejected close: retcode={result.retcode}, comment={getattr(result, 'comment', '')}, filling={filling_type}"
            if result.retcode == getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
                invalid_fill_messages.append(message)
                continue
            return ExecutionResult(False, None, None, int(getattr(result, "retcode", 0) or 0), price, volume, message, raw)

        return ExecutionResult(
            False,
            None,
            None,
            getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030),
            price,
            volume,
            "; ".join(invalid_fill_messages) or "MT5 rejected all supported close filling modes.",
            {"request": base_request, "filling_mode": spec.get("filling_mode")},
        )
    finally:
        mt5.shutdown()


def open_positions(symbol: str | None = None) -> tuple[bool, list[dict[str, Any]], str]:
    mt5 = _import_mt5()
    if mt5 is None:
        return False, [], "MetaTrader5 Python package is not installed."
    if not mt5.initialize():
        return False, [], f"MT5 initialize failed: {mt5.last_error()}"
    try:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return False, [], f"MT5 positions_get failed: {mt5.last_error()}"
        return True, [position._asdict() for position in positions], "OK"
    finally:
        mt5.shutdown()


def closed_position_result(position_id: int, lookback_days: int = 7) -> tuple[bool, dict[str, Any], str]:
    if not position_id:
        return False, {}, "Missing MT5 position id."

    mt5 = _import_mt5()
    if mt5 is None:
        return False, {}, "MetaTrader5 Python package is not installed."
    if not mt5.initialize():
        return False, {}, f"MT5 initialize failed: {mt5.last_error()}"

    try:
        to_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        from_time = to_time - timedelta(days=max(1, int(lookback_days)))
        deals = mt5.history_deals_get(from_time, to_time)
        if deals is None:
            return False, {}, f"MT5 history_deals_get failed: {mt5.last_error()}"

        matched = [deal._asdict() for deal in deals if int(getattr(deal, "position_id", 0) or 0) == int(position_id)]
        if not matched:
            return False, {}, f"No MT5 history deals found for position id {position_id}."

        exit_entries = {
            getattr(mt5, "DEAL_ENTRY_OUT", 1),
            getattr(mt5, "DEAL_ENTRY_INOUT", 2),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
        }
        exit_deals = [deal for deal in matched if int(deal.get("entry", -1) or -1) in exit_entries]
        closing_deal = exit_deals[-1] if exit_deals else matched[-1]
        pnl = sum(
            float(deal.get("profit") or 0.0)
            + float(deal.get("commission") or 0.0)
            + float(deal.get("swap") or 0.0)
            + float(deal.get("fee") or 0.0)
            for deal in matched
        )
        result = {
            "position_id": int(position_id),
            "exit_price": float(closing_deal.get("price") or 0.0),
            "pnl": pnl,
            "deal_count": len(matched),
            "exit_deal_id": int(closing_deal.get("ticket") or 0),
            "closed_at": int(closing_deal.get("time") or 0),
        }
        return True, result, "OK"
    finally:
        mt5.shutdown()
