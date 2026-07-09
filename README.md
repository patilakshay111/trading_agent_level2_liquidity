# Trading Agent

Local operator UI and engine for Binance WebSocket liquidity research with MT5 execution.

## Start

Double-click `Start Trading Agent.bat`, or run:

```powershell
python run.py
```

Then open:

```text
http://127.0.0.1:8767
```

## First Historical Liquidity Run

Before expecting historical levels to be complete, run the one-time backfill and level build:

```powershell
python -m trading_agent.history_backfill --years 2
python -m trading_agent.history_backfill --levels-only
python -m trading_agent.history_backfill --prune-old-m1 --force-prune
```

See `docs/FIRST_RUN_HISTORICAL_LIQUIDITY.md` for the full fresh-machine sequence, MT5/XAU notes, rebuild guidance, and how the app keeps itself updated after the first run.

## Live Operation

The default data path is Binance WebSocket depth/trade data. The execution mode is configured for MT5, but real live orders are still blocked until `allow_live_orders = true`.

Before enabling live orders:

1. Start MT5 and log in to the correct account.
2. Click `Scan Contracts` in the UI.
3. Confirm `BTCUSD`, `ETHUSD`, and any gold symbol names match your broker.
4. Confirm risk settings and symbol `trade_enabled` flags in `config/settings.toml`.
5. Only then set:

```toml
[execution]
mode = "mt5_live"
allow_live_orders = true
```

The app does not record an MT5 trade as open unless MT5 `order_send` succeeds.

## Main Files

- `config/settings.toml` - symbols, risk, strategy thresholds, execution settings
- `src/trading_agent/app.py` - local UI/API server
- `src/trading_agent/engine.py` - background trading loop
- `src/trading_agent/market/binance.py` - Binance REST adapter
- `src/trading_agent/market/websocket.py` - Binance WebSocket depth/trade feed
- `src/trading_agent/broker/mt5.py` - MT5 contract scanner and lot sizing
- `src/trading_agent/historical_liquidity.py` - historical candle backfill, level derivation, scoring, pruning
- `src/trading_agent/history_backfill.py` - CLI for backfill, level rebuild, and pruning
- `src/trading_agent/strategy/liquidity.py` - liquidity strategy scoring
- `data/trading_agent.db` - SQLite runtime state, created automatically
