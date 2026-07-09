import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.engine import TradingEngine
from trading_agent.broker.mt5 import LotSizingResult
from trading_agent.risk import RiskEngine
from trading_agent.state import StateStore


class FakeMarket:
    def klines(self, *args, **kwargs):
        return [
            [0, "100", "105", "95", "102", "2", 1, "204", 10, "1.25", "127.5"],
            [2, "102", "110", "101", "108", "3", 3, "318", 12, "2.00", "212"],
        ]


class SetupConfirmationTests(unittest.TestCase):
    def test_setup_does_not_execute_on_first_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 2
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "liquidity_sweep_reversal",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 102.5,
                "rr": 2.5,
                "score": 80.0,
                "liquidity_score": 80.0,
                "flow_score": 80.0,
                "regime_score": 70.0,
                "entry_zone_low": 99.95,
                "entry_zone_high": 100.05,
                "entry_reason": "test entry reason",
                "stop_reason": "test stop reason",
                "tp_reason": "test tp reason",
                "reason": "test reason",
            }
            features = {"mid": 100.0}

            first_ready, _ = engine._confirm_setup(candidate, features)
            second_ready, _ = engine._confirm_setup(candidate, features)

            self.assertFalse(first_ready)
            self.assertTrue(second_ready)

    def test_thesis_confirmation_expiry_keeps_setup_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 2
            engine.config["setups"]["confirmation_max_age_seconds"] = 180
            engine.config["setups"]["thesis_max_age_minutes"] = 120
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 64000.0,
                "stop_loss": 63850.0,
                "take_profit": 64600.0,
                "rr": 4.0,
                "score": 92.0,
                "liquidity_score": 76.0,
                "flow_score": 57.0,
                "regime_score": 62.0,
                "entry_zone_low": 63990.0,
                "entry_zone_high": 64010.0,
                "entry_reason": "unit thesis",
                "stop_reason": "unit stop",
                "tp_reason": "unit tp",
                "reason": "unit reason",
            }

            first_ready, _ = engine._confirm_setup(candidate, {"mid": 64000.0})
            key = engine._setup_key(candidate)
            original_created = engine.pending_setups[key]["thesis_created_ts"]
            engine.pending_setups[key]["confirmation_started_ts"] -= 181
            second_ready, reason = engine._confirm_setup(candidate, {"mid": 64000.0})

            self.assertFalse(first_ready)
            self.assertFalse(second_ready)
            self.assertIn("Confirmation expired", reason)
            self.assertEqual(engine.pending_setups[key]["confirmations"], 1)
            self.assertEqual(engine.pending_setups[key]["thesis_created_ts"], original_created)

    def test_thesis_outside_entry_zone_resets_confirmation_without_killing_thesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 2
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 64000.0,
                "stop_loss": 63850.0,
                "take_profit": 64600.0,
                "rr": 4.0,
                "score": 92.0,
                "liquidity_score": 76.0,
                "flow_score": 57.0,
                "regime_score": 62.0,
                "entry_zone_low": 63990.0,
                "entry_zone_high": 64010.0,
                "entry_reason": "unit thesis",
                "stop_reason": "unit stop",
                "tp_reason": "unit tp",
                "reason": "unit reason",
            }

            engine._confirm_setup(candidate, {"mid": 64000.0})
            key = engine._setup_key(candidate)
            ready, reason = engine._confirm_setup(candidate, {"mid": 64080.0})

            self.assertFalse(ready)
            self.assertIn("Thesis still active", reason)
            self.assertIn(key, engine.pending_setups)
            self.assertEqual(engine.pending_setups[key]["confirmations"], 0)

    def test_thesis_breaking_stop_invalidates_pending_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 2
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 64000.0,
                "stop_loss": 63850.0,
                "take_profit": 64600.0,
                "rr": 4.0,
                "score": 92.0,
                "liquidity_score": 76.0,
                "flow_score": 57.0,
                "regime_score": 62.0,
                "entry_zone_low": 63990.0,
                "entry_zone_high": 64010.0,
                "entry_reason": "unit thesis",
                "stop_reason": "unit stop",
                "tp_reason": "unit tp",
                "reason": "unit reason",
            }

            engine._confirm_setup(candidate, {"mid": 64000.0})
            key = engine._setup_key(candidate)
            ready, reason = engine._confirm_setup(candidate, {"mid": 63849.0})

            self.assertFalse(ready)
            self.assertIn("Thesis invalidated", reason)
            self.assertNotIn(key, engine.pending_setups)

    def test_buy_above_market_waits_for_breakout_then_retest(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 2
            engine.config["setups"]["breakout_buffer_bps"]["XAUUSDT"] = 0.0
            candidate = {
                "symbol": "XAUUSDT",
                "side": "BUY",
                "strategy": "liquidity_breakout_retest",
                "entry": 4181.0,
                "stop_loss": 4174.0,
                "take_profit": 4200.0,
                "rr": 2.5,
                "score": 90.0,
                "liquidity_score": 80.0,
                "flow_score": 80.0,
                "regime_score": 75.0,
                "entry_zone_low": 4180.5,
                "entry_zone_high": 4181.5,
                "reason": "unit test breakout retest",
            }

            waiting_breakout, reason = engine._confirm_setup(candidate, {"mid": 4178.0})
            breakout_seen, reason_after_break = engine._confirm_setup(candidate, {"mid": 4182.0})
            first_retest, reason_after_retest = engine._confirm_setup(candidate, {"mid": 4181.0})
            second_retest, final_reason = engine._confirm_setup(candidate, {"mid": 4181.1})

            self.assertFalse(waiting_breakout)
            self.assertIn("Waiting for price to break above 4181.0000", reason)
            self.assertFalse(breakout_seen)
            self.assertIn("waiting for retest", reason_after_break)
            self.assertFalse(first_retest)
            self.assertIn("confirmation cycle 1/2", reason_after_retest)
            self.assertTrue(second_retest)
            self.assertIn("Breakout-retest confirmed", final_reason)

    def test_buy_above_market_does_not_enter_on_first_touch(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["setups"]["confirmation_cycles"] = 1
            engine.config["setups"]["breakout_buffer_bps"]["XAUUSDT"] = 0.0
            candidate = {
                "symbol": "XAUUSDT",
                "side": "BUY",
                "strategy": "liquidity_breakout_retest",
                "entry": 4181.0,
                "stop_loss": 4174.0,
                "take_profit": 4200.0,
                "rr": 2.5,
                "score": 90.0,
                "liquidity_score": 80.0,
                "flow_score": 80.0,
                "regime_score": 75.0,
                "entry_zone_low": 4180.5,
                "entry_zone_high": 4181.5,
                "reason": "unit test breakout retest",
            }

            engine._confirm_setup(candidate, {"mid": 4178.0})
            ready, reason = engine._confirm_setup(candidate, {"mid": 4181.0})

            self.assertFalse(ready)
            self.assertIn("No first-touch/chase entry", reason)

    def test_session_volume_profile_uses_price_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["vwap"]["source"] = "live_trades_only"
            trades = [
                {"id": 1, "price": "60010", "qty": "2", "isBuyerMaker": False},
                {"id": 2, "price": "60090", "qty": "1", "isBuyerMaker": True},
                {"id": 3, "price": "60120", "qty": "3", "isBuyerMaker": False},
            ]

            features = engine._session_vwap_features("BTCUSDT", "BTCUSDT", trades, 60100.0, 100.0)

            labels = {row["label"]: row for row in features["session_volume_profile"]}
            self.assertIn("60000-60100", labels)
            self.assertIn("60100-60200", labels)
            self.assertEqual(labels["60000-60100"]["volume"], 3.0)
            self.assertEqual(labels["60100-60200"]["volume"], 3.0)

    def test_session_vwap_bootstraps_from_binance_kline_quote_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.market = FakeMarket()
            engine.config["vwap"]["source"] = "binance_klines_plus_live"

            features = engine._session_vwap_features("BTCUSDT", "BTCUSDT", [], 105.0, 10.0)

            self.assertAlmostEqual(features["session_volume"], 5.0)
            self.assertAlmostEqual(features["session_vwap"], 104.4)
            labels = {row["label"]: row for row in features["session_volume_profile"]}
            self.assertEqual(labels["100-110"]["volume"], 5.0)

    def test_session_vwap_skips_live_trades_already_in_closed_klines(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.market = FakeMarket()
            engine.config["vwap"]["source"] = "binance_klines_plus_live"
            engine._vwap_anchor_start_ms = lambda anchor: 0
            trades = [
                {"id": 1, "price": "103", "qty": "10", "time": 2, "isBuyerMaker": False},
                {"id": 2, "price": "106", "qty": "1", "time": 4, "isBuyerMaker": False},
            ]

            features = engine._session_vwap_features("BTCUSDT", "BTCUSDT", trades, 106.0, 10.0)

            self.assertAlmostEqual(features["session_volume"], 6.0)
            self.assertAlmostEqual(features["session_vwap"], (204.0 + 318.0 + 106.0) / 6.0)

    def test_session_vwap_resets_on_new_anchor_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine._ensure_vwap_session("BTCUSDT", 1000, True)
            engine.session_vwap["BTCUSDT"] = {"volume": 10.0}
            engine.session_volume_profile["BTCUSDT"] = {100.0: {"volume": 10.0}}
            engine.session_vwap_bootstrapped.add("BTCUSDT")
            engine.seen_session_trade_ids["BTCUSDT"] = {1}
            engine.seen_session_trade_order["BTCUSDT"] = []

            engine._ensure_vwap_session("BTCUSDT", 2000, True)

            self.assertEqual(engine.session_vwap_anchor_start_ms["BTCUSDT"], 2000)
            self.assertEqual(engine.session_vwap_live_start_ms["BTCUSDT"], 2000)
            self.assertNotIn("BTCUSDT", engine.session_vwap)
            self.assertNotIn("BTCUSDT", engine.session_volume_profile)
            self.assertNotIn("BTCUSDT", engine.session_vwap_bootstrapped)
            self.assertNotIn("BTCUSDT", engine.seen_session_trade_ids)

    def test_live_position_matching_requires_exact_stored_ticket(self):
        trade = {
            "symbol": "XAUUSDT",
            "mt5_symbol": "XAUUSD",
            "side": "SELL",
            "broker_position_id": 111,
        }
        positions = [
            {"ticket": 222, "identifier": 222, "symbol": "XAUUSD", "type": 1},
        ]

        self.assertIsNone(TradingEngine._match_position(trade, positions))

    def test_execution_guard_widens_tiny_xau_stop_and_recalculates_lots(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["execution_guards"]["max_lots"]["XAUUSDT"] = 0.20
            engine._sizing_for = lambda *args: LotSizingResult(0.23, 0.23, 70.0, 300.0, True, "OK")  # type: ignore[method-assign]
            sizing = LotSizingResult(2.06, 2.06, 70.0, 33.82, True, "OK")
            candidate = {"symbol": "XAUUSDT", "side": "BUY", "take_profit": 4155.0}

            guard = engine._execution_guard(candidate, "XAUUSD", 4134.783, 4134.445, sizing, 14000.0, 0.5)

            self.assertTrue(guard["ok"])
            self.assertAlmostEqual(guard["stop_loss"], 4131.783, places=3)
            self.assertEqual(guard["lots"], 0.20)
            self.assertEqual(guard["risk_cash"], 60.0)
            self.assertIn("widened SL", guard["reason"])

    def test_execution_guard_caps_oversized_lots(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["execution_guards"]["max_lots"]["XAUUSDT"] = 0.20
            sizing = LotSizingResult(2.0, 2.0, 200.0, 100.0, True, "OK")
            candidate = {"symbol": "XAUUSDT", "side": "SELL", "take_profit": 4130.0}

            guard = engine._execution_guard(candidate, "XAUUSD", 4150.0, 4146.0, sizing, 14000.0, 0.5)

            self.assertTrue(guard["ok"])
            self.assertEqual(guard["stop_loss"], 4146.0)
            self.assertEqual(guard["lots"], 0.20)
            self.assertEqual(guard["risk_cash"], 20.0)
            self.assertIn("Capped lots", guard["reason"])

    def test_sizing_equity_prefers_configured_account_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["risk"]["sizing_account_balance"] = 10000.0

            with patch("trading_agent.engine.account_equity") as account:
                equity, reason = engine._sizing_equity(allow_mt5=True, fallback_to_paper=False)

            account.assert_not_called()
            self.assertEqual(equity, 10000.0)
            self.assertIn("configured sizing account balance", reason)

    def test_regime_filter_blocks_sell_in_bullish_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "XAUUSDT",
                "side": "SELL",
                "strategy": "test",
                "entry": 4150.0,
                "stop_loss": 4155.0,
                "take_profit": 4130.0,
                "rr": 4.0,
                "score": 90.0,
            }
            features = {
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "delta_imbalance": 0.2,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
            }

            engine._annotate_candidate_regime(candidate, features)
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(candidate["regime_side_allowed"])
            self.assertFalse(decision.approved)
            self.assertIn("countertrend trading is disabled", decision.reason)

    def test_buy_support_bias_blocks_sells_until_support_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            engine.structure_bias["XAUUSDT"] = {
                "trade_id": 235,
                "symbol": "XAUUSDT",
                "mt5_symbol": "XAUUSD",
                "side": "BUY",
                "level": 4131.783,
                "created_at": 1000.0,
                "expires_at": 9999999999.0,
            }
            candidate = {"symbol": "XAUUSDT", "side": "SELL", "entry": 4134.0, "score": 90.0, "rr": 3.0}
            features = {"mid": 4134.0}

            engine._annotate_candidate_structure_bias(candidate, features)
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(candidate["structure_side_allowed"])
            self.assertFalse(decision.approved)
            self.assertIn("active BUY structure", decision.reason)

    def test_risk_allows_opposite_side_trade_on_same_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            store.open_trade(
                {
                    "symbol": "XAUUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 4193.0,
                    "stop_loss": 4188.0,
                    "take_profit": 4220.0,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 3.0,
                    "score": 90.0,
                    "execution_mode": "paper",
                }
            )
            candidate = {
                "symbol": "XAUUSDT",
                "side": "SELL",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 4194.0,
                "stop_loss": 4201.0,
                "take_profit": 4170.0,
                "rr": 3.4,
                "score": 100.0,
                "a_plus_allowed": True,
                "regime_side_allowed": True,
                "structure_side_allowed": True,
            }

            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertTrue(decision.approved)

    def test_risk_blocks_same_side_trade_on_same_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            store.open_trade(
                {
                    "symbol": "XAUUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 4193.0,
                    "stop_loss": 4188.0,
                    "take_profit": 4220.0,
                    "lots": 0.1,
                    "risk_cash": 25.0,
                    "rr": 3.0,
                    "score": 90.0,
                    "execution_mode": "paper",
                }
            )
            candidate = {
                "symbol": "XAUUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 4194.0,
                "stop_loss": 4188.0,
                "take_profit": 4220.0,
                "rr": 4.3,
                "score": 100.0,
                "a_plus_allowed": True,
                "regime_side_allowed": True,
                "structure_side_allowed": True,
            }

            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(decision.approved)
            self.assertIn("open BUY position", decision.reason)

    def test_a_plus_filter_allows_bullish_buy_from_support_liquidity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "locked_liquidity_thesis_entry",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 103.0,
                "rr": 3.0,
                "score": 90.0,
                "liquidity_score": 85.0,
                "target_memory_score": 25.0,
            }
            features = {
                "mid": 100.0,
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "delta_imbalance": 0.2,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
                "book_price_buckets": [
                    {
                        "price_low": 99.0,
                        "price_high": 100.0,
                        "label": "99-100",
                        "bid_notional": 300000.0,
                        "ask_notional": 0.0,
                        "strength": 80.0,
                        "memory_score": 50.0,
                    }
                ],
            }

            engine._annotate_candidate_regime(candidate, features)
            engine._annotate_candidate_a_plus(candidate, features, {"mt5_symbol": "BTCUSD", "trade_enabled": True})
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertTrue(candidate["a_plus_allowed"])
            self.assertTrue(decision.approved)

    def test_a_plus_filter_blocks_sell_in_bullish_broader_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "strategy": "locked_liquidity_thesis_entry",
                "entry": 100.0,
                "stop_loss": 101.0,
                "take_profit": 97.0,
                "rr": 3.0,
                "score": 92.0,
                "liquidity_score": 90.0,
                "target_memory_score": 30.0,
            }
            features = {
                "mid": 100.0,
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "delta_imbalance": 0.2,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
                "book_price_buckets": [
                    {
                        "price_low": 100.0,
                        "price_high": 101.0,
                        "label": "100-101",
                        "bid_notional": 0.0,
                        "ask_notional": 300000.0,
                        "strength": 80.0,
                        "memory_score": 50.0,
                    }
                ],
            }

            engine._annotate_candidate_regime(candidate, features)
            engine._annotate_candidate_a_plus(candidate, features, {"mt5_symbol": "BTCUSD", "trade_enabled": True})
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(candidate["a_plus_allowed"])
            self.assertFalse(decision.approved)
            self.assertIn("not aligned with broader bullish regime", decision.reason)

    def test_a_plus_filter_blocks_entry_without_visible_support_liquidity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "locked_liquidity_thesis_entry",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 103.0,
                "rr": 3.0,
                "score": 90.0,
                "liquidity_score": 85.0,
                "target_memory_score": 25.0,
            }
            features = {
                "mid": 100.0,
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "delta_imbalance": 0.2,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
                "book_price_buckets": [],
            }

            engine._annotate_candidate_regime(candidate, features)
            engine._annotate_candidate_a_plus(candidate, features, {"mt5_symbol": "BTCUSD", "trade_enabled": True})
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(candidate["a_plus_allowed"])
            self.assertFalse(decision.approved)
            self.assertIn("no liquidity zones", decision.reason)

    def test_a_plus_filter_allows_strong_b_tier_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 102.6,
                "rr": 2.6,
                "score": 92.0,
                "liquidity_score": 70.0,
                "target_memory_score": 20.0,
            }
            features = {
                "mid": 100.0,
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "delta_imbalance": 0.13,
                "session_delta_imbalance": 0.01,
                "open_interest_support": "supports_up",
                "book_price_buckets": [
                    {
                        "price_low": 99.0,
                        "price_high": 100.0,
                        "label": "99-100",
                        "bid_notional": 300000.0,
                        "ask_notional": 0.0,
                        "strength": 80.0,
                        "memory_score": 50.0,
                    }
                ],
            }

            engine._annotate_candidate_regime(candidate, features)
            engine._annotate_candidate_a_plus(candidate, features, {"mt5_symbol": "BTCUSD", "trade_enabled": True})
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertTrue(candidate["a_plus_allowed"])
            self.assertEqual(candidate["a_plus_tier"], "B")
            self.assertTrue(decision.approved)

    def test_a_plus_filter_blocks_weak_b_tier_range_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            engine = TradingEngine(store)
            candidate = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strategy": "repeated_liquidity_zone_thesis",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 102.1,
                "rr": 2.1,
                "score": 84.0,
                "liquidity_score": 60.0,
                "target_memory_score": 0.0,
            }
            features = {
                "mid": 100.0,
                "session_vwap_distance_bps": 0.2,
                "recent_vwap_distance_bps": -0.2,
                "trade_drift_bps": 0.1,
                "delta_imbalance": 0.13,
                "session_delta_imbalance": -0.01,
                "open_interest_support": "neutral",
                "book_price_buckets": [
                    {
                        "price_low": 99.0,
                        "price_high": 100.0,
                        "label": "99-100",
                        "bid_notional": 300000.0,
                        "ask_notional": 0.0,
                        "strength": 80.0,
                        "memory_score": 50.0,
                    }
                ],
            }

            engine._annotate_candidate_regime(candidate, features)
            engine._annotate_candidate_a_plus(candidate, features, {"mt5_symbol": "BTCUSD", "trade_enabled": True})
            decision = RiskEngine(engine.config, store).approve(candidate, {"trade_enabled": True})

            self.assertFalse(candidate["a_plus_allowed"])
            self.assertEqual(candidate["a_plus_tier"], "REJECTED")
            self.assertFalse(decision.approved)

    def test_active_exit_protects_short_on_first_support_absorption(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["active_exit"]["close_losing_trades"] = False
            trade = {
                "id": 1,
                "symbol": "XAUUSDT",
                "mt5_symbol": "XAUUSD",
                "side": "SELL",
                "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds"),
                "entry": 4135.0,
                "stop_loss": 4140.0,
                "take_profit": 4120.0,
            }
            features = {
                "symbol": "XAUUSDT",
                "mid": 4130.0,
                "delta_imbalance": 0.22,
                "cumulative_delta": 24.0,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
                "session_vwap_distance_bps": 4.0,
                "recent_vwap_distance_bps": 1.5,
                "trade_drift_bps": 2.2,
                "trade_m1_candles": [
                    {
                        "open": 4131.0,
                        "high": 4131.5,
                        "low": 4127.5,
                        "close": 4130.9,
                        "lower_wick_pct": 0.65,
                        "upper_wick_pct": 0.10,
                        "close_position": 0.85,
                        "delta": 18.0,
                    }
                ],
            }

            signal = engine._active_exit_signal(trade, features, 4130.0)

            self.assertFalse(signal["exit"])
            self.assertTrue(signal["protect"])
            self.assertIn("support absorption", signal["reason"])
            self.assertIn("protects profit instead of full close", signal["reason"])

    def test_active_exit_closes_short_after_absorption_when_profit_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["active_exit"]["close_losing_trades"] = False
            trade = {
                "id": 1,
                "symbol": "XAUUSDT",
                "mt5_symbol": "XAUUSD",
                "side": "SELL",
                "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds"),
                "entry": 4135.0,
                "initial_stop_loss": 4140.0,
                "stop_loss": 4134.5,
                "take_profit": 4120.0,
            }
            features = {
                "symbol": "XAUUSDT",
                "mid": 4130.0,
                "delta_imbalance": 0.22,
                "cumulative_delta": 24.0,
                "session_delta_imbalance": 0.02,
                "open_interest_support": "supports_up",
                "session_vwap_distance_bps": 4.0,
                "recent_vwap_distance_bps": 1.5,
                "trade_drift_bps": 2.2,
                "trade_m1_candles": [
                    {
                        "open": 4131.0,
                        "high": 4131.5,
                        "low": 4127.5,
                        "close": 4130.9,
                        "lower_wick_pct": 0.65,
                        "upper_wick_pct": 0.10,
                        "close_position": 0.85,
                        "delta": 18.0,
                    }
                ],
            }

            signal = engine._active_exit_signal(trade, features, 4130.0)

            self.assertTrue(signal["exit"])
            self.assertIn("support absorption", signal["reason"])

    def test_active_exit_does_not_cut_losing_trade_without_hard_invalidation_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["active_exit"]["close_losing_trades"] = False
            trade = {
                "id": 1,
                "symbol": "XAUUSDT",
                "mt5_symbol": "XAUUSD",
                "side": "SELL",
                "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=240)).isoformat(timespec="seconds"),
                "entry": 4135.0,
                "initial_stop_loss": 4140.0,
                "stop_loss": 4140.0,
                "take_profit": 4120.0,
            }
            features = {
                "symbol": "XAUUSDT",
                "mid": 4136.0,
                "delta_imbalance": 0.30,
                "cumulative_delta": 30.0,
                "session_delta_imbalance": 0.03,
                "open_interest_support": "supports_up",
                "session_vwap_distance_bps": 5.0,
                "recent_vwap_distance_bps": 2.0,
                "trade_drift_bps": 3.0,
                "trade_m1_candles": [
                    {
                        "open": 4134.0,
                        "high": 4136.2,
                        "low": 4132.0,
                        "close": 4135.9,
                        "lower_wick_pct": 0.50,
                        "upper_wick_pct": 0.07,
                        "close_position": 0.93,
                        "delta": 20.0,
                    }
                ],
            }

            signal = engine._active_exit_signal(trade, features, 4136.0)

            self.assertFalse(signal["exit"])
            self.assertIn("close_losing_trades=false", signal["reason"])

    def test_active_exit_books_profit_at_target_liquidity_absorption(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            trade = {
                "id": 1,
                "symbol": "BTCUSDT",
                "mt5_symbol": "BTCUSD",
                "side": "BUY",
                "opened_at": (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(timespec="seconds"),
                "entry": 100.0,
                "initial_stop_loss": 98.0,
                "stop_loss": 100.2,
                "take_profit": 110.0,
            }
            features = {
                "symbol": "BTCUSDT",
                "mid": 109.9,
                "major_liquidity_zones": [
                    {
                        "side": "resistance",
                        "price_low": 109.5,
                        "price_high": 110.5,
                        "label": "109-110",
                        "score": 90.0,
                    }
                ],
                "trade_m1_candles": [
                    {
                        "open": 109.8,
                        "high": 110.4,
                        "low": 109.2,
                        "close": 109.35,
                        "lower_wick_pct": 0.12,
                        "upper_wick_pct": 0.50,
                        "close_position": 0.12,
                        "delta": -15.0,
                    }
                ],
            }

            signal = engine._active_exit_signal(trade, features, 109.9)

            self.assertTrue(signal["exit"])
            self.assertIn("profit booked at opposing liquidity", signal["reason"])

    def test_paper_manager_locks_profit_after_favorable_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["execution"]["mode"] = "paper"
            engine._execution_tick = lambda symbol, mt5_symbol: {"bid": 102.2, "ask": 102.3}  # type: ignore[method-assign]
            trade_id = engine.store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 106.0,
                    "risk_cash": 50.0,
                    "rr": 3.0,
                    "score": 80.0,
                    "execution_mode": "paper",
                }
            )

            engine._manage_open_trades("BTCUSDT", 102.2)

            trade = engine.store.query("select * from trades where id = ?", (trade_id,))[0]
            self.assertEqual(trade["status"], "OPEN")
            self.assertGreater(trade["stop_loss"], 100.0)
            self.assertEqual(trade["initial_stop_loss"], 98.0)

    def test_paper_manager_closes_locked_profit_using_initial_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["execution"]["mode"] = "paper"
            mark = {"bid": 102.2, "ask": 102.3}
            engine._execution_tick = lambda symbol, mt5_symbol: mark  # type: ignore[method-assign]
            trade_id = engine.store.open_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "strategy": "test",
                    "entry": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 106.0,
                    "risk_cash": 50.0,
                    "rr": 3.0,
                    "score": 80.0,
                    "execution_mode": "paper",
                }
            )

            engine._manage_open_trades("BTCUSDT", 102.2)
            locked = engine.store.query("select * from trades where id = ?", (trade_id,))[0]["stop_loss"]
            mark["bid"] = locked
            mark["ask"] = locked + 0.1
            engine._manage_open_trades("BTCUSDT", locked)

            trade = engine.store.query("select * from trades where id = ?", (trade_id,))[0]
            self.assertEqual(trade["status"], "CLOSED")
            self.assertGreater(trade["pnl"], 0.0)
            self.assertAlmostEqual(trade["pnl"], ((locked - 100.0) / 2.0) * 50.0, places=4)


if __name__ == "__main__":
    unittest.main()
