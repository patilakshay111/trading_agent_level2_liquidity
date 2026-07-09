import sys
from pathlib import Path
from threading import Thread
import unittest
from urllib.request import urlopen
import json


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from http.server import ThreadingHTTPServer
from trading_agent.app import AppHandler, MAX_BACKTEST_DAYS


class AppApiTests(unittest.TestCase):
    def test_status_endpoint(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        host, port = server.server_address
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://{host}:{port}/api/status", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("running", payload)
            self.assertIn("symbols", payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_price_action_backtest_cap_allows_one_year(self):
        self.assertEqual(MAX_BACKTEST_DAYS, 365)


if __name__ == "__main__":
    unittest.main()
