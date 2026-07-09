import sys
from pathlib import Path
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.engine import TradingEngine
from trading_agent.state import StateStore


class FakeKeepAwake:
    def __init__(self):
        self.active = False
        self.last_reason = "idle"
        self.enabled = 0
        self.disabled = 0

    def enable(self):
        self.active = True
        self.enabled += 1
        self.last_reason = "enabled"
        return True, self.last_reason

    def disable(self):
        self.active = False
        self.disabled += 1
        self.last_reason = "disabled"
        return True, self.last_reason


class PowerTests(unittest.TestCase):
    def test_keep_awake_enable_honors_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            fake = FakeKeepAwake()
            engine.keep_awake = fake

            engine._enable_keep_awake()

            self.assertTrue(fake.active)
            self.assertEqual(fake.enabled, 1)

    def test_keep_awake_can_be_disabled_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            engine.config["engine"]["keep_awake"] = False
            fake = FakeKeepAwake()
            engine.keep_awake = fake

            engine._enable_keep_awake()

            self.assertFalse(fake.active)
            self.assertEqual(fake.enabled, 0)
            self.assertIn("Disabled", fake.last_reason)

    def test_keep_awake_disable_releases_active_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TradingEngine(StateStore(Path(tmp) / "state.db"))
            fake = FakeKeepAwake()
            fake.active = True
            engine.keep_awake = fake

            engine._disable_keep_awake()

            self.assertFalse(fake.active)
            self.assertEqual(fake.disabled, 1)


if __name__ == "__main__":
    unittest.main()
