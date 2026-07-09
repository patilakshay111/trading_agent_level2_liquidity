from __future__ import annotations

import pickle
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from statistics import mean
from typing import Iterable


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "settings.toml"
DB_PATH = ROOT / "data" / "trading_agent.db"
TICK_CACHE_DIR = ROOT / "data" / "mt5_tick_cache"
CONTRACT_SIZE_FALLBACK = 100.0
CACHE_REFRESH_GRACE = timedelta(minutes=5)
SIMULATION_CHUNK = timedelta(hours=6)
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.0
ATR_TP_MULTIPLIER = 2.5
HTF_WICK_TIMEFRAMES = (15, 30)
HTF_WICK_MIN_FRACTION = 0.30
HTF_WICK_CLOSE_ZONE = 0.35
HTF_WICK_MIN_BPS = 2.0
HTF_WICK_POLICIES = ("reversal", "opposing", "resistance", "support", "any")
HTF_WICK_TIMEFRAME_SETS = ((15,), (30,), (15, 30))
Mark = tuple[int, float]


@dataclass
class Trade:
    id: int
    opened_at: datetime
    status: str
    symbol: str
    mt5_symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    lots: float
    risk_cash: float
    actual_pnl: float
    actual_exit_reason: str


@dataclass
class SimResult:
    trade: Trade
    tp_mode: str
    simulated_tp: float
    simulated_sl: float
    outcome: str
    exit_price: float
    exit_time: datetime | None
    pnl: float
    ticks_checked: int
    partial_fraction: float = 0.0
    partial_exit_price: float | None = None
    partial_exit_time: datetime | None = None
    partial_pnl: float = 0.0
    skip_reason: str = ""


@dataclass(frozen=True)
class SimulationConfig:
    name: str
    family: str
    sl_multiplier: float
    tp_multiplier: float
    fixed_sl_distance: float | None = None
    fixed_tp_distance: float | None = None
    breakeven_trigger_r: float | None = None
    breakeven_lock_r: float | None = None
    partial_close_fraction: float | None = None
    partial_close_trigger_r: float | None = None
    exit_trigger_r: float | None = None
    profit_target_cash: float | None = None
    trail_trigger_r: float | None = None
    trail_distance_r: float | None = None
    structure_trailing: bool = False
    structure_trail_buffer_r: float = 0.1
    structure_min_pullback_r: float = 0.25
    min_stop_update_bps: float = 0.0
    poll_interval_seconds: float = 0.0
    close_at_mark: bool = False
    pnl_from_risk_cash: bool = False
    skip_htf_wick: bool = False
    htf_wick_filter: HtfWickFilter | None = None


@dataclass(frozen=True)
class HtfWickFilter:
    timeframes: tuple[int, ...] = HTF_WICK_TIMEFRAMES
    min_fraction: float = HTF_WICK_MIN_FRACTION
    close_zone: float = HTF_WICK_CLOSE_ZONE
    min_bps: float = HTF_WICK_MIN_BPS
    policy: str = "any"


@dataclass
class OptimizationRow:
    config: SimulationConfig
    stats: dict[str, float | int | str]


