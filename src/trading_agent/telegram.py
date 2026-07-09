from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .broker.mt5 import account_equity
from .state import StateStore


StatusProvider = Callable[[], dict[str, Any]]


class TelegramClient:
    def __init__(
        self,
        config: dict[str, Any],
        store: StateStore,
        status_provider: StatusProvider | None = None,
    ):
        self.config = self._telegram_config(config)
        self.store = store
        self.status_provider = status_provider
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.update_offset: int | None = None

    def update_config(self, config: dict[str, Any]) -> None:
        was_running = self.is_chat_running()
        if was_running:
            self.stop_chat()
        self.config = self._telegram_config(config)
        if was_running and self.chat_enabled:
            self.start_chat()

    @staticmethod
    def _telegram_config(config: dict[str, Any]) -> dict[str, Any]:
        telegram = dict(config.get("telegram", {}))
        telegram["bot_token"] = str(
            telegram.get("bot_token")
            or os.environ.get("TRADING_AGENT_TELEGRAM_BOT_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
            or ""
        )
        telegram["chat_id"] = str(
            telegram.get("chat_id")
            or os.environ.get("TRADING_AGENT_TELEGRAM_CHAT_ID")
            or os.environ.get("TELEGRAM_CHAT_ID")
            or ""
        )
        return telegram

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False)) and bool(self.config.get("bot_token"))

    @property
    def chat_enabled(self) -> bool:
        return self.enabled and bool(self.config.get("chat_enabled", False))

    def start_chat(self) -> bool:
        if not self.chat_enabled:
            return False
        if self.thread and self.thread.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._poll_loop, name="telegram-chat", daemon=True)
        self.thread.start()
        self.store.add_event("INFO", "telegram", "Telegram chat polling started.")
        return True

    def stop_chat(self) -> bool:
        if not self.thread or not self.thread.is_alive():
            return False
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.store.add_event("INFO", "telegram", "Telegram chat polling stopped.")
        return True

    def is_chat_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def send_trade_opened(self, trade_id: int, trade: dict[str, Any]) -> None:
        if not self._trade_alerts_enabled():
            return
        self.send_message(self._format_trade_opened(trade_id, trade))

    def send_trade_closed(
        self,
        trade: dict[str, Any],
        exit_price: float,
        pnl: float,
        reason: str,
        stats: dict[str, Any],
    ) -> None:
        if not self._trade_alerts_enabled():
            return
        self.send_message(self._format_trade_closed(trade, exit_price, pnl, reason, stats))

    def send_message(self, text: str, chat_id: str | int | None = None) -> bool:
        if not self.enabled:
            return False
        target = str(chat_id or self.config.get("chat_id") or "").strip()
        if not target:
            self._record_warning("Telegram send skipped: telegram.chat_id is not configured.")
            return False
        ok, _, message = self._api("sendMessage", {"chat_id": target, "text": text[:4096]})
        if not ok:
            self._record_warning(f"Telegram send failed: {message}")
        return ok

    def _trade_alerts_enabled(self) -> bool:
        return self.enabled and bool(self.config.get("send_trade_alerts", True))

    def _poll_loop(self) -> None:
        while not self.stop_event.is_set():
            timeout = max(1, int(self.config.get("long_poll_timeout_seconds", 10)))
            payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
            if self.update_offset is not None:
                payload["offset"] = self.update_offset
            ok, data, message = self._api("getUpdates", payload, timeout=timeout + 5)
            if ok:
                for update in data.get("result", []):
                    update_id = int(update.get("update_id", 0))
                    self.update_offset = update_id + 1
                    self._handle_update(update)
            else:
                self._record_warning(f"Telegram polling failed: {message}")
                self.stop_event.wait(float(self.config.get("poll_interval_seconds", 5.0)))

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if not self._chat_allowed(chat_id):
            self._record_warning(f"Telegram chat ignored unauthorized chat id {chat_id}.")
            return
        self.send_message(self._answer(text), chat_id=chat_id)

    def _chat_allowed(self, chat_id: Any) -> bool:
        chat_id_text = str(chat_id)
        allowed = [str(item) for item in self.config.get("allowed_chat_ids", []) if str(item).strip()]
        default_chat = str(self.config.get("chat_id") or "").strip()
        if allowed:
            return chat_id_text in allowed
        if default_chat:
            return chat_id_text == default_chat
        return False

    def _answer(self, text: str) -> str:
        normalized = text.strip().lower()
        command = normalized.split(maxsplit=1)[0].lstrip("/")
        if command in {"help", "start"}:
            return self._help_text()
        if command in {"health"}:
            return self._health_text()
        if command in {"status", "running"} or "status" in normalized:
            return self._status_text()
        if command in {"stats", "pnl", "performance"} or "stats" in normalized or "pnl" in normalized:
            return self._stats_text()
        if command in {"opentrades", "open", "positions"} or "open trades" in normalized or "open positions" in normalized:
            return self._open_trades_text()
        if command in {"last3"}:
            return self._closed_trades_text(limit=3, title="Last 3 Trades")
        if command in {"alltrades"}:
            return self._all_trades_text()
        if command in {"recent", "closed", "last"} or "recent trades" in normalized or "closed trades" in normalized:
            return self._closed_trades_text(limit=10, title="Recent Closed Trades")
        if command in {"events", "log", "logs"}:
            return self._events_text()
        return "I can answer: /health, /stats, /opentrades, /last3, /alltrades, /help."

    def _health_text(self) -> str:
        status = self.status_provider() if self.status_provider else {}
        market = status.get("market_data", {})
        watchdog = status.get("watchdog", {})
        mt5_ok, equity, mt5_message = account_equity()
        api_ok, _, api_message = self._api("getMe", {}, timeout=5.0) if self.enabled else (False, {}, "Telegram disabled.")
        return "\n".join(
            [
                "Bot Health",
                f"Engine running: {bool(status.get('running'))}",
                f"Execution: {status.get('execution_mode') or 'unknown'}",
                f"Market data: {'OK' if market.get('connected') else 'NOT CONNECTED'}",
                f"MT5: {'OK' if mt5_ok else 'ERROR'} ({mt5_message})",
                f"MT5 equity: {self._fmt_money(equity) if mt5_ok else 'n/a'}",
                f"Telegram API: {'OK' if api_ok else 'ERROR'} ({api_message})",
                f"Chat polling: {'ON' if self.is_chat_running() else 'OFF'}",
                f"Watchdog: {'ON' if watchdog.get('enabled') else 'OFF'}",
                f"Watchdog market: {'OK' if watchdog.get('market_ok') else 'WARN'} ({watchdog.get('market_message') or 'n/a'})",
                f"Watchdog MT5: {'OK' if watchdog.get('mt5_ok') else 'ERROR'} ({watchdog.get('mt5_message') or 'n/a'})",
                f"Live entries blocked: {bool(watchdog.get('live_entries_blocked'))}",
                f"Last MT5 restart: {watchdog.get('last_mt5_restart_message') or 'none'}",
                f"MT5 restart attempts: {int(watchdog.get('mt5_restart_attempts') or 0)}",
                (
                    "Next restart interval: "
                    f"{self._fmt_optional(watchdog.get('next_mt5_restart_interval_seconds'))}s"
                    if watchdog.get("next_mt5_restart_interval_seconds") is not None
                    else "Next restart interval: n/a"
                ),
                f"Last error: {status.get('last_error') or 'none'}",
            ]
        )

    def _status_text(self) -> str:
        status = self.status_provider() if self.status_provider else {}
        symbols = ", ".join(item.get("name", "?") for item in status.get("symbols", [])) or "none"
        market = status.get("market_data", {})
        return "\n".join(
            [
                "Trading Agent Status",
                f"Running: {bool(status.get('running'))}",
                f"Execution: {status.get('execution_mode') or 'unknown'}",
                f"Started: {status.get('started_at') or 'not running'}",
                f"Symbols: {symbols}",
                f"Market connected: {bool(market.get('connected'))}",
                f"Last error: {status.get('last_error') or 'none'}",
            ]
        )

    def _stats_text(self) -> str:
        return self._format_stats(self.store.stats())

    def _open_trades_text(self) -> str:
        trades = self.store.get_open_trades()
        if not trades:
            return "No open trades."
        lines = ["Open Trades"]
        for trade in trades[:10]:
            lines.append(self._format_trade_block(trade))
        return "\n".join(lines)

    def _closed_trades_text(self, *, limit: int, title: str) -> str:
        trades = self.store.query(
            "select * from trades where status = 'CLOSED' order by closed_at desc, id desc limit ?",
            (int(limit),),
        )
        if not trades:
            return "No closed trades yet."
        lines = [title]
        for trade in trades:
            lines.append(self._format_trade_block(trade))
        return "\n".join(lines)[:4096]

    def _all_trades_text(self) -> str:
        trades = self.store.query("select * from trades order by id")
        if not trades:
            return "No trades yet."
        lines = ["All Trades"]
        for trade in trades:
            lines.append(self._format_trade_block(trade))
        text = "\n".join(lines)
        if len(text) <= 4096:
            return text
        truncated = text[:4050].rstrip()
        return f"{truncated}\n...truncated for Telegram limit."

    def _events_text(self) -> str:
        rows = self.store.query("select * from events order by created_at desc limit 5")
        if not rows:
            return "No events yet."
        lines = ["Recent Events"]
        for row in rows:
            lines.append(f"{row['level']} {row['source']}: {row['message']}")
        return "\n".join(lines)[:4096]

    @staticmethod
    def _help_text() -> str:
        return "\n".join(
            [
                "Trading Agent Telegram Commands",
                "/health - bot health and MT5/API connection",
                "/stats - overall trading statistics",
                "/opentrades - all currently open trades",
                "/last3 - last 3 closed trades",
                "/alltrades - complete trade history",
            ]
        )

    def _api(self, method: str, payload: dict[str, Any], timeout: float = 10.0) -> tuple[bool, dict[str, Any], str]:
        token = str(self.config.get("bot_token") or "").strip()
        if not token:
            return False, {}, "missing bot token"
        url = f"https://api.telegram.org/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not data.get("ok"):
                return False, data, str(data.get("description") or "Telegram API returned ok=false")
            return True, data, "ok"
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
                message = str(data.get("description") or exc)
            except Exception:  # noqa: BLE001 - best effort logging only
                message = str(exc)
            return False, {}, message
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return False, {}, str(exc)

    def _record_warning(self, message: str) -> None:
        try:
            self.store.add_event("WARN", "telegram", message)
        except Exception:
            return

    def _format_trade_opened(self, trade_id: int, trade: dict[str, Any]) -> str:
        side = str(trade.get("side") or "").upper()
        side_emoji = "📈" if side == "BUY" else "📉" if side == "SELL" else "⚡"
        return "\n".join(
            [
                f"🟢 TRADE OPENED #{trade_id}",
                f"{side_emoji} {trade.get('symbol')} {side or trade.get('side')} ({trade.get('execution_mode') or 'unknown'})",
                f"Entry: {self._fmt_num(trade.get('entry'))}",
                f"SL: {self._fmt_num(trade.get('stop_loss'))}",
                f"TP: {self._fmt_num(trade.get('take_profit'))}",
                (
                    f"Lots: {self._fmt_num(trade.get('lots'))} | Risk: {self._fmt_money(trade.get('risk_cash'))} | "
                    f"RR: {self._fmt_num(trade.get('rr'))} | Score: {self._fmt_num(trade.get('score'))}"
                ),
                f"Entry logic: {self._clean_reason(trade.get('entry_reason') or trade.get('reason'))}",
                f"SL logic: {self._clean_reason(trade.get('stop_reason'))}",
                f"TP logic: {self._clean_reason(trade.get('tp_reason'))}",
            ]
        )

    def _format_trade_closed(
        self,
        trade: dict[str, Any],
        exit_price: float,
        pnl: float,
        reason: str,
        stats: dict[str, Any],
    ) -> str:
        pnl_emoji = self._pnl_emoji(pnl)
        return "\n".join(
            [
                f"{pnl_emoji} TRADE CLOSED #{trade.get('id')}",
                f"{trade.get('symbol')} {trade.get('side')} ({trade.get('execution_mode') or 'unknown'})",
                f"Exit: {self._fmt_num(exit_price)} | PnL: {pnl_emoji} {self._fmt_money(pnl)}",
                f"Reason: {self._clean_reason(reason)}",
                "",
                self._format_stats(stats),
            ]
        )

    def _format_stats(self, stats: dict[str, Any]) -> str:
        return "\n".join(
            [
                "Overall Stats",
                f"Total: {int(stats.get('total_trades') or 0)} | Open: {int(stats.get('open_trades') or 0)} | Closed: {int(stats.get('closed_trades') or 0)}",
                f"Wins: {int(stats.get('winning_trades') or 0)} | Losses: {int(stats.get('losing_trades') or 0)} | Win rate: {self._fmt_pct(stats.get('win_rate'))}",
                f"Net PnL: {self._fmt_money(stats.get('net_pnl'))} | Profit factor: {self._fmt_optional(stats.get('profit_factor'))}",
                f"Avg PnL: {self._fmt_money(stats.get('avg_pnl'))} | Max DD: {self._fmt_money(stats.get('max_drawdown'))}",
            ]
        )

    def _format_trade_block(self, trade: dict[str, Any]) -> str:
        status = str(trade.get("status") or "").upper()
        lines = [
            "",
            f"#{trade.get('id')} | {trade.get('symbol')} | {trade.get('side')} | {status or 'OPEN'}",
            (
                f"Entry {self._fmt_num(trade.get('entry'))} | "
                f"SL {self._fmt_num(trade.get('stop_loss'))} | "
                f"TP {self._fmt_num(trade.get('take_profit'))}"
            ),
            f"Lots {self._fmt_num(trade.get('lots'))} | Risk {self._fmt_money(trade.get('risk_cash'))} | RR {self._fmt_num(trade.get('rr'))}",
        ]
        if status == "CLOSED":
            lines.extend(
                [
                    f"Exit {self._fmt_num(trade.get('exit_price'))} | PnL {self._fmt_money(trade.get('pnl'))}",
                    f"Reason: {self._clean_reason(trade.get('exit_reason'))}",
                ]
            )
        else:
            lines.append(f"Opened: {self._clean_reason(trade.get('opened_at'))}")
        return "\n".join(lines)

    @staticmethod
    def _pnl_emoji(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "⚪"
        if number > 0:
            return "✅"
        if number < 0:
            return "🔴"
        return "⚪"

    @staticmethod
    def _fmt_num(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{number:.4f}".rstrip("0").rstrip(".")

    @staticmethod
    def _fmt_money(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        return f"{number:.2f}"

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return "n/a"

    @staticmethod
    def _fmt_optional(value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _clean_reason(value: Any) -> str:
        text = "n/a" if value is None else str(value)
        return " ".join(text.split())
