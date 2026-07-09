# Institutional Trading App Architecture

This app is designed around a strict separation of concerns:

1. Binance provides market data and liquidity intelligence.
2. Strategy modules produce trade candidates.
3. Risk modules approve or reject candidates.
4. MT5 is only an execution venue.
5. The UI is an operator console, not the trading logic.

## Runtime Flow

```text
Binance depth/trades
  -> data quality checks
  -> liquidity feature engine
  -> strategy scoring
  -> risk approval
  -> paper/MT5 execution
  -> position manager
  -> stats, audit log, PnL
```

## Safety Contract

- Binance WebSocket is the production data path. REST is only a diagnostic fallback when explicitly configured.
- MT5 execution is the production order path.
- Live MT5 execution is blocked unless `execution.mode = "mt5_live"` and `execution.allow_live_orders = true`.
- Lot sizing uses MT5 symbol specs, not Binance specs.
- Gold trading remains disabled by default until the data/execution symbol mapping is verified.
- All operational thresholds live in `config/settings.toml`.

## Next Production Modules

- Broker-side reconciliation of closed MT5 positions into local PnL.
- Event-driven replay backtester using recorded book/trade data.
- Model registry for trained liquidity classifiers.
- Walk-forward optimizer and drift monitor.
