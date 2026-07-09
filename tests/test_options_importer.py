import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_agent.options_importer import parse_cme_gold_options_text


class OptionsImporterTests(unittest.TestCase):
    def test_parse_cme_gold_options_text_pairs_call_and_put_oi_by_strike(self):
        text = """
        CME Daily Bulletin 2026-07-06
        GOLD OPTIONS CALLS JUL 2026
        2350 0 0 0 0 12 1520
        2400 0 0 0 0 42 3500
        GOLD OPTIONS PUTS JUL 2026
        2350 0 0 0 0 18 2600
        2400 0 0 0 0 35 4100
        """

        rows = parse_cme_gold_options_text(text, source="unit")
        by_strike = {row["strike"]: row for row in rows}

        self.assertEqual(len(rows), 2)
        self.assertEqual(by_strike[2350.0]["expiry"], "2026-07")
        self.assertEqual(by_strike[2350.0]["call_oi"], 1520.0)
        self.assertEqual(by_strike[2350.0]["put_oi"], 2600.0)
        self.assertEqual(by_strike[2400.0]["call_volume"], 42.0)
        self.assertEqual(by_strike[2400.0]["put_volume"], 35.0)


if __name__ == "__main__":
    unittest.main()
