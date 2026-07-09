import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.strategy.thesis import LiquidityThesisEngine


def _config() -> dict:
    return {
        "risk": {"min_rr": 2.0},
        "strategy": {
            "max_spread_bps": {"BTCUSDT": 10.0},
            "sl_buffer_bps": {"BTCUSDT": 6.0},
            "min_structural_stop_bps": {"BTCUSDT": 12.0},
            "max_structural_stop_bps": {"BTCUSDT": 80.0},
        },
        "thesis": {
            "enabled": True,
            "min_score": 72.0,
            "entry_trigger_score": 75.0,
            "max_age_seconds": 900.0,
            "zone_required_touches": 3,
            "zone_min_score": 70.0,
            "drift_required_pushes": 3,
            "drift_min_score": 70.0,
            "sharp_flow_min_score": 70.0,
            "sharp_flow_min_delta": {"BTCUSDT": 0.28},
            "sharp_flow_min_drift_bps": {"BTCUSDT": 5.0},
            "sweep_buffer_bps": {"BTCUSDT": 4.0},
            "entry_reclaim_buffer_bps": {"BTCUSDT": 2.0},
            "min_target_distance_bps": {"BTCUSDT": 35.0},
        },
        "liquidity_memory": {
            "enabled": True,
            "target_memory_weight": 0.35,
        },
    }


def _features(mid: float, recent_low: float, recent_high: float, delta: float = 0.35) -> dict:
    candle_range = max(recent_high - recent_low, 0.01)
    close_position = (mid - recent_low) / candle_range
    return {
        "symbol": "BTCUSDT",
        "mid": mid,
        "spread_bps": 1.0,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "delta_imbalance": delta,
        "flow_imbalance": delta,
        "cumulative_delta": delta * 20.0,
        "session_cumulative_delta": delta * 40.0,
        "session_delta_imbalance": delta,
        "trade_drift_bps": 0.0,
        "delta_divergence": "none",
        "session_vwap": 100.0,
        "session_vwap_lower_2": 98.0,
        "session_vwap_upper_2": 102.0,
        "open_interest_support": "supports_up",
        "open_interest_signal": "long_build",
        "book_price_buckets": [
            {
                "price_low": 110.0,
                "price_high": 111.0,
                "label": "110-111",
                "bid_notional": 0.0,
                "ask_notional": 100000.0,
                "strength": 100.0,
                "memory_score": 0.0,
                "pull_count": 0,
                "pull_rate": 0.0,
            },
            {
                "price_low": 98.0,
                "price_high": 99.0,
                "label": "98-99",
                "bid_notional": 20000.0,
                "ask_notional": 0.0,
                "strength": 20.0,
                "memory_score": 0.0,
                "pull_count": 0,
                "pull_rate": 0.0,
            },
        ],
        "session_volume_profile": [],
        "major_liquidity_zones": [],
        "trade_m1_candles": [
            {
                "open_time": 60_000,
                "open": mid,
                "high": recent_high,
                "low": recent_low,
                "close": mid,
                "volume": 10.0,
                "delta": delta,
                "trade_count": 12,
                "lower_wick_pct": max(0.0, min(1.0, close_position)),
                "upper_wick_pct": max(0.0, min(1.0, 1.0 - close_position)),
                "close_position": max(0.0, min(1.0, close_position)),
                "body_pct": 0.0,
            }
        ],
    }


