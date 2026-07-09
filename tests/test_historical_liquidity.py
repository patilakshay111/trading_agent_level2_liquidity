import copy
import sys
from pathlib import Path
import tempfile
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.config import DEFAULT_CONFIG
from trading_agent.engine import TradingEngine
from trading_agent.historical_liquidity import (
    HistoricalLiquidityService,
    aggregate_candles,
    binance_kline_to_candle,
    derive_historical_levels,
    derive_multitimeframe_historical_levels,
    historical_levels_as_major_zones,
    iter_binance_kline_pages,
    mt5_rate_to_candle,
)
from trading_agent.state import StateStore


def _config() -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["symbols"] = [
        {"name": "BTCUSDT", "data_symbol": "BTCUSDT", "mt5_symbol": "BTCUSD", "enabled": True, "trade_enabled": True},
        {"name": "XAUUSDT", "data_symbol": "XAUUSDT", "mt5_symbol": "XAUUSD", "enabled": True, "trade_enabled": True},
    ]
    config["historical_liquidity"].update({
        "enabled": True,
        "years": 1,
        "interval": "1m",
        "live_rebuild_levels": False,
        "derived_intervals": ["1m", "5m", "15m"],
        "raw_m1_retention_days": 60,
        "prune_interval_days": 30,
        "request_sleep_seconds": 0.0,
        "mt5_fallback_symbols": ["XAUUSDT"],
    })
    config["strategy"]["price_bucket_size"] = {"BTCUSDT": 10.0, "XAUUSDT": 5.0}
    return config


def _kline(open_time: int, open_price: float, high: float, low: float, close: float, volume: float = 100.0) -> list:
    return [
        open_time,
        str(open_price),
        str(high),
        str(low),
        str(close),
        str(volume),
        open_time + 59_999,
        str(volume * close),
        10,
        str(volume * 0.55),
        str(volume * close * 0.55),
        "0",
    ]


class FakeMarket:
    def __init__(self, klines: list[list]):
        self._klines = klines
        self.calls = []

    def klines(self, symbol: str, interval: str = "1m", start_time=None, end_time=None, limit: int = 1500):
        self.calls.append({"symbol": symbol, "start_time": start_time, "end_time": end_time, "limit": limit})
        rows = [
            row for row in self._klines
            if (start_time is None or int(row[0]) >= int(start_time))
            and (end_time is None or int(row[0]) <= int(end_time))
        ]
        return rows[:limit]


