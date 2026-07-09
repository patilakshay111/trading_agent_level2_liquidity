from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import time
from typing import Any, Callable, Iterable

from .config import enabled_symbols, load_config
from .market.binance import BinanceMarketData
from .state import StateStore


INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


@dataclass(frozen=True)
class HistoricalCandle:
    symbol: str
    source: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    taker_buy_volume: float = 0.0
    trade_count: int = 0
    raw_json: str = "{}"

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def interval_ms(interval: str) -> int:
    return INTERVAL_MS.get(interval, 60_000)


def closed_candle_end_ms(now_ms: int | None = None, interval: str = "1m") -> int:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    step = interval_ms(interval)
    return (now // step) * step - 1


def binance_kline_to_candle(symbol: str, kline: list[Any], *, source: str = "binance", interval: str = "1m") -> HistoricalCandle:
    return HistoricalCandle(
        symbol=symbol,
        source=source,
        interval=interval,
        open_time=int(kline[0]),
        close_time=int(kline[6]),
        open=float(kline[1]),
        high=float(kline[2]),
        low=float(kline[3]),
        close=float(kline[4]),
        volume=float(kline[5]),
        quote_volume=float(kline[7]) if len(kline) > 7 else 0.0,
        trade_count=int(kline[8]) if len(kline) > 8 else 0,
        taker_buy_volume=float(kline[9]) if len(kline) > 9 else 0.0,
        raw_json=json.dumps(kline),
    )


def mt5_rate_to_candle(symbol: str, rate: Any, *, source: str = "mt5", interval: str = "1m") -> HistoricalCandle:
    if hasattr(rate, "_asdict"):
        row = rate._asdict()
    elif hasattr(rate, "dtype") and getattr(rate.dtype, "names", None):
        row = {name: rate[name].item() if hasattr(rate[name], "item") else rate[name] for name in rate.dtype.names}
    else:
        row = dict(rate)
    open_time = int(float(row["time"]) * 1000)
    close_time = open_time + interval_ms(interval) - 1
    volume = float(row.get("real_volume") or row.get("tick_volume") or 0.0)
    close = float(row["close"])
    return HistoricalCandle(
        symbol=symbol,
        source=source,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=close,
        volume=volume,
        quote_volume=volume * close,
        taker_buy_volume=0.0,
        trade_count=int(row.get("tick_volume") or 0),
        raw_json=json.dumps(row, sort_keys=True),
    )


def iter_binance_kline_pages(
    market: BinanceMarketData,
    data_symbol: str,
    *,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1500,
) -> Iterable[list[list[Any]]]:
    cursor = int(start_ms)
    step = interval_ms(interval)
    while cursor <= end_ms:
        batch = market.klines(data_symbol, interval=interval, start_time=cursor, end_time=end_ms, limit=limit)
        if not batch:
            break
        yield batch
        last_open = int(batch[-1][0])
        next_cursor = last_open + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < limit:
            break


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc)


def _bucket_bounds(price: float, bucket_size: float) -> tuple[float, float]:
    if bucket_size <= 0:
        half = max(abs(price) * 0.00025, 0.01)
        return price - half, price + half
    low = (price // bucket_size) * bucket_size
    return low, low + bucket_size


def _side_for_level(price: float, reference: float) -> str:
    return "support" if price <= reference else "resistance"


def _age_decay(last_touch: int, now_ms: int, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now_ms - int(last_touch)) / 86_400_000.0)
    return 0.5 ** (age_days / half_life_days)


def _score_level(
    *,
    base: float,
    touches: int,
    volume: float,
    max_volume: float,
    last_touch: int,
    now_ms: int,
    half_life_days: float,
    bonus: float = 0.0,
) -> float:
    touch_score = min(22.0, max(0, touches - 1) * 4.0)
    volume_score = min(18.0, (volume / max(max_volume, 1.0)) * 18.0)
    decay = _age_decay(last_touch, now_ms, half_life_days)
    return round(max(0.0, min(100.0, (base + touch_score + volume_score + bonus) * decay)), 2)


