import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.strategy.liquidity import compute_features, score_liquidity_setup


class LiquidityStrategyTests(unittest.TestCase):
    def test_scores_aligned_long_setup(self):
        depth = {
            "bids": [["100.0", "10"], ["99.9", "8"]],
            "asks": [["100.1", "2"], ["100.2", "2"]],
        }
        trades = [
            {"price": "100.05", "qty": "3", "isBuyerMaker": False},
            {"price": "100.08", "qty": "2", "isBuyerMaker": False},
            {"price": "100.09", "qty": "1", "isBuyerMaker": True},
        ]
        config = {
            "strategy": {
                "max_spread_bps": {"BTCUSDT": 20},
                "imbalance_threshold": 0.18,
                "flow_threshold": 0.12,
                "tp_rr": 2.5,
                "stop_bps": {"BTCUSDT": 10},
            }
        }
        features = compute_features("BTCUSDT", depth, trades, levels=2)
        candidate = score_liquidity_setup(features, config)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["side"], "BUY")
        self.assertGreater(candidate["score"], 70)
        self.assertIn("vwap", features)
        self.assertIn("cumulative_delta", features)
        self.assertIn("liquidity_magnet", features)
        self.assertIn("book_price_buckets", features)
        self.assertTrue(any(bucket["label"] == "100-200" for bucket in features["book_price_buckets"]))
        self.assertTrue(features["volume_profile"])
        self.assertIn("entry_reason", candidate)
        self.assertIn("stop_reason", candidate)
        self.assertIn("tp_reason", candidate)

    def test_heatmap_range_includes_empty_price_buckets(self):
        depth = {
            "bids": [["999.0", "1"]],
            "asks": [["1001.0", "1"]],
        }
        trades = [{"price": "1000.0", "qty": "1", "isBuyerMaker": False}]

        features = compute_features("BTCUSDT", depth, trades, levels=1, price_bucket_size=100.0, heatmap_range=1000.0)
        labels = [bucket["label"] for bucket in features["book_price_buckets"]]

        self.assertIn("0-100", labels)
        self.assertIn("1000-1100", labels)
        self.assertIn("2000-2100", labels)
        self.assertGreaterEqual(len(labels), 21)

    def test_ignores_zero_or_missing_trade_prices_for_structure(self):
        depth = {
            "bids": [["100.0", "10"]],
            "asks": [["100.2", "10"]],
        }
        trades = [
            {"price": "0", "qty": "5", "isBuyerMaker": False},
            {"qty": "5", "isBuyerMaker": False},
            {"price": "100.1", "qty": "2", "isBuyerMaker": False},
            {"price": "100.15", "qty": "2", "isBuyerMaker": True},
        ]

        features = compute_features("BTCUSDT", depth, trades, levels=1)

        self.assertEqual(features["recent_low"], 100.1)
        self.assertGreater(features["realized_range_bps"], 0.0)
        self.assertNotEqual(features["recent_low"], 0.0)


if __name__ == "__main__":
    unittest.main()
