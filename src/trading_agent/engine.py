from __future__ import annotations

from datetime import datetime, timezone
from collections import deque
import math
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from .broker.mt5 import (
    account_equity,
    calculate_lot_size,
    close_position,
    closed_position_result,
    modify_position_sl_tp,
    normalize_price,
    open_positions,
    scan_symbol,
    send_market_order,
    symbol_tick,
)
from .config import enabled_symbols, load_config
from .data_quality import validate_book_features
from .market.binance import BinanceMarketData
from .market.websocket import BinanceWebSocketFeed
from .power import KeepAwakeGuard
from .risk import RiskEngine
from .state import StateStore
from .historical_liquidity import HistoricalLiquidityService, historical_levels_as_major_zones
from .strategy.liquidity import compute_features
from .strategy.liquidity_memory import LiquidityMemoryEngine
from .strategy.thesis import LiquidityThesisEngine
from .telegram import TelegramClient


class TradingEngine:
    def __init__(self, store: StateStore):
        self.store = store
        self.config = load_config()
        self.telegram = TelegramClient(self.config, store, self.status)
        self.market = self._build_market_adapter()
        self.feed: BinanceWebSocketFeed | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_features: dict[str, dict[str, Any]] = {}
        self.last_quality: dict[str, dict[str, Any]] = {}
        self.pending_setups: dict[str, dict[str, Any]] = {}
        self.thesis_engine = LiquidityThesisEngine()
        self.liquidity_memory = LiquidityMemoryEngine(store)
        self.historical_liquidity = HistoricalLiquidityService(store, self.config, market=self.market)
        self.keep_awake = KeepAwakeGuard()
        self.oi_history: dict[str, deque[dict[str, float]]] = {}
        self.last_oi_poll: dict[str, float] = {}
        self.session_vwap: dict[str, dict[str, Any]] = {}
        self.session_volume_profile: dict[str, dict[float, dict[str, float]]] = {}
        self.session_vwap_bootstrapped: set[str] = set()
        self.session_vwap_anchor_start_ms: dict[str, int] = {}
        self.session_vwap_live_start_ms: dict[str, int] = {}
        self.last_vwap_reconcile: dict[str, float] = {}
        self.seen_session_trade_ids: dict[str, set[Any]] = {}
        self.seen_session_trade_order: dict[str, deque[Any]] = {}
        self.execution_prices: dict[str, dict[str, Any]] = {}
        self.structure_bias: dict[str, dict[str, Any]] = {}
        self.last_historical_update_poll: dict[str, float] = {}
        self.last_historical_level_rebuild: dict[str, float] = {}
        self.historical_liquidity_status: dict[str, Any] = {}
        self.watchdog_status: dict[str, Any] = {
            "enabled": False,
            "market_ok": True,
            "market_message": "Not checked yet.",
            "mt5_ok": True,
            "mt5_message": "Not checked yet.",
            "mt5_failures": 0,
            "live_entries_blocked": False,
            "last_market_restart_at": None,
            "last_mt5_check_at": None,
            "last_mt5_restart_attempt_at": None,
            "last_mt5_restart_message": None,
            "mt5_restart_attempts": 0,
            "next_mt5_restart_interval_seconds": None,
        }
        self._last_mt5_watchdog_check = 0.0
        self._mt5_watchdog_failures = 0
        self._watchdog_live_entries_blocked = False
        self._last_market_feed_restart = 0.0
        self._last_mt5_restart_attempt = 0.0
        self._mt5_restart_attempts = 0
        self._watchdog_alert_times: dict[str, float] = {}
        self.last_error: str | None = None
        self.started_at: str | None = None

    def reload_config(self) -> None:
        with self.lock:
            self.config = load_config()
            self.telegram.update_config(self.config)
            self.market = self._build_market_adapter()
            self.historical_liquidity = HistoricalLiquidityService(self.store, self.config, market=self.market)
        if self.is_running():
            self._start_market_feed()
        if self.telegram.chat_enabled:
            self.telegram.start_chat()
        else:
            self.telegram.stop_chat()
        self.store.add_event("INFO", "engine", "Configuration reloaded.")

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self._enable_keep_awake()
        self.stop_event.clear()
        self._start_market_feed()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.thread = threading.Thread(target=self._run, name="trading-agent-engine", daemon=True)
        self.thread.start()
        self.store.add_event("INFO", "engine", "Engine started.")
        return True

    def stop(self) -> bool:
        if not self.thread or not self.thread.is_alive():
            return False
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.feed:
            self.feed.stop()
        self._disable_keep_awake()
        self.store.add_event("INFO", "engine", "Engine stopped.")
        return True

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "started_at": self.started_at,
            "last_error": self.last_error,
            "execution_mode": self.config.get("execution", {}).get("mode"),
            "market_data": self._market_status(),
            "keep_awake": {"active": self.keep_awake.active, "reason": self.keep_awake.last_reason},
            "symbols": enabled_symbols(self.config),
            "last_features": self.last_features,
            "last_quality": self.last_quality,
            "pending_setups": self.pending_setups,
            "locked_theses": self.thesis_engine.snapshot(),
            "execution_prices": self.execution_prices,
            "open_interest": {symbol: list(history) for symbol, history in self.oi_history.items()},
            "session_vwap": self.session_vwap,
            "historical_liquidity": self.historical_liquidity_status or self.historical_liquidity.status(),
            "watchdog": self.watchdog_status,
        }

    def scan_mt5_contracts(self) -> list[dict[str, Any]]:
        results = []
        for symbol in enabled_symbols(self.config):
            mt5_symbol = symbol.get("mt5_symbol") or symbol["name"]
            spec = scan_symbol(mt5_symbol)
            results.append({"symbol": symbol["name"], "mt5_symbol": mt5_symbol, "spec": spec})
            if spec.get("available"):
                self.store.save_contract(mt5_symbol, spec)
        return results

    def start_historical_backfill(self, years: int | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
        def worker() -> None:
            result = self.historical_liquidity.backfill_all(years=years, symbols=symbols, rebuild_levels=True)
            self.historical_liquidity_status = self.historical_liquidity.status()
            self.store.add_event(
                "INFO" if result.get("ok") else "WARN",
                "historical_liquidity",
                f"Historical liquidity backfill finished: {result}",
            )

        thread = threading.Thread(target=worker, name="historical-liquidity-backfill", daemon=True)
        thread.start()
        return {"ok": True, "started": True, "status": self.historical_liquidity.status()}

    def _build_market_adapter(self) -> BinanceMarketData:
        market_cfg = self.config.get("market_data", {})
        return BinanceMarketData(
            base_url=str(market_cfg.get("rest_base_url", "https://fapi.binance.com")),
            market=str(market_cfg.get("market", "um_futures")),
        )

    def _start_market_feed(self) -> None:
        market_cfg = self.config.get("market_data", {})
        if market_cfg.get("mode", "websocket") != "websocket":
            return
        symbols = [symbol.get("data_symbol", symbol["name"]) for symbol in enabled_symbols(self.config)]
        if not symbols:
            return
        if self.feed:
            self.feed.stop()
        self.feed = BinanceWebSocketFeed(
            symbols,
            snapshot_limit=int(market_cfg.get("snapshot_limit", 1000)),
            trade_buffer_size=int(market_cfg.get("trade_buffer_size", 300)),
            market=str(market_cfg.get("market", "um_futures")),
            rest_base_url=str(market_cfg.get("rest_base_url", "https://fapi.binance.com")),
            ws_host=str(market_cfg.get("ws_host", "fstream.binance.com")),
            ws_port=int(market_cfg.get("ws_port", 443)),
        )
        self.feed.start()
        self.store.add_event(
            "INFO",
            "market_data",
            f"Binance {market_cfg.get('market', 'um_futures')} WebSocket feed started for {', '.join(symbols)}.",
        )

    def _market_status(self) -> dict[str, Any]:
        market_cfg = self.config.get("market_data", {})
        if self.feed:
            return self.feed.status(float(market_cfg.get("stale_after_seconds", 5.0)))
        return {"mode": market_cfg.get("mode", "websocket"), "connected": False, "symbols": {}}

    def _watchdog_cycle(self, config: dict[str, Any]) -> None:
        cfg = config.get("watchdog", {})
        enabled = bool(cfg.get("enabled", True))
        if not enabled:
            self._watchdog_live_entries_blocked = False
            self.watchdog_status = {
                **self.watchdog_status,
                "enabled": False,
                "live_entries_blocked": False,
                "market_message": "Watchdog disabled.",
                "mt5_message": "Watchdog disabled.",
            }
            return

        now = time.time()
        self._watchdog_market_feed(config, cfg, now)
        self._watchdog_mt5(config, cfg, now)
        self.watchdog_status = {
            **self.watchdog_status,
            "enabled": True,
            "live_entries_blocked": self._watchdog_live_entries_blocked,
            "mt5_failures": self._mt5_watchdog_failures,
        }

    def _watchdog_market_feed(self, config: dict[str, Any], cfg: dict[str, Any], now: float) -> None:
        market_cfg = config.get("market_data", {})
        if market_cfg.get("mode", "websocket") != "websocket":
            self.watchdog_status.update({"market_ok": True, "market_message": "Market watchdog skipped: market_data.mode is not websocket."})
            return
        if not enabled_symbols(config):
            self.watchdog_status.update({"market_ok": True, "market_message": "Market watchdog skipped: no enabled symbols."})
            return

        status = self._market_status()
        symbols = status.get("symbols") or {}
        stale_after = float(cfg.get("market_stale_seconds", 30.0))
        stale_symbols: list[str] = []
        for symbol, details in symbols.items():
            age = details.get("age_seconds")
            stale = age is None or float(age) > stale_after
            if stale or not details.get("in_sync"):
                stale_symbols.append(str(symbol))

        connected = bool(status.get("connected"))
        market_ok = connected and not stale_symbols
        if market_ok:
            self.watchdog_status.update({"market_ok": True, "market_message": "Market feed healthy."})
            return

        reason = "not connected" if not connected else f"stale/out-of-sync symbols: {', '.join(stale_symbols)}"
        self.watchdog_status.update({"market_ok": False, "market_message": reason})
        cooldown = float(cfg.get("market_restart_cooldown_seconds", 30.0))
        if now - self._last_market_feed_restart < cooldown:
            self._watchdog_alert("market_stale", "WARN", f"Watchdog: market feed unhealthy ({reason}); waiting for restart cooldown.", cfg)
            return

        self._last_market_feed_restart = now
        self.watchdog_status["last_market_restart_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._watchdog_alert("market_restart", "WARN", f"Watchdog restarting Binance websocket feed: {reason}", cfg)
        self._start_market_feed()

    def _watchdog_mt5(self, config: dict[str, Any], cfg: dict[str, Any], now: float) -> None:
        execution = config.get("execution", {})
        mode = str(execution.get("mode", ""))
        needs_mt5 = mode.startswith("mt5") or mode == "hybrid"
        if not needs_mt5:
            self._mt5_watchdog_failures = 0
            self._watchdog_live_entries_blocked = False
            self.watchdog_status.update({"mt5_ok": True, "mt5_message": "MT5 watchdog skipped: execution mode does not use live MT5."})
            return

        interval = float(cfg.get("mt5_check_interval_seconds", 30.0))
        if now - self._last_mt5_watchdog_check < interval:
            return
        self._last_mt5_watchdog_check = now
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ok, equity, message = account_equity()
        self.watchdog_status["last_mt5_check_at"] = checked_at
        if ok:
            was_blocked = self._watchdog_live_entries_blocked
            self._mt5_watchdog_failures = 0
            self._mt5_restart_attempts = 0
            self._watchdog_live_entries_blocked = False
            self.watchdog_status.update(
                {
                    "mt5_ok": True,
                    "mt5_message": f"MT5 healthy. Equity {equity:.2f}.",
                    "mt5_restart_attempts": 0,
                    "next_mt5_restart_interval_seconds": None,
                }
            )
            if was_blocked:
                self._watchdog_alert("mt5_recovered", "INFO", "Watchdog: MT5 health recovered; live entries unblocked.", cfg)
            return

        self._mt5_watchdog_failures += 1
        threshold = max(1, int(cfg.get("mt5_failure_threshold", 3)))
        should_block = bool(cfg.get("block_live_entries_on_mt5_failure", True)) and self._mt5_watchdog_failures >= threshold
        self._watchdog_live_entries_blocked = should_block
        status_message = f"{message} (failure {self._mt5_watchdog_failures}/{threshold})"
        self.watchdog_status.update({"mt5_ok": False, "mt5_message": status_message})
        if should_block:
            restart_message = self._maybe_restart_mt5_terminal(cfg, now)
            notify_interval = float(cfg.get("mt5_failure_notify_interval_seconds", cfg.get("alert_cooldown_seconds", 300.0)))
            details = f" Restart: {restart_message}" if restart_message else ""
            self._watchdog_alert(
                "mt5_blocked",
                "ERROR",
                f"Watchdog: MT5 unhealthy; live entries blocked. {status_message}.{details}",
                cfg,
                cooldown_seconds=notify_interval,
            )
        else:
            self._watchdog_alert("mt5_warning", "WARN", f"Watchdog: MT5 health check failed. {status_message}", cfg)

    def _maybe_restart_mt5_terminal(self, cfg: dict[str, Any], now: float) -> str | None:
        if not cfg.get("mt5_restart_enabled", True):
            return "disabled by watchdog.mt5_restart_enabled=false."
        interval = self._mt5_restart_backoff_interval(cfg)
        self.watchdog_status["next_mt5_restart_interval_seconds"] = round(interval, 3)
        if now - self._last_mt5_restart_attempt < interval:
            remaining = max(0.0, interval - (now - self._last_mt5_restart_attempt))
            return f"waiting {remaining:.0f}s for restart interval."

        self._last_mt5_restart_attempt = now
        self._mt5_restart_attempts += 1
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ok, message = self._launch_mt5_terminal(cfg)
        self.watchdog_status["last_mt5_restart_attempt_at"] = attempted_at
        self.watchdog_status["last_mt5_restart_message"] = message
        self.watchdog_status["mt5_restart_attempts"] = self._mt5_restart_attempts
        self.watchdog_status["next_mt5_restart_interval_seconds"] = round(self._mt5_restart_backoff_interval(cfg), 3)
        level = "INFO" if ok else "WARN"
        self.store.add_event(level, "watchdog", f"MT5 restart attempt: {message}")
        return message

    def _mt5_restart_backoff_interval(self, cfg: dict[str, Any]) -> float:
        base = max(1.0, float(cfg.get("mt5_restart_interval_seconds", 120.0)))
        multiplier = max(1.0, float(cfg.get("mt5_restart_backoff_multiplier", 2.0)))
        max_interval = max(base, float(cfg.get("mt5_restart_max_interval_seconds", 900.0)))
        interval = base * (multiplier ** max(0, self._mt5_restart_attempts - 1))
        return min(interval, max_interval)

    def _launch_mt5_terminal(self, cfg: dict[str, Any]) -> tuple[bool, str]:
        configured_path = str(cfg.get("mt5_terminal_path") or "").strip()
        if not configured_path:
            return False, "watchdog.mt5_terminal_path is not configured."

        terminal_path = Path(configured_path).expanduser()
        if not terminal_path.exists():
            return False, f"MT5 terminal path does not exist: {terminal_path}"
        if not terminal_path.is_file():
            return False, f"MT5 terminal path is not a file: {terminal_path}"

        try:
            subprocess.Popen(
                [str(terminal_path)],
                cwd=str(terminal_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            return False, f"failed to launch MT5 terminal: {exc}"
        return True, f"launched MT5 terminal: {terminal_path}"

    def _watchdog_live_entry_block_reason(self) -> str | None:
        if not self._watchdog_live_entries_blocked:
            return None
        return str(self.watchdog_status.get("mt5_message") or "MT5 watchdog is blocking live entries.")

    def _watchdog_alert(
        self,
        key: str,
        level: str,
        message: str,
        cfg: dict[str, Any],
        *,
        cooldown_seconds: float | None = None,
    ) -> None:
        now = time.time()
        cooldown = float(cooldown_seconds if cooldown_seconds is not None else cfg.get("alert_cooldown_seconds", 300.0))
        if now - self._watchdog_alert_times.get(key, 0.0) < cooldown:
            return
        self._watchdog_alert_times[key] = now
        self.store.add_event(level, "watchdog", message)
        if cfg.get("telegram_alerts", True):
            self.telegram.send_message(message)

    def _enable_keep_awake(self) -> None:
        if not self.config.get("engine", {}).get("keep_awake", True):
            self.keep_awake.last_reason = "Disabled by engine.keep_awake=false."
            return
        ok, reason = self.keep_awake.enable()
        self.store.add_event("INFO" if ok else "WARN", "power", reason)

    def _disable_keep_awake(self) -> None:
        if not self.keep_awake.active:
            return
        ok, reason = self.keep_awake.disable()
        self.store.add_event("INFO" if ok else "WARN", "power", reason)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._cycle()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep engine alive and visible in UI
                self.last_error = str(exc)
                self.store.add_event("ERROR", "engine", str(exc))
            interval = float(self.config.get("engine", {}).get("poll_interval_seconds", 2.0))
            self.stop_event.wait(interval)

    def _cycle(self) -> None:
        config = self.config
        self._watchdog_cycle(config)
        levels = int(config.get("strategy", {}).get("book_levels", 20))
        volatility_window = int(config.get("strategy", {}).get("volatility_window_trades", 80))
        bucket_sizes = config.get("strategy", {}).get("price_bucket_size", {})
        heatmap_ranges = config.get("strategy", {}).get("heatmap_range", {})
        symbols = enabled_symbols(config)
        max_spread_cfg = config.get("strategy", {}).get("max_spread_bps", {})

        for symbol_cfg in symbols:
            symbol = symbol_cfg["name"]
            data_symbol = symbol_cfg.get("data_symbol", symbol)
            market_payload = self._market_payload(data_symbol, limit=max(100, levels))
            if market_payload is None:
                self.last_quality[symbol] = {"ok": False, "reason": "No synchronized real market-data snapshot."}
                self.store.add_event("WARN", "market_data", f"{symbol}: no synchronized real market-data snapshot.")
                continue

            depth, trades = market_payload
            bucket_size = float(bucket_sizes.get(symbol, self.config.get("market_data", {}).get("price_bucket_size", {}).get(symbol, 100.0)))
            heatmap_range = float(heatmap_ranges.get(symbol, 1000.0))
            features = compute_features(
                symbol,
                depth,
                trades,
                levels=levels,
                volatility_window=volatility_window,
                price_bucket_size=bucket_size,
                heatmap_range=heatmap_range,
            )
            features.update(self._session_vwap_features(symbol, data_symbol, trades, float(features["mid"]), bucket_size))
            features.update(self._open_interest_features(data_symbol, symbol, float(features["mid"])))
            self._maybe_update_historical_liquidity(symbol_cfg)
            features.update(self.liquidity_memory.update(symbol, features.get("book_price_buckets", []), float(features["mid"]), config))
            features.update(self._major_liquidity_features(symbol, features, bucket_size))
            self.last_features[symbol] = features
            quality = validate_book_features(features, float(max_spread_cfg.get(symbol, 6.0)))
            self.last_quality[symbol] = {"ok": quality.ok, "reason": quality.reason}
            if not quality.ok:
                self.store.add_event("WARN", "data_quality", f"{symbol}: {quality.reason}")
                continue
            self._manage_open_trades(symbol, features["mid"])

            outcome = self.thesis_engine.evaluate(symbol, features, config)
            if not outcome.candidate:
                continue

            self._annotate_candidate_sizing(outcome.candidate, symbol_cfg)
            self._annotate_candidate_regime(outcome.candidate, features)
            self._annotate_candidate_structure_bias(outcome.candidate, features)
            self._annotate_candidate_a_plus(outcome.candidate, features, symbol_cfg)
            self._annotate_candidate_session_sweep_guard(outcome.candidate)
            self.store.add_candidate(outcome.candidate, outcome.decision, outcome.reason)
            if not outcome.executable:
                continue

            candidate = outcome.candidate
            if candidate.get("session_sweep_allowed") is False:
                self.pending_setups.pop(self._setup_key(candidate), None)
                self.store.add_candidate(
                    candidate,
                    "REJECTED",
                    str(candidate.get("session_sweep_reason") or "Session sweep guard blocked new entry."),
                )
                continue

            ready, confirmation_reason = self._confirm_setup(candidate, features)
            if not ready:
                self.store.add_candidate(candidate, "WATCHING_ENTRY_ZONE", confirmation_reason)
                continue

            self._annotate_candidate_session_sweep_guard(candidate)
            if candidate.get("session_sweep_allowed") is False:
                self.pending_setups.pop(self._setup_key(candidate), None)
                self.store.add_candidate(
                    candidate,
                    "REJECTED",
                    str(candidate.get("session_sweep_reason") or "Session sweep guard blocked new entry."),
                )
                continue

            risk_decision = RiskEngine(config, self.store).approve(candidate, symbol_cfg)
            self.store.add_candidate(candidate, "APPROVED" if risk_decision.approved else "REJECTED", risk_decision.reason)
            if risk_decision.approved:
                self.pending_setups.pop(self._setup_key(candidate), None)
                self._execute_candidate(candidate, symbol_cfg)

    def _maybe_update_historical_liquidity(self, symbol_cfg: dict[str, Any]) -> None:
        cfg = self.config.get("historical_liquidity", {})
        if not cfg.get("enabled", True) or not cfg.get("update_while_running", True):
            return
        symbol = str(symbol_cfg["name"])
        now = time.time()
        interval = max(30.0, float(cfg.get("incremental_poll_seconds", 60.0)))
        if now - self.last_historical_update_poll.get(symbol, 0.0) < interval:
            return
        self.last_historical_update_poll[symbol] = now
        if not cfg.get("live_rebuild_levels", False):
            rebuild_levels = False
        else:
            rebuild_interval = max(300.0, float(cfg.get("level_rebuild_interval_minutes", 60.0)) * 60.0)
            last_rebuild = self.last_historical_level_rebuild.get(symbol)
            if last_rebuild is None:
                self.last_historical_level_rebuild[symbol] = now
                rebuild_levels = False
            else:
                rebuild_levels = now - last_rebuild >= rebuild_interval
        result = self.historical_liquidity.update_missing_recent(symbol_cfg, rebuild_levels=rebuild_levels)
        if rebuild_levels and result.get("ok"):
            self.last_historical_level_rebuild[symbol] = now
        self.historical_liquidity_status = self.historical_liquidity.status()
        if result.get("reason") == "needs_backfill":
            return
        updated_rows = int(result.get("updated", result.get("rows", 0)) or 0)
        incremental_levels = result.get("incremental_levels") or {}
        updated_levels = int(incremental_levels.get("updated", 0) or 0) if isinstance(incremental_levels, dict) else 0
        if updated_rows > 0 or updated_levels > 0:
            self.store.add_event("INFO", "historical_liquidity", f"{symbol}: updated historical candles: {result}")
        if cfg.get("prune_raw_m1_while_running", True):
            prune_result = self.historical_liquidity.prune_old_m1(symbol_cfg)
            if int(prune_result.get("pruned", 0) or 0) > 0:
                self.store.add_event("INFO", "historical_liquidity", f"{symbol}: pruned old raw M1 candles: {prune_result}")

    @staticmethod
    def _historical_liquidity_summary(symbol: str, mid: float, levels: list[dict[str, Any]]) -> dict[str, Any]:
        if not levels:
            return {
                "enabled": True,
                "symbol": symbol,
                "levels": 0,
                "price_discovery_up": False,
                "price_discovery_down": False,
                "strongest_above": None,
                "strongest_below": None,
            }
        above = [row for row in levels if str(row.get("side")) == "resistance" and float(row.get("price_low", 0.0)) > mid]
        below = [row for row in levels if str(row.get("side")) == "support" and float(row.get("price_high", 0.0)) < mid]
        strongest_above = max(above, key=lambda row: float(row.get("score", 0.0)), default=None)
        strongest_below = max(below, key=lambda row: float(row.get("score", 0.0)), default=None)
        max_high = max(float(row.get("price_high", 0.0)) for row in levels)
        min_low = min(float(row.get("price_low", 0.0)) for row in levels)
        return {
            "enabled": True,
            "symbol": symbol,
            "levels": len(levels),
            "price_discovery_up": mid > max_high,
            "price_discovery_down": mid < min_low,
            "strongest_above": strongest_above,
            "strongest_below": strongest_below,
        }

    def _major_liquidity_features(self, symbol: str, features: dict[str, Any], bucket_size: float) -> dict[str, Any]:
        cfg = self.config.get("major_liquidity", {})
        if not cfg.get("enabled", True):
            return {"major_liquidity_zones": [], "major_liquidity_summary": {"enabled": False}}

        mid = float(features.get("mid", 0.0) or 0.0)
        if mid <= 0:
            return {"major_liquidity_zones": [], "major_liquidity_summary": {"enabled": True, "reason": "missing mid"}}

        min_score = float(cfg.get("min_zone_score", 65.0))
        min_notional = self._symbol_config_value(cfg, "min_book_notional", symbol, symbol, 100000.0)
        profile_strength_min = float(cfg.get("profile_strength_min", 75.0))
        zones: list[dict[str, Any]] = []
        historical_cfg = self.config.get("historical_liquidity", {})
        historical_levels = []
        if historical_cfg.get("enabled", True):
            max_each_historical = int(historical_cfg.get("max_levels_each_side", 12))
            historical_levels = self.store.load_historical_levels(symbol, limit=max_each_historical * 6)
            zones.extend(
                historical_levels_as_major_zones(
                    historical_levels,
                    mid=mid,
                    weight=float(historical_cfg.get("weight", 0.30)),
                    max_each_side=max_each_historical,
                    min_zone_score=min_score,
                    decay_half_life_days=float(historical_cfg.get("decay_half_life_days", 30.0)),
                )
            )

        for bucket in features.get("book_price_buckets") or []:
            low = float(bucket.get("price_low", 0.0))
            high = float(bucket.get("price_high", 0.0))
            if high <= low:
                continue
            bid_notional = float(bucket.get("bid_notional", 0.0))
            ask_notional = float(bucket.get("ask_notional", 0.0))
            strength = float(bucket.get("strength", 0.0))
            memory = float(bucket.get("memory_score", 0.0))
            if high <= mid and (bid_notional >= min_notional or memory >= 35.0):
                score = min(100.0, (strength * 0.35) + (memory * 0.35) + min(30.0, bid_notional / max(min_notional, 1.0) * 30.0))
                self._append_major_zone(zones, "support", low, high, bucket.get("label"), score, "book_memory_bid", bid_notional, memory, strength, mid)
            if low >= mid and (ask_notional >= min_notional or memory >= 35.0):
                score = min(100.0, (strength * 0.35) + (memory * 0.35) + min(30.0, ask_notional / max(min_notional, 1.0) * 30.0))
                self._append_major_zone(zones, "resistance", low, high, bucket.get("label"), score, "book_memory_ask", ask_notional, memory, strength, mid)

        for row in features.get("session_volume_profile") or []:
            strength = float(row.get("strength", 0.0))
            if strength < profile_strength_min:
                continue
            low = float(row.get("price_low", 0.0))
            high = float(row.get("price_high", 0.0))
            side = "support" if high <= mid else "resistance" if low >= mid else "magnet"
            if side == "magnet":
                continue
            score = min(100.0, 40.0 + (strength * 0.45))
            self._append_major_zone(zones, side, low, high, row.get("label"), score, "session_volume_profile", float(row.get("volume", 0.0)), 0.0, strength, mid)

        vwap_score = float(cfg.get("vwap_zone_score", 62.0))
        for key, side, label in (
            ("session_vwap_lower_2", "support", "VWAP -2SD"),
            ("session_vwap_lower_1", "support", "VWAP -1SD"),
            ("session_vwap_upper_1", "resistance", "VWAP +1SD"),
            ("session_vwap_upper_2", "resistance", "VWAP +2SD"),
        ):
            price = float(features.get(key, 0.0) or 0.0)
            if price <= 0:
                continue
            if (side == "support" and price < mid) or (side == "resistance" and price > mid):
                half = max(bucket_size * 0.5, mid * 0.00025)
                self._append_major_zone(zones, side, price - half, price + half, label, vwap_score, key, 0.0, 0.0, vwap_score, mid)

        swing_score = float(cfg.get("swing_zone_score", 68.0))
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        half = max(bucket_size * 0.5, mid * 0.00025)
        if recent_low < mid:
            self._append_major_zone(zones, "support", recent_low - half, recent_low + half, "recent swing low", swing_score, "recent_low", 0.0, 0.0, swing_score, mid)
        if recent_high > mid:
            self._append_major_zone(zones, "resistance", recent_high - half, recent_high + half, "recent swing high", swing_score, "recent_high", 0.0, 0.0, swing_score, mid)

        merged = self._merge_major_zones(zones, symbol, cfg, mid)
        filtered = [zone for zone in merged if float(zone["score"]) >= min_score]
        supports = sorted([zone for zone in filtered if zone["side"] == "support"], key=lambda z: (float(z["score"]), -float(z["distance_bps"])), reverse=True)
        resistances = sorted([zone for zone in filtered if zone["side"] == "resistance"], key=lambda z: (float(z["score"]), -float(z["distance_bps"])), reverse=True)
        max_each = int(cfg.get("max_zones_each_side", 8))
        output = sorted([*supports[:max_each], *resistances[:max_each]], key=lambda z: float(z["price_low"]), reverse=True)
        historical_summary = self._historical_liquidity_summary(symbol, mid, historical_levels)
        return {
            "major_liquidity_zones": output,
            "major_liquidity_summary": {
                "enabled": True,
                "support_count": len(supports),
                "resistance_count": len(resistances),
                "strongest_support": supports[0] if supports else None,
                "strongest_resistance": resistances[0] if resistances else None,
                "historical": historical_summary,
            },
            "historical_liquidity_summary": historical_summary,
        }

    @staticmethod
    def _append_major_zone(
        zones: list[dict[str, Any]],
        side: str,
        low: float,
        high: float,
        label: Any,
        score: float,
        source: str,
        notional: float,
        memory: float,
        strength: float,
        mid: float,
    ) -> None:
        if high <= low:
            return
        center = (low + high) / 2.0
        zones.append(
            {
                "side": side,
                "price_low": round(low, 4),
                "price_high": round(high, 4),
                "label": str(label or f"{low:.0f}-{high:.0f}"),
                "score": round(score, 2),
                "sources": [source],
                "notional": round(notional, 4),
                "memory_score": round(memory, 2),
                "strength": round(strength, 2),
                "distance_bps": round(abs(center - mid) / mid * 10000.0, 2) if mid else 0.0,
            }
        )

    def _merge_major_zones(self, zones: list[dict[str, Any]], symbol: str, cfg: dict[str, Any], mid: float) -> list[dict[str, Any]]:
        merge_bps = self._symbol_config_value(cfg, "merge_distance_bps", symbol, symbol, 8.0)
        merged: list[dict[str, Any]] = []
        for zone in sorted(zones, key=lambda item: (str(item["side"]), float(item["price_low"]))):
            center = (float(zone["price_low"]) + float(zone["price_high"])) / 2.0
            match = None
            for existing in merged:
                if existing["side"] != zone["side"]:
                    continue
                existing_center = (float(existing["price_low"]) + float(existing["price_high"])) / 2.0
                distance_bps = abs(center - existing_center) / mid * 10000.0 if mid else 999999.0
                if distance_bps <= merge_bps:
                    match = existing
                    break
            if not match:
                merged.append(dict(zone))
                continue
            match["price_low"] = round(min(float(match["price_low"]), float(zone["price_low"])), 4)
            match["price_high"] = round(max(float(match["price_high"]), float(zone["price_high"])), 4)
            merged_score = max(float(match["score"]), float(zone["score"])) + min(12.0, float(zone["score"]) * 0.12)
            match["score"] = round(min(100.0, merged_score), 2)
            match["notional"] = round(float(match.get("notional", 0.0)) + float(zone.get("notional", 0.0)), 4)
            match["memory_score"] = round(max(float(match.get("memory_score", 0.0)), float(zone.get("memory_score", 0.0))), 2)
            match["strength"] = round(max(float(match.get("strength", 0.0)), float(zone.get("strength", 0.0))), 2)
            match["sources"] = sorted(set([*match.get("sources", []), *zone.get("sources", [])]))
            if match["sources"] and all(str(source).startswith("historical_") for source in match["sources"]):
                historical_cfg = self.config.get("historical_liquidity", {})
                historical_weight = float(historical_cfg.get("weight", 0.30))
                historical_cap = float(cfg.get("min_zone_score", 65.0)) + ((100.0 - float(cfg.get("min_zone_score", 65.0))) * historical_weight) + 10.0
                match["score"] = round(min(float(match["score"]), historical_cap), 2)
            live_confirmed = any(source in {"book_memory_bid", "book_memory_ask", "session_volume_profile"} for source in match["sources"])
            if not live_confirmed:
                match["score"] = round(min(float(match["score"]), 90.0), 2)
            match["label"] = f"{match['price_low']:.0f}-{match['price_high']:.0f}" if float(match["price_high"]) >= 100 else f"{match['price_low']:.2f}-{match['price_high']:.2f}"
            center = (float(match["price_low"]) + float(match["price_high"])) / 2.0
            match["distance_bps"] = round(abs(center - mid) / mid * 10000.0, 2) if mid else 0.0
        return merged

    def _annotate_candidate_sizing(self, candidate: dict[str, Any], symbol_cfg: dict[str, Any]) -> None:
        mt5_symbol = symbol_cfg.get("mt5_symbol") or candidate["symbol"]
        risk_pct = float(self.config.get("risk", {}).get("risk_per_trade_pct", 0.25))
        equity, equity_note = self._sizing_equity(allow_mt5=True, fallback_to_paper=True)

        rows = self.store.query("select * from symbol_contracts where symbol = ?", (mt5_symbol,))
        if not rows:
            spec = scan_symbol(mt5_symbol)
            if spec.get("available"):
                self.store.save_contract(mt5_symbol, spec)
                rows = self.store.query("select * from symbol_contracts where symbol = ?", (mt5_symbol,))
        if not rows:
            candidate.update(
                {
                    "planned_lots": 0.0,
                    "raw_lots": 0.0,
                    "risk_cash": equity * (risk_pct / 100.0),
                    "loss_per_lot": 0.0,
                    "sizing_reason": f"No verified MT5 contract spec for {mt5_symbol}.",
                }
            )
            return

        sizing = calculate_lot_size(
            equity=equity,
            risk_pct=risk_pct,
            entry=candidate["entry"],
            stop_loss=candidate["stop_loss"],
            spec=rows[0],
        )
        candidate.update(
            {
                "planned_lots": sizing.lots if sizing.valid else 0.0,
                "raw_lots": sizing.raw_lots,
                "risk_cash": sizing.risk_cash,
                "loss_per_lot": sizing.loss_per_lot,
                "sizing_reason": f"{sizing.reason}; {equity_note}.",
            }
        )

    def _sizing_equity(self, *, allow_mt5: bool, fallback_to_paper: bool) -> tuple[float, str]:
        risk_cfg = self.config.get("risk", {})
        configured = float(risk_cfg.get("sizing_account_balance", 0.0) or 0.0)
        if configured > 0:
            return configured, f"using configured sizing account balance {configured:.2f}"
        if allow_mt5:
            equity_ok, equity, equity_reason = account_equity()
            if equity_ok:
                return equity, "using MT5 account equity"
            if not fallback_to_paper:
                raise RuntimeError(equity_reason)
            fallback = float(self.config.get("engine", {}).get("paper_initial_equity", 10000.0))
            return fallback, f"using configured paper equity because {equity_reason}"
        fallback = float(self.config.get("engine", {}).get("paper_initial_equity", 10000.0))
        return fallback, "using configured paper equity"

    def _annotate_candidate_regime(self, candidate: dict[str, Any], features: dict[str, Any]) -> None:
        regime = self._market_regime(candidate["symbol"], features)
        candidate["regime_direction"] = regime["direction"]
        candidate["regime_score"] = max(float(candidate.get("regime_score", 0.0)), abs(float(regime["score"])) * 20.0)
        candidate["regime_reason"] = regime["reason"]
        candidate["regime_side_allowed"] = self._side_allowed_by_regime(candidate, regime)

    def _market_regime(self, symbol: str, features: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config.get("regime_filter", {})
        if not cfg.get("enabled", True):
            return {"direction": "range", "score": 0, "reason": "Regime filter disabled."}

        score = 0
        reasons: list[str] = []
        vwap_threshold = float(cfg.get("vwap_distance_bps", 3.0))
        recent_vwap_threshold = float(cfg.get("recent_vwap_distance_bps", 1.0))
        drift_threshold = float(cfg.get("trade_drift_bps", 2.0))
        delta_threshold = float(cfg.get("delta_imbalance", 0.12))
        session_delta_threshold = float(cfg.get("session_delta_imbalance", 0.015))
        magnet_min_confidence = float(cfg.get("magnet_min_confidence", 20.0))

        def add_signal(name: str, value: float, threshold: float) -> None:
            nonlocal score
            if value >= threshold:
                score += 1
                reasons.append(f"{name} bullish {value:.3f}")
            elif value <= -threshold:
                score -= 1
                reasons.append(f"{name} bearish {value:.3f}")

        add_signal("session VWAP distance bps", float(features.get("session_vwap_distance_bps", 0.0)), vwap_threshold)
        add_signal("recent VWAP distance bps", float(features.get("recent_vwap_distance_bps", 0.0)), recent_vwap_threshold)
        add_signal("trade drift bps", float(features.get("trade_drift_bps", 0.0)), drift_threshold)
        add_signal("delta imbalance", float(features.get("delta_imbalance", 0.0)), delta_threshold)
        add_signal("session delta", float(features.get("session_delta_imbalance", 0.0)), session_delta_threshold)

        oi_support = str(features.get("open_interest_support", "neutral"))
        if oi_support.startswith("supports_up"):
            score += 1
            reasons.append("OI supports up")
        elif oi_support.startswith("supports_down"):
            score -= 1
            reasons.append("OI supports down")

        magnet = features.get("liquidity_magnet", {})
        if isinstance(magnet, dict) and float(magnet.get("confidence", 0.0) or 0.0) >= magnet_min_confidence:
            direction = str(magnet.get("direction", "neutral"))
            if direction == "up":
                score += 1
                reasons.append("liquidity magnet up")
            elif direction == "down":
                score -= 1
                reasons.append("liquidity magnet down")

        min_score = int(cfg.get("min_direction_score", 2))
        if score >= min_score:
            direction = "bullish"
        elif score <= -min_score:
            direction = "bearish"
        else:
            direction = "range"
        reason = f"{symbol} regime {direction} score {score}: " + ("; ".join(reasons) if reasons else "no directional edge")
        return {"direction": direction, "score": score, "reason": reason}

    def _side_allowed_by_regime(self, candidate: dict[str, Any], regime: dict[str, Any]) -> bool:
        cfg = self.config.get("regime_filter", {})
        if not cfg.get("enabled", True) or not cfg.get("block_against_regime", True):
            return True
        direction = str(regime.get("direction", "range"))
        side = str(candidate.get("side", "")).upper()
        against = (direction == "bullish" and side == "SELL") or (direction == "bearish" and side == "BUY")
        if not against:
            return True
        if not cfg.get("allow_countertrend", False):
            candidate["regime_reason"] = f"{regime['reason']}; {side} blocked because countertrend trading is disabled."
            return False
        min_score = float(cfg.get("countertrend_min_score", 94.0))
        if float(candidate.get("score", 0.0)) < min_score:
            candidate["regime_reason"] = f"{regime['reason']}; {side} countertrend score below {min_score:.1f}."
            return False
        return True

    def _annotate_candidate_structure_bias(self, candidate: dict[str, Any], features: dict[str, Any]) -> None:
        cfg = self.config.get("structure_bias", {})
        if not cfg.get("enabled", True):
            candidate["structure_side_allowed"] = True
            candidate["structure_reason"] = "Structure bias disabled."
            return
        symbol = str(candidate["symbol"])
        bias = self.structure_bias.get(symbol)
        if not bias:
            candidate["structure_side_allowed"] = True
            candidate["structure_reason"] = "No active structure bias."
            return

        now = time.time()
        if now >= float(bias.get("expires_at", 0.0)):
            self.structure_bias.pop(symbol, None)
            candidate["structure_side_allowed"] = True
            candidate["structure_reason"] = "Structure bias expired."
            return

        mid = float(features.get("mid", candidate.get("entry", 0.0)) or 0.0)
        level = float(bias["level"])
        buffer = self._bps_value_from_price(
            cfg.get("break_buffer_bps", {}),
            symbol,
            str(bias.get("mt5_symbol") or symbol),
            float(cfg.get("default_break_buffer_bps", 4.0)),
            mid or level,
        )
        source_side = str(bias["side"])
        broken = (source_side == "BUY" and mid < level - buffer) or (source_side == "SELL" and mid > level + buffer)
        if broken:
            self.structure_bias.pop(symbol, None)
            candidate["structure_side_allowed"] = True
            candidate["structure_reason"] = f"{source_side} structure bias broken at {level:.4f}; opposite side allowed."
            return

        blocked_side = "SELL" if source_side == "BUY" else "BUY"
        if str(candidate.get("side", "")).upper() == blocked_side:
            candidate["structure_side_allowed"] = False
            candidate["structure_reason"] = (
                f"{blocked_side} blocked: active {source_side} structure from trade #{bias.get('trade_id')} "
                f"at invalidation {level:.4f} is still holding; mid {mid:.4f}."
            )
            return

        candidate["structure_side_allowed"] = True
        candidate["structure_reason"] = (
            f"Candidate aligns with active {source_side} structure from trade #{bias.get('trade_id')}."
        )

    def _annotate_candidate_session_sweep_guard(
        self,
        candidate: dict[str, Any],
        at: datetime | None = None,
    ) -> None:
        decision = self._session_sweep_guard_decision(candidate, at=at)
        candidate["session_sweep_allowed"] = bool(decision["allowed"])
        candidate["session_sweep_reason"] = str(decision["reason"])
        if decision.get("session"):
            candidate["session_sweep_name"] = str(decision["session"])

    def _session_sweep_guard_decision(
        self,
        candidate: dict[str, Any],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        cfg = self.config.get("session_sweep_guard", {})
        if not cfg.get("enabled", True):
            return {"allowed": True, "reason": "Session sweep guard disabled."}

        side = str(candidate.get("side", "")).upper()
        block_sides = {str(item).upper() for item in cfg.get("block_sides", ["BUY", "SELL"])}
        if block_sides and side not in block_sides:
            return {"allowed": True, "reason": f"Session sweep guard does not block {side} entries."}

        window = max(0.0, float(cfg.get("window_minutes", 30.0)))
        if window <= 0:
            return {"allowed": True, "reason": "Session sweep guard window is 0 minutes."}

        sessions = list(cfg.get("sessions") or [])
        if not sessions:
            return {"allowed": True, "reason": "Session sweep guard has no configured sessions."}

        now = at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        now_minutes = now.hour * 60.0 + now.minute + (now.second / 60.0) + (now.microsecond / 60_000_000.0)

        nearest: dict[str, Any] | None = None
        for session in sessions:
            if not isinstance(session, dict):
                continue
            open_text = str(session.get("open_utc") or session.get("open") or "").strip()
            open_minutes = self._parse_session_open_minutes(open_text)
            if open_minutes is None:
                continue
            diff = ((now_minutes - open_minutes + 720.0) % 1440.0) - 720.0
            distance = abs(diff)
            if nearest is None or distance < float(nearest["distance"]):
                nearest = {
                    "name": str(session.get("name") or open_text or "session"),
                    "open_utc": open_text,
                    "diff": diff,
                    "distance": distance,
                }

        if nearest is None:
            return {"allowed": True, "reason": "Session sweep guard found no valid session open times."}

        if float(nearest["distance"]) > window:
            return {
                "allowed": True,
                "reason": (
                    f"Outside session sweep guard windows; nearest open is {nearest['name']} "
                    f"at {nearest['open_utc']} UTC."
                ),
                "session": nearest["name"],
            }

        timing = "after" if float(nearest["diff"]) >= 0 else "before"
        return {
            "allowed": False,
            "session": nearest["name"],
            "reason": (
                f"Session sweep guard blocked {side}: {float(nearest['distance']):.1f} minutes "
                f"{timing} {nearest['name']} open ({nearest['open_utc']} UTC), within +/-{window:.0f}m "
                "liquidity-hunt protection window."
            ),
        }

    @staticmethod
    def _parse_session_open_minutes(value: str) -> float | None:
        parts = value.split(":")
        if len(parts) < 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return hour * 60.0 + minute + (second / 60.0)

    def _annotate_candidate_a_plus(self, candidate: dict[str, Any], features: dict[str, Any], symbol_cfg: dict[str, Any]) -> None:
        cfg = self.config.get("a_plus_filter", {})
        if not cfg.get("enabled", True):
            candidate["a_plus_allowed"] = True
            candidate["a_plus_reason"] = "A+ filter disabled."
            return

        symbol = str(candidate["symbol"])
        mt5_symbol = str(symbol_cfg.get("mt5_symbol") or symbol)
        side = str(candidate.get("side", "")).upper()
        reasons: list[str] = []

        allowed_strategies = set(cfg.get("allowed_strategies") or [])
        strategy = str(candidate.get("strategy", ""))
        if allowed_strategies and strategy not in allowed_strategies:
            reasons.append(f"strategy {strategy} is not in A+ allowed strategies")

        min_score = float(cfg.get("min_score", 88.0))
        score = float(candidate.get("score", 0.0))
        if score < min_score:
            reasons.append(f"score {score:.2f} below A+ minimum {min_score:.2f}")

        min_rr = float(cfg.get("min_rr", self.config.get("risk", {}).get("preferred_rr", 2.5)))
        rr = float(candidate.get("rr", 0.0))
        if rr < min_rr:
            reasons.append(f"RR {rr:.2f} below A+ minimum {min_rr:.2f}")

        min_liquidity = float(cfg.get("min_liquidity_score", 75.0))
        liquidity = float(candidate.get("liquidity_score", 0.0))
        if liquidity < min_liquidity:
            reasons.append(f"liquidity score {liquidity:.2f} below A+ minimum {min_liquidity:.2f}")

        min_target_memory = float(cfg.get("min_target_memory_score", 15.0))
        target_memory = float(candidate.get("target_memory_score") or 0.0)
        price_discovery = self._price_discovery_allowed(side, features)
        if target_memory < min_target_memory and not price_discovery:
            reasons.append(f"target memory {target_memory:.2f} below A+ minimum {min_target_memory:.2f}")
        elif target_memory < min_target_memory and price_discovery:
            candidate["price_discovery_mode"] = True

        target_distance = self._candidate_target_distance_bps(candidate)
        min_target_distance = self._strategy_symbol_config_value(
            cfg,
            "min_target_distance_bps",
            strategy,
            symbol,
            mt5_symbol,
            0.0,
        )
        if target_distance < min_target_distance:
            reasons.append(f"target distance {target_distance:.2f} bps below A+ minimum {min_target_distance:.2f} bps")

        regime_direction = str(candidate.get("regime_direction") or "range")
        if cfg.get("require_trend_alignment", True):
            if regime_direction == "range" and not cfg.get("allow_range_regime", False):
                reasons.append("broader regime is range; A+ filter waits for clear direction")
            elif (regime_direction == "bullish" and side != "BUY") or (regime_direction == "bearish" and side != "SELL"):
                reasons.append(f"{side} is not aligned with broader {regime_direction} regime")

        liquidity_zone = self._entry_liquidity_zone(candidate, features, cfg, mt5_symbol)
        candidate["entry_liquidity_score"] = round(float(liquidity_zone.get("score", 0.0)), 2)
        candidate["entry_liquidity_label"] = liquidity_zone.get("label")
        if cfg.get("require_entry_liquidity_zone", True) and not liquidity_zone["ok"]:
            reasons.append(str(liquidity_zone["reason"]))

        if not reasons:
            candidate["a_plus_allowed"] = True
            candidate["a_plus_tier"] = "A"
            candidate["a_plus_reason"] = (
                f"A+ passed: {side} aligns with {regime_direction} regime, entry liquidity "
                f"{liquidity_zone.get('label')} score {liquidity_zone.get('score', 0.0):.2f}, "
                f"target distance {target_distance:.2f} bps."
            )
            return

        fallback = self._a_plus_fallback_allowed(
            candidate,
            cfg,
            reasons,
            liquidity_zone,
            target_distance,
            regime_direction,
            side,
            symbol,
            mt5_symbol,
            price_discovery,
        )
        candidate["a_plus_allowed"] = bool(fallback["ok"])
        if fallback["ok"]:
            candidate["a_plus_tier"] = "B"
            candidate["a_plus_reason"] = (
                "B-tier allowed after A+ miss: "
                + str(fallback["reason"])
                + " A+ misses: "
                + "; ".join(reasons)
            )
        else:
            candidate["a_plus_tier"] = "REJECTED"
            candidate["a_plus_reason"] = "A+ rejected: " + "; ".join(reasons)

    def _a_plus_fallback_allowed(
        self,
        candidate: dict[str, Any],
        cfg: dict[str, Any],
        a_plus_reasons: list[str],
        liquidity_zone: dict[str, Any],
        target_distance: float,
        regime_direction: str,
        side: str,
        symbol: str,
        mt5_symbol: str,
        price_discovery: bool = False,
    ) -> dict[str, Any]:
        if not cfg.get("fallback_enabled", True):
            return {"ok": False, "reason": "B-tier fallback disabled."}

        fallback_reasons: list[str] = []
        allowed_strategies = set(cfg.get("fallback_allowed_strategies") or cfg.get("allowed_strategies") or [])
        strategy = str(candidate.get("strategy", ""))
        if allowed_strategies and strategy not in allowed_strategies:
            fallback_reasons.append(f"strategy {strategy} is not B-tier allowed")

        min_score = float(cfg.get("fallback_min_score", 82.0))
        score = float(candidate.get("score", 0.0))
        if score < min_score:
            fallback_reasons.append(f"score {score:.2f} below B-tier minimum {min_score:.2f}")

        min_rr = float(cfg.get("fallback_min_rr", self.config.get("risk", {}).get("min_rr", 2.0)))
        rr = float(candidate.get("rr", 0.0))
        if rr < min_rr:
            fallback_reasons.append(f"RR {rr:.2f} below B-tier minimum {min_rr:.2f}")

        min_liquidity = float(cfg.get("fallback_min_liquidity_score", 50.0))
        liquidity = float(candidate.get("liquidity_score", 0.0))
        if liquidity < min_liquidity:
            fallback_reasons.append(f"liquidity score {liquidity:.2f} below B-tier minimum {min_liquidity:.2f}")

        min_entry_liquidity = float(cfg.get("fallback_min_entry_liquidity_score", 75.0))
        entry_liquidity_score = float(liquidity_zone.get("score", 0.0) or 0.0)
        if not liquidity_zone.get("ok"):
            fallback_reasons.append(str(liquidity_zone.get("reason") or "no entry liquidity zone"))
        elif entry_liquidity_score < min_entry_liquidity:
            fallback_reasons.append(
                f"entry liquidity score {entry_liquidity_score:.2f} below B-tier minimum {min_entry_liquidity:.2f}"
            )

        min_target_distance = self._symbol_config_value(cfg, "fallback_min_target_distance_bps", symbol, mt5_symbol, 35.0)
        if target_distance < min_target_distance:
            fallback_reasons.append(
                f"target distance {target_distance:.2f} bps below B-tier minimum {min_target_distance:.2f} bps"
            )

        min_target_memory = float(cfg.get("fallback_min_target_memory_score", 0.0))
        target_memory = float(candidate.get("target_memory_score") or 0.0)
        if target_memory < min_target_memory and not price_discovery:
            fallback_reasons.append(f"target memory {target_memory:.2f} below B-tier minimum {min_target_memory:.2f}")

        opposite_regime = (regime_direction == "bullish" and side == "SELL") or (regime_direction == "bearish" and side == "BUY")
        if opposite_regime:
            fallback_reasons.append(f"{side} is against broader {regime_direction} regime")
        if regime_direction == "range" and not cfg.get("fallback_allow_range_regime", True):
            fallback_reasons.append("broader regime is range and B-tier range entries are disabled")

        if fallback_reasons:
            return {"ok": False, "reason": "; ".join(fallback_reasons)}
        return {
            "ok": True,
            "reason": (
                f"{side} {strategy} passed B-tier safety: score {score:.2f}, RR {rr:.2f}, "
                f"entry liquidity {liquidity_zone.get('label')} score {entry_liquidity_score:.2f}, "
                f"target distance {target_distance:.2f} bps."
            ),
        }

    @staticmethod
    def _price_discovery_allowed(side: str, features: dict[str, Any]) -> bool:
        historical = features.get("historical_liquidity_summary") or {}
        if not historical.get("levels"):
            return False
        if side == "BUY":
            return bool(historical.get("price_discovery_up"))
        if side == "SELL":
            return bool(historical.get("price_discovery_down"))
        return False

    @staticmethod
    def _candidate_target_distance_bps(candidate: dict[str, Any]) -> float:
        entry = float(candidate.get("entry") or 0.0)
        tp = float(candidate.get("take_profit") or 0.0)
        if entry <= 0 or tp <= 0:
            return 0.0
        return abs(tp - entry) / entry * 10000.0

    def _entry_liquidity_zone(
        self,
        candidate: dict[str, Any],
        features: dict[str, Any],
        cfg: dict[str, Any],
        mt5_symbol: str,
    ) -> dict[str, Any]:
        symbol = str(candidate["symbol"])
        side = str(candidate.get("side", "")).upper()
        entry = float(candidate.get("entry") or features.get("mid") or 0.0)
        stop = float(candidate.get("stop_loss") or 0.0)
        buckets = list(features.get("book_price_buckets") or [])
        major_zones = list(features.get("major_liquidity_zones") or [])
        if entry <= 0 or stop <= 0 or (not buckets and not major_zones):
            return {"ok": False, "score": 0.0, "label": "-", "reason": "no liquidity zones available around entry"}

        if side == "BUY":
            candidates = [
                bucket for bucket in buckets
                if float(bucket.get("price_high", 0.0)) <= entry and float(bucket.get("price_high", 0.0)) >= stop
            ]
            side_notional_key = "bid_notional"
            label = "support"
        else:
            candidates = [
                bucket for bucket in buckets
                if float(bucket.get("price_low", 0.0)) >= entry and float(bucket.get("price_low", 0.0)) <= stop
            ]
            side_notional_key = "ask_notional"
            label = "resistance"

        if not candidates:
            for zone in major_zones:
                if str(zone.get("side")) != label:
                    continue
                low = float(zone.get("price_low", 0.0))
                high = float(zone.get("price_high", 0.0))
                if side == "BUY" and high <= entry and high >= stop:
                    candidates.append(
                        {
                            "price_low": low,
                            "price_high": high,
                            "label": zone.get("label"),
                            "bid_notional": zone.get("notional", 0.0),
                            "ask_notional": 0.0,
                            "strength": zone.get("strength", zone.get("score", 0.0)),
                            "memory_score": zone.get("memory_score", 0.0),
                        }
                    )
                if side == "SELL" and low >= entry and low <= stop:
                    candidates.append(
                        {
                            "price_low": low,
                            "price_high": high,
                            "label": zone.get("label"),
                            "bid_notional": 0.0,
                            "ask_notional": zone.get("notional", 0.0),
                            "strength": zone.get("strength", zone.get("score", 0.0)),
                            "memory_score": zone.get("memory_score", 0.0),
                        }
                    )
        if not candidates:
            return {"ok": False, "score": 0.0, "label": "-", "reason": f"no visible {label} liquidity between entry and SL"}

        def zone_score(bucket: dict[str, Any]) -> float:
            side_notional = float(bucket.get(side_notional_key, 0.0))
            total = float(bucket.get("bid_notional", 0.0)) + float(bucket.get("ask_notional", 0.0))
            side_share = (side_notional / total) if total > 0 else 0.0
            strength = float(bucket.get("strength", 0.0))
            memory = float(bucket.get("memory_score", 0.0))
            return min(100.0, (strength * 0.50) + (memory * 0.30) + (side_share * 20.0))

        best = max(candidates, key=zone_score)
        score = zone_score(best)
        side_notional = float(best.get(side_notional_key, 0.0))
        min_score = float(cfg.get("min_entry_liquidity_score", 55.0))
        min_notional = self._symbol_config_value(cfg, "min_entry_liquidity_notional", symbol, mt5_symbol, 0.0)
        ok = score >= min_score and side_notional >= min_notional
        reason = (
            f"entry {label} liquidity {best.get('label', '-')} score {score:.2f}, "
            f"{side_notional_key} {side_notional:.0f}; needs score>={min_score:.2f} and notional>={min_notional:.0f}"
        )
        return {"ok": ok, "score": score, "label": best.get("label", "-"), "reason": reason}

    def _remember_structure_bias(self, trade_id: int, trade: dict[str, Any]) -> None:
        cfg = self.config.get("structure_bias", {})
        if not cfg.get("enabled", True):
            return
        symbol = str(trade["symbol"])
        side = str(trade["side"]).upper()
        level = float(trade["stop_loss"])
        ttl = float(cfg.get("ttl_seconds", 1800.0))
        self.structure_bias[symbol] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "mt5_symbol": trade.get("mt5_symbol"),
            "side": side,
            "level": level,
            "created_at": time.time(),
            "expires_at": time.time() + ttl,
        }
        blocked_side = "SELL" if side == "BUY" else "BUY"
        label = "support" if side == "BUY" else "resistance"
        self.store.add_event(
            "INFO",
            "structure",
            f"{symbol}: active {label} bias from trade #{trade_id}; blocking {blocked_side} until invalidation {level:.4f} breaks.",
        )

    def _setup_key(self, candidate: dict[str, Any]) -> str:
        return f"{candidate['symbol']}:{candidate['side']}:{candidate['strategy']}"

    @staticmethod
    def _is_thesis_setup(candidate: dict[str, Any]) -> bool:
        return "thesis" in str(candidate.get("strategy", "")).lower()

    def _setup_lifetimes(self, candidate: dict[str, Any]) -> tuple[float, float]:
        setup_cfg = self.config.get("setups", {})
        legacy_max_age = float(setup_cfg.get("max_setup_age_seconds", 60))
        confirmation_max_age = float(setup_cfg.get("confirmation_max_age_seconds", legacy_max_age))
        if not self._is_thesis_setup(candidate):
            return legacy_max_age, legacy_max_age

        thesis_max_age = setup_cfg.get("thesis_max_age_seconds")
        if thesis_max_age is None:
            thesis_max_age = float(setup_cfg.get("thesis_max_age_minutes", 120)) * 60.0
        return float(thesis_max_age), confirmation_max_age

    @staticmethod
    def _pending_thesis_age(existing: dict[str, Any], now: float) -> float:
        created = float(existing.get("thesis_created_ts") or existing.get("created_ts") or now)
        return max(0.0, now - created)

    @staticmethod
    def _pending_confirmation_age(existing: dict[str, Any], now: float) -> float:
        started = float(existing.get("confirmation_started_ts") or existing.get("created_ts") or now)
        return max(0.0, now - started)

    def _new_pending_setup(
        self,
        candidate: dict[str, Any],
        now: float,
        *,
        confirmations: int,
        state: str,
        existing: dict[str, Any] | None = None,
        reset_confirmation: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        thesis_created = now if existing is None else float(existing.get("thesis_created_ts") or existing.get("created_ts") or now)
        confirmation_started = (
            now
            if existing is None or reset_confirmation
            else float(existing.get("confirmation_started_ts") or existing.get("created_ts") or now)
        )
        thesis_stop_loss = (
            float(existing.get("thesis_stop_loss"))
            if existing and existing.get("thesis_stop_loss") is not None
            else float(candidate.get("stop_loss") or 0.0)
        )
        pending = {
            **(existing or {}),
            **candidate,
            "created_ts": thesis_created,
            "thesis_created_ts": thesis_created,
            "confirmation_started_ts": confirmation_started,
            "last_seen_ts": now,
            "confirmations": confirmations,
            "state": state,
            "thesis_stop_loss": thesis_stop_loss,
        }
        pending.update(extra)
        return pending

    @staticmethod
    def _setup_invalidated(candidate: dict[str, Any], existing: dict[str, Any] | None, price: float) -> tuple[bool, str]:
        side = str(candidate.get("side", "")).upper()
        stop = float((existing or {}).get("thesis_stop_loss") or candidate.get("stop_loss") or 0.0)
        if stop <= 0 or price <= 0:
            return False, ""
        if side == "BUY" and price <= stop:
            return True, f"Thesis invalidated: price {price:.4f} broke BUY invalidation/SL {stop:.4f}."
        if side == "SELL" and price >= stop:
            return True, f"Thesis invalidated: price {price:.4f} broke SELL invalidation/SL {stop:.4f}."
        return False, ""

    def _confirm_setup(self, candidate: dict[str, Any], features: dict[str, Any]) -> tuple[bool, str]:
        setup_cfg = self.config.get("setups", {})
        if not setup_cfg.get("require_confirmation", True):
            return True, "Setup confirmation disabled by config."

        key = self._setup_key(candidate)
        now = time.time()
        required = int(setup_cfg.get("confirmation_cycles", 2))
        thesis_max_age, confirmation_max_age = self._setup_lifetimes(candidate)
        price = float(features["mid"])
        zone_low = float(candidate.get("entry_zone_low", candidate["entry"]))
        zone_high = float(candidate.get("entry_zone_high", candidate["entry"]))
        if zone_low == zone_high:
            mt5_symbol = str(candidate.get("mt5_symbol") or candidate["symbol"])
            tolerance = self._symbol_config_value(
                self.config.get("strategy", {}),
                "entry_tolerance_bps",
                str(candidate["symbol"]),
                mt5_symbol,
                2.0,
            )
            expansion = abs(float(candidate["entry"])) * (float(tolerance) / 10000.0)
            zone_low -= expansion
            zone_high += expansion

        existing = self.pending_setups.get(key)
        invalidated, invalidation_reason = self._setup_invalidated(candidate, existing, price)
        if invalidated:
            self.pending_setups.pop(key, None)
            return False, invalidation_reason

        if existing and self._pending_thesis_age(existing, now) > thesis_max_age:
            self.pending_setups.pop(key, None)
            return False, f"Thesis expired after {thesis_max_age / 60.0:.0f}m without entry; waiting for a fresh setup."

        breakout_retest = self._breakout_retest_confirmation(
            candidate,
            features,
            key,
            price,
            zone_low,
            zone_high,
            now,
            required,
            thesis_max_age,
            confirmation_max_age,
        )
        if breakout_retest is not None:
            return breakout_retest

        if setup_cfg.get("reject_if_price_outside_entry_zone", True) and not (zone_low <= price <= zone_high):
            if existing:
                self.pending_setups[key] = self._new_pending_setup(
                    candidate,
                    now,
                    confirmations=0,
                    state="WAITING_ENTRY_ZONE",
                    existing=existing,
                    reset_confirmation=True,
                )
                return False, (
                    f"Thesis still active, but price {price:.4f} is outside entry zone "
                    f"{zone_low:.4f}-{zone_high:.4f}; confirmation reset, waiting for re-entry."
                )
            return False, (
                f"Setup found, but price {price:.4f} is outside entry zone "
                f"{zone_low:.4f}-{zone_high:.4f}; waiting for price to return."
            )

        if not existing:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=1,
                state="WATCHING_ENTRY_ZONE",
            )
            return False, (
                "Setup detected; waiting for confirmation cycle 1/"
                f"{required}. Entry zone {zone_low:.4f}-{zone_high:.4f}. "
                f"{candidate.get('entry_reason', '')}"
            )

        if self._pending_confirmation_age(existing, now) > confirmation_max_age:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=1,
                state="WATCHING_ENTRY_ZONE",
                existing=existing,
                reset_confirmation=True,
            )
            return False, f"Confirmation expired after {confirmation_max_age:.0f}s; thesis kept active and confirmation restarted."

        confirmations = int(existing.get("confirmations", 0)) + 1
        existing = self._new_pending_setup(
            candidate,
            now,
            confirmations=confirmations,
            state="CONFIRMED" if confirmations >= required else "WATCHING_ENTRY_ZONE",
            existing=existing,
        )
        self.pending_setups[key] = existing

        if confirmations < required:
            return False, (
                f"Setup still waiting: confirmation cycle {confirmations}/{required}. "
                f"SL reason: {candidate.get('stop_reason', '')}"
            )

        candidate["reason"] = (
            f"Confirmed after {confirmations} cycles inside entry zone. "
            f"{candidate.get('reason', '')}"
        )
        return True, (
            f"Setup confirmed after {confirmations} cycles inside entry zone "
            f"{zone_low:.4f}-{zone_high:.4f}. {candidate.get('tp_reason', '')}"
        )

    def _breakout_retest_confirmation(
        self,
        candidate: dict[str, Any],
        features: dict[str, Any],
        key: str,
        price: float,
        zone_low: float,
        zone_high: float,
        now: float,
        required: int,
        thesis_max_age: float,
        confirmation_max_age: float,
    ) -> tuple[bool, str] | None:
        setup_cfg = self.config.get("setups", {})
        if not setup_cfg.get("breakout_retest_enabled", True):
            return None

        side = str(candidate.get("side", "")).upper()
        symbol = str(candidate["symbol"])
        mt5_symbol = str(candidate.get("mt5_symbol") or symbol)
        entry = float(candidate["entry"])
        buffer_bps = self._symbol_config_value(setup_cfg, "breakout_buffer_bps", symbol, mt5_symbol, 2.0)
        fail_bps = self._symbol_config_value(setup_cfg, "retest_fail_bps", symbol, mt5_symbol, 4.0)
        breakout_buffer = abs(entry) * buffer_bps / 10000.0
        fail_buffer = abs(entry) * fail_bps / 10000.0
        existing = self.pending_setups.get(key)

        needs_buy_breakout = side == "BUY" and price < zone_low
        needs_sell_breakdown = side == "SELL" and price > zone_high
        breakout_state = str(existing.get("state", "")) if existing else ""
        if not (needs_buy_breakout or needs_sell_breakdown or breakout_state.startswith("WAITING_BREAK") or breakout_state.startswith("WAITING_RETEST")):
            return None

        invalidated, invalidation_reason = self._setup_invalidated(candidate, existing, price)
        if invalidated:
            self.pending_setups.pop(key, None)
            return False, invalidation_reason

        if existing and self._pending_thesis_age(existing, now) > thesis_max_age:
            self.pending_setups.pop(key, None)
            return False, f"Breakout-retest thesis expired after {thesis_max_age / 60.0:.0f}m without entry; waiting for fresh break of {entry:.4f}."

        if existing and self._pending_confirmation_age(existing, now) > confirmation_max_age:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_BREAKOUT_RETEST",
                existing=existing,
                reset_confirmation=True,
                breakout_level=entry,
                breakout_seen=False,
            )
            return False, f"Breakout-retest confirmation expired after {confirmation_max_age:.0f}s; thesis kept active and waiting for fresh break of {entry:.4f}."

        if not existing:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_BREAKOUT_RETEST",
                breakout_level=entry,
                breakout_seen=False,
            )
            direction = "above" if side == "BUY" else "below"
            return False, (
                f"Breakout-retest setup armed: {side} will not enter on first touch. "
                f"Waiting for price to break {direction} {entry:.4f}, then retest "
                f"{zone_low:.4f}-{zone_high:.4f} with confirmation."
            )

        breakout_seen = bool(existing.get("breakout_seen"))
        if side == "BUY" and price >= zone_high + breakout_buffer:
            breakout_seen = True
        if side == "SELL" and price <= zone_low - breakout_buffer:
            breakout_seen = True

        if not breakout_seen:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_BREAKOUT_RETEST",
                existing=existing,
                breakout_level=entry,
                breakout_seen=False,
            )
            direction = "above" if side == "BUY" else "below"
            return False, (
                f"Waiting for first breakout {direction} {entry:.4f}; current price {price:.4f}. "
                "No first-touch/chase entry."
            )

        if side == "BUY" and price < zone_low - fail_buffer:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_BREAKOUT_RETEST",
                existing=existing,
                reset_confirmation=True,
                breakout_level=entry,
                breakout_seen=False,
            )
            return False, (
                f"Breakout retest failed: price {price:.4f} accepted below retest zone "
                f"{zone_low:.4f}-{zone_high:.4f}; thesis kept active until invalidation."
            )
        if side == "SELL" and price > zone_high + fail_buffer:
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_BREAKOUT_RETEST",
                existing=existing,
                reset_confirmation=True,
                breakout_level=entry,
                breakout_seen=False,
            )
            return False, (
                f"Breakdown retest failed: price {price:.4f} accepted above retest zone "
                f"{zone_low:.4f}-{zone_high:.4f}; thesis kept active until invalidation."
            )

        existing = self._new_pending_setup(
            candidate,
            now,
            confirmations=int(existing.get("confirmations", 0)),
            state=str(existing.get("state", "WAITING_BREAKOUT_RETEST")),
            existing=existing,
            breakout_level=entry,
            breakout_seen=True,
        )

        if not (zone_low <= price <= zone_high):
            self.pending_setups[key] = self._new_pending_setup(
                candidate,
                now,
                confirmations=0,
                state="WAITING_RETEST",
                existing=existing,
                reset_confirmation=True,
                breakout_level=entry,
                breakout_seen=True,
            )
            return False, (
                f"Breakout seen at {entry:.4f}; waiting for retest into "
                f"{zone_low:.4f}-{zone_high:.4f}. Current price {price:.4f}."
            )

        confirmations = int(existing.get("confirmations", 0)) + 1
        existing = self._new_pending_setup(
            candidate,
            now,
            confirmations=confirmations,
            state="RETEST_CONFIRMED" if confirmations >= required else "WAITING_RETEST_CONFIRMATION",
            existing=existing,
            breakout_level=entry,
            breakout_seen=True,
        )
        self.pending_setups[key] = existing

        if confirmations < required:
            return False, (
                f"Retest holding at {zone_low:.4f}-{zone_high:.4f}; "
                f"confirmation cycle {confirmations}/{required}."
            )

        candidate["reason"] = (
            f"Breakout-retest confirmed after break of {entry:.4f} and "
            f"{confirmations} retest confirmations. {candidate.get('reason', '')}"
        )
        return True, (
            f"Breakout-retest confirmed: price broke {entry:.4f}, retested "
            f"{zone_low:.4f}-{zone_high:.4f}, and held for {confirmations}/{required} cycles."
        )

    def _market_payload(self, data_symbol: str, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        market_cfg = self.config.get("market_data", {})
        mode = market_cfg.get("mode", "websocket")
        if mode == "websocket":
            if not self.feed:
                self._start_market_feed()
            if self.feed:
                payload = self.feed.snapshot(data_symbol, limit)
                if payload:
                    return payload
            if market_cfg.get("real_data_only", True):
                return None

        depth = self.market.depth(data_symbol, limit=100)
        trades = self.market.recent_trades(data_symbol, limit=100)
        return depth, trades

    def _session_vwap_features(
        self,
        symbol: str,
        data_symbol: str,
        trades: list[dict[str, Any]],
        mid: float,
        bucket_size: float,
    ) -> dict[str, Any]:
        vwap_cfg = self.config.get("vwap", {})
        anchor = str(vwap_cfg.get("anchor", "utc_day"))
        anchor_start_ms = self._vwap_anchor_start_ms(anchor)
        self._ensure_vwap_session(symbol, anchor_start_ms, anchor != "rolling_24h")
        self._bootstrap_session_vwap(symbol, data_symbol, mid, bucket_size, anchor_start_ms)
        stats = self.session_vwap.setdefault(
            symbol,
            {
                "volume": 0.0,
                "notional": 0.0,
                "price2_notional": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "delta": 0.0,
                "trade_count": 0,
                "started_at": datetime.fromtimestamp(anchor_start_ms / 1000.0, timezone.utc).isoformat(timespec="seconds"),
                "anchor_start_ms": anchor_start_ms,
                "source": vwap_cfg.get("source", "binance_klines_plus_live"),
                "anchor": anchor,
            },
        )
        seen = self.seen_session_trade_ids.setdefault(symbol, set())
        order = self.seen_session_trade_order.setdefault(symbol, deque(maxlen=50000))
        profile = self.session_volume_profile.setdefault(symbol, {})
        live_start_ms = int(self.session_vwap_live_start_ms.get(symbol, anchor_start_ms))

        for trade in trades:
            trade_time = int(trade.get("time") or 0)
            if trade_time and trade_time < live_start_ms:
                continue
            trade_id = trade.get("id")
            if trade_id is None:
                trade_id = (trade.get("time"), trade.get("price"), trade.get("qty"), trade.get("isBuyerMaker"))
            if trade_id in seen:
                continue
            seen.add(trade_id)
            order.append(trade_id)
            if order.maxlen and len(seen) > order.maxlen:
                seen.clear()
                seen.update(order)
            price = float(trade.get("price", 0) or 0)
            qty = float(trade.get("qty", 0) or 0)
            if price <= 0 or qty <= 0:
                continue
            stats["volume"] += qty
            stats["notional"] += price * qty
            stats["price2_notional"] += price * price * qty
            stats["trade_count"] += 1
            bucket_low = (price // bucket_size) * bucket_size if bucket_size > 0 else price
            bucket = profile.setdefault(
                bucket_low,
                {
                    "price_low": bucket_low,
                    "price_high": bucket_low + bucket_size,
                    "volume": 0.0,
                    "buy_volume": 0.0,
                    "sell_volume": 0.0,
                    "delta": 0.0,
                },
            )
            is_buyer_maker = bool(trade.get("isBuyerMaker"))
            bucket["volume"] += qty
            if is_buyer_maker:
                stats["sell_volume"] += qty
                stats["delta"] -= qty
                bucket["sell_volume"] += qty
                bucket["delta"] -= qty
            else:
                stats["buy_volume"] += qty
                stats["delta"] += qty
                bucket["buy_volume"] += qty
                bucket["delta"] += qty

        volume = float(stats["volume"])
        if volume <= 0:
            return {
                "vwap": mid,
                "session_vwap": mid,
                "session_vwap_std": 0.0,
                "session_vwap_distance_bps": 0.0,
                "session_vwap_upper_1": mid,
                "session_vwap_lower_1": mid,
                "session_vwap_upper_2": mid,
                "session_vwap_lower_2": mid,
                "session_volume": 0.0,
                "session_buy_volume": 0.0,
                "session_sell_volume": 0.0,
                "session_cumulative_delta": 0.0,
                "session_delta_imbalance": 0.0,
                "session_trade_count": 0,
                "session_volume_profile": [],
            }

        vwap = float(stats["notional"]) / volume
        variance = max(0.0, (float(stats["price2_notional"]) / volume) - (vwap * vwap))
        std = math.sqrt(variance)
        distance_bps = ((mid - vwap) / mid) * 10000.0 if mid else 0.0
        session_delta = float(stats.get("delta", 0.0))
        flow_total = float(stats.get("buy_volume", 0.0)) + float(stats.get("sell_volume", 0.0))
        session_delta_imbalance = session_delta / flow_total if flow_total else 0.0
        payload = {
            "vwap": vwap,
            "vwap_distance_bps": distance_bps,
            "session_vwap": vwap,
            "session_vwap_std": std,
            "session_vwap_distance_bps": distance_bps,
            "session_vwap_upper_1": vwap + std,
            "session_vwap_lower_1": vwap - std,
            "session_vwap_upper_2": vwap + (2.0 * std),
            "session_vwap_lower_2": vwap - (2.0 * std),
            "session_volume": volume,
            "session_buy_volume": float(stats.get("buy_volume", 0.0)),
            "session_sell_volume": float(stats.get("sell_volume", 0.0)),
            "session_cumulative_delta": session_delta,
            "session_delta_imbalance": session_delta_imbalance,
            "session_trade_count": int(stats["trade_count"]),
            "session_vwap_started_at": stats["started_at"],
            "session_vwap_anchor": stats.get("anchor"),
            "session_vwap_source": stats.get("source"),
            "session_vwap_live_start_ms": live_start_ms,
            "session_vwap_reconciled_at": stats.get("reconciled_at"),
            "session_volume_profile": self._session_volume_profile_rows(symbol),
        }
        stats.update(payload)
        return payload

    def _ensure_vwap_session(self, symbol: str, anchor_start_ms: int, reset_on_boundary: bool) -> None:
        previous_anchor = self.session_vwap_anchor_start_ms.get(symbol)
        if previous_anchor == anchor_start_ms:
            return
        if previous_anchor is not None and not reset_on_boundary:
            return
        if previous_anchor is not None and reset_on_boundary:
            self.store.add_event(
                "INFO",
                "vwap",
                f"{symbol}: session VWAP reset at {datetime.fromtimestamp(anchor_start_ms / 1000.0, timezone.utc).isoformat(timespec='seconds')}.",
            )
        self.session_vwap.pop(symbol, None)
        self.session_volume_profile.pop(symbol, None)
        self.session_vwap_bootstrapped.discard(symbol)
        self.last_vwap_reconcile.pop(symbol, None)
        self.seen_session_trade_ids.pop(symbol, None)
        self.seen_session_trade_order.pop(symbol, None)
        self.session_vwap_live_start_ms[symbol] = anchor_start_ms
        self.session_vwap_anchor_start_ms[symbol] = anchor_start_ms

    def _bootstrap_session_vwap(self, symbol: str, data_symbol: str, mid: float, bucket_size: float, start_ms: int) -> None:
        vwap_cfg = self.config.get("vwap", {})
        if vwap_cfg.get("source", "binance_klines_plus_live") != "binance_klines_plus_live":
            return

        now = time.time()
        reconcile_seconds = float(vwap_cfg.get("reconcile_seconds", 60.0))
        already_bootstrapped = symbol in self.session_vwap_bootstrapped
        if already_bootstrapped and (now - self.last_vwap_reconcile.get(symbol, 0.0)) < reconcile_seconds:
            return

        now_ms = int(now * 1000)
        try:
            klines = self.market.klines(
                data_symbol,
                interval=str(vwap_cfg.get("interval", "1m")),
                start_time=start_ms,
                end_time=now_ms,
                limit=int(vwap_cfg.get("backfill_limit", 1500)),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_vwap_reconcile[symbol] = now
            self.store.add_event("WARN", "vwap", f"{symbol}: Binance kline VWAP bootstrap failed: {exc}")
            return

        stats = {
            "volume": 0.0,
            "notional": 0.0,
            "price2_notional": 0.0,
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "delta": 0.0,
            "trade_count": 0,
            "started_at": datetime.fromtimestamp(start_ms / 1000.0, timezone.utc).isoformat(timespec="seconds"),
            "source": "binance_klines_plus_live",
            "anchor": vwap_cfg.get("anchor", "utc_day"),
            "anchor_start_ms": start_ms,
            "reconciled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        profile: dict[float, dict[str, float]] = {}
        closed_candles = 0
        live_start_ms = start_ms
        for kline in klines:
            if len(kline) < 8:
                continue
            close_time = int(kline[6])
            if close_time > now_ms:
                continue
            live_start_ms = max(live_start_ms, close_time + 1)
            volume = float(kline[5] or 0.0)
            quote_volume = float(kline[7] or 0.0)
            if volume <= 0 or quote_volume <= 0:
                continue
            candle_vwap = quote_volume / volume
            stats["volume"] += volume
            stats["notional"] += quote_volume
            stats["price2_notional"] += candle_vwap * candle_vwap * volume
            stats["trade_count"] += int(kline[8]) if len(kline) > 8 else 0
            closed_candles += 1
            bucket_low = (candle_vwap // bucket_size) * bucket_size if bucket_size > 0 else candle_vwap
            bucket = profile.setdefault(
                bucket_low,
                {
                    "price_low": bucket_low,
                    "price_high": bucket_low + bucket_size,
                    "volume": 0.0,
                    "buy_volume": 0.0,
                    "sell_volume": 0.0,
                    "delta": 0.0,
                },
            )
            taker_buy_volume = float(kline[9] or 0.0) if len(kline) > 9 else 0.0
            sell_volume = max(0.0, volume - taker_buy_volume)
            stats["buy_volume"] += taker_buy_volume
            stats["sell_volume"] += sell_volume
            stats["delta"] += taker_buy_volume - sell_volume
            bucket["volume"] += volume
            bucket["buy_volume"] += taker_buy_volume
            bucket["sell_volume"] += sell_volume
            bucket["delta"] += taker_buy_volume - sell_volume

        if stats["volume"] <= 0:
            stats["volume"] = 0.0
            stats["notional"] = mid
        self.session_vwap[symbol] = stats
        self.session_volume_profile[symbol] = profile
        self.session_vwap_bootstrapped.add(symbol)
        self.session_vwap_anchor_start_ms[symbol] = start_ms
        self.session_vwap_live_start_ms[symbol] = live_start_ms
        self.last_vwap_reconcile[symbol] = now
        self.seen_session_trade_ids.pop(symbol, None)
        self.seen_session_trade_order.pop(symbol, None)
        action = "reconciled" if already_bootstrapped else "bootstrapped"
        self.store.add_event("INFO", "vwap", f"{symbol}: {action} session VWAP from {closed_candles} Binance closed klines.")

    @staticmethod
    def _vwap_anchor_start_ms(anchor: str) -> int:
        now = datetime.now(timezone.utc)
        if anchor == "rolling_24h":
            return int((time.time() - 86400.0) * 1000)
        session_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(session_start.timestamp() * 1000)

    def _session_volume_profile_rows(self, symbol: str) -> list[dict[str, Any]]:
        profile = self.session_volume_profile.get(symbol, {})
        rows = []
        max_volume = max((float(bucket["volume"]) for bucket in profile.values()), default=0.0) or 1.0
        for low, bucket in sorted(profile.items(), reverse=True):
            price_low = float(bucket["price_low"])
            price_high = float(bucket["price_high"])
            rows.append(
                {
                    "price_low": round(price_low, 4),
                    "price_high": round(price_high, 4),
                    "label": f"{price_low:.0f}-{price_high:.0f}" if price_high >= 100 else f"{price_low:.2f}-{price_high:.2f}",
                    "volume": round(float(bucket["volume"]), 8),
                    "buy_volume": round(float(bucket["buy_volume"]), 8),
                    "sell_volume": round(float(bucket["sell_volume"]), 8),
                    "delta": round(float(bucket["delta"]), 8),
                    "strength": round((float(bucket["volume"]) / max_volume) * 100.0, 2),
                }
            )
        return rows

    def _open_interest_features(self, data_symbol: str, symbol: str, price: float) -> dict[str, Any]:
        oi_cfg = self.config.get("open_interest", {})
        if not oi_cfg.get("enabled", True):
            return {
                "open_interest": None,
                "open_interest_change_pct": 0.0,
                "open_interest_signal": "disabled",
                "open_interest_support": "neutral",
            }

        now = time.time()
        poll_interval = float(oi_cfg.get("poll_interval_seconds", 15))
        history_size = int(oi_cfg.get("history_size", 20))
        min_change_pct = float(oi_cfg.get("min_change_pct", 0.03))
        history = self.oi_history.setdefault(symbol, deque(maxlen=history_size))

        should_poll = (now - self.last_oi_poll.get(symbol, 0.0)) >= poll_interval or not history
        if should_poll:
            try:
                payload = self.market.open_interest(data_symbol)
                open_interest = float(payload.get("openInterest") or 0.0)
                if open_interest > 0:
                    history.append({"ts": now, "open_interest": open_interest, "price": price})
                    self.last_oi_poll[symbol] = now
            except Exception as exc:  # noqa: BLE001
                self.store.add_event("WARN", "open_interest", f"{symbol}: {exc}")

        if not history:
            return {
                "open_interest": None,
                "open_interest_change_pct": 0.0,
                "open_interest_signal": "unknown",
                "open_interest_support": "neutral",
            }

        latest = history[-1]
        baseline = history[0] if len(history) > 1 else latest
        oi = float(latest["open_interest"])
        old_oi = float(baseline["open_interest"])
        old_price = float(baseline["price"])
        oi_change_pct = ((oi - old_oi) / old_oi) * 100.0 if old_oi else 0.0
        price_change_pct = ((price - old_price) / old_price) * 100.0 if old_price else 0.0

        if abs(oi_change_pct) < min_change_pct:
            signal = "oi_flat"
            support = "neutral"
        elif oi_change_pct > 0 and price_change_pct > 0:
            signal = "long_build"
            support = "supports_up"
        elif oi_change_pct > 0 and price_change_pct < 0:
            signal = "short_build"
            support = "supports_down"
        elif oi_change_pct < 0 and price_change_pct > 0:
            signal = "short_covering"
            support = "supports_up_but_less_durable"
        elif oi_change_pct < 0 and price_change_pct < 0:
            signal = "long_liquidation"
            support = "supports_down_but_less_durable"
        else:
            signal = "oi_mixed"
            support = "neutral"

        return {
            "open_interest": oi,
            "open_interest_change_pct": round(oi_change_pct, 4),
            "open_interest_price_change_pct": round(price_change_pct, 4),
            "open_interest_signal": signal,
            "open_interest_support": support,
            "open_interest_history_points": len(history),
        }

    def _execute_candidate(self, candidate: dict[str, Any], symbol_cfg: dict[str, Any]) -> None:
        execution = self.config.get("execution", {})
        mode = execution.get("mode", "mt5_live")
        risk_pct = float(self.config.get("risk", {}).get("risk_per_trade_pct", 0.25))
        mt5_symbol = symbol_cfg.get("mt5_symbol") or candidate["symbol"]

        if mode in {"paper", "hybrid"}:
            tick = self._execution_tick(candidate["symbol"], mt5_symbol)
            if not tick:
                self.store.add_event("WARN", "execution", f"Paper trade blocked: no MT5 tick for {mt5_symbol}.")
                return
            entry_price = self._normalize_broker_price(mt5_symbol, self._entry_price(candidate["side"], tick))
            stop_loss = self._normalize_broker_price(mt5_symbol, candidate["stop_loss"])
            take_profit = self._normalize_broker_price(mt5_symbol, candidate["take_profit"])
            if not self._valid_execution_plan(candidate["side"], entry_price, stop_loss, take_profit):
                self.store.add_event(
                    "WARN",
                    "execution",
                    f"Paper trade blocked: MT5 entry {entry_price:.4f} invalidates planned SL/TP for {mt5_symbol}.",
                )
                return
            equity, _ = self._sizing_equity(allow_mt5=False, fallback_to_paper=True)
            sizing = self._sizing_for(mt5_symbol, equity, risk_pct, entry_price, stop_loss)
            guard = self._execution_guard(candidate, mt5_symbol, entry_price, stop_loss, sizing, equity, risk_pct)
            if not guard["ok"]:
                self.store.add_event("WARN", "execution", f"Paper trade blocked: {guard['reason']}")
                return
            stop_loss = self._normalize_broker_price(mt5_symbol, guard["stop_loss"])
            trade = {
                **candidate,
                "entry": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "mt5_symbol": mt5_symbol,
                "lots": guard["lots"],
                "risk_cash": guard["risk_cash"],
                "execution_mode": mode,
                "broker_requested_price": entry_price,
                "broker_fill_price": entry_price,
                "broker_message": f"Paper mode trade filled from MT5 {mt5_symbol} {candidate['side']} price. {guard['reason']}",
            }
            trade_id = self.store.open_trade(trade)
            self._remember_structure_bias(trade_id, trade)
            self.store.add_event("INFO", "execution", f"Opened paper trade #{trade_id}: {candidate['symbol']} {candidate['side']} at MT5 {entry_price:.4f}.")
            self.telegram.send_trade_opened(trade_id, trade)
            if mode == "hybrid":
                self._open_hybrid_mt5_copy(trade_id, trade, mt5_symbol, execution)
            return

        if not mode.startswith("mt5"):
            self.store.add_event("WARN", "execution", f"Unsupported execution mode: {mode}")
            return

        watchdog_block = self._watchdog_live_entry_block_reason()
        if watchdog_block:
            self.store.add_event("ERROR", "execution", f"MT5 order blocked by watchdog: {watchdog_block}")
            return

        if mode == "mt5_live" and not execution.get("allow_live_orders", False):
            self.store.add_event("WARN", "execution", "Live MT5 order blocked because allow_live_orders=false.")
            return

        try:
            equity, _ = self._sizing_equity(allow_mt5=True, fallback_to_paper=False)
        except RuntimeError as exc:
            self.store.add_event("WARN", "execution", f"Cannot read sizing equity: {exc}")
            return
        tick = self._execution_tick(candidate["symbol"], mt5_symbol)
        if not tick:
            self.store.add_event("WARN", "execution", f"MT5 order blocked: no MT5 tick for {mt5_symbol}.")
            return
        entry_price = self._normalize_broker_price(mt5_symbol, self._entry_price(candidate["side"], tick))
        stop_loss = self._normalize_broker_price(mt5_symbol, candidate["stop_loss"])
        take_profit = self._normalize_broker_price(mt5_symbol, candidate["take_profit"])
        if not self._valid_execution_plan(candidate["side"], entry_price, stop_loss, take_profit):
            self.store.add_event(
                "WARN",
                "execution",
                f"MT5 order blocked: current MT5 entry {entry_price:.4f} invalidates planned SL/TP for {mt5_symbol}.",
            )
            return

        sizing = self._sizing_for(mt5_symbol, equity, risk_pct, entry_price, stop_loss)
        if not sizing or not sizing.valid:
            reason = sizing.reason if sizing else "No verified MT5 contract spec."
            self.store.add_event("WARN", "execution", f"Lot sizing failed for {mt5_symbol}: {reason}")
            return
        guard = self._execution_guard(candidate, mt5_symbol, entry_price, stop_loss, sizing, equity, risk_pct)
        if not guard["ok"]:
            self.store.add_event("WARN", "execution", f"MT5 order blocked: {guard['reason']}")
            return
        stop_loss = self._normalize_broker_price(mt5_symbol, guard["stop_loss"])

        result = send_market_order(
            symbol=mt5_symbol,
            side=candidate["side"],
            lots=guard["lots"],
            stop_loss=stop_loss,
            take_profit=take_profit,
            deviation_points=int(execution.get("deviation_points", 30)),
            magic_number=int(execution.get("magic_number", 260618)),
            comment=str(execution.get("comment", "TradingAgent")),
        )
        if not result.ok:
            self.store.add_event("ERROR", "execution", f"MT5 order failed for {mt5_symbol}: {result.message}")
            return

        trade = {
            **candidate,
            "entry": result.price or entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "mt5_symbol": mt5_symbol,
            "lots": result.volume or guard["lots"],
            "risk_cash": guard["risk_cash"],
            "execution_mode": mode,
            "broker_requested_price": entry_price,
            "broker_fill_price": result.price or entry_price,
            "broker_order_id": result.order_id,
            "broker_deal_id": result.deal_id,
            "broker_position_id": self._find_live_position_id(mt5_symbol, candidate["side"], result.order_id),
            "broker_retcode": result.retcode,
            "broker_message": f"{result.message}; {guard['reason']}",
        }
        trade_id = self.store.open_trade(trade)
        self._remember_structure_bias(trade_id, trade)
        self.store.add_event("INFO", "execution", f"Opened real MT5 trade #{trade_id}: {mt5_symbol} {candidate['side']} {trade['lots']} lots.")
        self.telegram.send_trade_opened(trade_id, trade)

    def _open_hybrid_mt5_copy(self, trade_id: int, trade: dict[str, Any], mt5_symbol: str, execution: dict[str, Any]) -> None:
        watchdog_block = self._watchdog_live_entry_block_reason()
        if watchdog_block:
            message = f"Hybrid MT5 mirror blocked by watchdog: {watchdog_block}"
            self.store.update_hybrid_mt5_failure(trade_id, message)
            self.store.add_event("ERROR", "execution", f"Trade #{trade_id}: {message}")
            return

        if not execution.get("allow_live_orders", False):
            message = "Hybrid MT5 mirror blocked because allow_live_orders=false."
            self.store.update_hybrid_mt5_failure(trade_id, message)
            self.store.add_event("WARN", "execution", f"Trade #{trade_id}: {message}")
            return

        lots = float(trade.get("lots") or 0.0)
        if lots <= 0:
            message = "Hybrid MT5 mirror blocked because paper lot size is zero."
            self.store.update_hybrid_mt5_failure(trade_id, message)
            self.store.add_event("WARN", "execution", f"Trade #{trade_id}: {message}")
            return

        side = str(trade["side"])
        tick = self._execution_tick(str(trade["symbol"]), mt5_symbol)
        requested_price = self._normalize_broker_price(mt5_symbol, self._entry_price(side, tick)) if tick else float(trade["entry"])
        result = send_market_order(
            symbol=mt5_symbol,
            side=side,
            lots=lots,
            stop_loss=float(trade["stop_loss"]),
            take_profit=float(trade["take_profit"]),
            deviation_points=int(execution.get("deviation_points", 30)),
            magic_number=int(execution.get("magic_number", 260618)),
            comment=str(execution.get("comment", "TradingAgent"))[:20],
        )
        if not result.ok:
            message = f"Hybrid MT5 mirror failed: {result.message}"
            self.store.update_hybrid_mt5_failure(trade_id, message)
            self.store.add_event("ERROR", "execution", f"Trade #{trade_id}: {message}")
            return

        fill_price = float(result.price or requested_price)
        position_id = self._find_live_position_id(mt5_symbol, side, result.order_id)
        self.store.update_hybrid_mt5_open(
            trade_id,
            requested_price=requested_price,
            fill_price=fill_price,
            stop_loss=float(trade["stop_loss"]),
            take_profit=float(trade["take_profit"]),
            order_id=result.order_id,
            deal_id=result.deal_id,
            position_id=position_id,
            retcode=result.retcode,
            message=f"Hybrid MT5 mirror opened. {result.message}",
        )
        self.store.add_event(
            "INFO",
            "execution",
            f"Opened hybrid MT5 mirror for paper trade #{trade_id}: {mt5_symbol} {side} {lots:.2f} lots.",
        )

    def _execution_tick(self, symbol: str, mt5_symbol: str) -> dict[str, Any] | None:
        ok, tick, message = symbol_tick(mt5_symbol)
        if not ok:
            self.execution_prices[symbol] = {"mt5_symbol": mt5_symbol, "ok": False, "message": message}
            return None
        bid = float(tick.get("bid") or 0.0)
        ask = float(tick.get("ask") or 0.0)
        if bid <= 0 or ask <= 0:
            self.execution_prices[symbol] = {"mt5_symbol": mt5_symbol, "ok": False, "message": "MT5 bid/ask is missing."}
            return None
        payload = {
            "mt5_symbol": mt5_symbol,
            "ok": True,
            "bid": bid,
            "ask": ask,
            "last": float(tick.get("last") or 0.0),
            "time": tick.get("time_msc") or tick.get("time"),
        }
        self.execution_prices[symbol] = payload
        return payload

    @staticmethod
    def _entry_price(side: str, tick: dict[str, Any]) -> float:
        return float(tick["ask"] if side == "BUY" else tick["bid"])

    @staticmethod
    def _close_mark_price(side: str, tick: dict[str, Any]) -> float:
        return float(tick["bid"] if side == "BUY" else tick["ask"])

    def _paper_mark_price(self, trade: dict[str, Any]) -> float | None:
        mt5_symbol = trade.get("mt5_symbol") or trade["symbol"]
        tick = self._execution_tick(str(trade["symbol"]), str(mt5_symbol))
        if not tick:
            return None
        return self._normalize_broker_price(str(mt5_symbol), self._close_mark_price(str(trade["side"]), tick))

    def _contract_spec(self, mt5_symbol: str) -> dict[str, Any] | None:
        rows = self.store.query("select * from symbol_contracts where symbol = ?", (mt5_symbol,))
        if not rows:
            spec = scan_symbol(mt5_symbol)
            if spec.get("available"):
                self.store.save_contract(mt5_symbol, spec)
                rows = self.store.query("select * from symbol_contracts where symbol = ?", (mt5_symbol,))
        if not rows:
            return None
        return rows[0]

    def _normalize_broker_price(self, mt5_symbol: str, price: float) -> float:
        spec = self._contract_spec(mt5_symbol)
        if not spec:
            return float(price)
        return normalize_price(float(price), spec)

    def _sizing_for(self, mt5_symbol: str, equity: float, risk_pct: float, entry: float, stop_loss: float):
        spec = self._contract_spec(mt5_symbol)
        if not spec:
            return None
        return calculate_lot_size(equity=equity, risk_pct=risk_pct, entry=entry, stop_loss=stop_loss, spec=spec)

    def _execution_guard(
        self,
        candidate: dict[str, Any],
        mt5_symbol: str,
        entry: float,
        stop_loss: float,
        sizing: Any,
        equity: float,
        risk_pct: float,
    ) -> dict[str, Any]:
        cfg = self.config.get("execution_guards", {})
        if not cfg.get("enabled", True):
            return {
                "ok": bool(sizing and sizing.valid),
                "stop_loss": float(stop_loss),
                "lots": float(getattr(sizing, "lots", 0.0) or 0.0),
                "risk_cash": float(getattr(sizing, "risk_cash", 0.0) or 0.0),
                "reason": "Execution guards disabled.",
            }
        if not sizing or not sizing.valid:
            return {"ok": False, "stop_loss": float(stop_loss), "lots": 0.0, "risk_cash": 0.0, "reason": getattr(sizing, "reason", "Invalid lot sizing.")}

        symbol = str(candidate["symbol"])
        stop_distance = abs(float(entry) - float(stop_loss))
        stop_bps = (stop_distance / float(entry)) * 10000.0 if entry else 0.0
        min_distance = self._symbol_config_value(cfg, "min_stop_distance", symbol, mt5_symbol, 0.0)
        min_stop_bps = self._symbol_config_value(cfg, "min_stop_bps", symbol, mt5_symbol, 0.0)
        min_bps_distance = abs(float(entry)) * (min_stop_bps / 10000.0) if min_stop_bps > 0 else 0.0
        required_distance = max(min_distance, min_bps_distance)
        adjusted_stop = float(stop_loss)
        adjusted = False
        adjustment_reason = ""
        if required_distance > 0 and stop_distance < required_distance:
            if not cfg.get("widen_tight_stops", True):
                return {
                    "ok": False,
                    "stop_loss": float(stop_loss),
                    "lots": 0.0,
                    "risk_cash": 0.0,
                    "reason": (
                        f"{symbol}/{mt5_symbol} stop distance {stop_distance:.4f} ({stop_bps:.2f} bps) is below "
                        f"minimum {required_distance:.4f}; tight-stop widening disabled."
                    ),
                }
            adjusted_stop = float(entry) - required_distance if str(candidate["side"]).upper() == "BUY" else float(entry) + required_distance
            adjusted_stop = self._normalize_broker_price(mt5_symbol, adjusted_stop)
            if not self._valid_execution_plan(str(candidate["side"]), float(entry), adjusted_stop, float(candidate["take_profit"])):
                return {
                    "ok": False,
                    "stop_loss": float(stop_loss),
                    "lots": 0.0,
                    "risk_cash": 0.0,
                    "reason": f"{symbol}/{mt5_symbol} widened SL would invalidate entry/TP plan.",
                }
            rr_after = self._candidate_rr(str(candidate["side"]), float(entry), adjusted_stop, float(candidate["take_profit"]))
            min_rr = float(self.config.get("risk", {}).get("min_rr", 2.0))
            if rr_after < min_rr:
                return {
                    "ok": False,
                    "stop_loss": adjusted_stop,
                    "lots": 0.0,
                    "risk_cash": 0.0,
                    "reason": f"{symbol}/{mt5_symbol} widened SL drops RR to {rr_after:.2f}, below minimum {min_rr:.2f}.",
                }
            adjusted = True
            adjustment_reason = (
                f"widened SL from {float(stop_loss):.4f} to {adjusted_stop:.4f} so stop distance "
                f"expands from {stop_distance:.4f} to {abs(float(entry) - adjusted_stop):.4f}"
            )
            sizing = self._sizing_for(mt5_symbol, float(equity), float(risk_pct), float(entry), adjusted_stop)
            if not sizing or not sizing.valid:
                return {
                    "ok": False,
                    "stop_loss": adjusted_stop,
                    "lots": 0.0,
                    "risk_cash": 0.0,
                    "reason": f"{symbol}/{mt5_symbol} lot sizing failed after SL widening: {getattr(sizing, 'reason', 'No contract spec.')}",
                }
            stop_distance = abs(float(entry) - adjusted_stop)
            stop_bps = (stop_distance / float(entry)) * 10000.0 if entry else 0.0

        lots = float(sizing.lots)
        risk_cash = float(sizing.risk_cash)
        max_lots = self._symbol_config_value(cfg, "max_lots", symbol, mt5_symbol, 0.0)
        reason = f"Execution guard passed: stop {stop_distance:.4f} ({stop_bps:.2f} bps), lots {lots:.4f}."
        if adjusted:
            reason = f"Execution guard {adjustment_reason}; recalculated lots {lots:.4f}."
        if max_lots > 0 and lots > max_lots:
            if not cfg.get("cap_oversized_lots", True):
                return {
                    "ok": False,
                    "stop_loss": adjusted_stop,
                    "lots": 0.0,
                    "risk_cash": 0.0,
                    "reason": f"{symbol}/{mt5_symbol} lot size {lots:.4f} exceeds max {max_lots:.4f}.",
                }
            lots = max_lots
            risk_cash = float(sizing.loss_per_lot) * lots
            reason = (
                f"{reason} Capped lots from {float(sizing.lots):.4f} to {lots:.4f}; "
                f"actual risk about {risk_cash:.2f}."
            )
        return {"ok": True, "stop_loss": adjusted_stop, "lots": lots, "risk_cash": risk_cash, "reason": reason}

    @staticmethod
    def _symbol_config_value(config: dict[str, Any], key: str, symbol: str, mt5_symbol: str, default: float) -> float:
        values = config.get(key, {})
        if isinstance(values, dict):
            for lookup in (symbol, mt5_symbol):
                if lookup in values:
                    return float(values[lookup])
            return float(default)
        if values is None:
            return float(default)
        return float(values)

    @classmethod
    def _strategy_symbol_config_value(
        cls,
        config: dict[str, Any],
        key: str,
        strategy: str,
        symbol: str,
        mt5_symbol: str,
        default: float,
    ) -> float:
        strategy_values = config.get(f"{key}_by_strategy", {})
        if isinstance(strategy_values, dict):
            values = strategy_values.get(strategy, {})
            if isinstance(values, dict):
                for lookup in (symbol, mt5_symbol):
                    if lookup in values:
                        return float(values[lookup])
            elif values is not None:
                return float(values)
        return cls._symbol_config_value(config, key, symbol, mt5_symbol, default)

    @staticmethod
    def _bps_value_from_price(values: Any, symbol: str, mt5_symbol: str, default_bps: float, price: float) -> float:
        bps = default_bps
        if isinstance(values, dict):
            if symbol in values:
                bps = float(values[symbol])
            elif mt5_symbol in values:
                bps = float(values[mt5_symbol])
        elif values is not None:
            bps = float(values)
        return abs(float(price)) * (bps / 10000.0)

    @staticmethod
    def _candidate_rr(side: str, entry: float, stop_loss: float, take_profit: float) -> float:
        risk = entry - stop_loss if side == "BUY" else stop_loss - entry
        reward = take_profit - entry if side == "BUY" else entry - take_profit
        if risk <= 0:
            return 0.0
        return reward / risk

    @staticmethod
    def _valid_execution_plan(side: str, entry: float, stop_loss: float, take_profit: float) -> bool:
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
            return False
        if side == "BUY":
            return stop_loss < entry < take_profit
        return take_profit < entry < stop_loss

    def _find_live_position_id(self, mt5_symbol: str, side: str, order_id: int | None) -> int | None:
        ok, positions, _ = open_positions(mt5_symbol)
        if not ok:
            return None
        side_type = 0 if side == "BUY" else 1
        matches = [p for p in positions if str(p.get("symbol")) == mt5_symbol and int(p.get("type", -1)) == side_type]
        if order_id is not None:
            for position in matches:
                if int(position.get("ticket", 0) or 0) == int(order_id):
                    return int(position.get("ticket"))
                if int(position.get("identifier", 0) or 0) == int(order_id):
                    return int(position.get("ticket"))
        if matches:
            return int(matches[-1].get("ticket", 0) or 0) or None
        return None

    @staticmethod
    def _match_position(trade: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any] | None:
        broker_position_id = int(trade.get("broker_position_id") or 0)
        if broker_position_id:
            for position in positions:
                if int(position.get("ticket", 0) or 0) == broker_position_id:
                    return position
                if int(position.get("identifier", 0) or 0) == broker_position_id:
                    return position
            return None
        side_type = 0 if trade["side"] == "BUY" else 1
        mt5_symbol = trade.get("mt5_symbol") or trade["symbol"]
        matches = [
            position
            for position in positions
            if str(position.get("symbol")) == str(mt5_symbol) and int(position.get("type", -1)) == side_type
        ]
        return matches[-1] if len(matches) == 1 else None

    def _manage_open_trades(self, symbol: str, price: float) -> None:
        mode = self.config.get("execution", {}).get("mode", "")
        if mode.startswith("mt5"):
            self._manage_live_mt5_trades(symbol)
            return
        open_trades = [trade for trade in self.store.get_open_trades() if trade["symbol"] == symbol]
        for trade in open_trades:
            side = trade["side"]
            mark = self._paper_mark_price(trade)
            if mark is None:
                self.store.add_event("WARN", "manager", f"Cannot manage paper trade #{trade['id']}: no MT5 mark price.")
                continue
            exit_signal = self._active_exit_signal(trade, self.last_features.get(symbol, {}), mark)
            if exit_signal["exit"]:
                pnl = self._paper_trade_pnl(trade, mark)
                self.store.close_trade(trade["id"], mark, pnl, exit_signal["reason"])
                self.store.add_event(
                    "INFO",
                    "manager",
                    f"Closed paper trade #{trade['id']} early at MT5 {mark:.4f}, PnL {pnl:.2f}: {exit_signal['reason']}",
                )
                self.telegram.send_trade_closed(trade, mark, pnl, exit_signal["reason"], self.store.stats())
                continue
            if exit_signal.get("protect"):
                self._protect_paper_trade_after_warning(trade, mark, exit_signal["reason"])
                continue
            self._lock_paper_trade_profit(trade, mark)
            trade = self.store.query("select * from trades where id = ?", (trade["id"],))[0]
            hit_sl = mark <= trade["stop_loss"] if side == "BUY" else mark >= trade["stop_loss"]
            hit_tp = mark >= trade["take_profit"] if side == "BUY" else mark <= trade["take_profit"]
            if not hit_sl and not hit_tp:
                continue

            pnl = self._paper_trade_pnl(trade, mark)
            exit_reason = "Paper TP hit." if hit_tp else "Paper SL hit."
            self.store.close_trade(trade["id"], mark, pnl, exit_reason)
            self.store.add_event("INFO", "manager", f"Closed paper trade #{trade['id']} at MT5 {mark:.4f}, PnL {pnl:.2f}: {exit_reason}")
            self.telegram.send_trade_closed(trade, mark, pnl, exit_reason, self.store.stats())
        if mode == "hybrid":
            self._manage_hybrid_mt5_trades(symbol)

    def _manage_live_mt5_trades(self, symbol: str) -> None:
        open_trades = [trade for trade in self.store.get_open_trades() if trade["symbol"] == symbol]
        if not open_trades:
            return
        mt5_symbol = open_trades[0].get("mt5_symbol") or symbol
        ok, positions, message = open_positions(mt5_symbol)
        if not ok:
            self.store.add_event("WARN", "manager", f"Cannot read MT5 positions for {mt5_symbol}: {message}")
            return
        for trade in open_trades:
            position = self._match_position(trade, positions)
            if not position:
                self._sync_closed_mt5_trade(trade)
                continue
            self._remember_position_execution_price(trade, position)
            exit_signal = self._active_exit_signal(trade, self.last_features.get(symbol, {}), float(position.get("price_current") or 0.0))
            if exit_signal["exit"]:
                self._close_live_mt5_trade_early(trade, position, exit_signal["reason"])
                continue
            if exit_signal.get("protect"):
                self._protect_live_mt5_trade_after_warning(trade, position, exit_signal["reason"])
                continue
            self._lock_live_mt5_profit(trade, position)

    def _manage_hybrid_mt5_trades(self, symbol: str) -> None:
        open_trades = self.store.get_open_hybrid_mt5_trades(symbol)
        if not open_trades:
            return
        mt5_symbols = sorted({str(trade.get("mt5_symbol") or trade["symbol"]) for trade in open_trades})
        positions_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for mt5_symbol in mt5_symbols:
            ok, positions, message = open_positions(mt5_symbol)
            if not ok:
                self.store.add_event("WARN", "manager", f"Cannot read MT5 hybrid positions for {mt5_symbol}: {message}")
                positions = []
            positions_by_symbol[mt5_symbol] = positions

        for trade in open_trades:
            mt5_symbol = str(trade.get("mt5_symbol") or trade["symbol"])
            position = self._match_hybrid_position(trade, positions_by_symbol.get(mt5_symbol, []))
            if not position:
                self._sync_closed_hybrid_mt5_trade(trade)
                continue
            self._remember_position_execution_price(trade, position)
            self._lock_hybrid_mt5_option6(trade, position)

    def _match_hybrid_position(self, trade: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any] | None:
        return self._match_position(
            {**trade, "broker_position_id": trade.get("hybrid_mt5_position_id")},
            positions,
        )

    def _sync_closed_hybrid_mt5_trade(self, trade: dict[str, Any]) -> None:
        trade_id = int(trade["id"])
        position_id = int(trade.get("hybrid_mt5_position_id") or trade.get("hybrid_mt5_order_id") or 0)
        if position_id <= 0:
            self.store.add_event("WARN", "manager", f"Hybrid MT5 mirror #{trade_id} is missing position id; cannot sync close.")
            return
        ok, result, message = closed_position_result(position_id, lookback_days=14)
        if not ok:
            self.store.add_event(
                "WARN",
                "manager",
                f"Hybrid MT5 mirror for trade #{trade_id} is gone, but close result is not available yet: {message}",
            )
            return
        exit_price = float(result.get("exit_price") or trade.get("hybrid_mt5_exit_price") or trade["entry"])
        pnl = float(result.get("pnl") or 0.0)
        reason = "Hybrid MT5 position closed by broker SL/TP."
        self.store.update_hybrid_mt5_close_result(trade_id, exit_price, pnl, reason)
        self.store.add_event("INFO", "manager", f"Synced hybrid MT5 mirror #{trade_id} at {exit_price:.4f}, PnL {pnl:.2f}.")

    def _lock_hybrid_mt5_option6(self, trade: dict[str, Any], position: dict[str, Any]) -> None:
        side = str(trade["side"])
        entry = float(trade.get("hybrid_mt5_fill_price") or position.get("price_open") or trade["entry"])
        current_sl = float(position.get("sl") or trade.get("hybrid_mt5_stop_loss") or trade["stop_loss"])
        current_tp = float(position.get("tp") or trade.get("hybrid_mt5_take_profit") or trade["take_profit"])
        initial_sl = float(trade.get("initial_stop_loss") or trade["stop_loss"])
        risk_distance = abs(entry - initial_sl)
        if risk_distance <= 0:
            return

        price = float(position.get("price_current") or 0.0)
        if price <= 0:
            tick = self._execution_tick(str(trade["symbol"]), str(trade.get("mt5_symbol") or trade["symbol"]))
            if not tick:
                return
            price = self._close_mark_price(side, tick)

        favorable = (price - entry) if side == "BUY" else (entry - price)
        current_r = favorable / risk_distance
        if current_r < 1.5:
            return

        desired_sl = entry + (risk_distance * 0.30) if side == "BUY" else entry - (risk_distance * 0.30)
        improves = desired_sl > current_sl if side == "BUY" else desired_sl < current_sl
        if not improves:
            return

        mt5_symbol = str(trade.get("mt5_symbol") or trade["symbol"])
        desired_sl = self._normalize_broker_price(mt5_symbol, desired_sl)
        result = modify_position_sl_tp(
            symbol=mt5_symbol,
            position_id=int(position.get("ticket")),
            stop_loss=desired_sl,
            take_profit=current_tp,
        )
        if not result.ok:
            self.store.add_event("WARN", "manager", f"Hybrid MT5 SL update failed for trade #{trade['id']}: {result.message}")
            return
        self.store.update_hybrid_mt5_stop_loss(
            int(trade["id"]),
            desired_sl,
            f"Hybrid option 6 BE lock 0.30R after {current_r:.2f}R; SL moved from {current_sl:.4f} to {desired_sl:.4f}.",
        )
        self.store.add_event(
            "INFO",
            "manager",
            f"Hybrid MT5 trade #{trade['id']} locked 0.30R after {current_r:.2f}R; SL moved to {desired_sl:.4f}.",
        )

    def _close_live_mt5_trade_early(self, trade: dict[str, Any], position: dict[str, Any], reason: str) -> None:
        mt5_symbol = str(trade.get("mt5_symbol") or trade["symbol"])
        position_id = int(trade.get("broker_position_id") or position.get("ticket") or 0)
        volume = float(position.get("volume") or trade.get("lots") or 0.0)
        if position_id <= 0 or volume <= 0:
            self.store.add_event("WARN", "manager", f"Cannot early-close trade #{trade['id']}: missing MT5 position id or volume.")
            return
        execution = self.config.get("execution", {})
        result = close_position(
            symbol=mt5_symbol,
            position_id=position_id,
            side=str(trade["side"]),
            volume=volume,
            deviation_points=int(execution.get("deviation_points", 30)),
            magic_number=int(execution.get("magic_number", 260618)),
            comment=str(execution.get("comment", "TradingAgent"))[:20],
        )
        if not result.ok:
            self.store.add_event("WARN", "manager", f"MT5 early close failed for trade #{trade['id']}: {result.message}")
            return

        history_ok, closed, history_message = closed_position_result(position_id, lookback_days=14)
        if history_ok:
            exit_price = float(closed.get("exit_price") or result.price or position.get("price_current") or trade["entry"])
            pnl = float(closed.get("pnl") or 0.0)
        else:
            exit_price = float(result.price or position.get("price_current") or trade["entry"])
            pnl = float(position.get("profit") or 0.0)
            self.store.add_event("WARN", "manager", f"MT5 history not ready after early close #{trade['id']}: {history_message}")
        self.store.update_trade_close_result(int(trade["id"]), exit_price, pnl, reason)
        self.store.add_event("INFO", "manager", f"Closed MT5 trade #{trade['id']} early at {exit_price:.4f}, PnL {pnl:.2f}: {reason}")
        self.telegram.send_trade_closed(trade, exit_price, pnl, reason, self.store.stats())

    def _active_exit_signal(self, trade: dict[str, Any], features: dict[str, Any], mark: float) -> dict[str, Any]:
        cfg = self.config.get("active_exit", {})
        if not cfg.get("enabled", True):
            return {"exit": False, "reason": "Active exit disabled."}
        if not features:
            return {"exit": False, "reason": "No live features for active exit."}

        age = self._trade_age_seconds(trade)
        min_age = float(cfg.get("min_trade_age_seconds", 20.0))
        if age < min_age:
            return {"exit": False, "reason": f"Trade age {age:.1f}s below active-exit minimum {min_age:.1f}s."}

        side = str(trade["side"]).upper()
        symbol = str(trade["symbol"])
        adverse_side = "SELL" if side == "BUY" else "BUY"
        required = int(cfg.get("min_adverse_passes", 3))
        unrealized_r = self._trade_unrealized_r(trade, mark)
        target_exit = self._target_liquidity_profit_exit(trade, features, mark, cfg, unrealized_r)
        if target_exit["exit"] or target_exit.get("protect"):
            return target_exit

        passes = 0
        details: list[str] = []

        absorption = self._opposite_absorption(adverse_side, features, cfg)
        if cfg.get("close_on_opposite_absorption", True) and absorption["ok"]:
            passes += 1
            details.append(absorption["reason"])

        delta_imbalance = float(features.get("delta_imbalance", features.get("flow_imbalance", 0.0)))
        min_delta = float(cfg.get("min_delta_imbalance", 0.10))
        if (side == "BUY" and delta_imbalance <= -min_delta) or (side == "SELL" and delta_imbalance >= min_delta):
            passes += 1
            details.append(f"adverse delta imbalance {delta_imbalance:.3f}")

        recent_cvd = float(features.get("cumulative_delta", features.get("delta", 0.0)))
        min_recent_cvd = self._symbol_config_value(cfg, "min_recent_cvd", symbol, str(trade.get("mt5_symbol") or symbol), 0.25)
        if (side == "BUY" and recent_cvd <= -min_recent_cvd) or (side == "SELL" and recent_cvd >= min_recent_cvd):
            passes += 1
            details.append(f"adverse recent CVD {recent_cvd:.4f}")

        session_delta = float(features.get("session_delta_imbalance", 0.0))
        min_session_delta = float(cfg.get("min_session_delta_imbalance", 0.01))
        if (side == "BUY" and session_delta <= -min_session_delta) or (side == "SELL" and session_delta >= min_session_delta):
            passes += 1
            details.append(f"adverse session delta {session_delta:.3f}")

        oi_support = str(features.get("open_interest_support", "neutral"))
        if (side == "BUY" and oi_support.startswith("supports_down")) or (side == "SELL" and oi_support.startswith("supports_up")):
            passes += 1
            details.append(f"OI flipped adverse: {oi_support}")

        regime = self._market_regime(symbol, features)
        regime_adverse = (side == "BUY" and regime["direction"] == "bearish") or (side == "SELL" and regime["direction"] == "bullish")
        if cfg.get("close_on_regime_flip", True) and regime_adverse:
            passes += 1
            details.append(regime["reason"])

        if absorption["ok"] and passes >= required:
            reason = f"opposite {adverse_side} absorption/reversal {passes}/{required}: " + "; ".join(details)
            protection = self._active_exit_protection_warning(trade, cfg, unrealized_r, reason)
            if protection["protect"]:
                return protection
            return self._active_exit_risk_gate(cfg, age, unrealized_r, passes, required, reason)
        if regime_adverse and passes >= required + 1:
            reason = f"adverse regime reversal {passes}/{required + 1}: " + "; ".join(details)
            return self._active_exit_risk_gate(cfg, age, unrealized_r, passes, required + 1, reason)
        return {"exit": False, "reason": f"Active exit held: adverse evidence {passes}/{required}; mark {mark:.4f}; r={unrealized_r:.2f}."}

    def _target_liquidity_profit_exit(
        self,
        trade: dict[str, Any],
        features: dict[str, Any],
        mark: float,
        cfg: dict[str, Any],
        unrealized_r: float,
    ) -> dict[str, Any]:
        if not cfg.get("target_absorption_exit_enabled", True):
            return {"exit": False, "reason": "Target absorption exit disabled."}
        side = str(trade["side"]).upper()
        symbol = str(trade["symbol"])
        mt5_symbol = str(trade.get("mt5_symbol") or symbol)
        min_profit_r = float(cfg.get("target_absorption_min_profit_r", 1.0))
        if unrealized_r < min_profit_r:
            return {"exit": False, "reason": f"Target absorption exit held: r={unrealized_r:.2f} below {min_profit_r:.2f}."}

        proximity_bps = self._symbol_config_value(cfg, "target_absorption_proximity_bps", symbol, mt5_symbol, 10.0)
        take_profit = float(trade.get("take_profit") or 0.0)
        near_tp = take_profit > 0 and (abs(mark - take_profit) / mark * 10000.0 if mark else 999999.0) <= proximity_bps
        target_side = "resistance" if side == "BUY" else "support"
        matching_zones = []
        for zone in features.get("major_liquidity_zones") or []:
            if str(zone.get("side")) != target_side:
                continue
            low = float(zone.get("price_low", 0.0))
            high = float(zone.get("price_high", 0.0))
            if high <= low:
                continue
            distance = 0.0 if low <= mark <= high else min(abs(mark - low), abs(mark - high))
            distance_bps = distance / mark * 10000.0 if mark else 999999.0
            if distance_bps <= proximity_bps:
                matching_zones.append((distance_bps, zone))

        if not near_tp and not matching_zones:
            return {"exit": False, "reason": "Target absorption exit held: price is not at opposing major liquidity."}

        adverse_side = "SELL" if side == "BUY" else "BUY"
        absorption = self._opposite_absorption(adverse_side, features, cfg)
        if not absorption["ok"]:
            return {"exit": False, "reason": f"Target liquidity reached but no absorption yet: {absorption['reason']}"}

        zone_text = "TP" if near_tp else ""
        if matching_zones:
            _, zone = sorted(matching_zones, key=lambda item: item[0])[0]
            zone_text = f"{zone.get('side')} {zone.get('label')} score {float(zone.get('score', 0.0)):.1f}"
        reason = (
            f"opposing liquidity absorption warning at {zone_text}; r={unrealized_r:.2f}; "
            f"{absorption['reason']}"
        )
        protection = self._active_exit_protection_warning(trade, cfg, unrealized_r, reason)
        if protection["protect"]:
            return protection
        return {
            "exit": True,
            "reason": (
                f"profit booked at opposing liquidity {zone_text}; r={unrealized_r:.2f}; "
                f"{absorption['reason']}"
            ),
        }

    def _active_exit_protection_warning(
        self,
        trade: dict[str, Any],
        cfg: dict[str, Any],
        unrealized_r: float,
        reason: str,
    ) -> dict[str, Any]:
        if not cfg.get("protect_on_first_absorption", True):
            return {"exit": False, "protect": False, "reason": "First absorption protection disabled."}
        min_profit_r = float(cfg.get("absorption_protection_min_profit_r", cfg.get("min_profit_r_to_close", 0.10)))
        if unrealized_r < min_profit_r:
            return {
                "exit": False,
                "protect": False,
                "reason": f"Absorption protection held: r={unrealized_r:.2f} below {min_profit_r:.2f}.",
            }
        if self._trade_profit_protected(trade):
            return {"exit": False, "protect": False, "reason": "Trade is already profit-protected."}
        return {
            "exit": False,
            "protect": True,
            "reason": f"{reason}; first warning protects profit instead of full close.",
        }

    @staticmethod
    def _trade_unrealized_r(trade: dict[str, Any], mark: float) -> float:
        entry = float(trade.get("entry") or 0.0)
        stop = float(trade.get("initial_stop_loss") or trade.get("stop_loss") or 0.0)
        if entry <= 0 or stop <= 0 or mark <= 0:
            return 0.0
        risk = abs(entry - stop)
        if risk <= 0:
            return 0.0
        side = str(trade.get("side", "")).upper()
        move = mark - entry if side == "BUY" else entry - mark
        return move / risk

    @staticmethod
    def _trade_profit_protected(trade: dict[str, Any]) -> bool:
        entry = float(trade.get("entry") or 0.0)
        stop = float(trade.get("stop_loss") or 0.0)
        if entry <= 0 or stop <= 0:
            return False
        side = str(trade.get("side", "")).upper()
        if side == "BUY":
            return stop >= entry
        return stop <= entry

    @staticmethod
    def _active_exit_risk_gate(
        cfg: dict[str, Any],
        age: float,
        unrealized_r: float,
        passes: int,
        required: int,
        reason: str,
    ) -> dict[str, Any]:
        min_profit_r = float(cfg.get("min_profit_r_to_close", 0.10))
        if unrealized_r >= min_profit_r:
            return {"exit": True, "reason": f"{reason}; protected/profitable r={unrealized_r:.2f}."}

        if unrealized_r >= 0:
            return {
                "exit": False,
                "reason": (
                    f"Active exit held: adverse evidence {passes}/{required}, but trade is only r={unrealized_r:.2f}; "
                    f"needs r>={min_profit_r:.2f} before discretionary close."
                ),
            }

        if not cfg.get("close_losing_trades", False):
            return {
                "exit": False,
                "reason": (
                    f"Active exit held: adverse evidence {passes}/{required}, but trade is negative r={unrealized_r:.2f}; "
                    "close_losing_trades=false."
                ),
            }

        hard_age = float(cfg.get("hard_invalidation_min_age_seconds", 180.0))
        hard_passes = int(cfg.get("hard_invalidation_min_adverse_passes", required + 2))
        hard_adverse_r = float(cfg.get("hard_invalidation_min_adverse_r", 0.50))
        adverse_r = abs(unrealized_r)
        if age >= hard_age and passes >= hard_passes and adverse_r >= hard_adverse_r:
            return {
                "exit": True,
                "reason": f"hard invalidation while losing r={unrealized_r:.2f}: {reason}",
            }
        return {
            "exit": False,
            "reason": (
                f"Active exit held: losing trade r={unrealized_r:.2f} needs hard invalidation "
                f"age>={hard_age:.0f}s, passes>={hard_passes}, adverse_r>={hard_adverse_r:.2f}."
            ),
        }

    @staticmethod
    def _trade_age_seconds(trade: dict[str, Any]) -> float:
        opened_at = trade.get("opened_at")
        if not opened_at:
            return 0.0
        try:
            opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())
        except ValueError:
            return 0.0

    @staticmethod
    def _opposite_absorption(side: str, features: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        candles = list(features.get("trade_m1_candles") or [])
        if not candles:
            return {"ok": False, "reason": "no M1 candle for active exit absorption"}
        min_wick = float(cfg.get("min_wick_pct", 0.45))
        min_reclaim = float(cfg.get("min_close_reclaim_pct", 0.55))
        candle = candles[-1]
        close_position = float(candle.get("close_position", 0.5))
        delta = float(candle.get("delta", 0.0))
        if side == "SELL":
            wick = float(candle.get("upper_wick_pct", 0.0))
            reclaimed = close_position <= 1.0 - min_reclaim
            delta_ok = delta <= 0
            ok = wick >= min_wick and reclaimed and delta_ok
            return {
                "ok": ok,
                "reason": f"upper-wick resistance absorption wick={wick:.2f}, close_pos={close_position:.2f}, delta={delta:.4f}",
            }
        wick = float(candle.get("lower_wick_pct", 0.0))
        reclaimed = close_position >= min_reclaim
        delta_ok = delta >= 0
        ok = wick >= min_wick and reclaimed and delta_ok
        return {
            "ok": ok,
            "reason": f"lower-wick support absorption wick={wick:.2f}, close_pos={close_position:.2f}, delta={delta:.4f}",
        }

    def _sync_closed_mt5_trade(self, trade: dict[str, Any]) -> None:
        trade_id = int(trade["id"])
        position_id = int(trade.get("broker_position_id") or trade.get("broker_order_id") or 0)
        ok, result, message = closed_position_result(position_id, lookback_days=14)
        if not ok:
            self.store.add_event(
                "WARN",
                "manager",
                f"MT5 position for local trade #{trade_id} is gone, but close result is not available yet: {message}",
            )
            return

        exit_price = float(result.get("exit_price") or trade.get("exit_price") or trade["entry"])
        pnl = float(result.get("pnl") or 0.0)
        self.store.update_trade_close_result(trade_id, exit_price, pnl, "MT5 position closed outside local manager or by broker SL/TP.")
        self.store.add_event(
            "INFO",
            "manager",
            f"Synced closed MT5 trade #{trade_id} at {exit_price:.4f}, PnL {pnl:.2f}.",
        )
        self.telegram.send_trade_closed(
            trade,
            exit_price,
            pnl,
            "MT5 position closed outside local manager or by broker SL/TP.",
            self.store.stats(),
        )

    def _remember_position_execution_price(self, trade: dict[str, Any], position: dict[str, Any]) -> None:
        symbol = str(trade["symbol"])
        mt5_symbol = str(trade.get("mt5_symbol") or trade["symbol"])
        price = float(position.get("price_current") or 0.0)
        if price <= 0:
            return
        if trade["side"] == "BUY":
            self.execution_prices[symbol] = {
                "mt5_symbol": mt5_symbol,
                "ok": True,
                "bid": price,
                "ask": float(self.execution_prices.get(symbol, {}).get("ask") or price),
                "last": price,
                "time": position.get("time_update_msc") or position.get("time_update"),
                "source": "mt5_position",
            }
        else:
            self.execution_prices[symbol] = {
                "mt5_symbol": mt5_symbol,
                "ok": True,
                "bid": float(self.execution_prices.get(symbol, {}).get("bid") or price),
                "ask": price,
                "last": price,
                "time": position.get("time_update_msc") or position.get("time_update"),
                "source": "mt5_position",
            }

    def _lock_paper_trade_profit(self, trade: dict[str, Any], price: float) -> None:
        management = self.config.get("trade_management", {})
        if not management.get("enabled", True):
            return

        side = str(trade["side"])
        entry = float(trade["entry"])
        current_sl = float(trade["stop_loss"])
        initial_sl = float(trade.get("initial_stop_loss") or current_sl)
        risk_distance = abs(entry - initial_sl)
        if risk_distance <= 0:
            return

        favorable = (price - entry) if side == "BUY" else (entry - price)
        current_r = favorable / risk_distance
        if current_r <= 0:
            return

        breakeven_trigger = float(management.get("breakeven_trigger_r", 1.0))
        breakeven_lock = float(management.get("breakeven_lock_r", 0.10))
        trail_trigger = float(management.get("trail_trigger_r", 1.5))
        trail_distance = float(management.get("trail_distance_r", 0.75))
        min_update_bps = float(management.get("min_stop_update_bps", 1.0))

        desired_sl: float | None = None
        reason = ""
        if current_r >= breakeven_trigger:
            desired_sl = entry + (risk_distance * breakeven_lock) if side == "BUY" else entry - (risk_distance * breakeven_lock)
            reason = f"locked {breakeven_lock:.2f}R after {current_r:.2f}R move"
        if current_r >= trail_trigger:
            trail_sl = price - (risk_distance * trail_distance) if side == "BUY" else price + (risk_distance * trail_distance)
            if desired_sl is None or (side == "BUY" and trail_sl > desired_sl) or (side == "SELL" and trail_sl < desired_sl):
                desired_sl = trail_sl
                reason = f"trailed {trail_distance:.2f}R behind price after {current_r:.2f}R move"

        if desired_sl is None:
            return
        improves = desired_sl > current_sl if side == "BUY" else desired_sl < current_sl
        min_move = entry * (min_update_bps / 10000.0)
        meaningful = abs(desired_sl - current_sl) >= min_move
        if not improves or not meaningful:
            return

        desired_sl = self._normalize_broker_price(str(trade.get("mt5_symbol") or trade["symbol"]), desired_sl)
        self.store.update_trade_stop_loss(
            int(trade["id"]),
            desired_sl,
            f"Paper manager {reason}; SL moved from {current_sl:.4f} to {desired_sl:.4f}.",
        )
        self.store.add_event("INFO", "manager", f"Trade #{trade['id']} {reason}; SL moved to {desired_sl:.4f}.")

    def _protect_paper_trade_after_warning(self, trade: dict[str, Any], price: float, warning_reason: str) -> None:
        desired = self._absorption_warning_stop(trade, price, float(trade.get("stop_loss") or 0.0))
        if desired is None:
            self.store.add_event("INFO", "manager", f"Trade #{trade['id']} absorption warning held open: {warning_reason}")
            return
        current_sl = float(trade.get("stop_loss") or 0.0)
        desired_sl, locked_r = desired
        desired_sl = self._normalize_broker_price(str(trade.get("mt5_symbol") or trade["symbol"]), desired_sl)
        self.store.update_trade_stop_loss(
            int(trade["id"]),
            desired_sl,
            f"Paper manager absorption warning lock {locked_r:.2f}R; SL moved from {current_sl:.4f} to {desired_sl:.4f}.",
        )
        self.store.add_event(
            "INFO",
            "manager",
            f"Trade #{trade['id']} absorption warning protected profit at {locked_r:.2f}R; SL moved to {desired_sl:.4f}.",
        )

    def _lock_live_mt5_profit(self, trade: dict[str, Any], position: dict[str, Any]) -> None:
        management = self.config.get("trade_management", {})
        if not management.get("enabled", True):
            return

        side = str(trade["side"])
        entry = float(position.get("price_open") or trade["entry"])
        current_sl = float(position.get("sl") or trade["stop_loss"])
        current_tp = float(position.get("tp") or trade["take_profit"])
        initial_sl = float(trade.get("initial_stop_loss") or current_sl)
        risk_distance = abs(entry - initial_sl)
        if risk_distance <= 0:
            return

        price = float(position.get("price_current") or 0.0)
        if price <= 0:
            tick = self._execution_tick(str(trade["symbol"]), str(trade.get("mt5_symbol") or trade["symbol"]))
            if not tick:
                return
            price = self._close_mark_price(side, tick)

        favorable = (price - entry) if side == "BUY" else (entry - price)
        current_r = favorable / risk_distance
        if current_r <= 0:
            return

        breakeven_trigger = float(management.get("breakeven_trigger_r", 1.0))
        breakeven_lock = float(management.get("breakeven_lock_r", 0.10))
        trail_trigger = float(management.get("trail_trigger_r", 1.5))
        trail_distance = float(management.get("trail_distance_r", 0.75))
        min_update_bps = float(management.get("min_stop_update_bps", 1.0))

        desired_sl: float | None = None
        reason = ""
        if current_r >= breakeven_trigger:
            desired_sl = entry + (risk_distance * breakeven_lock) if side == "BUY" else entry - (risk_distance * breakeven_lock)
            reason = f"locked {breakeven_lock:.2f}R after {current_r:.2f}R move"
        if current_r >= trail_trigger:
            trail_sl = price - (risk_distance * trail_distance) if side == "BUY" else price + (risk_distance * trail_distance)
            if desired_sl is None or (side == "BUY" and trail_sl > desired_sl) or (side == "SELL" and trail_sl < desired_sl):
                desired_sl = trail_sl
                reason = f"trailed {trail_distance:.2f}R behind MT5 price after {current_r:.2f}R move"
        if desired_sl is None:
            return

        improves = desired_sl > current_sl if side == "BUY" else desired_sl < current_sl
        min_move = entry * (min_update_bps / 10000.0)
        if not improves or abs(desired_sl - current_sl) < min_move:
            return

        desired_sl = self._normalize_broker_price(str(trade.get("mt5_symbol") or trade["symbol"]), desired_sl)
        result = modify_position_sl_tp(
            symbol=str(trade.get("mt5_symbol") or trade["symbol"]),
            position_id=int(position.get("ticket")),
            stop_loss=desired_sl,
            take_profit=current_tp,
        )
        if not result.ok:
            self.store.add_event("WARN", "manager", f"MT5 SL update failed for trade #{trade['id']}: {result.message}")
            return
        self.store.update_trade_stop_loss(
            int(trade["id"]),
            desired_sl,
            f"MT5 manager {reason}; SL moved from {current_sl:.4f} to {desired_sl:.4f}.",
        )
        self.store.add_event("INFO", "manager", f"MT5 trade #{trade['id']} {reason}; SL moved to {desired_sl:.4f}.")

    def _protect_live_mt5_trade_after_warning(self, trade: dict[str, Any], position: dict[str, Any], warning_reason: str) -> None:
        side = str(trade["side"])
        price = float(position.get("price_current") or 0.0)
        if price <= 0:
            tick = self._execution_tick(str(trade["symbol"]), str(trade.get("mt5_symbol") or trade["symbol"]))
            if not tick:
                self.store.add_event("INFO", "manager", f"MT5 trade #{trade['id']} absorption warning held open: {warning_reason}")
                return
            price = self._close_mark_price(side, tick)

        current_sl = float(position.get("sl") or trade.get("stop_loss") or 0.0)
        desired = self._absorption_warning_stop(trade, price, current_sl)
        if desired is None:
            self.store.add_event("INFO", "manager", f"MT5 trade #{trade['id']} absorption warning held open: {warning_reason}")
            return

        current_tp = float(position.get("tp") or trade["take_profit"])
        desired_sl, locked_r = desired
        desired_sl = self._normalize_broker_price(str(trade.get("mt5_symbol") or trade["symbol"]), desired_sl)
        result = modify_position_sl_tp(
            symbol=str(trade.get("mt5_symbol") or trade["symbol"]),
            position_id=int(position.get("ticket")),
            stop_loss=desired_sl,
            take_profit=current_tp,
        )
        if not result.ok:
            self.store.add_event("WARN", "manager", f"MT5 absorption warning SL update failed for trade #{trade['id']}: {result.message}")
            return
        self.store.update_trade_stop_loss(
            int(trade["id"]),
            desired_sl,
            f"MT5 manager absorption warning lock {locked_r:.2f}R; SL moved from {current_sl:.4f} to {desired_sl:.4f}.",
        )
        self.store.add_event(
            "INFO",
            "manager",
            f"MT5 trade #{trade['id']} absorption warning protected profit at {locked_r:.2f}R; SL moved to {desired_sl:.4f}.",
        )

    def _absorption_warning_stop(self, trade: dict[str, Any], price: float, current_sl: float) -> tuple[float, float] | None:
        cfg = self.config.get("active_exit", {})
        management = self.config.get("trade_management", {})
        side = str(trade["side"]).upper()
        entry = float(trade["entry"])
        initial_sl = float(trade.get("initial_stop_loss") or current_sl)
        risk_distance = abs(entry - initial_sl)
        if entry <= 0 or price <= 0 or risk_distance <= 0:
            return None

        favorable = (price - entry) if side == "BUY" else (entry - price)
        current_r = favorable / risk_distance
        min_profit_r = float(cfg.get("absorption_protection_min_profit_r", cfg.get("min_profit_r_to_close", 0.10)))
        if current_r < min_profit_r:
            return None

        lock_r = float(cfg.get("absorption_protection_lock_r", management.get("breakeven_lock_r", 0.10)))
        price_buffer_r = float(cfg.get("absorption_protection_price_buffer_r", 0.05))
        locked_r = max(0.0, min(lock_r, current_r - max(0.0, price_buffer_r)))
        desired_sl = entry + (risk_distance * locked_r) if side == "BUY" else entry - (risk_distance * locked_r)

        improves = desired_sl > current_sl if side == "BUY" else desired_sl < current_sl
        min_update_bps = float(management.get("min_stop_update_bps", 1.0))
        meaningful = abs(desired_sl - current_sl) >= entry * (min_update_bps / 10000.0)
        if not improves or not meaningful:
            return None
        return desired_sl, locked_r

    @staticmethod
    def _paper_trade_pnl(trade: dict[str, Any], price: float) -> float:
        entry = float(trade["entry"])
        initial_sl = float(trade.get("initial_stop_loss") or trade["stop_loss"])
        risk_distance = abs(entry - initial_sl)
        if risk_distance <= 0:
            return 0.0
        if trade["side"] == "BUY":
            return (price - entry) / risk_distance * float(trade["risk_cash"])
        return (entry - price) / risk_distance * float(trade["risk_cash"])