@dataclass(frozen=True)
class WickOptimizationRow:
    wick_filter: HtfWickFilter
    stats: dict[str, float | int | str]
    skipped: int
    skipped_losers: int
    skipped_winners: int
    loss_removed: float
    profit_removed: float
    net_improvement: float
    preserve_score: float
    positive_pnl_kept_pct: float
    skipped_ids: str


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def ms_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def load_trades() -> list[Trade]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select id, opened_at, status, symbol, mt5_symbol, side, entry,
                   initial_stop_loss, stop_loss, take_profit, lots, risk_cash, pnl, exit_reason
            from trades
            order by id
            """
        ).fetchall()
    finally:
        conn.close()

    trades: list[Trade] = []
    for row in rows:
        stop_loss = row["initial_stop_loss"] if row["initial_stop_loss"] is not None else row["stop_loss"]
        mt5_symbol = row["mt5_symbol"] or row["symbol"]
        trades.append(
            Trade(
                id=int(row["id"]),
                opened_at=parse_time(str(row["opened_at"])),
                status=str(row["status"]),
                symbol=str(row["symbol"]),
                mt5_symbol=str(mt5_symbol),
                side=str(row["side"]).upper(),
                entry=float(row["entry"]),
                stop_loss=float(stop_loss),
                take_profit=float(row["take_profit"]),
                lots=float(row["lots"] or 0.0),
                risk_cash=float(row["risk_cash"] or 0.0),
                actual_pnl=float(row["pnl"] or 0.0),
                actual_exit_reason=str(row["exit_reason"] or ""),
            )
        )
    return trades


def parse_trade_selection(raw: str, trades: list[Trade]) -> list[int]:
    raw = raw.strip().lower()
    valid_ids = {trade.id for trade in trades}
    if raw in {"", "all", "a"}:
        return sorted(valid_ids)

    selected: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))

    missing = sorted(selected - valid_ids)
    if missing:
        raise ValueError(f"Trade id(s) not found: {', '.join(str(item) for item in missing)}")
    return sorted(selected)


def choose_analysis_mode() -> str:
    print()
    print("Simulation mode:")
    print("  1. Original TP, no trailing")
    print("  2. Double TP, no trailing")
    print("  3. Optimize settings and rank by net PnL")
    print("  4. Fixed $15 SL, original TP, no trailing")
    print("  5. Fixed $15 SL, original TP, with breakeven")
    print("  6. Original SL/TP, with breakeven")
    print("  7. Original SL/TP, breakeven + book 50%")
    print("  8. Original SL/TP, no trailing, one active trade only")
    print("  9. Original SL, half TP distance, no trailing")
    print(" 10. ATR SL/TP, no trailing")
    print(" 11. Current app SL/TP + breakeven/trailing")
    print(" 12. Original SL/TP, breakeven lock 0R")
    print(" 13. Original SL/TP, book all at breakeven trigger")
    print(" 14. Original SL/TP, trail by higher low / lower high")
    print(" 15. Original SL/TP, book 100% profit at $15")
    print(" 16. Original SL, TP at entry +/- $15 price")
    print(" 17. Current app SL/TP + breakeven/trailing, skip if M15/M30 support/resistance wick is forming")
    print(" 18. Find best M15/M30 wick skip filter for current app management")
    choice = input("Select mode [1]: ").strip()
    if choice in {"", "1"}:
        return "original"
    if choice == "2":
        return "double"
    if choice == "3":
        return "optimize"
    if choice == "4":
        return "fixed15"
    if choice == "5":
        return "fixed15_be"
    if choice == "6":
        return "original_be"
    if choice == "7":
        return "original_be_partial50"
    if choice == "8":
        return "original_single_active"
    if choice == "9":
        return "half_tp"
    if choice == "10":
        return "atr"
    if choice == "11":
        return "current_app"
    if choice == "12":
        return "original_be_lock50"
    if choice == "13":
        return "original_book_all_be"
    if choice == "14":
        return "original_structure_trail"
    if choice == "15":
        return "original_book_profit15"
    if choice == "16":
        return "original_tp_price15"
    if choice == "17":
        return "original_skip_htf_wick"
    if choice == "18":
        return "optimize_htf_wick_skip"
    raise ValueError("Invalid TP mode. Choose 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, or 18.")


def simulated_tp(trade: Trade, mode: str) -> float:
    if mode == "original":
        return trade.take_profit
    if mode == "double":
        if trade.side == "BUY":
            return trade.entry + (2.0 * (trade.take_profit - trade.entry))
        return trade.entry - (2.0 * (trade.entry - trade.take_profit))
    raise ValueError(f"Unsupported TP mode: {mode}")


def parse_config_value(value: str) -> bool | float | str:
    cleaned = value.split("#", 1)[0].strip().strip('"').strip("'")
    if cleaned.lower() in {"true", "false"}:
        return cleaned.lower() == "true"
    try:
        return float(cleaned)
    except ValueError:
        return cleaned


def read_config_section(section_name: str) -> dict[str, bool | float | str]:
    if not CONFIG_PATH.exists():
        return {}

    values: dict[str, bool | float | str] = {}
    in_section = False
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1].strip() == section_name
            continue
        if in_section and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = parse_config_value(value)
    return values


def current_app_config() -> SimulationConfig:
    trade_management = read_config_section("trade_management")
    engine = read_config_section("engine")
    enabled = bool(trade_management.get("enabled", True))
    if not enabled:
        return SimulationConfig("current_app|SLx1.00|TPx1.00|management_off", "current_app", 1.0, 1.0)

    be_trigger = float(trade_management.get("breakeven_trigger_r", 1.5))
    be_lock = float(trade_management.get("breakeven_lock_r", 0.3))
    trail_trigger = float(trade_management.get("trail_trigger_r", 2.5))
    trail_distance = float(trade_management.get("trail_distance_r", 1.0))
    min_update_bps = float(trade_management.get("min_stop_update_bps", 1.0))
    poll_interval = float(engine.get("poll_interval_seconds", 2.0))
    return SimulationConfig(
        name=(
            f"current_app|SLx1.00|TPx1.00|BE@{be_trigger:.2f}R|"
            f"Lock{be_lock:.2f}R|Trail@{trail_trigger:.2f}R|Dist{trail_distance:.2f}R|"
            f"Min{min_update_bps:.2f}bps|Poll{poll_interval:.1f}s"
        ),
        family="current_app",
        sl_multiplier=1.0,
        tp_multiplier=1.0,
        breakeven_trigger_r=be_trigger,
        breakeven_lock_r=be_lock,
        trail_trigger_r=trail_trigger,
        trail_distance_r=trail_distance,
        min_stop_update_bps=min_update_bps,
        poll_interval_seconds=poll_interval,
        close_at_mark=True,
        pnl_from_risk_cash=True,
    )


def current_app_with_htf_wick_skip_config() -> SimulationConfig:
    config = current_app_config()
    return replace(
        config,
        name=f"{config.name}|SkipM15/M30Wick",
        skip_htf_wick=True,
        htf_wick_filter=HtfWickFilter(),
    )


def fixed_config(mode: str) -> SimulationConfig:
    if mode == "original":
        return SimulationConfig("no_trailing|SLx1.00|TPx1.00", "no_trailing", 1.0, 1.0)
    if mode == "double":
        return SimulationConfig("no_trailing|SLx1.00|TPx2.00", "no_trailing", 1.0, 2.0)
    if mode == "half_tp":
        return SimulationConfig("no_trailing|SLx1.00|TPx0.50", "no_trailing", 1.0, 0.5)
    if mode == "fixed15":
        return SimulationConfig("no_trailing|SL$15|TPx1.00", "no_trailing", 1.0, 1.0, fixed_sl_distance=15.0)
    if mode == "fixed15_be":
        return SimulationConfig(
            "breakeven|SL$15|TPx1.00|BE@1.50R|Lock0.30R",
            "breakeven",
            1.0,
            1.0,
            fixed_sl_distance=15.0,
            breakeven_trigger_r=1.5,
            breakeven_lock_r=0.3,
        )
    if mode == "original_be":
        return SimulationConfig(
            "breakeven|SLx1.00|TPx1.00|BE@1.50R|Lock0.30R",
            "breakeven",
            1.0,
            1.0,
            breakeven_trigger_r=1.5,
            breakeven_lock_r=0.3,
        )
    if mode == "original_be_lock50":
        return SimulationConfig(
            "breakeven|SLx1.00|TPx1.00|BE@1.50R|Lock0.00R",
            "breakeven",
            1.0,
            1.0,
            breakeven_trigger_r=1.5,
            breakeven_lock_r=0.0,
        )
    if mode == "original_book_all_be":
        return SimulationConfig(
            "book_all|SLx1.00|TPx1.00|Exit@1.50R",
            "book_all",
            1.0,
            1.0,
            exit_trigger_r=1.5,
        )
    if mode == "original_be_partial50":
        return SimulationConfig(
            "partial_be|SLx1.00|TPx1.00|BE@1.50R|Lock0.30R|Book50%",
            "partial_be",
            1.0,
            1.0,
            breakeven_trigger_r=1.5,
            breakeven_lock_r=0.3,
            partial_close_fraction=0.5,
            partial_close_trigger_r=1.5,
        )
    if mode == "original_structure_trail":
        return SimulationConfig(
            "structure_trailing|SLx1.00|TPx1.00|Trail@1.00R|HL/LH|Pullback0.25R|Buffer0.10R",
            "structure_trailing",
            1.0,
            1.0,
            trail_trigger_r=1.0,
            structure_trailing=True,
            structure_min_pullback_r=0.25,
            structure_trail_buffer_r=0.1,
        )
    if mode == "original_book_profit15":
        return SimulationConfig(
            "book_profit|SLx1.00|TPx1.00|Exit+$15.00",
            "book_profit",
            1.0,
            1.0,
            profit_target_cash=15.0,
        )
    if mode == "original_tp_price15":
        return SimulationConfig(
            "no_trailing|SLx1.00|TP$15.00",
            "no_trailing",
            1.0,
            1.0,
            fixed_tp_distance=15.0,
        )
    if mode == "original_skip_htf_wick":
        return current_app_with_htf_wick_skip_config()
    raise ValueError(f"Unsupported fixed mode: {mode}")


def generate_optimizer_configs() -> list[SimulationConfig]:
    sl_values = [0.75, 1.0, 1.25, 1.5]
    tp_values = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    breakeven_triggers = [1.0, 1.25, 1.5, 2.0]
    breakeven_locks = [0.0, 0.1, 0.3, 0.5]
    trail_triggers = [1.5, 2.0, 2.5]
    trail_distances = [0.5, 0.75, 1.0, 1.25]

    configs: list[SimulationConfig] = []
    for sl_mult, tp_mult in product(sl_values, tp_values):
        configs.append(
            SimulationConfig(
                name=f"no_trailing|SLx{sl_mult:.2f}|TPx{tp_mult:.2f}",
                family="no_trailing",
                sl_multiplier=sl_mult,
                tp_multiplier=tp_mult,
            )
        )

    for sl_mult, tp_mult, trigger, lock in product(sl_values, tp_values, breakeven_triggers, breakeven_locks):
        configs.append(
            SimulationConfig(
                name=f"breakeven|SLx{sl_mult:.2f}|TPx{tp_mult:.2f}|BE@{trigger:.2f}R|Lock{lock:.2f}R",
                family="breakeven",
                sl_multiplier=sl_mult,
                tp_multiplier=tp_mult,
                breakeven_trigger_r=trigger,
                breakeven_lock_r=lock,
            )
        )

    for sl_mult, tp_mult, be_trigger, be_lock, trail_trigger, trail_distance in product(
        sl_values,
        tp_values,
        [1.0, 1.5],
        [0.1, 0.3],
        trail_triggers,
        trail_distances,
    ):
        configs.append(
            SimulationConfig(
                name=(
                    f"trailing|SLx{sl_mult:.2f}|TPx{tp_mult:.2f}|BE@{be_trigger:.2f}R|"
                    f"Lock{be_lock:.2f}R|Trail@{trail_trigger:.2f}R|Dist{trail_distance:.2f}R"
                ),
                family="trailing",
                sl_multiplier=sl_mult,
                tp_multiplier=tp_mult,
                breakeven_trigger_r=be_trigger,
                breakeven_lock_r=be_lock,
                trail_trigger_r=trail_trigger,
                trail_distance_r=trail_distance,
            )
        )
    return configs


def contract_size_for(symbol: str) -> float:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select contract_size from symbol_contracts where symbol = ?", (symbol,)).fetchone()
    finally:
        conn.close()
    if row and row["contract_size"]:
        return float(row["contract_size"])
    return CONTRACT_SIZE_FALLBACK if symbol.upper().startswith("XAU") else 1.0


def normalize_price_for(symbol: str, price: float) -> float:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select tick_size, digits from symbol_contracts where symbol = ?", (symbol,)).fetchone()
    finally:
        conn.close()

    normalized = float(price)
    if not row:
        return normalized
    tick_size = float(row["tick_size"] or 0.0)
    digits = row["digits"]
    if tick_size > 0:
        normalized = round(normalized / tick_size) * tick_size
    if digits is not None and int(digits) >= 0:
        normalized = round(normalized, int(digits))
    return normalized


def pnl_for_lots(trade: Trade, exit_price: float, contract_size: float, lots: float) -> float:
    if trade.side == "BUY":
        return (exit_price - trade.entry) * contract_size * lots
    return (trade.entry - exit_price) * contract_size * lots


def pnl_for(trade: Trade, exit_price: float, contract_size: float) -> float:
    return pnl_for_lots(trade, exit_price, contract_size, trade.lots)


def pnl_from_risk_cash(trade: Trade, exit_price: float) -> float:
    risk_distance = abs(trade.entry - trade.stop_loss)
    if risk_distance <= 0:
        return 0.0
    risk_cash = trade.risk_cash
    if risk_cash <= 0:
        return 0.0
    if trade.side == "BUY":
        return (exit_price - trade.entry) / risk_distance * risk_cash
    return (trade.entry - exit_price) / risk_distance * risk_cash


def simulate_trade(mt5, trade: Trade, mode: str, horizon: datetime, contract_size: float) -> SimResult:
    config = fixed_config(mode)
    return simulate_trade_incremental(mt5, trade, config, horizon, contract_size)


def cache_name_for(trade: Trade) -> str:
    safe_symbol = "".join(char if char.isalnum() else "_" for char in trade.mt5_symbol)
    opened = trade.opened_at.strftime("%Y%m%d%H%M%S")
    return f"trade_{trade.id}_{safe_symbol}_{trade.side}_{opened}.pkl"


def read_cached_marks(path: Path) -> list[Mark]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            rows = pickle.load(handle)
        return [(int(mark_time), float(mark)) for mark_time, mark in rows]
    except (EOFError, OSError, TypeError, ValueError, pickle.PickleError):
        return []


def write_cached_marks(path: Path, marks: list[Mark]) -> None:
    TICK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("wb") as handle:
        pickle.dump(marks, handle, protocol=pickle.HIGHEST_PROTOCOL)
    tmp_path.replace(path)


def fetch_trade_marks(mt5, trade: Trade, start: datetime, horizon: datetime) -> list[Mark]:
    ticks = mt5.copy_ticks_range(trade.mt5_symbol, start, horizon, mt5.COPY_TICKS_INFO)
    marks: list[Mark] = []
    if ticks is None:
        return marks
    for tick in ticks:
        bid = float(tick["bid"])
        ask = float(tick["ask"])
        if bid <= 0 or ask <= 0:
            continue
        mark = bid if trade.side == "BUY" else ask
        mark_time = int(tick["time_msc"])
        marks.append((mark_time, mark))
    return marks


def load_trade_marks(mt5, trade: Trade, horizon: datetime) -> list[Mark]:
    cache_path = TICK_CACHE_DIR / cache_name_for(trade)
    opened_ms = datetime_to_ms(trade.opened_at)
    horizon_ms = datetime_to_ms(horizon)
    refresh_cutoff_ms = datetime_to_ms(horizon - CACHE_REFRESH_GRACE)
    cached = read_cached_marks(cache_path)
    cached = [mark for mark in cached if mark[0] >= opened_ms and mark[0] <= horizon_ms]

    fetch_start = trade.opened_at
    if cached:
        latest_cached = max(mark_time for mark_time, _ in cached)
        if latest_cached >= refresh_cutoff_ms:
            return cached
        fetch_start = ms_to_datetime(latest_cached + 1)

    fresh = fetch_trade_marks(mt5, trade, fetch_start, horizon)
    if not fresh and cached:
        return cached

    merged_by_time: dict[int, float] = {mark_time: mark for mark_time, mark in cached}
    for mark_time, mark in fresh:
        if opened_ms <= mark_time <= horizon_ms:
            merged_by_time[mark_time] = mark
    merged = sorted(merged_by_time.items(), key=lambda item: item[0])
    write_cached_marks(cache_path, merged)
    return merged


def timeframe_start(value: datetime, minutes: int) -> datetime:
    minute = (value.minute // minutes) * minutes
    return value.replace(minute=minute, second=0, microsecond=0)


def tick_mid(tick) -> float | None:
    bid = float(tick["bid"]) if "bid" in tick.dtype.names else 0.0
    ask = float(tick["ask"]) if "ask" in tick.dtype.names else 0.0
    last = float(tick["last"]) if "last" in tick.dtype.names else 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if bid > 0:
        return bid
    if ask > 0:
        return ask
    if last > 0:
        return last
    return None


def forming_candle_from_ticks(mt5, symbol: str, start: datetime, end: datetime) -> dict[str, float] | None:
    if end <= start:
        return None
    ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_INFO)
    if ticks is None or len(ticks) < 3:
        return None

    prices = [price for tick in ticks if (price := tick_mid(tick)) is not None]
    if len(prices) < 3:
        return None
    return {
        "open": prices[0],
        "high": max(prices),
        "low": min(prices),
        "close": prices[-1],
    }


def wick_policy_matches(policy: str, side: str, kind: str) -> bool:
    side = side.upper()
    if policy == "any":
        return True
    if policy == "resistance":
        return kind == "resistance"
    if policy == "support":
        return kind == "support"
    if policy == "reversal":
        return (side == "SELL" and kind == "resistance") or (side == "BUY" and kind == "support")
    if policy == "opposing":
        return (side == "SELL" and kind == "support") or (side == "BUY" and kind == "resistance")
    return False


def htf_wick_reason_from_candle(
    trade: Trade,
    minutes: int,
    candle: dict[str, float] | None,
    wick_filter: HtfWickFilter,
) -> str | None:
    if candle is None:
        return None

    high = candle["high"]
    low = candle["low"]
    open_price = candle["open"]
    close = candle["close"]
    candle_range = high - low
    if candle_range <= 0:
        return None

    body_high = max(open_price, close)
    body_low = min(open_price, close)
    upper_wick = high - body_high
    lower_wick = body_low - low
    close_position = (close - low) / candle_range
    upper_fraction = upper_wick / candle_range
    lower_fraction = lower_wick / candle_range
    min_wick = trade.entry * wick_filter.min_bps / 10000.0

    resistance_wick = (
        upper_wick >= min_wick
        and upper_fraction >= wick_filter.min_fraction
        and close_position <= wick_filter.close_zone
    )
    support_wick = (
        lower_wick >= min_wick
        and lower_fraction >= wick_filter.min_fraction
        and close_position >= 1.0 - wick_filter.close_zone
    )

    if resistance_wick and wick_policy_matches(wick_filter.policy, trade.side, "resistance"):
        return (
            f"M{minutes} resistance wick forming: upper wick {upper_wick:.3f} "
            f"({upper_fraction:.0%} of candle), close position {close_position:.0%}, policy={wick_filter.policy}."
        )
    if support_wick and wick_policy_matches(wick_filter.policy, trade.side, "support"):
        return (
            f"M{minutes} support wick forming: lower wick {lower_wick:.3f} "
            f"({lower_fraction:.0%} of candle), close position {close_position:.0%}, policy={wick_filter.policy}."
        )
    return None


def htf_wick_skip_reason(mt5, trade: Trade, wick_filter: HtfWickFilter | None = None) -> str | None:
    wick_filter = wick_filter or HtfWickFilter()
    for minutes in wick_filter.timeframes:
        start = timeframe_start(trade.opened_at, minutes)
        candle = forming_candle_from_ticks(mt5, trade.mt5_symbol, start, trade.opened_at)
        reason = htf_wick_reason_from_candle(trade, minutes, candle, wick_filter)
        if reason:
            return reason
    return None


def atr_for_trade(mt5, trade: Trade, period: int = ATR_PERIOD) -> float:
    timeframe = mt5.TIMEFRAME_M5
    start = trade.opened_at - timedelta(days=7)
    rates = mt5.copy_rates_range(trade.mt5_symbol, timeframe, start, trade.opened_at)
    if rates is None:
        raise RuntimeError(f"Could not load M5 candles for ATR on trade {trade.id}: {mt5.last_error()}")

    opened_ts = int(trade.opened_at.timestamp())
    candles = [
        {
            "time": int(rate["time"]),
            "high": float(rate["high"]),
            "low": float(rate["low"]),
            "close": float(rate["close"]),
        }
        for rate in rates
        if int(rate["time"]) < opened_ts and float(rate["high"]) > 0 and float(rate["low"]) > 0 and float(rate["close"]) > 0
    ]
    candles.sort(key=lambda item: item["time"])
    if len(candles) < period + 1:
        raise RuntimeError(f"Not enough M5 candles to calculate ATR({period}) for trade {trade.id}.")

    recent = candles[-(period + 1) :]
    true_ranges: list[float] = []
    for index in range(1, len(recent)):
        current = recent[index]
        previous_close = recent[index - 1]["close"]
        true_ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous_close),
                abs(current["low"] - previous_close),
            )
        )
    return mean(true_ranges)


def atr_config_for_trade(mt5, trade: Trade) -> SimulationConfig:
    atr = atr_for_trade(mt5, trade)
    return SimulationConfig(
        name=f"atr|M5|P{ATR_PERIOD}|SL{ATR_SL_MULTIPLIER:.2f}ATR|TP{ATR_TP_MULTIPLIER:.2f}ATR|ATR{atr:.3f}",
        family="atr",
        sl_multiplier=1.0,
        tp_multiplier=1.0,
        fixed_sl_distance=atr * ATR_SL_MULTIPLIER,
        fixed_tp_distance=atr * ATR_TP_MULTIPLIER,
    )


def config_for_mode(mt5, mode: str, trade: Trade) -> SimulationConfig:
    if mode == "atr":
        return atr_config_for_trade(mt5, trade)
    if mode == "current_app":
        return current_app_config()
    if mode == "original_skip_htf_wick":
        return current_app_with_htf_wick_skip_config()
    return fixed_config(mode)


def trade_levels(trade: Trade, config: SimulationConfig) -> tuple[float, float, float]:
    original_risk = abs(trade.entry - trade.stop_loss)
    original_reward = abs(trade.take_profit - trade.entry)
    risk = float(config.fixed_sl_distance) if config.fixed_sl_distance is not None else original_risk * config.sl_multiplier
    reward = float(config.fixed_tp_distance) if config.fixed_tp_distance is not None else original_reward * config.tp_multiplier
    if trade.side == "BUY":
        stop_loss = trade.entry - risk
        take_profit = trade.entry + reward
    else:
        stop_loss = trade.entry + risk
        take_profit = trade.entry - reward
    return stop_loss, take_profit, risk


def simulate_trade_from_marks(
    trade: Trade,
    config: SimulationConfig,
    marks: list[Mark],
    horizon: datetime,
    contract_size: float,
) -> SimResult:
    stop_loss, take_profit, risk_distance = trade_levels(trade, config)
    current_sl = stop_loss
    profit_target_price: float | None = None
    if config.profit_target_cash is not None and contract_size > 0 and trade.lots > 0:
        profit_delta = float(config.profit_target_cash) / (contract_size * trade.lots)
        profit_target_price = trade.entry + profit_delta if trade.side == "BUY" else trade.entry - profit_delta
    outcome = "OPEN_TO_LAST"
    exit_price: float | None = None
    exit_time: datetime | None = None
    last_mark: float | None = None
    ticks_checked = 0
    partial_fraction = 0.0
    partial_exit_price: float | None = None
    partial_exit_time: datetime | None = None
    partial_pnl = 0.0
    structure_active = False
    structure_extreme = trade.entry
    structure_pullback: float | None = None
    trailed_by_structure = False

    for mark_time_ms, mark in marks:
        ticks_checked += 1
        last_mark = mark
        hit_sl = mark <= current_sl if trade.side == "BUY" else mark >= current_sl
        hit_tp = mark >= take_profit if trade.side == "BUY" else mark <= take_profit
        hit_profit_target = (
            profit_target_price is not None
            and (mark >= profit_target_price if trade.side == "BUY" else mark <= profit_target_price)
        )
        profit_target_is_closer = (
            hit_profit_target
            and (not hit_tp or abs(float(profit_target_price) - trade.entry) <= abs(take_profit - trade.entry))
        )
        if hit_sl or hit_tp or hit_profit_target:
            if hit_sl:
                outcome = "TRAIL_SL" if trailed_by_structure else "SL"
                exit_price = current_sl
            elif profit_target_is_closer:
                outcome = "BOOK_PROFIT"
                exit_price = float(profit_target_price)
            else:
                outcome = "TP"
                exit_price = take_profit
            exit_time = ms_to_datetime(mark_time_ms)
            break

        if risk_distance <= 0:
            continue
        favorable = mark - trade.entry if trade.side == "BUY" else trade.entry - mark
        current_r = favorable / risk_distance
        if config.exit_trigger_r is not None and current_r >= config.exit_trigger_r:
            outcome = "BOOK_ALL"
            exit_price = mark
            exit_time = ms_to_datetime(mark_time_ms)
            break

        partial_trigger = config.partial_close_trigger_r
        if partial_trigger is None:
            partial_trigger = config.breakeven_trigger_r
        if (
            partial_fraction <= 0.0
            and config.partial_close_fraction is not None
            and partial_trigger is not None
            and current_r >= partial_trigger
        ):
            partial_fraction = max(0.0, min(1.0, float(config.partial_close_fraction)))
            partial_exit_price = mark
            partial_exit_time = ms_to_datetime(mark_time_ms)
            partial_pnl = pnl_for_lots(trade, mark, contract_size, trade.lots * partial_fraction)

        desired_sl: float | None = None
        if config.breakeven_trigger_r is not None and current_r >= config.breakeven_trigger_r:
            lock_r = float(config.breakeven_lock_r or 0.0)
            desired_sl = trade.entry + (risk_distance * lock_r) if trade.side == "BUY" else trade.entry - (risk_distance * lock_r)
        if config.trail_trigger_r is not None and config.trail_distance_r is not None and current_r >= config.trail_trigger_r:
            distance = float(config.trail_distance_r or 0.0)
            trail_sl = mark - (risk_distance * distance) if trade.side == "BUY" else mark + (risk_distance * distance)
            if desired_sl is None:
                desired_sl = trail_sl
            elif trade.side == "BUY":
                desired_sl = max(desired_sl, trail_sl)
            else:
                desired_sl = min(desired_sl, trail_sl)
        if desired_sl is not None:
            if trade.side == "BUY" and desired_sl > current_sl:
                current_sl = desired_sl
            elif trade.side == "SELL" and desired_sl < current_sl:
                current_sl = desired_sl

        if config.structure_trailing and config.trail_trigger_r is not None and current_r >= config.trail_trigger_r:
            if not structure_active:
                structure_active = True
                structure_extreme = mark
                structure_pullback = None
                continue

            min_pullback = risk_distance * max(0.0, float(config.structure_min_pullback_r))
            buffer = risk_distance * max(0.0, float(config.structure_trail_buffer_r))
            if trade.side == "BUY":
                if mark > structure_extreme:
                    if structure_pullback is not None and structure_extreme - structure_pullback >= min_pullback:
                        structure_sl = structure_pullback - buffer
                        if structure_sl > current_sl:
                            current_sl = structure_sl
                            trailed_by_structure = True
                    structure_extreme = mark
                    structure_pullback = None
                else:
                    structure_pullback = mark if structure_pullback is None else min(structure_pullback, mark)
            else:
                if mark < structure_extreme:
                    if structure_pullback is not None and structure_pullback - structure_extreme >= min_pullback:
                        structure_sl = structure_pullback + buffer
                        if structure_sl < current_sl:
                            current_sl = structure_sl
                            trailed_by_structure = True
                    structure_extreme = mark
                    structure_pullback = None
                else:
                    structure_pullback = mark if structure_pullback is None else max(structure_pullback, mark)

    if exit_price is None:
        exit_price = last_mark if last_mark is not None else trade.entry
        exit_time = horizon

    remaining_fraction = 1.0 - partial_fraction
    final_pnl = pnl_for_lots(trade, exit_price, contract_size, trade.lots * remaining_fraction)
    total_pnl = partial_pnl + final_pnl
    if partial_fraction > 0.0:
        outcome = f"PARTIAL+{outcome}"

    return SimResult(
        trade=trade,
        tp_mode=config.name,
        simulated_tp=take_profit,
        simulated_sl=stop_loss,
        outcome=outcome,
        exit_price=exit_price,
        exit_time=exit_time,
        pnl=total_pnl,
        ticks_checked=ticks_checked,
        partial_fraction=partial_fraction,
        partial_exit_price=partial_exit_price,
        partial_exit_time=partial_exit_time,
        partial_pnl=partial_pnl,
    )


def simulate_current_app_from_marks(
    trade: Trade,
    config: SimulationConfig,
    marks: list[Mark],
    horizon: datetime,
    contract_size: float,
) -> SimResult:
    stop_loss, take_profit, risk_distance = trade_levels(trade, config)
    current_sl = stop_loss
    outcome = "OPEN_TO_LAST"
    exit_price: float | None = None
    exit_time: datetime | None = None
    last_mark: float | None = None
    ticks_checked = 0
    poll_ms = max(1, int(config.poll_interval_seconds * 1000))
    next_check_ms = datetime_to_ms(trade.opened_at)

    for mark_time_ms, mark in marks:
        if mark_time_ms < next_check_ms:
            continue
        ticks_checked += 1
        next_check_ms = mark_time_ms + poll_ms
        mark = normalize_price_for(trade.mt5_symbol, mark)
        last_mark = mark

        if risk_distance > 0:
            favorable = mark - trade.entry if trade.side == "BUY" else trade.entry - mark
            current_r = favorable / risk_distance
            if current_r > 0:
                desired_sl: float | None = None
                if config.breakeven_trigger_r is not None and current_r >= config.breakeven_trigger_r:
                    lock_r = float(config.breakeven_lock_r or 0.0)
                    desired_sl = (
                        trade.entry + (risk_distance * lock_r)
                        if trade.side == "BUY"
                        else trade.entry - (risk_distance * lock_r)
                    )
                if config.trail_trigger_r is not None and config.trail_distance_r is not None and current_r >= config.trail_trigger_r:
                    distance = float(config.trail_distance_r or 0.0)
                    trail_sl = mark - (risk_distance * distance) if trade.side == "BUY" else mark + (risk_distance * distance)
                    if desired_sl is None:
                        desired_sl = trail_sl
                    elif trade.side == "BUY":
                        desired_sl = max(desired_sl, trail_sl)
                    else:
                        desired_sl = min(desired_sl, trail_sl)

                if desired_sl is not None:
                    improves = desired_sl > current_sl if trade.side == "BUY" else desired_sl < current_sl
                    min_move = trade.entry * (config.min_stop_update_bps / 10000.0)
                    meaningful = abs(desired_sl - current_sl) >= min_move
                    if improves and meaningful:
                        current_sl = normalize_price_for(trade.mt5_symbol, desired_sl)

        hit_sl = mark <= current_sl if trade.side == "BUY" else mark >= current_sl
        hit_tp = mark >= take_profit if trade.side == "BUY" else mark <= take_profit
        if hit_sl or hit_tp:
            outcome = "SL" if hit_sl else "TP"
            exit_price = mark
            exit_time = ms_to_datetime(mark_time_ms)
            break

    if exit_price is None:
        exit_price = last_mark if last_mark is not None else trade.entry
        exit_time = horizon

    pnl = pnl_from_risk_cash(trade, exit_price)
    if pnl == 0.0:
        pnl = pnl_for(trade, exit_price, contract_size)

    return SimResult(
        trade=trade,
        tp_mode=config.name,
        simulated_tp=take_profit,
        simulated_sl=stop_loss,
        outcome=outcome,
        exit_price=exit_price,
        exit_time=exit_time,
        pnl=pnl,
        ticks_checked=ticks_checked,
    )


def simulate_trade_incremental(
    mt5,
    trade: Trade,
    config: SimulationConfig,
    horizon: datetime,
    contract_size: float,
    chunk: timedelta = SIMULATION_CHUNK,
) -> SimResult:
    if config.skip_htf_wick:
        skip_reason = htf_wick_skip_reason(mt5, trade, config.htf_wick_filter)
        if skip_reason:
            stop_loss, take_profit, _ = trade_levels(trade, config)
            return SimResult(
                trade=trade,
                tp_mode=config.name,
                simulated_tp=take_profit,
                simulated_sl=stop_loss,
                outcome="SKIPPED_HTF_WICK",
                exit_price=trade.entry,
                exit_time=trade.opened_at,
                pnl=0.0,
                ticks_checked=0,
                skip_reason=skip_reason,
            )

    chunk_end = min(trade.opened_at + chunk, horizon)
    last_result: SimResult | None = None

    while True:
        marks = load_trade_marks(mt5, trade, chunk_end)
        if config.family == "current_app":
            last_result = simulate_current_app_from_marks(trade, config, marks, chunk_end, contract_size)
        else:
            last_result = simulate_trade_from_marks(trade, config, marks, chunk_end, contract_size)
        if not last_result.outcome.endswith("OPEN_TO_LAST") or chunk_end >= horizon:
            return last_result
        chunk_end = min(chunk_end + chunk, horizon)


def stats(results: list[SimResult]) -> dict[str, float | int | str]:
    skipped = [result for result in results if result.outcome.startswith("SKIPPED")]
    traded = [result for result in results if not result.outcome.startswith("SKIPPED")]
    pnls = [result.pnl for result in traded]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

    return {
        "trades": len(traded),
        "skipped": len(skipped),
        "wins": len(wins),
        "losses": len(losses),
        "tp_hits": sum(1 for result in traded if result.outcome.endswith("TP")),
        "sl_hits": sum(1 for result in traded if result.outcome.endswith("SL")),
        "open_to_last": sum(1 for result in traded if result.outcome.endswith("OPEN_TO_LAST")),
        "win_rate_pct": (len(wins) / len(traded) * 100.0) if traded else 0.0,
        "net_pnl": sum(pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss else (float("inf") if gross_profit > 0 else 0.0),
        "avg_trade": mean(pnls) if pnls else 0.0,
        "avg_win": mean(wins) if wins else 0.0,
        "avg_loss": mean(losses) if losses else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "max_drawdown": max_drawdown,
    }


def money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def number(value: float) -> str:
    return f"{value:,.2f}"


def print_results(results: list[SimResult], title: str) -> None:
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)
    header = f"{'ID':>4} {'Side':>4} {'Outcome':>18} {'Entry':>10} {'SL':>10} {'TP':>10} {'Exit':>10} {'PnL':>12}"
    print(header)
    print("-" * len(header))
    for result in results:
        trade = result.trade
        print(
            f"{trade.id:>4} {trade.side:>4} {result.outcome:>18} "
            f"{trade.entry:>10.3f} {result.simulated_sl:>10.3f} {result.simulated_tp:>10.3f} "
            f"{result.exit_price:>10.3f} {money(result.pnl):>12}"
        )

    summary = stats(results)
    print("-" * len(header))
    print(
        f"Trades: {summary['trades']} | Skipped: {summary['skipped']} | Wins: {summary['wins']} | Losses: {summary['losses']} | "
        f"Win rate: {summary['win_rate_pct']:.2f}%"
    )
    print(
        f"Net: {money(float(summary['net_pnl']))} | Gross profit: {money(float(summary['gross_profit']))} | "
        f"Gross loss: {money(float(summary['gross_loss']))} | Profit factor: {summary['profit_factor']:.2f}"
    )
    print(
        f"Avg trade: {money(float(summary['avg_trade']))} | Avg win: {money(float(summary['avg_win']))} | "
        f"Avg loss: {money(float(summary['avg_loss']))} | Max DD: {money(float(summary['max_drawdown']))}"
    )
    skipped = [result for result in results if result.skip_reason]
    if skipped:
        print()
        print("Skipped trades:")
        for result in skipped:
            print(f"  #{result.trade.id}: {result.skip_reason}")


def fixed_mode_label(mode: str) -> str:
    labels = {
        "original": "Original TP, no trailing",
        "double": "Double TP, no trailing",
        "half_tp": "Original SL, half TP distance, no trailing",
        "fixed15": "Fixed $15 SL, original TP, no trailing",
        "fixed15_be": "Fixed $15 SL, original TP, with breakeven",
        "original_be": "Original SL/TP, with breakeven",
        "original_be_lock50": "Original SL/TP, breakeven lock 0R",
        "original_book_all_be": "Original SL/TP, book all at breakeven trigger",
        "original_be_partial50": "Original SL/TP, breakeven + book 50%",
        "original_single_active": "Original SL/TP, no trailing, one active trade only",
        "atr": f"ATR({ATR_PERIOD}) M5 SL {ATR_SL_MULTIPLIER:.2f}x, TP {ATR_TP_MULTIPLIER:.2f}x, no trailing",
        "current_app": "Current app SL/TP + breakeven/trailing",
        "original_structure_trail": "Original SL/TP, trail by higher low / lower high",
        "original_book_profit15": "Original SL/TP, book 100% profit at $15",
        "original_tp_price15": "Original SL, TP at entry +/- $15 price",
        "original_skip_htf_wick": "Current app SL/TP + breakeven/trailing, skip M15/M30 wick",
        "optimize_htf_wick_skip": "Find best M15/M30 wick skip filter for current app management",
    }
    return labels.get(mode, mode)


def print_optimization_rows(rows: list[OptimizationRow], *, limit: int = 50) -> None:
    print()
    print("=" * 150)
    print("Optimization ranking by net PnL")
    print("=" * 150)
    header = (
        f"{'Rank':>4} {'Family':>11} {'Net':>12} {'PF':>6} {'Win%':>7} {'TP':>4} {'SL':>4} "
        f"{'MaxDD':>12} {'Setting':<80}"
    )
    print(header)
    print("-" * len(header))
    for rank, row in enumerate(rows[:limit], start=1):
        s = row.stats
        print(
            f"{rank:>4} {row.config.family:>11} {money(float(s['net_pnl'])):>12} "
            f"{float(s['profit_factor']):>6.2f} {float(s['win_rate_pct']):>6.2f}% "
            f"{int(s['tp_hits']):>4} {int(s['sl_hits']):>4} {money(float(s['max_drawdown'])):>12} "
            f"{row.config.name:<80}"
        )
    if len(rows) > limit:
        print(f"...showing top {limit} of {len(rows)} settings.")


def simulate_single_active(
    mt5,
    selected: list[Trade],
    horizon_by_symbol: dict[str, datetime],
    contract_sizes: dict[str, float],
) -> tuple[list[SimResult], list[tuple[Trade, SimResult]]]:
    config = fixed_config("original")
    results: list[SimResult] = []
    skipped: list[tuple[Trade, SimResult]] = []
    active_result: SimResult | None = None
    active_until: datetime | None = None

    for trade in sorted(selected, key=lambda item: (item.opened_at, item.id)):
        if active_result is not None and active_until is not None and trade.opened_at < active_until:
            skipped.append((trade, active_result))
            continue

        horizon = horizon_by_symbol[trade.mt5_symbol]
        result = simulate_trade_incremental(
            mt5,
            trade,
            config,
            horizon,
            contract_sizes[trade.mt5_symbol],
        )
        results.append(result)
        active_result = result
        active_until = result.exit_time or horizon

    return results, skipped


def print_skipped_trades(skipped: list[tuple[Trade, SimResult]]) -> None:
    print()
    print(f"Skipped because another simulated trade was still running: {len(skipped)}")
    if not skipped:
        return

    header = f"{'ID':>4} {'Side':>4} {'Opened UTC':>25} {'Active ID':>9} {'Active Exit UTC':>25}"
    print(header)
    print("-" * len(header))
    for skipped_trade, active_result in skipped:
        active_exit = active_result.exit_time.isoformat() if active_result.exit_time else ""
        print(
            f"{skipped_trade.id:>4} {skipped_trade.side:>4} {skipped_trade.opened_at.isoformat():>25} "
            f"{active_result.trade.id:>9} {active_exit:>25}"
        )


def optimize_settings(
    selected: list[Trade],
    marks_by_trade: dict[int, list[Mark]],
    horizon_by_symbol: dict[str, datetime],
    contract_sizes: dict[str, float],
) -> list[OptimizationRow]:
    rows: list[OptimizationRow] = []
    configs = generate_optimizer_configs()
    print(f"Testing {len(configs)} settings across {len(selected)} trade(s)...")
    for cfg in configs:
        results = [
            simulate_trade_from_marks(
                trade,
                cfg,
                marks_by_trade[trade.id],
                horizon_by_symbol[trade.mt5_symbol],
                contract_sizes[trade.mt5_symbol],
            )
            for trade in selected
        ]
        rows.append(OptimizationRow(cfg, stats(results)))
    rows.sort(key=lambda row: float(row.stats["net_pnl"]), reverse=True)
    return rows


def generate_wick_filters() -> list[HtfWickFilter]:
    filters: list[HtfWickFilter] = []
    for timeframes, policy, min_fraction, close_zone, min_bps in product(
        HTF_WICK_TIMEFRAME_SETS,
        HTF_WICK_POLICIES,
        [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60],
        [0.20, 0.25, 0.30, 0.35, 0.40],
        [1.0, 2.0, 3.0, 5.0],
    ):
        filters.append(
            HtfWickFilter(
                timeframes=tuple(timeframes),
                min_fraction=float(min_fraction),
                close_zone=float(close_zone),
                min_bps=float(min_bps),
                policy=str(policy),
            )
        )
    return filters


def load_htf_wick_candles(mt5, selected: list[Trade]) -> dict[int, dict[int, dict[str, float] | None]]:
    candles: dict[int, dict[int, dict[str, float] | None]] = {}
    all_timeframes = sorted({minutes for item in HTF_WICK_TIMEFRAME_SETS for minutes in item})
    for trade in selected:
        candles[trade.id] = {}
        for minutes in all_timeframes:
            start = timeframe_start(trade.opened_at, minutes)
            candles[trade.id][minutes] = forming_candle_from_ticks(mt5, trade.mt5_symbol, start, trade.opened_at)
    return candles


def wick_skip_reason_from_preloaded(
    trade: Trade,
    candles: dict[int, dict[int, dict[str, float] | None]],
    wick_filter: HtfWickFilter,
) -> str | None:
    trade_candles = candles.get(trade.id, {})
    for minutes in wick_filter.timeframes:
        reason = htf_wick_reason_from_candle(trade, minutes, trade_candles.get(minutes), wick_filter)
        if reason:
            return reason
    return None


def optimize_htf_wick_filters(
    mt5,
    selected: list[Trade],
    horizon_by_symbol: dict[str, datetime],
    contract_sizes: dict[str, float],
) -> tuple[list[WickOptimizationRow], list[SimResult]]:
    base_config = current_app_config()
    print(f"Simulating option 11 baseline across {len(selected)} trade(s)...")
    base_results = [
        simulate_trade_incremental(
            mt5,
            trade,
            base_config,
            horizon_by_symbol[trade.mt5_symbol],
            contract_sizes[trade.mt5_symbol],
        )
        for trade in selected
    ]
    baseline_positive_pnl = sum(result.pnl for result in base_results if result.pnl > 0)

    candles = load_htf_wick_candles(mt5, selected)
    filters = generate_wick_filters()
    print(f"Testing {len(filters)} wick skip filters...")
    rows: list[WickOptimizationRow] = []
    for wick_filter in filters:
        filtered_results: list[SimResult] = []
        skipped_results: list[SimResult] = []
        for result in base_results:
            reason = wick_skip_reason_from_preloaded(result.trade, candles, wick_filter)
            if reason:
                skipped = replace(
                    result,
                    outcome="SKIPPED_HTF_WICK",
                    exit_price=result.trade.entry,
                    exit_time=result.trade.opened_at,
                    pnl=0.0,
                    ticks_checked=0,
                    skip_reason=reason,
                )
                filtered_results.append(skipped)
                skipped_results.append(result)
            else:
                filtered_results.append(result)

        if not skipped_results:
            continue
        loss_removed = sum(abs(result.pnl) for result in skipped_results if result.pnl < 0)
        profit_removed = sum(result.pnl for result in skipped_results if result.pnl > 0)
        skipped_losers = sum(1 for result in skipped_results if result.pnl < 0)
        skipped_winners = sum(1 for result in skipped_results if result.pnl > 0)
        net_improvement = loss_removed - profit_removed
        preserve_score = loss_removed - (3.0 * profit_removed)
        positive_pnl_kept_pct = (
            max(0.0, (baseline_positive_pnl - profit_removed) / baseline_positive_pnl * 100.0)
            if baseline_positive_pnl > 0
            else 100.0
        )
        row_stats = stats(filtered_results)
        skipped_ids = ",".join(str(result.trade.id) for result in skipped_results[:20])
        if len(skipped_results) > 20:
            skipped_ids = f"{skipped_ids},..."
        rows.append(
            WickOptimizationRow(
                wick_filter=wick_filter,
                stats=row_stats,
                skipped=len(skipped_results),
                skipped_losers=skipped_losers,
                skipped_winners=skipped_winners,
                loss_removed=loss_removed,
                profit_removed=profit_removed,
                net_improvement=net_improvement,
                preserve_score=preserve_score,
                positive_pnl_kept_pct=positive_pnl_kept_pct,
                skipped_ids=skipped_ids,
            )
        )

    rows.sort(
        key=lambda row: (
            row.preserve_score,
            row.positive_pnl_kept_pct,
            -row.skipped_winners,
            row.loss_removed,
            row.net_improvement,
        ),
        reverse=True,
    )
    return rows, base_results


def print_wick_optimization_rows(rows: list[WickOptimizationRow], base_results: list[SimResult], *, limit: int = 50) -> None:
    base_summary = stats(base_results)
    print()
    print("=" * 170)
    print("M15/M30 wick skip optimizer for current app management")
    print("=" * 170)
    print(
        f"Baseline option 11: Trades {base_summary['trades']} | Net {money(float(base_summary['net_pnl']))} | "
        f"Win rate {base_summary['win_rate_pct']:.2f}% | Max DD {money(float(base_summary['max_drawdown']))}"
    )
    header = (
        f"{'Rank':>4} {'Score':>10} {'Net+':>10} {'Net':>12} {'Keep+':>7} {'Skip':>5} {'Bad':>4} {'Good':>5} "
        f"{'Loss rm':>12} {'Profit rm':>12} {'TF':>7} {'Policy':>10} {'Wick%':>7} {'Close':>6} {'Bps':>5} {'IDs':<28}"
    )
    print(header)
    print("-" * len(header))
    for rank, row in enumerate(rows[:limit], start=1):
        filt = row.wick_filter
        tf = "/".join(f"M{minutes}" for minutes in filt.timeframes)
        print(
            f"{rank:>4} {money(row.preserve_score):>10} {money(row.net_improvement):>10} "
            f"{money(float(row.stats['net_pnl'])):>12} {row.positive_pnl_kept_pct:>6.1f}% "
            f"{row.skipped:>5} {row.skipped_losers:>4} {row.skipped_winners:>5} "
            f"{money(row.loss_removed):>12} {money(row.profit_removed):>12} "
            f"{tf:>7} {filt.policy:>10} {filt.min_fraction:>6.0%} {filt.close_zone:>5.0%} "
            f"{filt.min_bps:>5.1f} {row.skipped_ids:<28}"
        )
    if len(rows) > limit:
        print(f"...showing top {limit} of {len(rows)} filters.")


def show_available_trades(trades: Iterable[Trade]) -> None:
    print()
    print("Executed trades available in DB:")
    print(f"{'ID':>4} {'Status':>8} {'MT5':>8} {'Side':>4} {'Opened UTC':>25} {'Actual PnL':>12}")
    print("-" * 72)
    for trade in trades:
        print(
            f"{trade.id:>4} {trade.status:>8} {trade.mt5_symbol:>8} {trade.side:>4} "
            f"{trade.opened_at.isoformat():>25} {money(trade.actual_pnl):>12}"
        )


def main() -> None:
    print("MT5 no-trailing trade simulator")
    print("Read-only: uses trades from data/trading_agent.db and MT5 tick history.")
    trades = load_trades()
    if not trades:
        print("No trades found.")
        return

    show_available_trades(trades)
    raw_selection = input("\nSelect trades: all, one id, or ranges like 38,40-49 [all]: ")
    selected_ids = parse_trade_selection(raw_selection, trades)
    analysis_mode = choose_analysis_mode()
    selected = [trade for trade in trades if trade.id in set(selected_ids)]

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit("MetaTrader5 package is not installed in this Python environment.") from exc

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        latest_by_symbol: dict[str, datetime] = {}
        contract_sizes = {trade.mt5_symbol: contract_size_for(trade.mt5_symbol) for trade in selected}
        for symbol in {trade.mt5_symbol for trade in selected}:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(f"No latest MT5 tick available for {symbol}.")
            raw_time = getattr(tick, "time_msc", 0) or (getattr(tick, "time", 0) * 1000)
            latest_by_symbol[symbol] = datetime.fromtimestamp(raw_time / 1000, tz=timezone.utc)

        if analysis_mode == "original_single_active":
            mode_results, skipped = simulate_single_active(mt5, selected, latest_by_symbol, contract_sizes)
            print_results(mode_results, fixed_mode_label(analysis_mode))
            print_skipped_trades(skipped)
            return

        if analysis_mode == "optimize":
            marks_by_trade = {
                trade.id: load_trade_marks(mt5, trade, latest_by_symbol[trade.mt5_symbol])
                for trade in selected
            }
            optimization_rows = optimize_settings(selected, marks_by_trade, latest_by_symbol, contract_sizes)
            print_optimization_rows(optimization_rows)
            return

        if analysis_mode == "optimize_htf_wick_skip":
            wick_rows, base_results = optimize_htf_wick_filters(mt5, selected, latest_by_symbol, contract_sizes)
            print_wick_optimization_rows(wick_rows, base_results)
            return

        for mode in [analysis_mode]:
            mode_results = [
                simulate_trade_incremental(
                    mt5,
                    trade,
                    config_for_mode(mt5, mode, trade),
                    latest_by_symbol[trade.mt5_symbol],
                    contract_sizes[trade.mt5_symbol],
                )
                for trade in selected
            ]
            print_results(mode_results, fixed_mode_label(mode))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print()
        print(f"Error: {exc}")
