from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Any
from urllib.parse import quote

from .binance import BinanceMarketData


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WebSocketProtocolError(RuntimeError):
    pass


class MinimalWebSocket:
    """Small RFC 6455 client for Binance market-data streams.

    It avoids a third-party dependency so the desktop app can run from the
    existing Python install. It implements the subset needed for text market
    data, ping/pong, and reconnect handling.
    """

    def __init__(self, host: str, path: str, port: int = 9443, timeout: float = 15.0):
        self.host = host
        self.path = path
        self.port = port
        self.timeout = timeout
        self.sock: ssl.SSLSocket | None = None

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: TradingAgent/0.1\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_response()
        if " 101 " not in response.split("\r\n", 1)[0]:
            raise WebSocketProtocolError(f"WebSocket upgrade failed: {response.splitlines()[0]}")
        accept_expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept_expected.lower() not in response.lower():
            raise WebSocketProtocolError("WebSocket accept key mismatch.")

    def close(self) -> None:
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def recv_json(self) -> dict[str, Any] | None:
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:
                raise WebSocketProtocolError("Server closed websocket.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            raise WebSocketProtocolError(f"Unsupported websocket opcode: {opcode}")

    def _read_http_response(self) -> str:
        assert self.sock is not None
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("iso-8859-1", errors="replace")

    def _read_exact(self, size: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise WebSocketProtocolError("Socket closed while reading frame.")
            data += chunk
        return data

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._read_exact(2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        mask = os.urandom(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)


@dataclass
class BookState:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    trades: deque[dict[str, Any]] = field(default_factory=deque)
    pending_depth: deque[dict[str, Any]] = field(default_factory=deque)
    last_update_id: int = 0
    snapshot_loaded: bool = False
    in_sync: bool = False
    last_event_ts: float = 0.0
    last_error: str | None = None
    resyncs: int = 0

    def depth_payload(self, limit: int) -> dict[str, Any]:
        bids = sorted(((price, qty) for price, qty in self.bids.items() if qty > 0), reverse=True)[:limit]
        asks = sorted(((price, qty) for price, qty in self.asks.items() if qty > 0))[:limit]
        return {
            "lastUpdateId": self.last_update_id,
            "bids": [[f"{price:.8f}", f"{qty:.8f}"] for price, qty in bids],
            "asks": [[f"{price:.8f}", f"{qty:.8f}"] for price, qty in asks],
        }

    def trade_payload(self) -> list[dict[str, Any]]:
        return list(self.trades)

    def status(self, stale_after: float) -> dict[str, Any]:
        age = time.time() - self.last_event_ts if self.last_event_ts else None
        return {
            "symbol": self.symbol,
            "snapshot_loaded": self.snapshot_loaded,
            "in_sync": self.in_sync,
            "last_update_id": self.last_update_id,
            "last_event_at": datetime.fromtimestamp(self.last_event_ts, timezone.utc).isoformat(timespec="seconds")
            if self.last_event_ts
            else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "stale": age is None or age > stale_after,
            "last_error": self.last_error,
            "resyncs": self.resyncs,
        }


class BinanceWebSocketFeed:
    def __init__(
        self,
        symbols: list[str],
        *,
        snapshot_limit: int = 1000,
        trade_buffer_size: int = 300,
        market: str = "um_futures",
        rest_base_url: str = "https://fapi.binance.com",
        ws_host: str = "fstream.binance.com",
        ws_port: int = 443,
    ):
        self.symbols = [symbol.upper() for symbol in symbols]
        self.snapshot_limit = snapshot_limit
        self.trade_buffer_size = trade_buffer_size
        self.market = market
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.rest = BinanceMarketData(base_url=rest_base_url, market=market)
        self.books = {symbol: BookState(symbol=symbol, trades=deque(maxlen=trade_buffer_size)) for symbol in self.symbols}
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.connected = False
        self.last_error: str | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="binance-websocket-feed", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.connected = False

    def snapshot(self, symbol: str, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        with self.lock:
            book = self.books.get(symbol.upper())
            if not book or not book.snapshot_loaded or not book.in_sync:
                return None
            return book.depth_payload(limit), book.trade_payload()

    def status(self, stale_after: float) -> dict[str, Any]:
        with self.lock:
            return {
                "mode": "websocket",
                "market": self.market,
                "host": self.ws_host,
                "connected": self.connected,
                "last_error": self.last_error,
                "symbols": {symbol: book.status(stale_after) for symbol, book in self.books.items()},
            }

    def _reset_books_for_stream(self) -> None:
        with self.lock:
            for book in self.books.values():
                book.bids.clear()
                book.asks.clear()
                book.pending_depth.clear()
                book.last_update_id = 0
                book.snapshot_loaded = False
                book.in_sync = False
                book.last_error = None

    def _load_snapshot(self, symbol: str) -> None:
        depth = self.rest.depth(symbol, limit=self.snapshot_limit)
        trades = self.rest.recent_trades(symbol, limit=min(self.trade_buffer_size, 1000))
        with self.lock:
            book = self.books[symbol]
            book.bids = {float(price): float(qty) for price, qty in depth.get("bids", [])}
            book.asks = {float(price): float(qty) for price, qty in depth.get("asks", [])}
            book.trades.clear()
            for trade in trades:
                book.trades.append(trade)
            book.last_update_id = int(depth["lastUpdateId"])
            book.snapshot_loaded = True
            book.in_sync = False
            book.last_error = None

    def _run(self) -> None:
        streams = []
        for symbol in self.symbols:
            lower = symbol.lower()
            streams.append(f"{lower}@depth@100ms")
            streams.append(f"{lower}@trade")
        path = "/stream?streams=" + quote("/".join(streams), safe="/@?=&")

        while not self.stop_event.is_set():
            ws = MinimalWebSocket(self.ws_host, path, port=self.ws_port)
            try:
                ws.connect()
                self._reset_books_for_stream()
                self.connected = True
                self.last_error = None
                while not self.stop_event.is_set():
                    payload = ws.recv_json()
                    if payload is None:
                        continue
                    self._handle_payload(payload)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = str(exc)
                with self.lock:
                    for book in self.books.values():
                        book.last_error = str(exc)
                        book.in_sync = False
                if not self.stop_event.wait(3):
                    continue
            finally:
                ws.close()

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        data = payload.get("data", payload)
        event_type = data.get("e")
        symbol = data.get("s", "").upper()
        if symbol not in self.books:
            return
        if event_type == "depthUpdate":
            self._apply_depth(symbol, data)
        elif event_type == "trade":
            self._apply_trade(symbol, data)

    def _apply_depth(self, symbol: str, event: dict[str, Any]) -> None:
        first_id = int(event["U"])
        final_id = int(event["u"])
        with self.lock:
            book = self.books[symbol]
            if not book.snapshot_loaded:
                book.pending_depth.append(event)
                if len(book.pending_depth) < 5:
                    return
        if not self.books[symbol].snapshot_loaded:
            try:
                self._load_snapshot(symbol)
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    book = self.books[symbol]
                    book.last_error = str(exc)
                    book.snapshot_loaded = False
                    book.in_sync = False
                return
            self._drain_pending_depth(symbol)
            return

        with self.lock:
            book = self.books[symbol]
            if final_id <= book.last_update_id:
                return
            previous_id = int(event["pu"]) if "pu" in event else None
            if previous_id is not None:
                has_gap = not (
                    previous_id == book.last_update_id
                    or (previous_id < book.last_update_id and first_id <= book.last_update_id + 1 <= final_id)
                )
            else:
                has_gap = first_id > book.last_update_id + 1
            if has_gap:
                book.in_sync = False
                book.resyncs += 1
                expected = book.last_update_id if previous_id is not None else book.last_update_id + 1
                got = previous_id if previous_id is not None else first_id
                book.last_error = f"Depth gap: got={got}, expected={expected}"
                book.snapshot_loaded = False
                book.pending_depth.append(event)
                return
            self._update_side(book.bids, event.get("b", []))
            self._update_side(book.asks, event.get("a", []))
            book.last_update_id = final_id
            book.in_sync = True
            book.last_event_ts = time.time()
            book.last_error = None

    def _drain_pending_depth(self, symbol: str) -> None:
        with self.lock:
            book = self.books[symbol]
            pending = list(book.pending_depth)
            book.pending_depth.clear()
            pending.sort(key=lambda item: int(item["U"]))

            for event in pending:
                first_id = int(event["U"])
                final_id = int(event["u"])
                if final_id <= book.last_update_id:
                    continue
                if first_id <= book.last_update_id + 1 <= final_id:
                    self._update_side(book.bids, event.get("b", []))
                    self._update_side(book.asks, event.get("a", []))
                    book.last_update_id = final_id
                    book.in_sync = True
                    book.last_event_ts = time.time()
                    book.last_error = None
                    continue
                if first_id > book.last_update_id + 1:
                    book.in_sync = False
                    book.resyncs += 1
                    book.last_error = f"Buffered depth gap: U={first_id}, expected<={book.last_update_id + 1}"
                    return

    def _apply_trade(self, symbol: str, event: dict[str, Any]) -> None:
        with self.lock:
            book = self.books[symbol]
            book.trades.append(
                {
                    "id": event.get("t"),
                    "price": event.get("p"),
                    "qty": event.get("q"),
                    "time": event.get("T"),
                    "isBuyerMaker": event.get("m"),
                    "isBestMatch": event.get("M"),
                }
            )
            book.last_event_ts = time.time()

    @staticmethod
    def _update_side(side: dict[float, float], levels: list[list[str]]) -> None:
        for price_raw, qty_raw in levels:
            price = float(price_raw)
            qty = float(qty_raw)
            if qty == 0:
                side.pop(price, None)
            else:
                side[price] = qty
