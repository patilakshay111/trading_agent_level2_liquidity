# First Run: Historical Liquidity Bootstrap

This app keeps live order-book memory separate from historical candle structure. On a fresh machine, run the historical bootstrap once, then the app keeps itself updated with only small closed-candle updates while it is running.

## 1. Check Config

Open `config/settings.toml` and confirm:

```toml
[server]
host = "127.0.0.1"
port = 8767

[historical_liquidity]
enabled = true
years = 2
interval = "1m"
derived_intervals = ["1m", "5m", "15m", "30m", "1h"]
live_rebuild_levels = false
update_on_startup = true
update_while_running = true
raw_m1_retention_days = 60
prune_interval_days = 30
binance_symbols = ["BTCUSDT", "ETHUSDT", "XAUUSDT"]
mt5_fallback_symbols = []
```

XAU history is configured to use Binance `XAUUSDT`. MT5 is only needed for XAU historical candles if you explicitly add `XAUUSDT` back to `mt5_fallback_symbols`.

## 2. Start The App

Use the normal launcher:

```powershell
python run.py
```

Then open:

```text
http://127.0.0.1:8767
```

At this point the app can run, but historical liquidity may show `needs_backfill` until the first bootstrap is done.

## 3. Backfill Historical Candles Once

Run this in a separate terminal from the project root:

```powershell
python -m trading_agent.history_backfill --years 2
```

What this does:

- Downloads BTCUSDT and ETHUSDT M1 candles from Binance using paginated 1500-candle requests.
- Downloads XAUUSDT M1 candles from Binance unless the symbol is explicitly configured in `mt5_fallback_symbols`.
- Stores raw candles in SQLite.
- Derives structural liquidity levels from the stored candles.

The command is safe to rerun. It resumes from the latest stored candle and upserts duplicate candles instead of duplicating them.

## 4. Rebuild Levels Manually When Needed

If scoring or level-detection rules change, rebuild derived levels from stored candles without downloading everything again:

```powershell
python -m trading_agent.history_backfill --levels-only
```

This rebuilds:

- session highs/lows for Asia, London, and New York
- daily, weekly, and monthly highs/lows
- swing highs/lows
- equal highs/lows
- wick rejection zones
- high-volume zones
- breakout/retest levels
- derived 5m, 15m, 30m, and 1h levels

## 5. Prune Old Raw M1 Candles

The app keeps derived levels permanently, so old raw M1 candles do not need to grow forever. To prune manually:

```powershell
python -m trading_agent.history_backfill --prune-old-m1 --force-prune
```

Normal live operation also prunes raw M1 monthly when enabled in config. The default keeps about 60 days of raw M1 candles while preserving derived liquidity levels.

## 6. Normal Long-Running Operation

After the first backfill:

- Startup fetches only missing closed M1 candles.
- The live loop inserts only newly closed M1 candles.
- New derived levels are updated incrementally from a small recent window.
- Full level rebuilds do not run during live trading unless `live_rebuild_levels = true`.
- Historical levels are merged into major liquidity zones at lower weight than live order-book memory.
- Current forming candles are ignored for historical memory.

The important UI places to check are:

- `Important Levels` for current support/resistance context by symbol.
- `Liquidity Map` for live book, memory, magnet, VWAP, and active setup context.
- `GET /api/history/backfill-status` for backfill/update status.
- `GET /api/historical-liquidity` for derived historical level status and sample levels.

## 7. Suggested Fresh Machine Sequence

```powershell
python run.py
python -m trading_agent.history_backfill --years 2
python -m trading_agent.history_backfill --levels-only
python -m trading_agent.history_backfill --prune-old-m1 --force-prune
python run.py
```

Once this sequence completes, the app should maintain itself through lightweight incremental updates during normal use.