class HistoricalLiquidityTests(unittest.TestCase):
    def test_binance_pagination_advances_by_last_open_time(self):
        rows = [_kline(index * 60_000, 100 + index, 102 + index, 99 + index, 101 + index) for index in range(5)]
        market = FakeMarket(rows)

        pages = list(iter_binance_kline_pages(market, "BTCUSDT", interval="1m", start_ms=0, end_ms=300_000, limit=2))

        self.assertEqual([len(page) for page in pages], [2, 2, 1])
        self.assertEqual(market.calls[1]["start_time"], 120_000)
        self.assertEqual(market.calls[2]["start_time"], 240_000)

    def test_candle_upserts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            candle = binance_kline_to_candle("BTCUSDT", _kline(0, 100, 110, 90, 105))

            store.upsert_historical_klines([candle.to_row()])
            store.upsert_historical_klines([candle.to_row()])

            self.assertEqual(store.historical_kline_count("BTCUSDT", "binance"), 1)
            self.assertEqual(store.latest_historical_kline_open_time("BTCUSDT", "binance"), 0)

    def test_mt5_rate_conversion(self):
        candle = mt5_rate_to_candle(
            "XAUUSDT",
            {"time": 1_700_000_000, "open": 2400.0, "high": 2402.0, "low": 2398.0, "close": 2401.0, "tick_volume": 77},
        )

        self.assertEqual(candle.symbol, "XAUUSDT")
        self.assertEqual(candle.source, "mt5")
        self.assertEqual(candle.open_time, 1_700_000_000_000)
        self.assertEqual(candle.quote_volume, 77 * 2401.0)

    def test_backfill_resumes_from_latest_stored_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            config = _config()
            rows = [_kline(index * 60_000, 100 + index, 103 + index, 98 + index, 101 + index) for index in range(6)]
            first = binance_kline_to_candle("BTCUSDT", rows[0])
            store.upsert_historical_klines([first.to_row()])
            market = FakeMarket(rows)
            service = HistoricalLiquidityService(store, config, market=market, sleep_fn=lambda _: None)

            result = service.backfill_symbol(config["symbols"][0], start_ms=None, end_ms=rows[-1][0] + 59_999, page_limit=2, rebuild_levels=False)

            self.assertTrue(result["ok"])
            self.assertEqual(store.historical_kline_count("BTCUSDT", "binance"), 6)
            self.assertEqual(market.calls[0]["start_time"], 60_000)

    def test_level_derivation_includes_core_sources_and_decay(self):
        config = _config()
        candles = []
        for index in range(40):
            base = 100.0 + (index % 6)
            high = 120.0 if index in {8, 18, 28} else base + 4.0
            low = 90.0 if index in {10, 20, 30} else base - 4.0
            volume = 1000.0 if index == 22 else 100.0
            candles.append(binance_kline_to_candle("BTCUSDT", _kline(index * 60_000, base, high, low, base + 1.0, volume)).to_row())

        levels = derive_historical_levels("BTCUSDT", candles, config, now_ms=40 * 60_000)
        sources = {level["source"] for level in levels}

        self.assertIn("historical_daily_high", sources)
        self.assertIn("historical_asia_session_high", sources)
        self.assertIn("historical_equal_high", sources)
        self.assertIn("historical_high_volume", sources)
        self.assertGreater(levels[0]["score"], 0)

    def test_aggregate_candles_builds_closed_higher_timeframe_rows(self):
        rows = [
            binance_kline_to_candle("BTCUSDT", _kline(index * 60_000, 100 + index, 105 + index, 95 + index, 101 + index, 10 + index)).to_row()
            for index in range(10)
        ]

        aggregated = aggregate_candles(rows, "5m")

        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0]["open_time"], 0)
        self.assertEqual(aggregated[0]["open"], 100.0)
        self.assertEqual(aggregated[0]["close"], 105.0)
        self.assertEqual(aggregated[0]["high"], 109.0)
        self.assertEqual(aggregated[0]["low"], 95.0)
        self.assertEqual(aggregated[0]["volume"], sum(10 + index for index in range(5)))

    def test_multitimeframe_rebuild_adds_higher_timeframe_sources(self):
        config = _config()
        candles = []
        for index in range(90):
            base = 100.0 + (index % 12)
            high = 130.0 if index in {20, 40, 60} else base + 4.0
            low = 80.0 if index in {25, 45, 65} else base - 4.0
            candles.append(binance_kline_to_candle("BTCUSDT", _kline(index * 60_000, base, high, low, base + 1.0, 100.0)).to_row())

        levels = derive_multitimeframe_historical_levels("BTCUSDT", candles, config, now_ms=90 * 60_000)
        sources = {level["source"] for level in levels}

        self.assertIn("historical_swing_high", sources)
        self.assertTrue(any(source.startswith("historical_5m_") for source in sources))

    def test_prune_old_m1_deletes_raw_candles_but_keeps_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            config = _config()
            service = HistoricalLiquidityService(store, config, market=FakeMarket([]), sleep_fn=lambda _: None)
            now_ms = 100 * 86_400_000
            old_candle = binance_kline_to_candle("BTCUSDT", _kline(0, 100, 110, 90, 105)).to_row()
            recent_open = now_ms - (10 * 86_400_000)
            recent_candle = binance_kline_to_candle("BTCUSDT", _kline(recent_open, 100, 110, 90, 105)).to_row()
            store.upsert_historical_klines([old_candle, recent_candle])
            store.replace_historical_levels(
                "BTCUSDT",
                [
                    {
                        "symbol": "BTCUSDT",
                        "source": "historical_daily_low",
                        "level_type": "period_low",
                        "timeframe": "daily",
                        "price_low": 90.0,
                        "price_high": 100.0,
                        "label": "daily low",
                        "side": "support",
                        "score": 95.0,
                        "strength": 95.0,
                    }
                ],
            )

            result = service.prune_old_m1(config["symbols"][0], now_ms=now_ms, force=True)

            self.assertTrue(result["ok"])
            self.assertEqual(result["pruned"], 1)
            self.assertEqual(store.historical_kline_count("BTCUSDT", "binance"), 1)
            self.assertEqual(len(store.load_historical_levels("BTCUSDT")), 1)

    def test_incremental_levels_process_only_closed_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            config = _config()
            config["historical_liquidity"]["derived_intervals"] = ["1m", "5m"]
            service = HistoricalLiquidityService(store, config, market=FakeMarket([]), sleep_fn=lambda _: None)
            candles = [
                binance_kline_to_candle(
                    "BTCUSDT",
                    _kline(index * 60_000, 100 + (index % 4), 110 + (index % 6), 90 - (index % 5), 101 + (index % 3), 100.0),
                ).to_row()
                for index in range(20)
            ]
            store.upsert_historical_klines(candles)

            result = service.update_incremental_levels(config["symbols"][0], end_ms=(20 * 60_000) - 1)
            second = service.update_incremental_levels(config["symbols"][0], end_ms=(20 * 60_000) - 1)
            cursor = store.latest_historical_maintenance_run("BTCUSDT", "incremental_levels_5m")

            self.assertTrue(result["ok"])
            self.assertGreaterEqual(result["updated"], 0)
            self.assertEqual(second["updated"], 0)
            self.assertEqual(cursor["last_run_ms"], 15 * 60_000)

    def test_historical_zones_merge_without_overpowering_live_zones(self):
        levels = [
            {
                "symbol": "BTCUSDT",
                "source": "historical_daily_low",
                "level_type": "period_low",
                "timeframe": "daily",
                "price_low": 90.0,
                "price_high": 100.0,
                "label": "daily low",
                "side": "support",
                "score": 95.0,
                "strength": 95.0,
                "volume": 10_000.0,
                "touches": 3,
            }
        ]
        zones = historical_levels_as_major_zones(levels, mid=120.0, weight=0.30, max_each_side=4, min_zone_score=65.0)

        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["sources"], ["historical_daily_low"])
        self.assertLess(zones[0]["score"], 80.0)
        self.assertGreaterEqual(zones[0]["score"], 65.0)
        self.assertEqual(zones[0]["cached_score"], 95.0)

    def test_live_historical_score_decays_without_db_rebuild(self):
        fresh_level = {
            "symbol": "BTCUSDT",
            "source": "historical_daily_low",
            "level_type": "period_low",
            "timeframe": "daily",
            "price_low": 90.0,
            "price_high": 100.0,
            "label": "daily low",
            "side": "support",
            "score": 95.0,
            "strength": 95.0,
            "volume": 10_000.0,
            "touches": 3,
            "last_touch": 1_700_000_000_000,
        }
        old_level = dict(fresh_level, last_touch=1_700_000_000_000 - (90 * 86_400_000))

        fresh = historical_levels_as_major_zones(
            [fresh_level],
            mid=120.0,
            weight=0.30,
            max_each_side=4,
            min_zone_score=0.0,
            now_ms=1_700_000_000_000,
            decay_half_life_days=30,
        )[0]
        old = historical_levels_as_major_zones(
            [old_level],
            mid=120.0,
            weight=0.30,
            max_each_side=4,
            min_zone_score=0.0,
            now_ms=1_700_000_000_000,
            decay_half_life_days=30,
        )[0]

        self.assertLess(old["memory_score"], fresh["memory_score"])
        self.assertEqual(old["cached_score"], fresh["cached_score"])

    def test_live_historical_update_does_not_rebuild_by_default(self):
        class FakeHistoricalService:
            def __init__(self):
                self.calls = []

            def update_missing_recent(self, symbol_cfg, *, rebuild_levels=True):
                self.calls.append(rebuild_levels)
                return {"ok": True, "symbol": symbol_cfg["name"], "rows": 1, "levels": 0}

            def status(self):
                return {}

            def prune_old_m1(self, symbol_cfg):
                return {"ok": True, "symbol": symbol_cfg["name"], "pruned": 0, "reason": "not_due"}

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            engine.config = _config()
            fake_service = FakeHistoricalService()
            engine.historical_liquidity = fake_service

            engine._maybe_update_historical_liquidity({"name": "BTCUSDT"})

            self.assertEqual(fake_service.calls, [False])

    def test_engine_uses_historical_zone_and_price_discovery_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            engine.config = _config()
            store.replace_historical_levels(
                "BTCUSDT",
                [
                    {
                        "symbol": "BTCUSDT",
                        "source": "historical_daily_low",
                        "level_type": "period_low",
                        "timeframe": "daily",
                        "price_low": 120.0,
                        "price_high": 130.0,
                        "label": "daily low",
                        "side": "support",
                        "score": 95.0,
                        "strength": 95.0,
                        "touches": 3,
                        "last_touch": int(time.time() * 1000),
                        "volume": 10_000.0,
                    }
                ],
            )
            features = {
                "mid": 131.0,
                "book_price_buckets": [],
                "session_volume_profile": [],
                "recent_low": 120.0,
                "recent_high": 133.0,
            }

            result = engine._major_liquidity_features("BTCUSDT", features, 10.0)

            self.assertTrue(result["historical_liquidity_summary"]["price_discovery_up"])
            self.assertTrue(TradingEngine._price_discovery_allowed("BUY", result))
            self.assertTrue(any("historical_daily_low" in zone["sources"] for zone in result["major_liquidity_zones"]))


if __name__ == "__main__":
    unittest.main()
