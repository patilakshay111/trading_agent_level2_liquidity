from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


class BinanceMarketData:
    def __init__(self, base_url: str = "https://fapi.binance.com", market: str = "um_futures", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.market = market
        self.timeout = timeout

    @property
    def prefix(self) -> str:
        return "/fapi/v1" if self.market == "um_futures" else "/api/v3"

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
        query = urlencode(params)
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        with urlopen(url, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        return self._get(f"{self.prefix}/depth", {"symbol": symbol.upper(), "limit": limit})

    def recent_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        data = self._get(f"{self.prefix}/trades", {"symbol": symbol.upper(), "limit": limit})
        return list(data)

    def klines(
        self,
        symbol: str,
        interval: str = "1m",
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1500,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        data = self._get(f"{self.prefix}/klines", params)
        return list(data)

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return self._get(f"{self.prefix}/exchangeInfo", {"symbol": symbol.upper()})

    def open_interest(self, symbol: str) -> dict[str, Any]:
        if self.market != "um_futures":
            return {"symbol": symbol.upper(), "openInterest": None, "time": None, "error": "Open interest is only available for USD-M futures."}
        return self._get("/fapi/v1/openInterest", {"symbol": symbol.upper()})
