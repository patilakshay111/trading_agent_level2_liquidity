import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.backtest import Candle, backtest_equal_extreme_rejection


class PriceActionBacktestTests(unittest.TestCase):
    def test_resistance_box_upper_wick_rejection_sell_wins_at_rr_target(self):
        candles = [
            Candle(0, 99.5, 100.0, 99.0, 99.8),
            Candle(60_000, 102.0, 105.0, 100.0, 103.0),
            Candle(120_000, 102.0, 103.0, 100.0, 102.4),
            Candle(180_000, 101.0, 102.0, 99.0, 100.5),
            Candle(240_000, 102.0, 105.02, 100.0, 102.5),
            Candle(300_000, 102.0, 103.0, 100.0, 101.8),
            Candle(360_000, 102.0, 105.01, 100.0, 101.0),
            Candle(420_000, 100.8, 101.0, 92.0, 93.0),
        ]

        result = backtest_equal_extreme_rejection(
            candles,
            side="SELL",
            rr=2.0,
            lookback_candles=6,
            equal_tolerance_bps=5.0,
            min_zone_touches=2,
            pivot_span=1,
            min_wick_pct=0.45,
            min_close_reclaim_pct=0.60,
            stop_buffer_bps=0.0,
            max_hold_candles=10,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["trades"], 1)
        self.assertEqual(result["summary"]["wins"], 1)
        self.assertEqual(result["trades"][0]["side"], "SELL")
        self.assertEqual(result["trades"][0]["outcome"], "WIN")
        self.assertAlmostEqual(result["trades"][0]["r"], 2.0)

    def test_support_box_lower_wick_rejection_buy_wins_at_rr_target(self):
        candles = [
            Candle(0, 100.0, 101.0, 99.0, 100.5),
            Candle(60_000, 98.0, 100.0, 95.0, 97.0),
            Candle(120_000, 98.0, 100.0, 96.0, 99.0),
            Candle(180_000, 99.0, 101.0, 97.0, 100.0),
            Candle(240_000, 98.0, 100.0, 94.98, 97.0),
            Candle(300_000, 98.0, 100.0, 96.0, 98.5),
            Candle(360_000, 99.0, 100.0, 94.99, 99.0),
            Candle(420_000, 99.5, 108.0, 99.0, 107.0),
        ]

        result = backtest_equal_extreme_rejection(
            candles,
            side="BUY",
            rr=2.0,
            lookback_candles=6,
            equal_tolerance_bps=5.0,
            min_zone_touches=2,
            pivot_span=1,
            min_wick_pct=0.45,
            min_close_reclaim_pct=0.60,
            stop_buffer_bps=0.0,
            max_hold_candles=10,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["trades"], 1)
        self.assertEqual(result["summary"]["wins"], 1)
        self.assertEqual(result["trades"][0]["side"], "BUY")
        self.assertIn("Support box", result["trades"][0]["reason"])

    def test_live_style_trailing_stop_locks_profit(self):
        candles = [
            Candle(0, 100.0, 101.0, 99.0, 100.5),
            Candle(60_000, 98.0, 100.0, 95.0, 97.0),
            Candle(120_000, 98.0, 100.0, 96.0, 99.0),
            Candle(180_000, 99.0, 101.0, 97.0, 100.0),
            Candle(240_000, 98.0, 100.0, 94.98, 97.0),
            Candle(300_000, 98.0, 100.0, 96.0, 98.5),
            Candle(360_000, 99.0, 100.0, 94.99, 99.0),
            Candle(420_000, 99.5, 106.0, 104.0, 105.0),
            Candle(480_000, 105.0, 105.2, 102.9, 103.5),
        ]

        result = backtest_equal_extreme_rejection(
            candles,
            side="BUY",
            rr=5.0,
            lookback_candles=6,
            equal_tolerance_bps=5.0,
            min_zone_touches=2,
            pivot_span=1,
            min_wick_pct=0.45,
            min_close_reclaim_pct=0.60,
            stop_buffer_bps=0.0,
            max_hold_candles=10,
            trade_management={
                "enabled": True,
                "breakeven_trigger_r": 1.0,
                "breakeven_lock_r": 0.10,
                "trail_trigger_r": 1.5,
                "trail_distance_r": 0.75,
                "min_stop_update_bps": 1.0,
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "TRAIL_STOP")
        self.assertEqual(trade["outcome"], "WIN")
        self.assertGreater(trade["final_stop_loss"], trade["entry"])
        self.assertGreater(trade["r"], 0.0)
        self.assertEqual(trade["lot_size"], 0.01)
        self.assertGreater(trade["pnl_usd"], 0.0)
        self.assertEqual(result["summary"]["trail_stops"], 1)
        self.assertEqual(result["summary"]["lot_size"], 0.01)

    def test_fixed_lot_pnl_uses_contract_size(self):
        candles = [
            Candle(0, 99.5, 100.0, 99.0, 99.8),
            Candle(60_000, 102.0, 105.0, 100.0, 103.0),
            Candle(120_000, 102.0, 103.0, 100.0, 102.4),
            Candle(180_000, 101.0, 102.0, 99.0, 100.5),
            Candle(240_000, 102.0, 105.02, 100.0, 102.5),
            Candle(300_000, 102.0, 103.0, 100.0, 101.8),
            Candle(360_000, 102.0, 105.01, 100.0, 101.0),
            Candle(420_000, 100.8, 101.0, 92.0, 93.0),
        ]

        result = backtest_equal_extreme_rejection(
            candles,
            side="SELL",
            rr=2.0,
            lookback_candles=6,
            equal_tolerance_bps=5.0,
            min_zone_touches=2,
            pivot_span=1,
            min_wick_pct=0.45,
            min_close_reclaim_pct=0.60,
            stop_buffer_bps=0.0,
            max_hold_candles=10,
            lot_size=0.01,
            contract_spec={"contract_size": 100.0},
        )

        trade = result["trades"][0]
        expected = (trade["entry"] - trade["exit_price"]) * 100.0 * 0.01
        self.assertAlmostEqual(trade["pnl_usd"], expected, places=2)
        self.assertAlmostEqual(result["summary"]["total_pnl_usd"], expected, places=2)

    def test_trailing_can_be_disabled(self):
        candles = [
            Candle(0, 100.0, 101.0, 99.0, 100.5),
            Candle(60_000, 98.0, 100.0, 95.0, 97.0),
            Candle(120_000, 98.0, 100.0, 96.0, 99.0),
            Candle(180_000, 99.0, 101.0, 97.0, 100.0),
            Candle(240_000, 98.0, 100.0, 94.98, 97.0),
            Candle(300_000, 98.0, 100.0, 96.0, 98.5),
            Candle(360_000, 99.0, 100.0, 94.99, 99.0),
            Candle(420_000, 99.5, 106.0, 104.0, 105.0),
            Candle(480_000, 105.0, 105.2, 102.9, 103.5),
        ]

        result = backtest_equal_extreme_rejection(
            candles,
            side="BUY",
            rr=5.0,
            lookback_candles=6,
            equal_tolerance_bps=5.0,
            min_zone_touches=2,
            pivot_span=1,
            min_wick_pct=0.45,
            min_close_reclaim_pct=0.60,
            stop_buffer_bps=0.0,
            max_hold_candles=2,
            trade_management={"enabled": False},
        )

        self.assertEqual(result["summary"]["trail_stops"], 0)
        self.assertEqual(result["summary"]["management_updates"], 0)
        self.assertNotEqual(result["trades"][0]["exit_reason"], "TRAIL_STOP")


if __name__ == "__main__":
    unittest.main()
