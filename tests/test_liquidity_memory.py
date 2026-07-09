import sys
from pathlib import Path
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.state import StateStore
from trading_agent.strategy.liquidity_memory import LiquidityMemoryEngine


def _config(retention_minutes: float = 180.0) -> dict:
    return {
        "liquidity_memory": {
            "enabled": True,
            "retention_minutes": retention_minutes,
            "ema_alpha": 0.5,
            "min_bucket_notional": {"BTCUSDT": 100000.0},
            "target_memory_weight": 0.35,
            "pull_warning_decay_seconds": 120,
        }
    }


def _bucket(low: float, bid: float = 0.0, ask: float = 0.0) -> dict:
    return {
        "price_low": low,
        "price_high": low + 100.0,
        "label": f"{low:.0f}-{low + 100.0:.0f}",
        "bid_notional": bid,
        "ask_notional": ask,
        "strength": min(100.0, (bid + ask) / 2000.0),
    }


class LiquidityMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.engine = LiquidityMemoryEngine(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_memory_initializes_from_bucket_rows(self):
        result = self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 90.0, _config(), now=10.0)

        rows = self.store.load_liquidity_memory("BTCUSDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "100-200")
        self.assertEqual(rows[0]["current_notional"], 150000.0)
        self.assertEqual(result["book_price_buckets"][0]["memory_score"], rows[0]["memory_score"])

    def test_persistence_increases_across_samples(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 90.0, _config(), now=10.0)
        result = self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 90.0, _config(), now=40.0)

        row = result["book_price_buckets"][0]
        self.assertEqual(row["sample_count"], 2)
        self.assertEqual(row["persistence_seconds"], 30.0)

    def test_stack_rate_rises_when_notional_increases(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=100000.0)], 90.0, _config(), now=10.0)
        result = self.engine.update("BTCUSDT", [_bucket(100.0, ask=180000.0)], 90.0, _config(), now=12.0)

        self.assertGreater(result["book_price_buckets"][0]["stack_rate"], 0.0)
        self.assertEqual(result["book_price_buckets"][0]["memory_marker"], "stacking")

    def test_pull_rate_rises_when_notional_drops(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=200000.0)], 90.0, _config(), now=10.0)
        result = self.engine.update("BTCUSDT", [_bucket(100.0, ask=20000.0)], 90.0, _config(), now=12.0)

        row = result["book_price_buckets"][0]
        self.assertGreater(row["pull_rate"], 0.0)
        self.assertGreater(row["pull_count"], 0)
        self.assertEqual(row["memory_marker"], "pulling")

    def test_memory_score_favors_persistent_liquidity_over_flash_liquidity(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=120000.0)], 90.0, _config(), now=10.0)
        persistent = self.engine.update("BTCUSDT", [_bucket(100.0, ask=120000.0), _bucket(300.0, ask=400000.0)], 90.0, _config(), now=910.0)
        rows = {row["label"]: row for row in persistent["book_price_buckets"]}

        self.assertGreater(rows["100-200"]["memory_score"], rows["300-400"]["memory_score"])

    def test_old_buckets_expire_after_retention(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 90.0, _config(retention_minutes=0.1), now=10.0)
        self.engine.update("BTCUSDT", [_bucket(200.0, ask=150000.0)], 90.0, _config(retention_minutes=0.1), now=20.0)

        rows = self.store.load_liquidity_memory("BTCUSDT")
        labels = {row["label"] for row in rows}
        self.assertNotIn("100-200", labels)
        self.assertIn("200-300", labels)

    def test_traded_through_bucket_increments_when_price_crosses_memory(self):
        self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 90.0, _config(), now=10.0)
        result = self.engine.update("BTCUSDT", [_bucket(100.0, ask=150000.0)], 220.0, _config(), now=12.0)

        self.assertEqual(result["book_price_buckets"][0]["traded_through_count"], 1)


if __name__ == "__main__":
    unittest.main()