class LiquidityThesisTests(unittest.TestCase):
    def test_locks_buy_thesis_after_downside_sweep_without_execution(self):
        engine = LiquidityThesisEngine()
        config = _config()

        first = engine.evaluate("BTCUSDT", _features(100.5, 100.0, 101.0), config, now=1.0)
        second = engine.evaluate("BTCUSDT", _features(100.2, 99.8, 100.8), config, now=2.0)

        self.assertEqual(first.decision, "SCANNING")
        self.assertEqual(second.decision, "THESIS_LOCKED")
        self.assertFalse(second.executable)
        self.assertEqual(second.candidate["side"], "BUY")
        self.assertEqual(second.candidate["take_profit"], 110.0)
        self.assertIn("BTCUSDT", engine.snapshot())
        self.assertEqual(engine.snapshot()["BTCUSDT"]["state"], "WAITING_FOR_ENTRY_SWEEP")

    def test_planned_sl_sweep_and_reclaim_triggers_executable_entry(self):
        engine = LiquidityThesisEngine()
        config = _config()

        engine.evaluate("BTCUSDT", _features(100.5, 100.0, 101.0), config, now=1.0)
        locked = engine.evaluate("BTCUSDT", _features(100.2, 99.8, 100.8), config, now=2.0)
        planned_sl = locked.candidate["stop_loss"]

        swept = engine.evaluate("BTCUSDT", _features(planned_sl - 0.02, planned_sl - 0.04, 100.4), config, now=3.0)
        triggered = engine.evaluate("BTCUSDT", _features(100.25, planned_sl - 0.04, 100.4), config, now=4.0)

        self.assertEqual(swept.decision, "ENTRY_SWEEP_SEEN")
        self.assertFalse(swept.executable)
        self.assertEqual(triggered.decision, "ENTRY_TRIGGERED")
        self.assertTrue(triggered.executable)
        self.assertEqual(triggered.candidate["strategy"], "locked_liquidity_thesis_entry")
        self.assertEqual(triggered.candidate["side"], "BUY")
        self.assertEqual(triggered.candidate["take_profit"], 110.0)
        self.assertGreater(triggered.candidate["stop_loss"], 0)
        self.assertNotIn("BTCUSDT", engine.snapshot())

    def test_planned_sl_reclaim_waits_for_closed_m1_entry_candle(self):
        engine = LiquidityThesisEngine()
        config = _config()

        engine.evaluate("BTCUSDT", _features(100.5, 100.0, 101.0), config, now=1.0)
        locked = engine.evaluate("BTCUSDT", _features(100.2, 99.8, 100.8), config, now=2.0)
        planned_sl = locked.candidate["stop_loss"]

        engine.evaluate("BTCUSDT", _features(planned_sl - 0.02, planned_sl - 0.04, 100.4), config, now=3.0)
        current_candle = _features(100.05, planned_sl - 0.04, 100.4)
        current_candle["trade_m1_candles"][0]["open_time"] = 9_999_999_999_999
        current_candle["trade_m1_candles"][0]["is_closed"] = False
        waiting = engine.evaluate("BTCUSDT", current_candle, config, now=4.0)

        self.assertEqual(waiting.decision, "WAITING_FOR_ENTRY_CANDLE")
        self.assertFalse(waiting.executable)
        self.assertIn("closed M1 entry candle", waiting.reason)
        self.assertIn("BTCUSDT", engine.snapshot())

    def test_major_liquidity_sweep_displacement_retest_triggers(self):
        engine = LiquidityThesisEngine()
        config = _config()

        def major_features(mid: float, low: float, high: float, delta: float = 0.35) -> dict:
            features = _features(mid, low, high, delta)
            features["major_liquidity_zones"] = [
                {
                    "side": "support",
                    "price_low": 99.0,
                    "price_high": 100.0,
                    "label": "99-100",
                    "score": 88.0,
                    "notional": 300000.0,
                    "memory_score": 45.0,
                    "strength": 80.0,
                    "sources": ["book_memory_bid"],
                },
                {
                    "side": "resistance",
                    "price_low": 110.0,
                    "price_high": 111.0,
                    "label": "110-111",
                    "score": 90.0,
                    "notional": 300000.0,
                    "memory_score": 45.0,
                    "strength": 85.0,
                    "sources": ["book_memory_ask"],
                },
            ]
            return features

        engine.evaluate("BTCUSDT", major_features(96.0, 95.0, 97.0), config, now=1.0)
        swept = engine.evaluate("BTCUSDT", major_features(100.2, 98.8, 101.0), config, now=2.0)
        displaced = engine.evaluate("BTCUSDT", major_features(102.0, 100.8, 102.2), config, now=3.0)
        retested = engine.evaluate("BTCUSDT", major_features(100.25, 98.98, 100.55), config, now=4.0)

        self.assertEqual(swept.decision, "SCANNING")
        self.assertEqual(displaced.decision, "SCANNING")
        self.assertEqual(retested.decision, "MAJOR_LIQUIDITY_TRIGGERED")
        self.assertTrue(retested.executable)
        self.assertEqual(retested.candidate["strategy"], "major_liquidity_sweep_retest_thesis")
        self.assertEqual(retested.candidate["side"], "BUY")

    def test_invalid_zero_sweep_extreme_does_not_lock_thesis(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        engine.evaluate("BTCUSDT", _features(100.5, 98.2, 101.0), config, now=1.0)
        outcome = engine.evaluate("BTCUSDT", _features(100.2, 0.0, 100.8), config, now=2.0)

        self.assertEqual(outcome.decision, "SCANNING")
        self.assertIsNone(outcome.candidate)
        self.assertNotIn("BTCUSDT", engine.snapshot())

    def test_target_selection_prefers_memory_backed_liquidity(self):
        engine = LiquidityThesisEngine()
        config = _config()
        features = _features(100.0, 99.0, 101.0)
        features["book_price_buckets"] = [
            {
                "price_low": 110.0,
                "price_high": 111.0,
                "label": "110-111",
                "bid_notional": 0.0,
                "ask_notional": 250000.0,
                "strength": 30.0,
                "memory_score": 0.0,
                "pull_count": 0,
                "pull_rate": 0.0,
            },
            {
                "price_low": 120.0,
                "price_high": 121.0,
                "label": "120-121",
                "bid_notional": 0.0,
                "ask_notional": 75000.0,
                "strength": 20.0,
                "memory_score": 92.0,
                "persistence_seconds": 1800.0,
                "pull_count": 0,
                "pull_rate": 0.0,
            },
        ]

        target = engine._select_target("BUY", features, config)

        self.assertEqual(target["label"], "120-121")
        self.assertGreater(target["memory_score"], 90.0)

    def test_recently_pulled_target_reduces_score(self):
        engine = LiquidityThesisEngine()
        config = _config()
        features = _features(100.0, 99.0, 101.0)
        clean_target = {
            "price": 110.0,
            "label": "110-111",
            "distance_bps": 1000.0,
            "notional": 120000.0,
            "strength": 80.0,
            "memory_score": 90.0,
            "pull_count": 0,
            "pull_rate": 0.0,
        }
        pulled_target = {**clean_target, "pull_count": 3, "pull_rate": 60000.0}

        clean_score, clean_scores, _ = engine._score_confluence("BUY", features, clean_target, config)
        pulled_score, pulled_scores, _ = engine._score_confluence("BUY", features, pulled_target, config)

        self.assertLess(pulled_scores["target_liquidity"], clean_scores["target_liquidity"])
        self.assertLess(pulled_score, clean_score)

    def test_repeated_support_zone_triggers_thesis(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        engine.evaluate("BTCUSDT", _features(100.5, 98.2, 101.0), config, now=1.0)
        first = engine.evaluate("BTCUSDT", _features(100.4, 98.4, 100.7), config, now=20.0)
        second = engine.evaluate("BTCUSDT", _features(100.5, 98.5, 100.8), config, now=40.0)
        third = engine.evaluate("BTCUSDT", _features(100.6, 98.6, 100.9), config, now=60.0)

        self.assertEqual(first.decision, "SCANNING")
        self.assertEqual(second.decision, "SCANNING")
        self.assertEqual(third.decision, "ZONE_THESIS_TRIGGERED")
        self.assertTrue(third.executable)
        self.assertEqual(third.candidate["strategy"], "repeated_liquidity_zone_thesis")
        self.assertEqual(third.candidate["side"], "BUY")

    def test_drift_exhaustion_triggers_thesis(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["zone_thesis_enabled"] = False
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        engine.evaluate("BTCUSDT", _features(101.0, 100.8, 101.2), config, now=1.0)
        engine.evaluate("BTCUSDT", _features(100.7, 100.5, 101.0), config, now=10.0)
        engine.evaluate("BTCUSDT", _features(100.4, 100.2, 100.7), config, now=20.0)
        outcome = engine.evaluate("BTCUSDT", _features(100.35, 100.0, 100.6), config, now=30.0)

        self.assertEqual(outcome.decision, "DRIFT_THESIS_TRIGGERED")
        self.assertTrue(outcome.executable)
        self.assertEqual(outcome.candidate["strategy"], "drift_sweep_retrace_thesis")
        self.assertEqual(outcome.candidate["side"], "BUY")

    def test_drift_exhaustion_requires_m1_wick_absorption(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["zone_thesis_enabled"] = False
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        engine.evaluate("BTCUSDT", _features(101.0, 100.8, 101.2), config, now=1.0)
        engine.evaluate("BTCUSDT", _features(100.7, 100.5, 101.0), config, now=10.0)
        engine.evaluate("BTCUSDT", _features(100.4, 100.2, 100.7), config, now=20.0)
        no_wick = _features(100.3, 100.0, 100.6)
        no_wick["trade_m1_candles"] = []
        outcome = engine.evaluate("BTCUSDT", no_wick, config, now=30.0)

        self.assertEqual(outcome.decision, "SCANNING")
        self.assertIsNone(outcome.candidate)

    def test_upper_wick_defended_resistance_triggers_sell_zone_thesis(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        def resistance_features() -> dict:
            features = _features(109.5, 109.4, 110.6, delta=-0.35)
            features["open_interest_support"] = "supports_down"
            features["open_interest_signal"] = "short_build"
            features["trade_m1_candles"] = [
                {
                    "open_time": 60_000,
                    "open": 109.8,
                    "high": 110.6,
                    "low": 109.4,
                    "close": 109.5,
                    "volume": 18.0,
                    "delta": -6.0,
                    "trade_count": 20,
                    "lower_wick_pct": 0.08,
                    "upper_wick_pct": 0.67,
                    "close_position": 0.08,
                    "body_pct": 0.25,
                }
            ]
            return features

        engine.evaluate("BTCUSDT", resistance_features(), config, now=1.0)
        first = engine.evaluate("BTCUSDT", resistance_features(), config, now=20.0)
        second = engine.evaluate("BTCUSDT", resistance_features(), config, now=40.0)
        third = engine.evaluate("BTCUSDT", resistance_features(), config, now=60.0)

        self.assertEqual(first.decision, "SCANNING")
        self.assertEqual(second.decision, "SCANNING")
        self.assertEqual(third.decision, "ZONE_THESIS_TRIGGERED")
        self.assertTrue(third.executable)
        self.assertEqual(third.candidate["strategy"], "repeated_liquidity_zone_thesis")
        self.assertEqual(third.candidate["side"], "SELL")

    def test_sell_zone_thesis_requires_flow_confirmation(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        def conflicting_flow_features() -> dict:
            features = _features(109.5, 109.4, 110.6, delta=0.35)
            features["open_interest_support"] = "supports_up"
            features["open_interest_signal"] = "long_build"
            features["trade_m1_candles"] = [
                {
                    "open_time": 60_000,
                    "open": 109.8,
                    "high": 110.6,
                    "low": 109.4,
                    "close": 109.5,
                    "volume": 18.0,
                    "delta": -6.0,
                    "trade_count": 20,
                    "lower_wick_pct": 0.08,
                    "upper_wick_pct": 0.67,
                    "close_position": 0.08,
                    "body_pct": 0.25,
                }
            ]
            return features

        engine.evaluate("BTCUSDT", conflicting_flow_features(), config, now=1.0)
        engine.evaluate("BTCUSDT", conflicting_flow_features(), config, now=20.0)
        engine.evaluate("BTCUSDT", conflicting_flow_features(), config, now=40.0)
        outcome = engine.evaluate("BTCUSDT", conflicting_flow_features(), config, now=60.0)

        self.assertEqual(outcome.decision, "SCANNING")
        self.assertIsNone(outcome.candidate)

    def test_buy_zone_thesis_rejects_fresh_upper_wick_double_top(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        def trapped_buy_features() -> dict:
            features = _features(99.5, 98.5, 101.0, delta=0.35)
            features["trade_m1_candles"] = [
                {
                    "open_time": 60_000,
                    "open": 100.2,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 99.2,
                    "volume": 18.0,
                    "delta": -5.0,
                    "trade_count": 20,
                    "lower_wick_pct": 0.10,
                    "upper_wick_pct": 0.40,
                    "close_position": 0.10,
                    "body_pct": 0.50,
                    "is_closed": True,
                },
                {
                    "open_time": 120_000,
                    "open": 100.1,
                    "high": 100.95,
                    "low": 99.1,
                    "close": 99.3,
                    "volume": 16.0,
                    "delta": -4.0,
                    "trade_count": 18,
                    "lower_wick_pct": 0.11,
                    "upper_wick_pct": 0.46,
                    "close_position": 0.11,
                    "body_pct": 0.43,
                    "is_closed": True,
                },
                {
                    "open_time": 180_000,
                    "open": 98.8,
                    "high": 99.8,
                    "low": 98.5,
                    "close": 99.5,
                    "volume": 20.0,
                    "delta": 6.0,
                    "trade_count": 22,
                    "lower_wick_pct": 0.23,
                    "upper_wick_pct": 0.23,
                    "close_position": 0.77,
                    "body_pct": 0.54,
                    "is_closed": True,
                },
            ]
            return features

        engine.evaluate("BTCUSDT", trapped_buy_features(), config, now=1.0)
        engine.evaluate("BTCUSDT", trapped_buy_features(), config, now=20.0)
        outcome = engine.evaluate("BTCUSDT", trapped_buy_features(), config, now=40.0)

        self.assertEqual(outcome.decision, "SCANNING")
        self.assertIsNone(outcome.candidate)

    def test_sell_zone_thesis_rejects_fresh_lower_wick_support(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["sweep_buffer_bps"]["BTCUSDT"] = 500.0

        def trapped_sell_features() -> dict:
            features = _features(109.5, 108.9, 110.6, delta=-0.35)
            features["open_interest_support"] = "supports_down"
            features["open_interest_signal"] = "short_build"
            features["trade_m1_candles"] = [
                {
                    "open_time": 60_000,
                    "open": 109.2,
                    "high": 110.6,
                    "low": 109.0,
                    "close": 109.1,
                    "volume": 18.0,
                    "delta": -6.0,
                    "trade_count": 20,
                    "lower_wick_pct": 0.06,
                    "upper_wick_pct": 0.88,
                    "close_position": 0.06,
                    "body_pct": 0.06,
                    "is_closed": True,
                },
                {
                    "open_time": 120_000,
                    "open": 109.2,
                    "high": 109.8,
                    "low": 108.9,
                    "close": 109.7,
                    "volume": 16.0,
                    "delta": 5.0,
                    "trade_count": 18,
                    "lower_wick_pct": 0.33,
                    "upper_wick_pct": 0.11,
                    "close_position": 0.89,
                    "body_pct": 0.56,
                    "is_closed": True,
                },
            ]
            return features

        engine.evaluate("BTCUSDT", trapped_sell_features(), config, now=1.0)
        engine.evaluate("BTCUSDT", trapped_sell_features(), config, now=20.0)
        outcome = engine.evaluate("BTCUSDT", trapped_sell_features(), config, now=40.0)

        self.assertEqual(outcome.decision, "SCANNING")
        self.assertIsNone(outcome.candidate)

    def test_sharp_buying_flow_triggers_thesis(self):
        engine = LiquidityThesisEngine()
        config = _config()
        config["thesis"]["zone_thesis_enabled"] = False
        config["thesis"]["drift_thesis_enabled"] = False

        engine.evaluate("BTCUSDT", _features(100.0, 99.8, 100.2), config, now=1.0)
        impulse = _features(101.0, 100.4, 101.2, delta=0.45)
        impulse["trade_drift_bps"] = 8.0
        outcome = engine.evaluate("BTCUSDT", impulse, config, now=2.0)

        self.assertEqual(outcome.decision, "SHARP_FLOW_TRIGGERED")
        self.assertTrue(outcome.executable)
        self.assertEqual(outcome.candidate["strategy"], "sharp_flow_impulse_thesis")
        self.assertEqual(outcome.candidate["side"], "BUY")


if __name__ == "__main__":
    unittest.main()
