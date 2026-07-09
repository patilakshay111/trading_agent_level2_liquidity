from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import time
import webbrowser

from .backtest import backtest_equal_extreme_rejection, candles_from_klines
from .broker.mt5 import open_positions
from .config import ROOT, load_config
from .engine import TradingEngine
from .market.binance import BinanceMarketData
from .state import StateStore


STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_BACKTEST_DAYS = 365


store = StateStore()
engine = TradingEngine(store)


class TradingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class AppHandler(BaseHTTPRequestHandler):
    server_version = "TradingAgent/0.1"

    def log_message(self, format: str, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_static("index.html", "text/html")
        elif self.path.startswith("/static/"):
            name = self.path.replace("/static/", "", 1).split("?", 1)[0]
            content_type = "text/css" if name.endswith(".css") else "application/javascript"
            self._serve_static(name, content_type)
        elif self.path == "/api/status":
            self._json(engine.status())
        elif self.path == "/api/config":
            self._json(load_config())
        elif self.path == "/api/stats":
            self._json(store.stats())
        elif self.path == "/api/trades":
            self._json(store.query("select * from trades order by opened_at desc limit 500"))
        elif self.path == "/api/candidates":
            self._json(store.query("select * from candidates order by created_at desc limit 500"))
        elif self.path == "/api/contracts":
            self._json(store.query("select * from symbol_contracts order by symbol"))
        elif self.path == "/api/events":
            self._json(store.query("select * from events order by created_at desc limit 200"))
        elif self.path == "/api/broker-positions":
            ok, positions, message = open_positions()
            self._json({"ok": ok, "message": message, "positions": positions})
        elif self.path == "/api/historical-liquidity":
            self._json({
                "status": engine.historical_liquidity.status(),
                "levels": store.query("select * from historical_liquidity_levels order by symbol, score desc limit 500"),
            })
        elif self.path == "/api/history/backfill-status":
            self._json(engine.historical_liquidity.status())
        else:
            self._not_found()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/start":
            started = engine.start()
            self._json({"ok": True, "started": started, "status": engine.status()})
        elif self.path == "/api/stop":
            stopped = engine.stop()
            self._json({"ok": True, "stopped": stopped, "status": engine.status()})
        elif self.path == "/api/reload":
            engine.reload_config()
            self._json({"ok": True, "config": load_config()})
        elif self.path == "/api/scan-contracts":
            self._json({"ok": True, "results": engine.scan_mt5_contracts()})
        elif self.path == "/api/backtest/price-action":
            self._json(self._price_action_backtest())
        elif self.path == "/api/history/backfill":
            payload = self._read_json()
            years = payload.get("years")
            symbols = payload.get("symbols")
            self._json(engine.start_historical_backfill(
                years=int(years) if years else None,
                symbols=[str(item) for item in symbols] if isinstance(symbols, list) else None,
            ))
        else:
            self._not_found()

    def _serve_static(self, name: str, content_type: str) -> None:
        path = (STATIC_ROOT / name).resolve()
        if not str(path).startswith(str(STATIC_ROOT.resolve())) or not path.exists():
            self._not_found()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, data, status: int = 200) -> None:
        encoded = json.dumps(data, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
            return

    def _not_found(self) -> None:
        self._json({"error": "not found"}, 404)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _price_action_backtest(self) -> dict:
        payload = self._read_json()
        config = load_config()
        symbol_name = str(payload.get("symbol") or "BTCUSDT").upper()
        symbol_cfg = next((item for item in config.get("symbols", []) if str(item.get("name", "")).upper() == symbol_name), None)
        data_symbol = str((symbol_cfg or {}).get("data_symbol") or symbol_name).upper()
        mt5_symbol = str((symbol_cfg or {}).get("mt5_symbol") or symbol_name).upper()
        market_cfg = config.get("market_data", {})
        market = BinanceMarketData(
            base_url=str(market_cfg.get("rest_base_url", "https://fapi.binance.com")),
            market=str(market_cfg.get("market", "um_futures")),
            timeout=10.0,
        )

        days = max(1, min(MAX_BACKTEST_DAYS, int(payload.get("days") or 5)))
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
        klines = []
        cursor = start_ms
        try:
            while cursor < end_ms:
                batch = market.klines(data_symbol, interval="1m", start_time=cursor, end_time=end_ms, limit=1500)
                if not batch:
                    break
                klines.extend(batch)
                last_open = int(batch[-1][0])
                next_cursor = last_open + 60_000
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                if len(batch) < 1500:
                    break
        except Exception as exc:  # noqa: BLE001 - return API error to the UI
            return {"ok": False, "error": f"Could not fetch Binance M1 candles for {data_symbol}: {exc}"}

        candles = candles_from_klines(klines)
        if not candles:
            return {"ok": False, "error": f"No M1 candles returned for {data_symbol}."}

        trailing_enabled = bool(payload.get("trailing_enabled", True))
        trade_management = dict(config.get("trade_management", {}))
        trade_management["enabled"] = trailing_enabled and bool(trade_management.get("enabled", True))
        contract_rows = store.query("select * from symbol_contracts where symbol = ?", (mt5_symbol,))
        contract_spec = dict(contract_rows[0]) if contract_rows else self._default_contract_spec(mt5_symbol)

        result = backtest_equal_extreme_rejection(
            candles,
            side=str(payload.get("side") or "BOTH").upper(),
            rr=float(payload.get("rr") or 2.0),
            lookback_candles=int(payload.get("lookback_candles") or 20),
            equal_tolerance_bps=float(payload.get("equal_tolerance_bps") or 3.0),
            min_zone_touches=int(payload.get("min_zone_touches") or 2),
            pivot_span=int(payload.get("pivot_span") or 2),
            min_wick_pct=float(payload.get("min_wick_pct") or 0.45),
            min_close_reclaim_pct=float(payload.get("min_close_reclaim_pct") or 0.60),
            stop_buffer_bps=float(payload.get("stop_buffer_bps") or 1.0),
            max_hold_candles=int(payload.get("max_hold_candles") or 120),
            trade_management=trade_management,
            lot_size=0.01,
            contract_spec=contract_spec,
        )
        result.update({
            "symbol": symbol_name,
            "data_symbol": data_symbol,
            "mt5_symbol": mt5_symbol,
            "days": days,
            "max_days": MAX_BACKTEST_DAYS,
            "trailing_enabled": trailing_enabled,
            "lot_size": 0.01,
            "contract_spec_source": "mt5_scan" if contract_rows else "fallback",
            "trade_management": trade_management,
        })
        return result

    @staticmethod
    def _default_contract_spec(mt5_symbol: str) -> dict[str, float]:
        symbol = mt5_symbol.upper()
        contract_size = 100.0 if symbol.startswith("XAU") else 1.0
        return {"contract_size": contract_size}


def main() -> None:
    config = load_config()
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = int(config.get("server", {}).get("port", 8765))
    server = TradingHTTPServer((host, port), AppHandler)
    url = f"http://{host}:{port}"
    print(f"Trading Agent UI: {url}")
    print(f"Config: {ROOT / 'config' / 'settings.toml'}")
    if config.get("engine", {}).get("auto_start", False):
        engine.start()
    if engine.telegram.chat_enabled:
        engine.telegram.start_chat()
    if config.get("server", {}).get("open_browser_on_start", False):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCtrl+C received. Shutting down Trading Agent...")
    finally:
        engine.telegram.stop_chat()
        engine.stop()
        server.server_close()
        print("Trading Agent stopped.")
