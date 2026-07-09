import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.state import StateStore
from trading_agent.telegram import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def _client(self, store: StateStore | None = None) -> TelegramClient:
        return TelegramClient(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "12345",
                    "send_trade_alerts": True,
                    "chat_enabled": True,
                }
            },
            store or StateStore(Path(tempfile.mkdtemp()) / "state.db"),
            lambda: {
                "running": True,
                "started_at": "2026-06-29T10:00:00+00:00",
                "execution_mode": "paper",
                "last_error": None,
                "symbols": [{"name": "XAUUSDT"}],
                "market_data": {"connected": True},
            },
        )

    def test_open_trade_message_includes_entry_sl_tp_and_logic(self):
        client = self._client()
        text = client._format_trade_opened(
            7,
            {
                "symbol": "XAUUSDT",
                "side": "BUY",
                "execution_mode": "paper",
                "entry": 2350.12,
                "stop_loss": 2345.0,
                "take_profit": 2365.5,
                "lots": 0.2,
                "risk_cash": 50.0,
                "rr": 3.0,
                "score": 91.5,
                "entry_reason": "sweep reclaim entry",
                "stop_reason": "SL beyond swept low",
                "tp_reason": "TP at opposing liquidity",
            },
        )

        self.assertIn("🟢 TRADE OPENED #7", text)
        self.assertIn("📈 XAUUSDT BUY", text)
        self.assertIn("TRADE OPENED #7", text)
        self.assertIn("Entry: 2350.12", text)
        self.assertIn("SL: 2345", text)
        self.assertIn("TP: 2365.5", text)
        self.assertIn("Entry logic: sweep reclaim entry", text)
        self.assertIn("SL logic: SL beyond swept low", text)
        self.assertIn("TP logic: TP at opposing liquidity", text)

    def test_close_trade_message_includes_id_pnl_reason_and_stats(self):
        client = self._client()
        text = client._format_trade_closed(
            {"id": 3, "symbol": "BTCUSDT", "side": "SELL", "execution_mode": "mt5_live"},
            100.25,
            -12.5,
            "Paper SL hit.",
            {
                "total_trades": 4,
                "open_trades": 0,
                "closed_trades": 4,
                "winning_trades": 2,
                "losing_trades": 2,
                "win_rate": 50.0,
                "net_pnl": 87.5,
                "profit_factor": 2.1,
                "avg_pnl": 21.875,
                "max_drawdown": 40.0,
            },
        )

        self.assertIn("🔴 TRADE CLOSED #3", text)
        self.assertIn("PnL: 🔴 -12.50", text)
        self.assertIn("TRADE CLOSED #3", text)
        self.assertIn("Reason: Paper SL hit.", text)
        self.assertIn("Overall Stats", text)
        self.assertIn("Win rate: 50.00%", text)
        self.assertIn("Net PnL: 87.50", text)

    def test_chat_answers_open_trades_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            store.open_trade(
                {
                    "symbol": "XAUUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 2350.0,
                    "stop_loss": 2345.0,
                    "take_profit": 2365.0,
                    "lots": 0.2,
                    "risk_cash": 50.0,
                    "rr": 3.0,
                    "score": 90.0,
                    "execution_mode": "paper",
                }
            )
            client = self._client(store)

            text = client._answer("/open")

            self.assertIn("Open Trades", text)
            self.assertIn("#1 | XAUUSDT | BUY | OPEN", text)
            self.assertIn("Entry 2350 | SL 2345 | TP 2365", text)

    def test_chat_answers_last3_and_alltrades_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            for index in range(4):
                trade_id = store.open_trade(
                    {
                        "symbol": "XAUUSDT",
                        "side": "BUY",
                        "strategy": "test",
                        "entry": 2350.0 + index,
                        "stop_loss": 2345.0,
                        "take_profit": 2365.0,
                        "lots": 0.2,
                        "risk_cash": 50.0,
                        "rr": 3.0,
                        "score": 90.0,
                        "execution_mode": "paper",
                    }
                )
                store.close_trade(trade_id, 2360.0 + index, 10.0 + index, "Paper TP hit.")
            client = self._client(store)

            last3 = client._answer("/last3")
            all_trades = client._answer("/alltrades")

            self.assertIn("Last 3 Trades", last3)
            self.assertIn("#4 | XAUUSDT | BUY | CLOSED", last3)
            self.assertIn("Entry 2353", last3)
            self.assertIn("Exit 2363", last3)
            self.assertIn("PnL 13.00", last3)
            self.assertIn("Reason: Paper TP hit.", last3)
            self.assertNotIn("#1 | XAUUSDT | BUY | CLOSED", last3)
            self.assertIn("All Trades", all_trades)
            self.assertIn("#1 | XAUUSDT | BUY | CLOSED", all_trades)
            self.assertIn("#4 | XAUUSDT | BUY | CLOSED", all_trades)

    def test_chat_health_reports_mt5_and_telegram_api(self):
        client = self._client()
        client._api = lambda method, payload, timeout=10.0: (True, {"ok": True}, "ok")  # type: ignore[method-assign]

        with patch("trading_agent.telegram.account_equity", return_value=(True, 12345.67, "OK")):
            text = client._answer("/health")

        self.assertIn("Bot Health", text)
        self.assertIn("MT5: OK (OK)", text)
        self.assertIn("MT5 equity: 12345.67", text)
        self.assertIn("Telegram API: OK (ok)", text)

    def test_chat_status_uses_status_provider(self):
        text = self._client()._answer("/status")

        self.assertIn("Trading Agent Status", text)
        self.assertIn("Running: True", text)
        self.assertIn("Execution: paper", text)


if __name__ == "__main__":
    unittest.main()
