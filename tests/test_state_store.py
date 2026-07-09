import sys
from pathlib import Path
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_trade_records_broker_execution_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            trade_id = store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "mt5_symbol": "BTCUSD",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 100.0,
                    "stop_loss": 99.0,
                    "take_profit": 102.5,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 2.5,
                    "score": 80.0,
                    "execution_mode": "mt5_live",
                    "broker_requested_price": 100.12,
                    "broker_fill_price": 100.15,
                    "broker_order_id": 123,
                    "broker_deal_id": 456,
                    "broker_retcode": 10009,
                    "broker_message": "OK",
                    "reason": "unit test",
                }
            )
            row = store.query("select * from trades where id = ?", (trade_id,))[0]
            self.assertEqual(row["execution_mode"], "mt5_live")
            self.assertEqual(row["broker_requested_price"], 100.12)
            self.assertEqual(row["broker_fill_price"], 100.15)
            self.assertEqual(row["broker_order_id"], 123)
            self.assertEqual(row["broker_deal_id"], 456)

    def test_trade_records_setup_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            trade_id = store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "strategy": "locked_liquidity_thesis_entry",
                    "a_plus_tier": "B",
                    "entry": 100.0,
                    "stop_loss": 99.0,
                    "take_profit": 102.5,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 2.5,
                    "score": 84.0,
                    "execution_mode": "paper",
                }
            )

            row = store.query("select * from trades where id = ?", (trade_id,))[0]

            self.assertEqual(row["setup_tier"], "B")

    def test_counts_open_trades_by_symbol_and_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            base = {
                "symbol": "XAUUSDT",
                "strategy": "test",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 103.0,
                "lots": 0.1,
                "risk_cash": 25.0,
                "rr": 3.0,
                "score": 80.0,
                "execution_mode": "paper",
            }
            store.open_trade({**base, "side": "BUY"})
            store.open_trade({**base, "side": "SELL", "stop_loss": 101.0, "take_profit": 97.0})

            self.assertEqual(store.count_open_trades("XAUUSDT"), 2)
            self.assertEqual(store.count_open_trades("XAUUSDT", "BUY"), 1)
            self.assertEqual(store.count_open_trades("XAUUSDT", "SELL"), 1)

    def test_closed_trade_result_can_be_corrected_without_resetting_close_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            trade_id = store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 100.0,
                    "stop_loss": 99.0,
                    "take_profit": 102.5,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 2.5,
                    "score": 80.0,
                    "execution_mode": "mt5_live",
                }
            )
            store.close_trade(trade_id, 101.0, 0.0)
            before = store.query("select * from trades where id = ?", (trade_id,))[0]

            store.update_trade_close_result(trade_id, 101.5, 42.25)

            after = store.query("select * from trades where id = ?", (trade_id,))[0]
            self.assertEqual(after["closed_at"], before["closed_at"])
            self.assertEqual(after["exit_price"], 101.5)
            self.assertEqual(after["pnl"], 42.25)
            self.assertEqual(after["status"], "CLOSED")

    def test_closed_trade_keeps_exit_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            trade_id = store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "strategy": "test",
                    "entry": 100.0,
                    "stop_loss": 101.0,
                    "take_profit": 97.0,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 3.0,
                    "score": 80.0,
                    "execution_mode": "paper",
                }
            )

            store.close_trade(trade_id, 99.0, 10.0, "opposite absorption exit")
            row = store.query("select * from trades where id = ?", (trade_id,))[0]

            self.assertEqual(row["exit_reason"], "opposite absorption exit")

    def test_hybrid_mt5_fields_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            base = {
                "symbol": "XAUUSD",
                "mt5_symbol": "XAUUSD",
                "side": "BUY",
                "strategy": "test",
                "entry": 2400.0,
                "stop_loss": 2390.0,
                "take_profit": 2430.0,
                "lots": 0.1,
                "risk_cash": 100.0,
                "rr": 3.0,
                "score": 80.0,
                "execution_mode": "hybrid",
            }
            open_id = store.open_trade(base)
            closed_id = store.open_trade({**base, "side": "SELL", "stop_loss": 2410.0, "take_profit": 2370.0})

            store.update_hybrid_mt5_open(
                open_id,
                requested_price=2400.1,
                fill_price=2400.2,
                stop_loss=2390.0,
                take_profit=2430.0,
                order_id=11,
                deal_id=22,
                position_id=33,
                retcode=10009,
                message="opened",
            )
            store.update_hybrid_mt5_open(
                closed_id,
                requested_price=2400.0,
                fill_price=2399.8,
                stop_loss=2410.0,
                take_profit=2370.0,
                order_id=44,
                deal_id=55,
                position_id=66,
                retcode=10009,
                message="opened",
            )
            store.update_hybrid_mt5_close_result(closed_id, 2395.0, 48.5, "closed")

            row = store.query("select * from trades where id = ?", (open_id,))[0]
            stats = store.stats()["hybrid_mt5"]

            self.assertEqual(row["hybrid_mt5_status"], "OPEN")
            self.assertEqual(row["hybrid_mt5_position_id"], 33)
            self.assertEqual(stats["total_trades"], 2)
            self.assertEqual(stats["open_trades"], 1)
            self.assertEqual(stats["closed_trades"], 1)
            self.assertEqual(stats["net_pnl"], 48.5)
            self.assertEqual(stats["win_rate"], 100.0)

    def test_stats_include_win_rate_and_max_drawdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            trade_ids = []
            for index in range(4):
                trade_ids.append(
                    store.open_trade(
                        {
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "strategy": "test",
                            "entry": 100.0,
                            "stop_loss": 99.0,
                            "take_profit": 102.5,
                            "lots": 0.1,
                            "risk_cash": 25.0,
                            "rr": 2.5,
                            "score": 80.0,
                            "execution_mode": "paper",
                        }
                    )
                )

            for trade_id, pnl in zip(trade_ids, [100.0, -40.0, -80.0, 60.0]):
                store.close_trade(trade_id, 100.0, pnl)

            stats = store.stats()

            self.assertEqual(stats["winning_trades"], 2)
            self.assertEqual(stats["losing_trades"], 2)
            self.assertEqual(stats["win_rate"], 50.0)
            self.assertEqual(stats["max_drawdown"], 120.0)
            self.assertEqual(stats["max_profit_trade"], 100.0)
            self.assertEqual(stats["max_loss_trade"], -80.0)


if __name__ == "__main__":
    unittest.main()
