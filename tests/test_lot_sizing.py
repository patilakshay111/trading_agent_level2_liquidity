import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.broker.mt5 import calculate_lot_size, normalize_price, supported_filling_types


class LotSizingTests(unittest.TestCase):
    def test_calculates_volume_from_tick_value(self):
        spec = {
            "trade_tick_size": 0.01,
            "trade_tick_value_loss": 1.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        result = calculate_lot_size(equity=10000, risk_pct=0.25, entry=100.0, stop_loss=99.5, spec=spec)
        self.assertTrue(result.valid)
        self.assertEqual(result.risk_cash, 25.0)
        self.assertEqual(result.lots, 0.5)

    def test_calculates_volume_from_persisted_contract_row(self):
        spec = {
            "tick_size": 0.01,
            "tick_value_loss": 1.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        result = calculate_lot_size(equity=10000, risk_pct=0.25, entry=100.0, stop_loss=99.5, spec=spec)
        self.assertTrue(result.valid)
        self.assertEqual(result.risk_cash, 25.0)
        self.assertEqual(result.lots, 0.5)

    def test_normalizes_price_to_broker_tick_and_digits(self):
        spec = {"tick_size": 0.001, "digits": 3}
        self.assertEqual(normalize_price(4215.1544, spec), 4215.154)

        spec = {"trade_tick_size": 0.01, "digits": 2}
        self.assertEqual(normalize_price(62906.756, spec), 62906.76)

    def test_maps_mt5_symbol_filling_flags_to_order_filling_types(self):
        class FakeMT5:
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_BOC = 3

        self.assertEqual(supported_filling_types(FakeMT5, {"filling_mode": 1}), [0])
        self.assertEqual(supported_filling_types(FakeMT5, {"filling_mode": 3}), [0, 1])


if __name__ == "__main__":
    unittest.main()
