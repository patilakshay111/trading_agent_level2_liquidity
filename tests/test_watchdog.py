import sys
from pathlib import Path
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import trading_agent.engine as engine_module
from trading_agent.engine import TradingEngine
from trading_agent.state import StateStore


class WatchdogTests(unittest.TestCase):
    def test_mt5_failures_block_and_recovery_unblocks_live_entries(self):
        original_account_equity = engine_module.account_equity
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
                engine.config["execution"]["mode"] = "mt5_live"
                engine.config["watchdog"] = {
                    "enabled": True,
                    "mt5_check_interval_seconds": 0,
                    "mt5_failure_threshold": 2,
                    "block_live_entries_on_mt5_failure": True,
                    "mt5_restart_enabled": True,
                    "mt5_restart_interval_seconds": 60,
                    "mt5_restart_backoff_multiplier": 2,
                    "mt5_restart_max_interval_seconds": 300,
                    "mt5_failure_notify_interval_seconds": 0,
                    "mt5_terminal_path": "C:/MT5/terminal64.exe",
                    "telegram_alerts": False,
                    "alert_cooldown_seconds": 0,
                }
                launches = []
                engine._launch_mt5_terminal = lambda cfg: launches.append(cfg["mt5_terminal_path"]) or (True, "launched test terminal")  # type: ignore[method-assign]

                engine_module.account_equity = lambda: (False, 0.0, "MT5 down")
                engine._watchdog_cycle(engine.config)
                self.assertIsNone(engine._watchdog_live_entry_block_reason())

                engine._watchdog_cycle(engine.config)
                self.assertIn("MT5 down", engine._watchdog_live_entry_block_reason() or "")
                self.assertTrue(engine.status()["watchdog"]["live_entries_blocked"])
                self.assertEqual(launches, ["C:/MT5/terminal64.exe"])
                self.assertEqual(engine.status()["watchdog"]["next_mt5_restart_interval_seconds"], 60)

                engine._watchdog_cycle(engine.config)
                self.assertEqual(launches, ["C:/MT5/terminal64.exe"])

                engine._last_mt5_restart_attempt -= 60
                engine._watchdog_cycle(engine.config)
                self.assertEqual(launches, ["C:/MT5/terminal64.exe", "C:/MT5/terminal64.exe"])
                self.assertEqual(engine.status()["watchdog"]["next_mt5_restart_interval_seconds"], 120)

                engine_module.account_equity = lambda: (True, 10000.0, "OK")
                engine._watchdog_cycle(engine.config)
                self.assertIsNone(engine._watchdog_live_entry_block_reason())
                self.assertFalse(engine.status()["watchdog"]["live_entries_blocked"])
                self.assertEqual(engine.status()["watchdog"]["mt5_restart_attempts"], 0)
        finally:
            engine_module.account_equity = original_account_equity


if __name__ == "__main__":
    unittest.main()