def aggregate_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if interval == "1m":
        return list(candles)
    step = interval_ms(interval)
    if step <= interval_ms("1m"):
        return list(candles)
    buckets: dict[int, list[dict[str, Any]]] = {}
    for candle in candles:
        open_time = int(candle["open_time"])
        bucket_open = (open_time // step) * step
        buckets.setdefault(bucket_open, []).append(candle)

    aggregated: list[dict[str, Any]] = []
    for bucket_open in sorted(buckets):
        rows = sorted(buckets[bucket_open], key=lambda row: int(row["open_time"]))
        if len(rows) < max(1, step // interval_ms("1m")):
            continue
        first = rows[0]
        last = rows[-1]
        aggregated.append(
            {
                "symbol": first["symbol"],
                "source": first["source"],
                "interval": interval,
                "open_time": bucket_open,
                "close_time": bucket_open + step - 1,
                "open": float(first["open"]),
                "high": max(float(row["high"]) for row in rows),
                "low": min(float(row["low"]) for row in rows),
                "close": float(last["close"]),
                "volume": sum(float(row.get("volume", 0.0) or 0.0) for row in rows),
                "quote_volume": sum(float(row.get("quote_volume", 0.0) or 0.0) for row in rows),
                "taker_buy_volume": sum(float(row.get("taker_buy_volume", 0.0) or 0.0) for row in rows),
                "trade_count": sum(int(row.get("trade_count", 0) or 0) for row in rows),
                "raw_json": "{}",
            }
        )
    return aggregated


def derive_historical_levels(
    symbol: str,
    candles: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now_ms: int | None = None,
    structural_timeframe: str = "m1",
    include_period_session: bool = True,
) -> list[dict[str, Any]]:
    if not candles:
        return []
    historical_cfg = config.get("historical_liquidity", {})
    strategy_cfg = config.get("strategy", {})
    bucket_size = float((strategy_cfg.get("price_bucket_size") or {}).get(symbol, 10.0))
    half_life = float(historical_cfg.get("decay_half_life_days", 30.0))
    now = int(now_ms if now_ms is not None else max(int(row["close_time"]) for row in candles))
    current_price = float(candles[-1]["close"])
    max_volume = max(float(row.get("volume", 0.0) or 0.0) for row in candles) or 1.0
    levels: dict[tuple[str, str, float, float], dict[str, Any]] = {}

    def add(
        source: str,
        level_type: str,
        timeframe: str,
        price: float,
        *,
        label: str,
        base: float,
        candle: dict[str, Any],
        touches: int = 1,
        volume: float | None = None,
        bonus: float = 0.0,
        side: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        low, high = _bucket_bounds(price, bucket_size)
        key = (source, level_type, round(low, 8), round(high, 8))
        last_touch = int(candle["open_time"])
        item = levels.get(key)
        item_volume = float(volume if volume is not None else candle.get("volume", 0.0) or 0.0)
        if item:
            item["touches"] += touches
            item["last_touch"] = max(int(item["last_touch"]), last_touch)
            item["volume"] += item_volume
            item["strength"] = min(100.0, float(item["strength"]) + 3.0)
            item["score"] = _score_level(
                base=base,
                touches=int(item["touches"]),
                volume=float(item["volume"]),
                max_volume=max_volume,
                last_touch=int(item["last_touch"]),
                now_ms=now,
                half_life_days=half_life,
                bonus=bonus,
            )
            return
        score = _score_level(
            base=base,
            touches=touches,
            volume=item_volume,
            max_volume=max_volume,
            last_touch=last_touch,
            now_ms=now,
            half_life_days=half_life,
            bonus=bonus,
        )
        levels[key] = {
            "symbol": symbol,
            "source": source,
            "level_type": level_type,
            "timeframe": timeframe,
            "price_low": round(low, 4),
            "price_high": round(high, 4),
            "label": label,
            "side": side or _side_for_level(price, current_price),
            "strength": min(100.0, base + bonus),
            "score": score,
            "touches": touches,
            "first_seen": last_touch,
            "last_touch": last_touch,
            "volume": item_volume,
            "metadata": metadata or {},
        }

    if include_period_session:
        _derive_period_levels(candles, add)
        _derive_session_levels(candles, add)
    _derive_swings(candles, add, structural_timeframe=structural_timeframe)
    _derive_equal_levels(candles, add, bucket_size, structural_timeframe=structural_timeframe)
    _derive_wick_rejections(candles, add, structural_timeframe=structural_timeframe)
    _derive_high_volume_zones(candles, add, max_volume, structural_timeframe=structural_timeframe)
    _derive_breakout_retests(candles, add, bucket_size, structural_timeframe=structural_timeframe)

    return sorted(levels.values(), key=lambda row: float(row["score"]), reverse=True)


def derive_multitimeframe_historical_levels(
    symbol: str,
    m1_candles: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    intervals = [str(item) for item in config.get("historical_liquidity", {}).get("derived_intervals", ["1m"])]
    combined: list[dict[str, Any]] = []
    for interval in intervals:
        candles = m1_candles if interval == "1m" else aggregate_candles(m1_candles, interval)
        if not candles:
            continue
        timeframe = "m1" if interval == "1m" else interval
        combined.extend(
            derive_historical_levels(
                symbol,
                candles,
                config,
                now_ms=now_ms,
                structural_timeframe=timeframe,
                include_period_session=(interval == "1m"),
            )
        )
    return sorted(combined, key=lambda row: float(row["score"]), reverse=True)


def _derive_period_levels(candles: list[dict[str, Any]], add: Callable[..., None]) -> None:
    periods: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candle in candles:
        ts = _dt(int(candle["open_time"]))
        keys = [
            ("daily", ts.strftime("%Y-%m-%d")),
            ("weekly", f"{ts.isocalendar().year}-W{ts.isocalendar().week:02d}"),
            ("monthly", ts.strftime("%Y-%m")),
        ]
        for key in keys:
            periods.setdefault(key, []).append(candle)
    for (timeframe, stamp), rows in periods.items():
        if not rows:
            continue
        high = max(rows, key=lambda row: float(row["high"]))
        low = min(rows, key=lambda row: float(row["low"]))
        volume = sum(float(row.get("volume", 0.0) or 0.0) for row in rows)
        add(f"historical_{timeframe}_high", "period_high", timeframe, float(high["high"]), label=f"{timeframe} high {stamp}", base=68.0, candle=high, volume=volume, bonus=8.0, side="resistance")
        add(f"historical_{timeframe}_low", "period_low", timeframe, float(low["low"]), label=f"{timeframe} low {stamp}", base=68.0, candle=low, volume=volume, bonus=8.0, side="support")


def _derive_session_levels(candles: list[dict[str, Any]], add: Callable[..., None]) -> None:
    sessions = {
        "Asia": (0, 7),
        "London": (7, 13.5),
        "New York": (13.5, 21),
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candle in candles:
        ts = _dt(int(candle["open_time"]))
        hour = ts.hour + (ts.minute / 60.0)
        for name, (start, end) in sessions.items():
            if start <= hour < end:
                grouped.setdefault((name, ts.strftime("%Y-%m-%d")), []).append(candle)
                break
    for (name, stamp), rows in grouped.items():
        high = max(rows, key=lambda row: float(row["high"]))
        low = min(rows, key=lambda row: float(row["low"]))
        volume = sum(float(row.get("volume", 0.0) or 0.0) for row in rows)
        slug = name.lower().replace(" ", "_")
        add(f"historical_{slug}_session_high", "session_high", name, float(high["high"]), label=f"{name} high {stamp}", base=62.0, candle=high, volume=volume, bonus=6.0, side="resistance")
        add(f"historical_{slug}_session_low", "session_low", name, float(low["low"]), label=f"{name} low {stamp}", base=62.0, candle=low, volume=volume, bonus=6.0, side="support")


def _tf_source(structural_timeframe: str, suffix: str) -> str:
    return f"historical_{suffix}" if structural_timeframe == "m1" else f"historical_{structural_timeframe}_{suffix}"


def _derive_swings(candles: list[dict[str, Any]], add: Callable[..., None], structural_timeframe: str = "m1", span: int = 2) -> None:
    if len(candles) < (span * 2) + 1:
        return
    for index in range(span, len(candles) - span):
        window = candles[index - span : index + span + 1]
        candle = candles[index]
        high = float(candle["high"])
        low = float(candle["low"])
        if high == max(float(row["high"]) for row in window):
            add(_tf_source(structural_timeframe, "swing_high"), "swing_high", structural_timeframe, high, label=f"{structural_timeframe} swing high", base=58.0, candle=candle, side="resistance")
        if low == min(float(row["low"]) for row in window):
            add(_tf_source(structural_timeframe, "swing_low"), "swing_low", structural_timeframe, low, label=f"{structural_timeframe} swing low", base=58.0, candle=candle, side="support")


def _derive_equal_levels(candles: list[dict[str, Any]], add: Callable[..., None], bucket_size: float, structural_timeframe: str = "m1") -> None:
    high_buckets: dict[float, list[dict[str, Any]]] = {}
    low_buckets: dict[float, list[dict[str, Any]]] = {}
    tolerance = max(bucket_size, 1.0)
    span = 2
    if len(candles) < (span * 2) + 1:
        return
    for index in range(span, len(candles) - span):
        candle = candles[index]
        window = candles[index - span : index + span + 1]
        is_swing_high = float(candle["high"]) == max(float(row["high"]) for row in window)
        is_swing_low = float(candle["low"]) == min(float(row["low"]) for row in window)
        high_key = round(float(candle["high"]) / tolerance) * tolerance
        low_key = round(float(candle["low"]) / tolerance) * tolerance
        if is_swing_high:
            high_buckets.setdefault(high_key, []).append(candle)
        if is_swing_low:
            low_buckets.setdefault(low_key, []).append(candle)
    for price, rows in high_buckets.items():
        if len(rows) >= 2:
            add(_tf_source(structural_timeframe, "equal_high"), "equal_high", structural_timeframe, price, label=f"{structural_timeframe} equal highs", base=64.0, candle=rows[-1], touches=len(rows), volume=sum(float(row.get("volume", 0.0) or 0.0) for row in rows), bonus=6.0, side="resistance")
    for price, rows in low_buckets.items():
        if len(rows) >= 2:
            add(_tf_source(structural_timeframe, "equal_low"), "equal_low", structural_timeframe, price, label=f"{structural_timeframe} equal lows", base=64.0, candle=rows[-1], touches=len(rows), volume=sum(float(row.get("volume", 0.0) or 0.0) for row in rows), bonus=6.0, side="support")


def _derive_wick_rejections(candles: list[dict[str, Any]], add: Callable[..., None], structural_timeframe: str = "m1") -> None:
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close = float(candle["close"])
        candle_range = high - low
        if candle_range <= 0:
            continue
        upper = (high - max(open_price, close)) / candle_range
        lower = (min(open_price, close) - low) / candle_range
        if upper >= 0.45:
            add(_tf_source(structural_timeframe, "wick_rejection_high"), "wick_rejection", structural_timeframe, high, label=f"{structural_timeframe} upper wick rejection", base=60.0, candle=candle, bonus=upper * 12.0, side="resistance", metadata={"wick_pct": round(upper, 4)})
        if lower >= 0.45:
            add(_tf_source(structural_timeframe, "wick_rejection_low"), "wick_rejection", structural_timeframe, low, label=f"{structural_timeframe} lower wick rejection", base=60.0, candle=candle, bonus=lower * 12.0, side="support", metadata={"wick_pct": round(lower, 4)})


def _derive_high_volume_zones(candles: list[dict[str, Any]], add: Callable[..., None], max_volume: float, structural_timeframe: str = "m1") -> None:
    if max_volume <= 0:
        return
    for candle in candles:
        volume = float(candle.get("volume", 0.0) or 0.0)
        if volume / max_volume >= 0.80:
            price = (float(candle["high"]) + float(candle["low"]) + float(candle["close"])) / 3.0
            add(_tf_source(structural_timeframe, "high_volume"), "high_volume", structural_timeframe, price, label=f"{structural_timeframe} high-volume candle", base=56.0, candle=candle, volume=volume, bonus=12.0)


def _derive_breakout_retests(candles: list[dict[str, Any]], add: Callable[..., None], bucket_size: float, structural_timeframe: str = "m1") -> None:
    lookback = 20
    if len(candles) <= lookback + 2:
        return
    for index in range(lookback, len(candles) - 2):
        previous = candles[index - lookback : index]
        candle = candles[index]
        next_rows = candles[index + 1 : index + 3]
        prior_high = max(float(row["high"]) for row in previous)
        prior_low = min(float(row["low"]) for row in previous)
        buffer = max(bucket_size * 0.25, float(candle["close"]) * 0.0005)
        if float(candle["close"]) > prior_high + buffer and any(float(row["low"]) <= prior_high + buffer for row in next_rows):
            add(_tf_source(structural_timeframe, "breakout_retest_high"), "breakout_retest", structural_timeframe, prior_high, label=f"{structural_timeframe} breakout retest high", base=66.0, candle=next_rows[-1], touches=2, bonus=8.0, side="support")
        if float(candle["close"]) < prior_low - buffer and any(float(row["high"]) >= prior_low - buffer for row in next_rows):
            add(_tf_source(structural_timeframe, "breakout_retest_low"), "breakout_retest", structural_timeframe, prior_low, label=f"{structural_timeframe} breakout retest low", base=66.0, candle=next_rows[-1], touches=2, bonus=8.0, side="resistance")


class HistoricalLiquidityService:
    def __init__(
        self,
        store: StateStore,
        config: dict[str, Any] | None = None,
        *,
        market: BinanceMarketData | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.config = config if config is not None else load_config()
        market_cfg = self.config.get("market_data", {})
        self.market = market or BinanceMarketData(
            base_url=str(market_cfg.get("rest_base_url", "https://fapi.binance.com")),
            market=str(market_cfg.get("market", "um_futures")),
            timeout=10.0,
        )
        self.sleep_fn = sleep_fn or time.sleep

    def status(self) -> dict[str, Any]:
        payload = self.store.historical_backfill_status()
        payload["enabled"] = bool(self.config.get("historical_liquidity", {}).get("enabled", True))
        return payload

    def levels_for_symbol(self, symbol: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.store.load_historical_levels(symbol, limit=limit)

    def update_missing_recent(
        self,
        symbol_cfg: dict[str, Any],
        *,
        now_ms: int | None = None,
        rebuild_levels: bool = True,
    ) -> dict[str, Any]:
        cfg = self.config.get("historical_liquidity", {})
        if not cfg.get("enabled", True) or not cfg.get("update_on_startup", True):
            return {"ok": True, "updated": 0, "reason": "historical liquidity startup update disabled"}
        symbol = str(symbol_cfg["name"])
        source = self._source_for_symbol(symbol)
        latest = self.store.latest_historical_kline_open_time(symbol, source, str(cfg.get("interval", "1m")))
        if latest is None:
            return {"ok": False, "updated": 0, "reason": "needs_backfill"}
        start_ms = latest + interval_ms(str(cfg.get("interval", "1m")))
        end_ms = closed_candle_end_ms(now_ms, str(cfg.get("interval", "1m")))
        if start_ms > end_ms:
            return {"ok": True, "updated": 0, "reason": "already_current"}
        limit = int(cfg.get("startup_recent_limit", 1500))
        result = self.backfill_symbol(symbol_cfg, start_ms=start_ms, end_ms=end_ms, page_limit=limit, rebuild_levels=rebuild_levels)
        if result.get("ok") and not rebuild_levels:
            incremental = self.update_incremental_levels(symbol_cfg, end_ms=end_ms)
            result["incremental_levels"] = incremental
        return result

    def backfill_all(self, *, years: int | None = None, symbols: list[str] | None = None, rebuild_levels: bool = True) -> dict[str, Any]:
        wanted = {item.upper() for item in symbols or []}
        results = []
        for symbol_cfg in enabled_symbols(self.config):
            if wanted and str(symbol_cfg["name"]).upper() not in wanted:
                continue
            results.append(self.backfill_symbol(symbol_cfg, years=years, rebuild_levels=rebuild_levels))
        return {"ok": all(item.get("ok") for item in results), "results": results}

    def backfill_symbol(
        self,
        symbol_cfg: dict[str, Any],
        *,
        years: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        page_limit: int = 1500,
        rebuild_levels: bool = True,
    ) -> dict[str, Any]:
        cfg = self.config.get("historical_liquidity", {})
        interval = str(cfg.get("interval", "1m"))
        symbol = str(symbol_cfg["name"])
        source = self._source_for_symbol(symbol)
        now_end = closed_candle_end_ms(interval=interval)
        end = int(end_ms if end_ms is not None else now_end)
        step = interval_ms(interval)
        lookback_years = int(years if years is not None else cfg.get("years", 2))
        default_start = min(
            int((datetime.now(timezone.utc) - timedelta(days=365 * lookback_years)).timestamp() * 1000),
            end,
        )
        if start_ms is not None:
            ranges = [(int(start_ms), end)]
        else:
            earliest = self.store.earliest_historical_kline_open_time(symbol, source, interval)
            latest = self.store.latest_historical_kline_open_time(symbol, source, interval)
            if earliest is None or latest is None:
                ranges = [(default_start, end)]
            else:
                expected_rows = max(1, int((end - default_start) // step) + 1)
                stored_rows = self.store.historical_kline_count(symbol, source, interval)
                if stored_rows < expected_rows * 0.98:
                    ranges = [(default_start, end)]
                else:
                    ranges = []
                    older_end = int(earliest) - step
                    if default_start <= older_end:
                        ranges.append((default_start, older_end))
                    recent_start = int(latest) + step
                    if recent_start <= end:
                        ranges.append((recent_start, end))

        ranges = [(start, stop) for start, stop in ranges if start <= stop]
        if not ranges:
            if rebuild_levels:
                self.rebuild_levels(symbol, interval=interval)
            return {"ok": True, "symbol": symbol, "source": source, "rows": 0, "reason": "already_current"}

        self.store.update_historical_backfill_run(
            symbol=symbol,
            source=source,
            interval=interval,
            status="running",
            start_time=min(start for start, _ in ranges),
            end_time=max(stop for _, stop in ranges),
        )
        try:
            rows = 0
            for range_start, range_end in ranges:
                rows += self._fetch_and_store(symbol_cfg, source, interval, range_start, range_end, page_limit)
            if rebuild_levels:
                level_count = self.rebuild_levels(symbol, interval=interval)
            else:
                level_count = 0
            self.store.update_historical_backfill_run(symbol=symbol, source=source, interval=interval, status="complete")
            return {"ok": True, "symbol": symbol, "source": source, "rows": rows, "levels": level_count}
        except Exception as exc:  # noqa: BLE001
            self.store.update_historical_backfill_run(symbol=symbol, source=source, interval=interval, status="failed", error=str(exc))
            return {"ok": False, "symbol": symbol, "source": source, "rows": 0, "error": str(exc)}

    def rebuild_levels(self, symbol: str, *, interval: str = "1m") -> int:
        candles = self.store.load_historical_klines(symbol, interval=interval)
        levels = derive_multitimeframe_historical_levels(symbol, candles, self.config)
        self.store.replace_historical_levels(symbol, levels)
        return len(levels)

    def update_incremental_levels(self, symbol_cfg: dict[str, Any], *, end_ms: int | None = None) -> dict[str, Any]:
        cfg = self.config.get("historical_liquidity", {})
        symbol = str(symbol_cfg["name"])
        source = self._source_for_symbol(symbol)
        intervals = [str(item) for item in cfg.get("derived_intervals", ["1m"]) if str(item) != "1m"]
        if not intervals:
            return {"ok": True, "symbol": symbol, "updated": 0, "reason": "no derived intervals"}
        end = int(end_ms if end_ms is not None else closed_candle_end_ms(interval="1m"))
        window_ms = max(1, int(cfg.get("incremental_level_window_days", 7))) * 86_400_000
        start = max(0, end - window_ms)
        m1_candles = self.store.load_historical_klines(symbol, source=source, interval="1m", start_time=start, end_time=end)
        if not m1_candles:
            return {"ok": True, "symbol": symbol, "updated": 0, "reason": "no recent candles"}

        total_levels = 0
        processed: dict[str, int] = {}
        for interval in intervals:
            step = interval_ms(interval)
            closed_boundary = ((end + 1) // step) * step - step
            if closed_boundary < 0:
                continue
            task = f"incremental_levels_{interval}"
            last = self.store.latest_historical_maintenance_run(symbol, task)
            last_processed = int(last["last_run_ms"]) if last and last.get("last_run_ms") is not None else None
            if last_processed is not None and closed_boundary <= last_processed:
                continue
            aggregated = [
                candle for candle in aggregate_candles(m1_candles, interval)
                if (last_processed is None or int(candle["open_time"]) > last_processed)
                and int(candle["open_time"]) <= closed_boundary
            ]
            if not aggregated:
                self.store.update_historical_maintenance_run(symbol=symbol, task=task, status="complete", last_run_ms=closed_boundary, rows_affected=0)
                processed[interval] = 0
                continue

            context_start = max(0, int(aggregated[0]["open_time"]) - (window_ms // 2))
            context = [
                candle for candle in aggregate_candles(m1_candles, interval)
                if int(candle["open_time"]) >= context_start and int(candle["open_time"]) <= closed_boundary
            ]
            levels = derive_historical_levels(
                symbol,
                context,
                self.config,
                now_ms=end,
                structural_timeframe=interval,
                include_period_session=False,
            )
            levels = [
                level for level in levels
                if int(level.get("last_touch") or 0) >= int(aggregated[0]["open_time"])
            ]
            count = self.store.upsert_historical_levels(levels)
            self.store.update_historical_maintenance_run(symbol=symbol, task=task, status="complete", last_run_ms=closed_boundary, rows_affected=count)
            processed[interval] = count
            total_levels += count

        return {"ok": True, "symbol": symbol, "updated": total_levels, "processed": processed}

    def prune_old_m1(
        self,
        symbol_cfg: dict[str, Any],
        *,
        now_ms: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        cfg = self.config.get("historical_liquidity", {})
        retention_days = int(cfg.get("raw_m1_retention_days", 60))
        prune_interval_days = int(cfg.get("prune_interval_days", 30))
        if retention_days <= 0:
            return {"ok": True, "symbol": symbol_cfg["name"], "pruned": 0, "reason": "retention disabled"}
        symbol = str(symbol_cfg["name"])
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        task = "prune_old_m1"
        last = self.store.latest_historical_maintenance_run(symbol, task)
        if not force and last and last.get("last_run_ms") is not None:
            elapsed = now - int(last["last_run_ms"])
            if elapsed < prune_interval_days * 86_400_000:
                return {"ok": True, "symbol": symbol, "pruned": 0, "reason": "not_due"}
        source = self._source_for_symbol(symbol)
        cutoff = now - (retention_days * 86_400_000)
        try:
            pruned = self.store.prune_historical_klines(
                symbol,
                source=source,
                interval=str(cfg.get("interval", "1m")),
                before_open_time=cutoff,
            )
            self.store.update_historical_maintenance_run(
                symbol=symbol,
                task=task,
                status="complete",
                last_run_ms=now,
                rows_affected=pruned,
            )
            return {"ok": True, "symbol": symbol, "source": source, "pruned": pruned, "cutoff": cutoff}
        except Exception as exc:  # noqa: BLE001
            self.store.update_historical_maintenance_run(
                symbol=symbol,
                task=task,
                status="failed",
                rows_affected=0,
                error=str(exc),
            )
            return {"ok": False, "symbol": symbol, "pruned": 0, "error": str(exc)}

    def _fetch_and_store(self, symbol_cfg: dict[str, Any], source: str, interval: str, start_ms: int, end_ms: int, page_limit: int) -> int:
        symbol = str(symbol_cfg["name"])
        if source == "mt5":
            candles = self._fetch_mt5(symbol_cfg, interval, start_ms, end_ms)
            self.store.upsert_historical_klines([candle.to_row() for candle in candles])
            if candles:
                self.store.update_historical_backfill_run(
                    symbol=symbol,
                    source=source,
                    interval=interval,
                    status="running",
                    last_open_time=candles[-1].open_time,
                    rows_fetched=len(candles),
                )
            return len(candles)

        data_symbol = str(symbol_cfg.get("data_symbol") or symbol)
        total = 0
        for batch in iter_binance_kline_pages(self.market, data_symbol, interval=interval, start_ms=start_ms, end_ms=end_ms, limit=page_limit):
            candles = [
                binance_kline_to_candle(symbol, kline, source="binance", interval=interval)
                for kline in batch
                if int(kline[6]) <= end_ms
            ]
            self.store.upsert_historical_klines([candle.to_row() for candle in candles])
            if candles:
                total += len(candles)
                self.store.update_historical_backfill_run(
                    symbol=symbol,
                    source=source,
                    interval=interval,
                    status="running",
                    last_open_time=candles[-1].open_time,
                    rows_fetched=len(candles),
                )
            self.sleep_fn(float(self.config.get("historical_liquidity", {}).get("request_sleep_seconds", 0.05)))
        return total

    def _fetch_mt5(self, symbol_cfg: dict[str, Any], interval: str, start_ms: int, end_ms: int) -> list[HistoricalCandle]:
        if interval != "1m":
            raise ValueError("MT5 fallback currently supports interval='1m' only.")
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 Python package is not installed.") from exc
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        mt5_symbol = str(symbol_cfg.get("mt5_symbol") or symbol_cfg["name"])
        try:
            if not mt5.symbol_select(mt5_symbol, True):
                raise RuntimeError(f"MT5 symbol_select failed: {mt5.last_error()}")
            candles: list[HistoricalCandle] = []
            chunk_ms = 30 * 86_400_000
            cursor = int(start_ms)
            while cursor <= end_ms:
                chunk_end = min(int(end_ms), cursor + chunk_ms - interval_ms(interval))
                rates = mt5.copy_rates_range(mt5_symbol, mt5.TIMEFRAME_M1, _dt(cursor), _dt(chunk_end))
                if rates is None:
                    code, message = mt5.last_error()
                    if code == -2:
                        cursor = chunk_end + interval_ms(interval)
                        continue
                    raise RuntimeError(f"MT5 copy_rates_range failed: {(code, message)}")
                candles.extend(mt5_rate_to_candle(str(symbol_cfg["name"]), rate, interval=interval) for rate in rates)
                cursor = chunk_end + interval_ms(interval)
            return candles
        finally:
            mt5.shutdown()

    def _source_for_symbol(self, symbol: str) -> str:
        cfg = self.config.get("historical_liquidity", {})
        mt5_symbols = {str(item).upper() for item in cfg.get("mt5_fallback_symbols", [])}
        return "mt5" if symbol.upper() in mt5_symbols else "binance"


def historical_levels_as_major_zones(
    levels: list[dict[str, Any]],
    *,
    mid: float,
    weight: float,
    max_each_side: int,
    min_zone_score: float = 65.0,
    decay_half_life_days: float = 30.0,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    zones = []
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    max_volume = max((float(level.get("volume", 0.0) or 0.0) for level in levels), default=0.0)
    for level in levels:
        side = str(level.get("side") or "")
        if side not in {"support", "resistance"}:
            continue
        if side == "support" and float(level["price_high"]) > mid:
            continue
        if side == "resistance" and float(level["price_low"]) < mid:
            continue
        raw_score = _live_historical_level_score(
            level,
            mid=mid,
            now_ms=now,
            max_volume=max_volume,
            half_life_days=decay_half_life_days,
        )
        if raw_score < min_zone_score:
            continue
        score = min_zone_score + (max(0.0, raw_score - min_zone_score) * max(0.0, min(1.0, weight)))
        center = (float(level["price_low"]) + float(level["price_high"])) / 2.0
        zones.append(
            {
                "side": side,
                "price_low": round(float(level["price_low"]), 4),
                "price_high": round(float(level["price_high"]), 4),
                "label": str(level.get("label") or level.get("level_type") or "historical"),
                "score": round(score, 2),
                "sources": [str(level.get("source") or "historical")],
                "notional": round(float(level.get("volume", 0.0) or 0.0), 4),
                "memory_score": round(raw_score, 2),
                "strength": round(float(level.get("strength", 0.0) or level.get("score", 0.0)), 2),
                "distance_bps": round(abs(center - mid) / mid * 10000.0, 2) if mid else 0.0,
                "historical": True,
                "cached_score": round(float(level.get("score", 0.0) or 0.0), 2),
            }
        )
    supports = sorted([zone for zone in zones if zone["side"] == "support"], key=lambda row: float(row["score"]), reverse=True)
    resistances = sorted([zone for zone in zones if zone["side"] == "resistance"], key=lambda row: float(row["score"]), reverse=True)
    return [*supports[:max_each_side], *resistances[:max_each_side]]


def _live_historical_level_score(
    level: dict[str, Any],
    *,
    mid: float,
    now_ms: int,
    max_volume: float,
    half_life_days: float,
) -> float:
    cached_score = float(level.get("score", 0.0) or 0.0)
    strength = float(level.get("strength", 0.0) or cached_score)
    touches = max(0, int(level.get("touches", 0) or 0))
    volume = float(level.get("volume", 0.0) or 0.0)
    last_touch = int(level.get("last_touch") or level.get("first_seen") or now_ms)
    low = float(level.get("price_low", 0.0) or 0.0)
    high = float(level.get("price_high", 0.0) or 0.0)
    center = (low + high) / 2.0 if high > low else low
    distance_bps = abs(center - mid) / mid * 10000.0 if mid > 0 and center > 0 else 0.0
    level_type = str(level.get("level_type") or "")
    timeframe = str(level.get("timeframe") or "")

    type_bonus = {
        "period_high": 8.0,
        "period_low": 8.0,
        "session_high": 7.0,
        "session_low": 7.0,
        "breakout_retest": 7.0,
        "equal_high": 6.0,
        "equal_low": 6.0,
        "high_volume": 6.0,
        "swing_high": 5.0,
        "swing_low": 5.0,
        "wick_rejection": 5.0,
    }.get(level_type, 3.0)
    timeframe_bonus = {
        "monthly": 6.0,
        "weekly": 4.0,
        "daily": 3.0,
        "asia": 2.0,
        "london": 3.0,
        "new_york": 3.0,
        "m1": 1.0,
        "5m": 1.5,
        "15m": 2.5,
        "30m": 3.0,
        "1h": 4.0,
    }.get(timeframe, 1.0)

    strength_score = min(46.0, max(0.0, strength) * 0.46)
    touch_score = min(20.0, math.log1p(touches) * 4.5)
    volume_score = min(12.0, (volume / max(max_volume, 1.0)) * 12.0)
    distance_score = max(0.0, 12.0 - min(12.0, distance_bps / 25.0))
    decay = _age_decay(last_touch, now_ms, half_life_days)
    score = (strength_score + touch_score + volume_score + distance_score + type_bonus + timeframe_bonus) * decay
    return round(max(0.0, min(100.0, score)), 2)
