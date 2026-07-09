from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json

from .config import enabled_symbols, load_config
from .historical_liquidity import HistoricalLiquidityService
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill historical M1 candles and rebuild historical liquidity levels.")
    parser.add_argument("--years", type=int, default=None, help="Lookback years to fetch. Defaults to historical_liquidity.years.")
    parser.add_argument("--days", type=int, default=None, help="Small test backfill window in days. Overrides --years when provided.")
    parser.add_argument("--symbols", nargs="*", help="Optional app symbols to backfill, e.g. BTCUSDT ETHUSDT XAUUSDT.")
    parser.add_argument("--rebuild-levels", action="store_true", help="Rebuild levels after fetching. Fetch commands rebuild by default too.")
    parser.add_argument("--levels-only", action="store_true", help="Only rebuild derived levels from stored candles.")
    parser.add_argument("--prune-old-m1", action="store_true", help="Prune raw M1 candles older than historical_liquidity.raw_m1_retention_days.")
    parser.add_argument("--force-prune", action="store_true", help="Run M1 pruning even if the monthly prune interval is not due.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    store = StateStore()
    service = HistoricalLiquidityService(store, config)
    wanted = {item.upper() for item in args.symbols or []}

    if args.prune_old_m1:
        results = []
        for symbol_cfg in enabled_symbols(config):
            symbol = str(symbol_cfg.get("name", "")).upper()
            if wanted and symbol not in wanted:
                continue
            results.append(service.prune_old_m1(symbol_cfg, force=args.force_prune))
        print(json.dumps({"ok": all(item.get("ok") for item in results), "results": results}, indent=2, default=str))
        return

    if args.levels_only:
        results = []
        for symbol_cfg in config.get("symbols", []):
            symbol = str(symbol_cfg.get("name", "")).upper()
            if not symbol_cfg.get("enabled", False):
                continue
            if wanted and symbol not in wanted:
                continue
            count = service.rebuild_levels(symbol)
            results.append({"symbol": symbol, "levels": count})
        print(json.dumps({"ok": True, "results": results}, indent=2))
        return

    if args.days is not None:
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=max(1, args.days))).timestamp() * 1000)
        results = []
        for symbol_cfg in enabled_symbols(config):
            symbol = str(symbol_cfg.get("name", "")).upper()
            if wanted and symbol not in wanted:
                continue
            results.append(service.backfill_symbol(symbol_cfg, start_ms=start_ms, end_ms=end_ms, rebuild_levels=True))
        print(json.dumps({"ok": all(item.get("ok") for item in results), "results": results}, indent=2, default=str))
        return

    result = service.backfill_all(years=args.years, symbols=args.symbols, rebuild_levels=True)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
