from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any

from .config import ROOT


DB_PATH = ROOT / "data" / "trading_agent.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout = 60000")
        try:
            conn.execute("pragma journal_mode = wal")
        except sqlite3.OperationalError:
            pass
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists trades (
                    id integer primary key autoincrement,
                    opened_at text not null,
                    closed_at text,
                    symbol text not null,
                    mt5_symbol text,
                    side text not null,
                    strategy text not null,
                    setup_tier text,
                    entry real not null,
                    initial_stop_loss real,
                    stop_loss real not null,
                    take_profit real not null,
                    lots real not null default 0,
                    risk_cash real not null default 0,
                    status text not null,
                    exit_price real,
                    pnl real not null default 0,
                    rr real not null default 0,
                    score real not null default 0,
                    execution_mode text,
                    broker_requested_price real,
                    broker_fill_price real,
                    broker_order_id integer,
                    broker_deal_id integer,
                    broker_position_id integer,
                    broker_retcode integer,
                    broker_message text,
                    hybrid_mt5_status text,
                    hybrid_mt5_requested_price real,
                    hybrid_mt5_fill_price real,
                    hybrid_mt5_stop_loss real,
                    hybrid_mt5_take_profit real,
                    hybrid_mt5_exit_price real,
                    hybrid_mt5_pnl real not null default 0,
                    hybrid_mt5_order_id integer,
                    hybrid_mt5_deal_id integer,
                    hybrid_mt5_position_id integer,
                    hybrid_mt5_retcode integer,
                    hybrid_mt5_message text,
                    hybrid_mt5_closed_at text,
                    reason text,
                    exit_reason text
                );

                create table if not exists candidates (
                    id integer primary key autoincrement,
                    created_at text not null,
                    symbol text not null,
                    side text not null,
                    strategy text not null,
                    setup_tier text,
                    entry real not null,
                    stop_loss real not null,
                    take_profit real not null,
                    rr real not null,
                    score real not null,
                    liquidity_score real not null,
                    flow_score real not null,
                    regime_score real not null,
                    planned_lots real not null default 0,
                    raw_lots real not null default 0,
                    risk_cash real not null default 0,
                    loss_per_lot real not null default 0,
                    sizing_reason text,
                    decision text not null,
                    reason text
                );

                create table if not exists symbol_contracts (
                    symbol text primary key,
                    scanned_at text not null,
                    contract_size real,
                    tick_size real,
                    tick_value real,
                    tick_value_loss real,
                    volume_min real,
                    volume_max real,
                    volume_step real,
                    stops_level real,
                    digits integer,
                    raw_json text
                );

                create table if not exists events (
                    id integer primary key autoincrement,
                    created_at text not null,
                    level text not null,
                    source text not null,
                    message text not null
                );

                create table if not exists liquidity_memory (
                    symbol text not null,
                    price_low real not null,
                    price_high real not null,
                    label text not null,
                    first_seen real not null,
                    last_seen real not null,
                    sample_count integer not null,
                    current_bid_notional real not null default 0,
                    current_ask_notional real not null default 0,
                    current_notional real not null default 0,
                    max_notional_seen real not null default 0,
                    ema_notional real not null default 0,
                    previous_notional real not null default 0,
                    stack_rate real not null default 0,
                    pull_rate real not null default 0,
                    pull_count integer not null default 0,
                    last_pull_ts real,
                    traded_through_count integer not null default 0,
                    last_mid real,
                    absorption_score real not null default 0,
                    memory_score real not null default 0,
                    primary key (symbol, price_low)
                );

                create table if not exists historical_klines (
                    symbol text not null,
                    source text not null,
                    interval text not null,
                    open_time integer not null,
                    close_time integer not null,
                    open real not null,
                    high real not null,
                    low real not null,
                    close real not null,
                    volume real not null default 0,
                    quote_volume real not null default 0,
                    taker_buy_volume real not null default 0,
                    trade_count integer not null default 0,
                    raw_json text,
                    primary key (symbol, source, interval, open_time)
                );

                create table if not exists historical_liquidity_levels (
                    symbol text not null,
                    source text not null,
                    level_type text not null,
                    timeframe text not null,
                    price_low real not null,
                    price_high real not null,
                    label text not null,
                    side text not null,
                    strength real not null default 0,
                    score real not null default 0,
                    touches integer not null default 0,
                    first_seen integer,
                    last_touch integer,
                    volume real not null default 0,
                    metadata_json text,
                    updated_at text not null,
                    primary key (symbol, source, level_type, timeframe, price_low, price_high)
                );

                create table if not exists historical_backfill_runs (
                    id integer primary key autoincrement,
                    symbol text not null,
                    source text not null,
                    interval text not null,
                    started_at text not null,
                    updated_at text not null,
                    status text not null,
                    start_time integer,
                    end_time integer,
                    last_open_time integer,
                    rows_fetched integer not null default 0,
                    error text,
                    unique(symbol, source, interval)
                );

                create table if not exists historical_maintenance_runs (
                    symbol text not null,
                    task text not null,
                    updated_at text not null,
                    last_run_ms integer,
                    rows_affected integer not null default 0,
                    status text not null,
                    error text,
                    primary key (symbol, task)
                );
                """
            )
            conn.execute(
                "create index if not exists idx_historical_klines_symbol_time on historical_klines(symbol, interval, open_time)"
            )
            conn.execute(
                "create index if not exists idx_historical_levels_symbol_score on historical_liquidity_levels(symbol, score)"
            )
            self._ensure_column(conn, "trades", "execution_mode", "text")
            self._ensure_column(conn, "trades", "broker_requested_price", "real")
            self._ensure_column(conn, "trades", "broker_fill_price", "real")
            self._ensure_column(conn, "trades", "broker_order_id", "integer")
            self._ensure_column(conn, "trades", "broker_deal_id", "integer")
            self._ensure_column(conn, "trades", "broker_position_id", "integer")
            self._ensure_column(conn, "trades", "broker_retcode", "integer")
            self._ensure_column(conn, "trades", "broker_message", "text")
            self._ensure_column(conn, "trades", "hybrid_mt5_status", "text")
            self._ensure_column(conn, "trades", "hybrid_mt5_requested_price", "real")
            self._ensure_column(conn, "trades", "hybrid_mt5_fill_price", "real")
            self._ensure_column(conn, "trades", "hybrid_mt5_stop_loss", "real")
            self._ensure_column(conn, "trades", "hybrid_mt5_take_profit", "real")
            self._ensure_column(conn, "trades", "hybrid_mt5_exit_price", "real")
            self._ensure_column(conn, "trades", "hybrid_mt5_pnl", "real not null default 0")
            self._ensure_column(conn, "trades", "hybrid_mt5_order_id", "integer")
            self._ensure_column(conn, "trades", "hybrid_mt5_deal_id", "integer")
            self._ensure_column(conn, "trades", "hybrid_mt5_position_id", "integer")
            self._ensure_column(conn, "trades", "hybrid_mt5_retcode", "integer")
            self._ensure_column(conn, "trades", "hybrid_mt5_message", "text")
            self._ensure_column(conn, "trades", "hybrid_mt5_closed_at", "text")
            self._ensure_column(conn, "trades", "initial_stop_loss", "real")
            self._ensure_column(conn, "trades", "exit_reason", "text")
            self._ensure_column(conn, "trades", "setup_tier", "text")
            conn.execute("update trades set initial_stop_loss = stop_loss where initial_stop_loss is null")
            self._ensure_column(conn, "candidates", "setup_tier", "text")
            self._ensure_column(conn, "candidates", "planned_lots", "real not null default 0")
            self._ensure_column(conn, "candidates", "raw_lots", "real not null default 0")
            self._ensure_column(conn, "candidates", "risk_cash", "real not null default 0")
            self._ensure_column(conn, "candidates", "loss_per_lot", "real not null default 0")
            self._ensure_column(conn, "candidates", "sizing_reason", "text")
            for column, column_type in (
                ("current_bid_notional", "real not null default 0"),
                ("current_ask_notional", "real not null default 0"),
                ("current_notional", "real not null default 0"),
                ("max_notional_seen", "real not null default 0"),
                ("ema_notional", "real not null default 0"),
                ("previous_notional", "real not null default 0"),
                ("stack_rate", "real not null default 0"),
                ("pull_rate", "real not null default 0"),
                ("pull_count", "integer not null default 0"),
                ("last_pull_ts", "real"),
                ("traded_through_count", "integer not null default 0"),
                ("last_mid", "real"),
                ("absorption_score", "real not null default 0"),
                ("memory_score", "real not null default 0"),
            ):
                self._ensure_column(conn, "liquidity_memory", column, column_type)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        existing = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"alter table {table} add column {column} {column_type}")

    def add_event(self, level: str, source: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into events (created_at, level, source, message) values (?, ?, ?, ?)",
                (utc_now(), level, source, message),
            )

    def add_candidate(self, candidate: dict[str, Any], decision: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into candidates (
                    created_at, symbol, side, strategy, setup_tier, entry, stop_loss, take_profit,
                    rr, score, liquidity_score, flow_score, regime_score,
                    planned_lots, raw_lots, risk_cash, loss_per_lot, sizing_reason,
                    decision, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    candidate["symbol"],
                    candidate["side"],
                    candidate["strategy"],
                    candidate.get("a_plus_tier"),
                    candidate["entry"],
                    candidate["stop_loss"],
                    candidate["take_profit"],
                    candidate["rr"],
                    candidate["score"],
                    candidate["liquidity_score"],
                    candidate["flow_score"],
                    candidate["regime_score"],
                    candidate.get("planned_lots", 0.0),
                    candidate.get("raw_lots", 0.0),
                    candidate.get("risk_cash", 0.0),
                    candidate.get("loss_per_lot", 0.0),
                    candidate.get("sizing_reason"),
                    decision,
                    reason,
                ),
            )

    def open_trade(self, trade: dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into trades (
                    opened_at, symbol, mt5_symbol, side, strategy, setup_tier, entry, initial_stop_loss, stop_loss,
                    take_profit, lots, risk_cash, status, rr, score, execution_mode,
                    broker_requested_price, broker_fill_price, broker_order_id, broker_deal_id,
                    broker_position_id, broker_retcode, broker_message, reason
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    trade["symbol"],
                    trade.get("mt5_symbol"),
                    trade["side"],
                    trade["strategy"],
                    trade.get("a_plus_tier") or trade.get("setup_tier"),
                    trade["entry"],
                    trade.get("initial_stop_loss", trade["stop_loss"]),
                    trade["stop_loss"],
                    trade["take_profit"],
                    trade.get("lots", 0),
                    trade.get("risk_cash", 0),
                    "OPEN",
                    trade["rr"],
                    trade["score"],
                    trade.get("execution_mode"),
                    trade.get("broker_requested_price"),
                    trade.get("broker_fill_price"),
                    trade.get("broker_order_id"),
                    trade.get("broker_deal_id"),
                    trade.get("broker_position_id"),
                    trade.get("broker_retcode"),
                    trade.get("broker_message"),
                    trade.get("reason"),
                ),
            )
            return int(cursor.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, exit_reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set closed_at = ?, exit_price = ?, pnl = ?, status = 'CLOSED',
                    exit_reason = coalesce(?, exit_reason)
                where id = ?
                """,
                (utc_now(), exit_price, pnl, exit_reason, trade_id),
            )

    def update_trade_close_result(self, trade_id: int, exit_price: float, pnl: float, exit_reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set closed_at = coalesce(closed_at, ?),
                    exit_price = ?,
                    pnl = ?,
                    status = 'CLOSED',
                    exit_reason = coalesce(?, exit_reason)
                where id = ?
                """,
                (utc_now(), exit_price, pnl, exit_reason, trade_id),
            )

    def update_trade_stop_loss(self, trade_id: int, stop_loss: float, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set stop_loss = ?,
                    broker_message = coalesce(?, broker_message)
                where id = ? and status = 'OPEN'
                """,
                (stop_loss, message, trade_id),
            )

    def update_hybrid_mt5_open(
        self,
        trade_id: int,
        *,
        requested_price: float,
        fill_price: float,
        stop_loss: float,
        take_profit: float,
        order_id: int | None,
        deal_id: int | None,
        position_id: int | None,
        retcode: int | None,
        message: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set hybrid_mt5_status = 'OPEN',
                    hybrid_mt5_requested_price = ?,
                    hybrid_mt5_fill_price = ?,
                    hybrid_mt5_stop_loss = ?,
                    hybrid_mt5_take_profit = ?,
                    hybrid_mt5_order_id = ?,
                    hybrid_mt5_deal_id = ?,
                    hybrid_mt5_position_id = ?,
                    hybrid_mt5_retcode = ?,
                    hybrid_mt5_message = ?
                where id = ?
                """,
                (
                    requested_price,
                    fill_price,
                    stop_loss,
                    take_profit,
                    order_id,
                    deal_id,
                    position_id,
                    retcode,
                    message,
                    trade_id,
                ),
            )

    def update_hybrid_mt5_failure(self, trade_id: int, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set hybrid_mt5_status = 'FAILED',
                    hybrid_mt5_message = ?
                where id = ?
                """,
                (message, trade_id),
            )

    def update_hybrid_mt5_stop_loss(self, trade_id: int, stop_loss: float, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set hybrid_mt5_stop_loss = ?,
                    hybrid_mt5_message = coalesce(?, hybrid_mt5_message)
                where id = ? and hybrid_mt5_status = 'OPEN'
                """,
                (stop_loss, message, trade_id),
            )

    def update_hybrid_mt5_close_result(self, trade_id: int, exit_price: float, pnl: float, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update trades
                set hybrid_mt5_status = 'CLOSED',
                    hybrid_mt5_closed_at = coalesce(hybrid_mt5_closed_at, ?),
                    hybrid_mt5_exit_price = ?,
                    hybrid_mt5_pnl = ?,
                    hybrid_mt5_message = coalesce(?, hybrid_mt5_message)
                where id = ?
                """,
                (utc_now(), exit_price, pnl, message, trade_id),
            )

    def get_open_trades(self) -> list[dict[str, Any]]:
        return self.query("select * from trades where status = 'OPEN' order by opened_at desc")

    def get_open_hybrid_mt5_trades(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            return self.query(
                """
                select * from trades
                where hybrid_mt5_status = 'OPEN' and symbol = ?
                order by opened_at desc
                """,
                (symbol,),
            )
        return self.query(
            """
            select * from trades
            where hybrid_mt5_status = 'OPEN'
            order by opened_at desc
            """
        )

    def count_open_trades(self, symbol: str | None = None, side: str | None = None) -> int:
        conditions = ["status = 'OPEN'"]
        params: list[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if side:
            conditions.append("upper(side) = upper(?)")
            params.append(side)
        rows = self.query(f"select count(*) as count from trades where {' and '.join(conditions)}", tuple(params))
        return int(rows[0]["count"])

    def save_contract(self, symbol: str, spec: dict[str, Any]) -> None:
        import json

        with self.connect() as conn:
            conn.execute(
                """
                insert into symbol_contracts (
                    symbol, scanned_at, contract_size, tick_size, tick_value,
                    tick_value_loss, volume_min, volume_max, volume_step,
                    stops_level, digits, raw_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol) do update set
                    scanned_at = excluded.scanned_at,
                    contract_size = excluded.contract_size,
                    tick_size = excluded.tick_size,
                    tick_value = excluded.tick_value,
                    tick_value_loss = excluded.tick_value_loss,
                    volume_min = excluded.volume_min,
                    volume_max = excluded.volume_max,
                    volume_step = excluded.volume_step,
                    stops_level = excluded.stops_level,
                    digits = excluded.digits,
                    raw_json = excluded.raw_json
                """,
                (
                    symbol,
                    utc_now(),
                    spec.get("trade_contract_size"),
                    spec.get("trade_tick_size"),
                    spec.get("trade_tick_value"),
                    spec.get("trade_tick_value_loss"),
                    spec.get("volume_min"),
                    spec.get("volume_max"),
                    spec.get("volume_step"),
                    spec.get("trade_stops_level"),
                    spec.get("digits"),
                    json.dumps(spec, sort_keys=True),
                ),
            )

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def load_liquidity_memory(self, symbol: str) -> list[dict[str, Any]]:
        return self.query(
            """
            select * from liquidity_memory
            where symbol = ?
            order by price_low desc
            """,
            (symbol,),
        )

    def save_liquidity_memory(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into liquidity_memory (
                    symbol, price_low, price_high, label, first_seen, last_seen,
                    sample_count, current_bid_notional, current_ask_notional,
                    current_notional, max_notional_seen, ema_notional,
                    previous_notional, stack_rate, pull_rate, pull_count,
                    last_pull_ts, traded_through_count, last_mid,
                    absorption_score, memory_score
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, price_low) do update set
                    price_high = excluded.price_high,
                    label = excluded.label,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    sample_count = excluded.sample_count,
                    current_bid_notional = excluded.current_bid_notional,
                    current_ask_notional = excluded.current_ask_notional,
                    current_notional = excluded.current_notional,
                    max_notional_seen = excluded.max_notional_seen,
                    ema_notional = excluded.ema_notional,
                    previous_notional = excluded.previous_notional,
                    stack_rate = excluded.stack_rate,
                    pull_rate = excluded.pull_rate,
                    pull_count = excluded.pull_count,
                    last_pull_ts = excluded.last_pull_ts,
                    traded_through_count = excluded.traded_through_count,
                    last_mid = excluded.last_mid,
                    absorption_score = excluded.absorption_score,
                    memory_score = excluded.memory_score
                """,
                [
                    (
                        row["symbol"],
                        row["price_low"],
                        row["price_high"],
                        row["label"],
                        row["first_seen"],
                        row["last_seen"],
                        row["sample_count"],
                        row["current_bid_notional"],
                        row["current_ask_notional"],
                        row["current_notional"],
                        row["max_notional_seen"],
                        row["ema_notional"],
                        row["previous_notional"],
                        row["stack_rate"],
                        row["pull_rate"],
                        row["pull_count"],
                        row.get("last_pull_ts"),
                        row["traded_through_count"],
                        row.get("last_mid"),
                        row["absorption_score"],
                        row["memory_score"],
                    )
                    for row in rows
                ],
            )

    def prune_liquidity_memory(self, cutoff_ts: float) -> None:
        with self.connect() as conn:
            conn.execute("delete from liquidity_memory where last_seen < ?", (cutoff_ts,))

    def upsert_historical_klines(self, candles: list[dict[str, Any]]) -> int:
        if not candles:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                insert into historical_klines (
                    symbol, source, interval, open_time, close_time, open, high, low, close,
                    volume, quote_volume, taker_buy_volume, trade_count, raw_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, source, interval, open_time) do update set
                    close_time = excluded.close_time,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    quote_volume = excluded.quote_volume,
                    taker_buy_volume = excluded.taker_buy_volume,
                    trade_count = excluded.trade_count,
                    raw_json = excluded.raw_json
                """,
                [
                    (
                        row["symbol"],
                        row["source"],
                        row.get("interval", "1m"),
                        int(row["open_time"]),
                        int(row["close_time"]),
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume", 0.0)),
                        float(row.get("quote_volume", 0.0)),
                        float(row.get("taker_buy_volume", 0.0)),
                        int(row.get("trade_count", 0)),
                        row.get("raw_json") if isinstance(row.get("raw_json"), str) else json.dumps(row.get("raw_json", {}), sort_keys=True),
                    )
                    for row in candles
                ],
            )
        return len(candles)

    def latest_historical_kline_open_time(self, symbol: str, source: str, interval: str = "1m") -> int | None:
        rows = self.query(
            """
            select max(open_time) as open_time
            from historical_klines
            where symbol = ? and source = ? and interval = ?
            """,
            (symbol, source, interval),
        )
        value = rows[0]["open_time"] if rows else None
        return int(value) if value is not None else None

    def earliest_historical_kline_open_time(self, symbol: str, source: str, interval: str = "1m") -> int | None:
        rows = self.query(
            """
            select min(open_time) as open_time
            from historical_klines
            where symbol = ? and source = ? and interval = ?
            """,
            (symbol, source, interval),
        )
        value = rows[0]["open_time"] if rows else None
        return int(value) if value is not None else None

    def historical_kline_count(self, symbol: str, source: str | None = None, interval: str = "1m") -> int:
        conditions = ["symbol = ?", "interval = ?"]
        params: list[Any] = [symbol, interval]
        if source:
            conditions.append("source = ?")
            params.append(source)
        rows = self.query(
            f"select count(*) as count from historical_klines where {' and '.join(conditions)}",
            tuple(params),
        )
        return int(rows[0]["count"] or 0)

    def load_historical_klines(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        source: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["symbol = ?", "interval = ?"]
        params: list[Any] = [symbol, interval]
        if source:
            conditions.append("source = ?")
            params.append(source)
        if start_time is not None:
            conditions.append("open_time >= ?")
            params.append(int(start_time))
        if end_time is not None:
            conditions.append("open_time <= ?")
            params.append(int(end_time))
        sql = f"select * from historical_klines where {' and '.join(conditions)} order by open_time"
        if limit is not None:
            sql += " limit ?"
            params.append(int(limit))
        return self.query(sql, tuple(params))

    def prune_historical_klines(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        source: str | None = None,
        before_open_time: int,
    ) -> int:
        conditions = ["symbol = ?", "interval = ?", "open_time < ?"]
        params: list[Any] = [symbol, interval, int(before_open_time)]
        if source:
            conditions.append("source = ?")
            params.append(source)
        with self.connect() as conn:
            cursor = conn.execute(
                f"delete from historical_klines where {' and '.join(conditions)}",
                tuple(params),
            )
            return int(cursor.rowcount or 0)

    def replace_historical_levels(self, symbol: str, levels: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("delete from historical_liquidity_levels where symbol = ?", (symbol,))
            if not levels:
                return
            conn.executemany(
                """
                insert into historical_liquidity_levels (
                    symbol, source, level_type, timeframe, price_low, price_high, label, side,
                    strength, score, touches, first_seen, last_touch, volume, metadata_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, source, level_type, timeframe, price_low, price_high) do update set
                    label = excluded.label,
                    side = excluded.side,
                    strength = excluded.strength,
                    score = excluded.score,
                    touches = excluded.touches,
                    first_seen = excluded.first_seen,
                    last_touch = excluded.last_touch,
                    volume = excluded.volume,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        level["symbol"],
                        level["source"],
                        level["level_type"],
                        level["timeframe"],
                        float(level["price_low"]),
                        float(level["price_high"]),
                        level["label"],
                        level["side"],
                        float(level.get("strength", 0.0)),
                        float(level.get("score", 0.0)),
                        int(level.get("touches", 0)),
                        level.get("first_seen"),
                        level.get("last_touch"),
                        float(level.get("volume", 0.0)),
                        json.dumps(level.get("metadata", {}), sort_keys=True),
                        utc_now(),
                    )
                    for level in levels
                ],
            )

    def upsert_historical_levels(self, levels: list[dict[str, Any]]) -> int:
        if not levels:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                insert into historical_liquidity_levels (
                    symbol, source, level_type, timeframe, price_low, price_high, label, side,
                    strength, score, touches, first_seen, last_touch, volume, metadata_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, source, level_type, timeframe, price_low, price_high) do update set
                    label = excluded.label,
                    side = excluded.side,
                    strength = max(historical_liquidity_levels.strength, excluded.strength),
                    score = max(historical_liquidity_levels.score, excluded.score),
                    touches = historical_liquidity_levels.touches + excluded.touches,
                    first_seen = case
                        when historical_liquidity_levels.first_seen is null then excluded.first_seen
                        when excluded.first_seen is null then historical_liquidity_levels.first_seen
                        else min(historical_liquidity_levels.first_seen, excluded.first_seen)
                    end,
                    last_touch = case
                        when historical_liquidity_levels.last_touch is null then excluded.last_touch
                        when excluded.last_touch is null then historical_liquidity_levels.last_touch
                        else max(historical_liquidity_levels.last_touch, excluded.last_touch)
                    end,
                    volume = historical_liquidity_levels.volume + excluded.volume,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        level["symbol"],
                        level["source"],
                        level["level_type"],
                        level["timeframe"],
                        float(level["price_low"]),
                        float(level["price_high"]),
                        level["label"],
                        level["side"],
                        float(level.get("strength", 0.0)),
                        float(level.get("score", 0.0)),
                        int(level.get("touches", 0)),
                        level.get("first_seen"),
                        level.get("last_touch"),
                        float(level.get("volume", 0.0)),
                        json.dumps(level.get("metadata", {}), sort_keys=True),
                        utc_now(),
                    )
                    for level in levels
                ],
            )
        return len(levels)

    def load_historical_levels(self, symbol: str, *, min_score: float = 0.0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            select * from historical_liquidity_levels
            where symbol = ? and score >= ?
            order by score desc, last_touch desc
        """
        params: list[Any] = [symbol, float(min_score)]
        if limit is not None:
            sql += " limit ?"
            params.append(int(limit))
        rows = self.query(sql, tuple(params))
        for row in rows:
            raw = row.get("metadata_json")
            try:
                row["metadata"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                row["metadata"] = {}
        return rows

    def update_historical_backfill_run(
        self,
        *,
        symbol: str,
        source: str,
        interval: str,
        status: str,
        start_time: int | None = None,
        end_time: int | None = None,
        last_open_time: int | None = None,
        rows_fetched: int = 0,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into historical_backfill_runs (
                    symbol, source, interval, started_at, updated_at, status,
                    start_time, end_time, last_open_time, rows_fetched, error
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, source, interval) do update set
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    start_time = coalesce(excluded.start_time, historical_backfill_runs.start_time),
                    end_time = coalesce(excluded.end_time, historical_backfill_runs.end_time),
                    last_open_time = coalesce(excluded.last_open_time, historical_backfill_runs.last_open_time),
                    rows_fetched = historical_backfill_runs.rows_fetched + excluded.rows_fetched,
                    error = excluded.error
                """,
                (
                    symbol,
                    source,
                    interval,
                    now,
                    now,
                    status,
                    start_time,
                    end_time,
                    last_open_time,
                    int(rows_fetched),
                    error,
                ),
            )

    def historical_backfill_status(self) -> dict[str, Any]:
        runs = self.query("select * from historical_backfill_runs order by symbol, source, interval")
        maintenance = self.query("select * from historical_maintenance_runs order by symbol, task")
        candles = self.query(
            """
            select symbol, source, interval, count(*) as candles,
                   min(open_time) as first_open_time, max(open_time) as last_open_time
            from historical_klines
            group by symbol, source, interval
            order by symbol, source, interval
            """
        )
        levels = self.query(
            """
            select symbol, count(*) as levels, max(score) as max_score
            from historical_liquidity_levels
            group by symbol
            order by symbol
            """
        )
        return {"runs": runs, "maintenance": maintenance, "candles": candles, "levels": levels}

    def latest_historical_maintenance_run(self, symbol: str, task: str) -> dict[str, Any] | None:
        rows = self.query(
            "select * from historical_maintenance_runs where symbol = ? and task = ?",
            (symbol, task),
        )
        return rows[0] if rows else None

    def update_historical_maintenance_run(
        self,
        *,
        symbol: str,
        task: str,
        status: str,
        last_run_ms: int | None = None,
        rows_affected: int = 0,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into historical_maintenance_runs (
                    symbol, task, updated_at, last_run_ms, rows_affected, status, error
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, task) do update set
                    updated_at = excluded.updated_at,
                    last_run_ms = coalesce(excluded.last_run_ms, historical_maintenance_runs.last_run_ms),
                    rows_affected = excluded.rows_affected,
                    status = excluded.status,
                    error = excluded.error
                """,
                (symbol, task, now, last_run_ms, int(rows_affected), status, error),
            )

    def stats(self) -> dict[str, Any]:
        rows = self.query(
            """
            select
                count(*) as total_trades,
                sum(case when status = 'OPEN' then 1 else 0 end) as open_trades,
                sum(case when status = 'CLOSED' then 1 else 0 end) as closed_trades,
                sum(case when status = 'CLOSED' and pnl > 0 then 1 else 0 end) as winning_trades,
                sum(case when status = 'CLOSED' and pnl < 0 then 1 else 0 end) as losing_trades,
                coalesce(sum(pnl), 0) as net_pnl,
                coalesce(sum(case when pnl > 0 then pnl else 0 end), 0) as gross_profit,
                coalesce(abs(sum(case when pnl < 0 then pnl else 0 end)), 0) as gross_loss,
                coalesce(avg(case when status = 'CLOSED' then pnl end), 0) as avg_pnl,
                max(case when status = 'CLOSED' then pnl end) as max_profit_trade,
                min(case when status = 'CLOSED' then pnl end) as max_loss_trade
            from trades
            """
        )[0]
        gross_loss = float(rows["gross_loss"] or 0)
        gross_profit = float(rows["gross_profit"] or 0)
        closed_trades = int(rows["closed_trades"] or 0)
        winning_trades = int(rows["winning_trades"] or 0)
        rows["profit_factor"] = round(gross_profit / gross_loss, 3) if gross_loss else None
        rows["win_rate"] = round((winning_trades / closed_trades) * 100.0, 2) if closed_trades else None
        rows["max_drawdown"] = self._max_closed_trade_drawdown()
        rows["hybrid_mt5"] = self.hybrid_mt5_stats()
        return rows

    def hybrid_mt5_stats(self) -> dict[str, Any]:
        rows = self.query(
            """
            select
                count(case when hybrid_mt5_status is not null then 1 end) as total_trades,
                sum(case when hybrid_mt5_status = 'OPEN' then 1 else 0 end) as open_trades,
                sum(case when hybrid_mt5_status = 'CLOSED' then 1 else 0 end) as closed_trades,
                sum(case when hybrid_mt5_status = 'CLOSED' and hybrid_mt5_pnl > 0 then 1 else 0 end) as winning_trades,
                sum(case when hybrid_mt5_status = 'CLOSED' and hybrid_mt5_pnl < 0 then 1 else 0 end) as losing_trades,
                coalesce(sum(case when hybrid_mt5_status = 'CLOSED' then hybrid_mt5_pnl else 0 end), 0) as net_pnl,
                coalesce(sum(case when hybrid_mt5_status = 'CLOSED' and hybrid_mt5_pnl > 0 then hybrid_mt5_pnl else 0 end), 0) as gross_profit,
                coalesce(abs(sum(case when hybrid_mt5_status = 'CLOSED' and hybrid_mt5_pnl < 0 then hybrid_mt5_pnl else 0 end)), 0) as gross_loss,
                coalesce(avg(case when hybrid_mt5_status = 'CLOSED' then hybrid_mt5_pnl end), 0) as avg_pnl,
                max(case when hybrid_mt5_status = 'CLOSED' then hybrid_mt5_pnl end) as max_profit_trade,
                min(case when hybrid_mt5_status = 'CLOSED' then hybrid_mt5_pnl end) as max_loss_trade
            from trades
            """
        )[0]
        gross_loss = float(rows["gross_loss"] or 0)
        gross_profit = float(rows["gross_profit"] or 0)
        closed_trades = int(rows["closed_trades"] or 0)
        winning_trades = int(rows["winning_trades"] or 0)
        rows["profit_factor"] = round(gross_profit / gross_loss, 3) if gross_loss else None
        rows["win_rate"] = round((winning_trades / closed_trades) * 100.0, 2) if closed_trades else None
        rows["max_drawdown"] = self._max_hybrid_mt5_drawdown()
        return rows

    def _max_hybrid_mt5_drawdown(self) -> float:
        closed = self.query(
            """
            select hybrid_mt5_pnl as pnl
            from trades
            where hybrid_mt5_status = 'CLOSED'
            order by hybrid_mt5_closed_at, id
            """
        )
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for row in closed:
            equity += float(row["pnl"] or 0.0)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return round(max_drawdown, 2)

    def _max_closed_trade_drawdown(self) -> float:
        closed = self.query(
            """
            select pnl
            from trades
            where status = 'CLOSED'
            order by closed_at, id
            """
        )
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for row in closed:
            equity += float(row["pnl"] or 0.0)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return round(max_drawdown, 2)
