const state = {
  view: "dashboard",
  status: null,
  stats: null,
  config: null,
  liquiditySymbol: null,
  theme: document.documentElement.dataset.theme === "dark" ? "dark" : "light",
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

function q(selector) {
  return document.querySelector(selector);
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  if (state.theme === "dark") {
    document.documentElement.dataset.theme = "dark";
  } else {
    delete document.documentElement.dataset.theme;
  }
  localStorage.setItem("theme", state.theme);

  const toggle = q("#theme-toggle");
  if (!toggle) return;
  const isDark = state.theme === "dark";
  toggle.textContent = isDark ? "Light Mode" : "Dark Mode";
  toggle.setAttribute("aria-pressed", String(isDark));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

function row(cells, attrs = "") {
  return `<tr${attrs ? ` ${attrs}` : ""}>${cells.map((value) => `<td>${value ?? ""}</td>`).join("")}</tr>`;
}

function clsPnL(value) {
  const n = Number(value || 0);
  if (n > 0) return "good";
  if (n < 0) return "bad";
  return "";
}

function fmt(value, digits = 4) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return n.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[char]);
}

function fmtDuration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 60) return `${Math.round(n)}s`;
  if (n < 3600) return `${Math.round(n / 60)}m`;
  return `${(n / 3600).toFixed(1)}h`;
}

function tradeRiskDistance(trade) {
  const entry = Number(trade.entry);
  const initialStop = Number(trade.initial_stop_loss ?? trade.stop_loss);
  const risk = Math.abs(entry - initialStop);
  return Number.isFinite(risk) && risk > 0 ? risk : 0;
}

function tradePnlAtPrice(trade, price) {
  const mark = Number(price);
  const risk = tradeRiskDistance(trade);
  const riskCash = Number(trade.risk_cash || 0);
  const entry = Number(trade.entry);
  if (!Number.isFinite(mark) || !Number.isFinite(entry) || risk <= 0 || riskCash <= 0) return null;
  const move = trade.side === "BUY" ? mark - entry : entry - mark;
  return (move / risk) * riskCash;
}

function positionSideMatches(trade, position) {
  const positionType = position?.type;
  if (trade.side === "BUY") return positionType === 0 || positionType === "0" || String(positionType).toUpperCase() === "BUY";
  return positionType === 1 || positionType === "1" || String(positionType).toUpperCase() === "SELL";
}

function isMt5Trade(trade) {
  return String(trade.execution_mode || "").startsWith("mt5");
}

function brokerPositionForTrade(trade, brokerPositions) {
  const positions = brokerPositions?.positions || [];
  const positionId = Number(trade.broker_position_id || 0);
  if (positionId) {
    const byTicket = positions.find((position) => (
      Number(position.ticket || 0) === positionId || Number(position.identifier || 0) === positionId
    ));
    if (byTicket) return byTicket;
    if (isMt5Trade(trade)) return null;
  }
  if (isMt5Trade(trade)) return null;
  const mt5Symbol = trade.mt5_symbol || trade.symbol;
  return positions.find((position) => (
    position.symbol === mt5Symbol && positionSideMatches(trade, position)
  )) || null;
}

function hybridBrokerPositionForTrade(trade, brokerPositions) {
  const positions = brokerPositions?.positions || [];
  const positionId = Number(trade.hybrid_mt5_position_id || 0);
  if (positionId) {
    const byTicket = positions.find((position) => (
      Number(position.ticket || 0) === positionId || Number(position.identifier || 0) === positionId
    ));
    if (byTicket) return byTicket;
  }
  if (String(trade.hybrid_mt5_status || "") !== "OPEN") return null;
  const mt5Symbol = trade.mt5_symbol || trade.symbol;
  return positions.find((position) => (
    position.symbol === mt5Symbol && positionSideMatches(trade, position)
  )) || null;
}

function markPriceForTrade(trade, status, brokerPositions = null) {
  if (trade.status !== "OPEN") {
    const exitPrice = Number(trade.exit_price);
    return Number.isFinite(exitPrice) && exitPrice > 0 ? exitPrice : null;
  }
  if (trade.status === "OPEN" && isMt5Trade(trade)) {
    const position = brokerPositionForTrade(trade, brokerPositions);
    const brokerMark = Number(position?.price_current);
    if (Number.isFinite(brokerMark) && brokerMark > 0) return brokerMark;
    return null;
  }
  const execution = (status.execution_prices || {})[trade.symbol];
  if (!execution?.ok) return null;
  const mark = Number(trade.side === "BUY" ? execution.bid : execution.ask);
  return Number.isFinite(mark) ? mark : null;
}

function setupTierText(trade) {
  const tier = String(trade.setup_tier || trade.a_plus_tier || "").toUpperCase();
  if (tier === "A") return "Setup A";
  if (tier === "B") return "Setup B";
  return "-";
}

function displayPnlForTrade(trade, status, brokerPositions = null) {
  if (trade.status !== "OPEN") return Number(trade.pnl || 0);
  if (isMt5Trade(trade)) {
    const position = brokerPositionForTrade(trade, brokerPositions);
    const brokerPnl = Number(position?.profit);
    if (Number.isFinite(brokerPnl)) return brokerPnl;
    return null;
  }
  const mark = markPriceForTrade(trade, status, brokerPositions);
  const livePnl = tradePnlAtPrice(trade, mark);
  return livePnl == null ? null : livePnl;
}

function mt5PnlForTrade(trade, status, brokerPositions = null) {
  const hybridStatus = String(trade.hybrid_mt5_status || "");
  if (hybridStatus) {
    if (hybridStatus === "OPEN") {
      const position = hybridBrokerPositionForTrade(trade, brokerPositions);
      const brokerPnl = Number(position?.profit);
      return Number.isFinite(brokerPnl) ? brokerPnl : null;
    }
    if (hybridStatus === "CLOSED") return Number(trade.hybrid_mt5_pnl || 0);
    return null;
  }
  if (isMt5Trade(trade)) return displayPnlForTrade(trade, status, brokerPositions);
  return null;
}

function hybridOpenPnl(trades, brokerPositions) {
  return trades.reduce((total, trade) => {
    if (String(trade.hybrid_mt5_status || "") !== "OPEN") return total;
    const position = hybridBrokerPositionForTrade(trade, brokerPositions);
    const brokerPnl = Number(position?.profit);
    return Number.isFinite(brokerPnl) ? total + brokerPnl : total;
  }, 0);
}

function statsFromTrades(baseStats, trades) {
  const closed = trades.filter((trade) => trade.status === "CLOSED");
  const wins = closed.filter((trade) => Number(trade.pnl || 0) > 0).length;
  const losses = closed.filter((trade) => Number(trade.pnl || 0) < 0).length;
  const closedPnls = closed.map((trade) => Number(trade.pnl || 0));
  let equity = 0;
  let peak = 0;
  let maxDrawdown = 0;
  [...closed].sort((a, b) => (
    String(a.closed_at || "").localeCompare(String(b.closed_at || "")) || Number(a.id || 0) - Number(b.id || 0)
  )).forEach((trade) => {
    equity += Number(trade.pnl || 0);
    peak = Math.max(peak, equity);
    maxDrawdown = Math.max(maxDrawdown, peak - equity);
  });
  return {
    ...baseStats,
    closed_trades: baseStats.closed_trades ?? closed.length,
    winning_trades: baseStats.winning_trades ?? wins,
    losing_trades: baseStats.losing_trades ?? losses,
    win_rate: baseStats.win_rate ?? (closed.length ? (wins / closed.length) * 100 : null),
    max_drawdown: baseStats.max_drawdown ?? maxDrawdown,
    max_profit_trade: baseStats.max_profit_trade ?? (closedPnls.length ? Math.max(...closedPnls) : null),
    max_loss_trade: baseStats.max_loss_trade ?? (closedPnls.length ? Math.min(...closedPnls) : null),
  };
}

function maxDrawdownForTrades(trades) {
  let equity = 0;
  let peak = 0;
  let maxDrawdown = 0;
  [...trades].sort((a, b) => (
    String(a.closed_at || "").localeCompare(String(b.closed_at || "")) || Number(a.id || 0) - Number(b.id || 0)
  )).forEach((trade) => {
    equity += Number(trade.pnl || 0);
    peak = Math.max(peak, equity);
    maxDrawdown = Math.max(maxDrawdown, peak - equity);
  });
  return maxDrawdown;
}

function groupedPnlRows(trades, status, brokerPositions, groupBy, labelKey) {
  const grouped = new Map();
  trades.forEach((trade) => {
    const label = groupBy(trade) || "-";
    if (!grouped.has(label)) {
      grouped.set(label, {
        [labelKey]: label,
        closedPnl: 0,
        openPnl: 0,
        closed: 0,
        open: 0,
        wins: 0,
        losses: 0,
        closedTrades: [],
      });
    }
    const rowData = grouped.get(label);
    if (trade.status === "CLOSED") {
      const pnl = Number(trade.pnl || 0);
      rowData.closedPnl += pnl;
      rowData.closed += 1;
      rowData.closedTrades.push(trade);
      if (pnl > 0) rowData.wins += 1;
      if (pnl < 0) rowData.losses += 1;
      return;
    }
    if (trade.status === "OPEN") {
      const livePnl = displayPnlForTrade(trade, status, brokerPositions);
      rowData.openPnl += livePnl == null ? 0 : livePnl;
      rowData.open += 1;
    }
  });

  return [...grouped.values()].map((item) => ({
    ...item,
    totalPnl: item.closedPnl + item.openPnl,
    winRate: item.closed ? (item.wins / item.closed) * 100 : null,
    maxDrawdown: maxDrawdownForTrades(item.closedTrades),
    maxProfitTrade: item.closedTrades.length ? Math.max(...item.closedTrades.map((trade) => Number(trade.pnl || 0))) : null,
    maxLossTrade: item.closedTrades.length ? Math.min(...item.closedTrades.map((trade) => Number(trade.pnl || 0))) : null,
  })).sort((a, b) => b.totalPnl - a.totalPnl || String(a[labelKey]).localeCompare(String(b[labelKey])));
}

function symbolPnlRows(trades, status, brokerPositions) {
  return groupedPnlRows(trades, status, brokerPositions, (trade) => trade.symbol, "symbol");
}

function thesisPnlRows(trades, status, brokerPositions) {
  return groupedPnlRows(trades, status, brokerPositions, (trade) => trade.strategy, "thesis");
}

function memoryTargetLine(target) {
  if (!target) return "-";
  return `${target.label} / M${fmt(target.memory_score, 1)} / ${fmtDuration(target.persistence_seconds)}`;
}

function activeSetupFor(symbol, status) {
  const thesis = status.locked_theses?.[symbol];
  if (thesis) {
    return {
      ...thesis,
      strategy: "locked_liquidity_thesis",
      entry: thesis.planned_entry,
      stop_loss: thesis.planned_sl,
      take_profit: thesis.planned_tp,
      entry_zone_low: thesis.planned_entry,
      entry_zone_high: thesis.planned_entry,
      confirmations: thesis.entry_sweep_seen ? 1 : 0,
      open_interest_signal: thesis.open_interest_signal,
      open_interest_alignment: thesis.open_interest_alignment,
    };
  }
  const setups = Object.values(status.pending_setups || {});
  return setups.find((setup) => setup.symbol === symbol) || null;
}

function titleForView(view) {
  return String(view || "")
    .split("-")
    .map((part) => part ? part[0].toUpperCase() + part.slice(1) : "")
    .join(" ");
}

function stateClass(value) {
  const text = String(value || "");
  if (text.includes("OPEN") || text.includes("EXECUTED")) return "good";
  if (text.includes("CLOSED")) return "bad";
  if (text.includes("READY") || text.includes("CONFIRMED")) return "good";
  if (text.includes("WAITING") || text.includes("WATCHING")) return "warn";
  if (text.includes("REJECTED") || text.includes("EXPIRED")) return "bad";
  return "";
}

function entrySweepText(item) {
  if (item.entry_sweep_seen === true) return "<span class=\"good\">Seen</span>";
  if ("entry_sweep_seen" in item || String(item.strategy || "").includes("locked_liquidity")) {
    return "<span class=\"warn\">Waiting</span>";
  }
  return "-";
}

function isThesisTrade(trade) {
  const strategy = String(trade.strategy || "").toLowerCase();
  return strategy.includes("thesis") || Boolean(trade.setup_tier);
}

function tradeLink(trade) {
  if (!trade?.id) return "-";
  return `<button class="link-button trade-link" data-trade-id="${trade.id}">#${trade.id}</button>`;
}

function activeThesisRows(status, trades, brokerPositions) {
  const locked = Object.values(status.locked_theses || {}).map((item) => ({
    ...item,
    source: "Waiting",
    strategy: item.strategy || "locked_liquidity_thesis",
    entry: item.planned_entry ?? item.entry,
    stop_loss: item.planned_sl ?? item.stop_loss,
    take_profit: item.planned_tp ?? item.take_profit,
    trade_id: null,
    setup_tier: item.setup_tier,
    pnl: null,
    lifecycle_rank: 0,
    sort_ts: Number(item.last_seen_ts ?? item.created_ts ?? 0),
  }));
  const pending = Object.values(status.pending_setups || {}).map((item) => ({
    ...item,
    source: "Pending",
    trade_id: null,
    pnl: null,
    lifecycle_rank: 1,
    sort_ts: Number(item.last_seen_ts ?? item.created_ts ?? 0),
  }));
  const executed = (trades || []).filter(isThesisTrade).map((trade) => {
    const isOpen = trade.status === "OPEN";
    const mark = markPriceForTrade(trade, status, brokerPositions);
    const pnl = displayPnlForTrade(trade, status, brokerPositions);
    return {
      ...trade,
      source: isOpen ? "Executed" : "Thesis Closed",
      state: isOpen ? "OPEN" : "CLOSED",
      trade_id: trade.id,
      target_label: trade.target_label,
      entry_sweep_seen: null,
      pnl,
      mark,
      lifecycle_rank: isOpen ? 2 : 3,
      sort_ts: Date.parse(isOpen ? trade.opened_at : (trade.closed_at || trade.opened_at)) / 1000 || Number(trade.id || 0),
    };
  });

  return [...locked, ...pending, ...executed].sort((a, b) => (
    a.lifecycle_rank - b.lifecycle_rank
    || b.sort_ts - a.sort_ts
    || Number(b.trade_id || 0) - Number(a.trade_id || 0)
    || String(a.symbol || "").localeCompare(String(b.symbol || ""))
  ));
}

function activeThesisReason(item) {
  const reasons = [
    item.entry_reason,
    item.stop_reason,
    item.tp_reason,
    item.a_plus_reason,
    Array.isArray(item.reasons) ? item.reasons.join(" ") : item.reason,
    item.invalidation,
    item.sizing_reason ? `Sizing: ${item.sizing_reason}` : "",
  ].filter(Boolean);
  return reasons.join(" | ") || "-";
}

function renderActiveThesis(status, trades = [], brokerPositions = null) {
  const body = q("#active-thesis-body");
  if (!body) return;
  const items = activeThesisRows(status, trades, brokerPositions);
  body.innerHTML = items.length ? items.map((item) => row([
    item.symbol,
    `<span class="${stateClass(item.state)}">${item.source}: ${item.state || "-"}</span>`,
    item.trade_id ? tradeLink(item) : "-",
    item.side || "-",
    item.strategy || "-",
    setupTierText(item),
    fmt(item.planned_entry ?? item.entry),
    fmt(item.planned_sl ?? item.stop_loss),
    fmt(item.planned_tp ?? item.take_profit),
    fmt(item.rr, 2),
    fmt(item.score, 2),
    fmt(item.planned_lots ?? item.lots ?? 0, 2),
    item.pnl == null ? "-" : `<span class="${clsPnL(item.pnl)}">${money.format(Number(item.pnl || 0))}${item.state === "OPEN" ? " live" : ""}</span>`,
    item.target_label || "-",
    entrySweepText(item),
    activeThesisReason(item),
  ])).join("") : "<tr><td colspan=\"16\" class=\"empty-state\">No thesis, executed thesis trade, or closed thesis trade yet.</td></tr>";
}

function renderBacktestSymbols(config) {
  const select = q("#backtest-symbol");
  if (!select) return;
  const current = select.value;
  const symbols = (config.symbols || []).map((symbol) => symbol.name).filter(Boolean);
  const fallback = symbols.length ? symbols : ["BTCUSDT", "ETHUSDT"];
  select.innerHTML = fallback.map((symbol) => (
    `<option value="${symbol}" ${symbol === current ? "selected" : ""}>${symbol}</option>`
  )).join("");
}

function renderBacktestResult(result) {
  const summaryPanel = q("#backtest-summary");
  const tradesBody = q("#backtest-trades-body");
  if (!summaryPanel || !tradesBody) return;
  if (!result.ok) {
    summaryPanel.innerHTML = `<div class="empty-state">${result.error || "Backtest failed."}</div>`;
    tradesBody.innerHTML = "";
    return;
  }
  const s = result.summary || {};
  summaryPanel.innerHTML = renderStatList([
    ["Symbol", `${result.symbol || "-"} / ${result.data_symbol || "-"}`],
    ["Days", result.days ?? "-"],
    ["Lot Size", fmt(s.lot_size ?? result.lot_size, 4)],
    ["Trailing", result.trailing_enabled ? "On" : "Off"],
    ["Candles", fmt(s.candles, 0)],
    ["Trades", fmt(s.trades, 0)],
    ["Wins / Losses", `${s.wins || 0} / ${s.losses || 0}`],
    ["Win Rate", `${fmt(s.win_rate, 2)}%`, Number(s.win_rate || 0) >= 50 ? "good" : "warn"],
    ["Total PnL $", money.format(Number(s.total_pnl_usd || 0)), clsPnL(s.total_pnl_usd)],
    ["Average PnL $", money.format(Number(s.average_pnl_usd || 0)), clsPnL(s.average_pnl_usd)],
    ["Total R", fmt(s.total_r, 2), clsPnL(s.total_r)],
    ["Average R", fmt(s.average_r, 3), clsPnL(s.average_r)],
    ["Profit Factor", s.profit_factor_usd == null ? s.profit_factor == null ? "-" : fmt(s.profit_factor, 3) : fmt(s.profit_factor_usd, 3)],
    ["Max Drawdown", `${fmt(s.max_drawdown_r, 2)}R`, Number(s.max_drawdown_r || 0) > 0 ? "bad" : ""],
    ["Max DD $", money.format(Number(s.max_drawdown_usd || 0)), Number(s.max_drawdown_usd || 0) > 0 ? "bad" : ""],
    ["Trail Stops", fmt(s.trail_stops, 0)],
    ["SL Updates", fmt(s.management_updates, 0)],
  ]);

  const trades = result.trades || [];
  tradesBody.innerHTML = trades.length ? trades.map((trade) => row([
    trade.entry_time,
    trade.exit_time,
    trade.side,
    fmt(trade.lot_size, 4),
    fmt(trade.entry),
    fmt(trade.stop_loss),
    fmt(trade.final_stop_loss),
    fmt(trade.take_profit),
    fmt(trade.exit_price),
    `<span class="${trade.outcome === "WIN" ? "good" : trade.outcome === "LOSS" ? "bad" : "warn"}">${trade.outcome}</span>`,
    trade.exit_reason || "-",
    `<span class="${clsPnL(trade.r)}">${fmt(trade.r, 2)}</span>`,
    `<span class="${clsPnL(trade.pnl_usd)}">${money.format(Number(trade.pnl_usd || 0))}</span>`,
    `${trade.holding_minutes}m`,
    trade.reason,
  ])).join("") : "<tr><td colspan=\"15\" class=\"empty-state\">No support/resistance box rejection trades found.</td></tr>";
}

async function runBacktest() {
  const button = q("#backtest-run");
  if (button) {
    button.disabled = true;
    button.textContent = "Running...";
  }
  try {
    const payload = {
      symbol: q("#backtest-symbol").value,
      side: q("#backtest-side").value,
      days: Number(q("#backtest-days").value),
      rr: Number(q("#backtest-rr").value),
      lookback_candles: Number(q("#backtest-lookback").value),
      equal_tolerance_bps: Number(q("#backtest-equal-bps").value),
      min_zone_touches: Number(q("#backtest-zone-touches").value),
      pivot_span: Number(q("#backtest-pivot-span").value),
      min_wick_pct: Number(q("#backtest-wick").value),
      min_close_reclaim_pct: Number(q("#backtest-reclaim").value),
      stop_buffer_bps: Number(q("#backtest-stop-buffer").value),
      max_hold_candles: Number(q("#backtest-max-hold").value),
      trailing_enabled: q("#backtest-trailing").checked,
    };
    const result = await api("/api/backtest/price-action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderBacktestResult(result);
  } catch (err) {
    const message = String(err.message || "");
    renderBacktestResult({
      ok: false,
      error: message.includes("404")
        ? "Backtest API is not loaded in the running server yet. Stop and restart the Trading Agent app, then run the backtest again."
        : message,
    });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Run Backtest";
    }
  }
}

function bandClass(level, setup, mid) {
  if (!setup) return "";
  const hasRange = typeof level === "object";
  const p = hasRange ? (Number(level.price_low) + Number(level.price_high)) / 2 : Number(level);
  const low = hasRange ? Number(level.price_low) : p;
  const high = hasRange ? Number(level.price_high) : p;
  const entryLow = Number(setup.entry_zone_low);
  const entryHigh = Number(setup.entry_zone_high);
  const sl = Number(setup.stop_loss);
  const tp = Number(setup.take_profit);
  const tolerance = Math.max(Math.abs(mid) * 0.00008, 0.0001);
  const contains = (value) => Number.isFinite(value) && value >= low - tolerance && value <= high + tolerance;
  if (contains(sl)) return "sl-zone";
  if (contains(tp)) return "tp-zone";
  if (Number.isFinite(entryLow) && Number.isFinite(entryHigh) && high >= entryLow - tolerance && low <= entryHigh + tolerance) return "entry-zone";
  return "";
}

function renderStatList(items) {
  return `<div class="stat-list">${items.map(([label, value, klass = ""]) => (
    `<div class="stat-line"><span>${label}</span><strong class="${klass}">${value}</strong></div>`
  )).join("")}</div>`;
}

function zoneCenter(zone) {
  const low = Number(zone?.price_low);
  const high = Number(zone?.price_high);
  if (Number.isFinite(low) && Number.isFinite(high)) return (low + high) / 2;
  return Number.isFinite(low) ? low : high;
}

function zoneDistanceBps(zone, mid) {
  const explicit = Number(zone?.distance_bps);
  if (Number.isFinite(explicit)) return explicit;
  const center = zoneCenter(zone);
  return Number.isFinite(center) && Number.isFinite(mid) && mid > 0
    ? Math.abs(center - mid) / mid * 10000
    : null;
}

function zoneRangeLabel(zone) {
  const label = zone?.label;
  if (label) return escapeHtml(label);
  const low = Number(zone?.price_low);
  const high = Number(zone?.price_high);
  if (!Number.isFinite(low) || !Number.isFinite(high)) return "-";
  return `${fmt(low)} - ${fmt(high)}`;
}

function sourceLabel(source) {
  return String(source || "-")
    .replace(/^historical_/, "hist ")
    .replace(/_/g, " ");
}

function zoneSourceText(zone) {
  const sources = Array.isArray(zone?.sources) ? zone.sources : [];
  if (!sources.length) return "-";
  const visible = sources.slice(0, 4).map((source) => escapeHtml(sourceLabel(source)));
  if (sources.length > visible.length) visible.push(`+${sources.length - visible.length}`);
  return visible.join(", ");
}

function importantLevelRows(status, perSide = 4) {
  const features = status.last_features || {};
  const rows = [];
  Object.keys(features).sort().forEach((symbol) => {
    const f = features[symbol] || {};
    const mid = Number(f.mid);
    const zones = Array.isArray(f.major_liquidity_zones) ? f.major_liquidity_zones : [];
    ["support", "resistance"].forEach((side) => {
      zones
        .filter((zone) => String(zone.side) === side)
        .map((zone) => {
          const distance = zoneDistanceBps(zone, mid);
          const score = Number(zone.score || 0);
          const importance = score - Math.min(Number(distance || 0) / 8, 30);
          return { symbol, mid, zone, side, score, distance, importance };
        })
        .sort((a, b) => b.importance - a.importance || a.distance - b.distance)
        .slice(0, perSide)
        .forEach((item) => rows.push(item));
    });
  });
  return rows;
}

function renderImportantLevels(status) {
  const body = q("#important-levels-body");
  if (!body) return;
  const summary = q("#important-levels-summary");
  const features = status.last_features || {};
  const symbols = Object.keys(features).sort();
  if (summary) {
    summary.innerHTML = symbols.length ? symbols.map((symbol) => {
      const f = features[symbol] || {};
      const mid = Number(f.mid);
      const zones = Array.isArray(f.major_liquidity_zones) ? f.major_liquidity_zones : [];
      const nearest = (side) => zones
        .filter((zone) => String(zone.side) === side)
        .map((zone) => ({ zone, distance: zoneDistanceBps(zone, mid), score: Number(zone.score || 0) }))
        .sort((a, b) => Number(a.distance ?? 999999) - Number(b.distance ?? 999999) || b.score - a.score)[0];
      const support = nearest("support");
      const resistance = nearest("resistance");
      const magnet = f.liquidity_magnet || {};
      const magnetClass = magnet.direction === "up" ? "good" : magnet.direction === "down" ? "bad" : "warn";
      return `
        <div class="level-snapshot">
          <div><strong>${escapeHtml(symbol)}</strong><span>Mid ${fmt(mid)}</span></div>
          <div><span>Support</span><strong class="good">${support ? zoneRangeLabel(support.zone) : "-"}</strong></div>
          <div><span>Resistance</span><strong class="bad">${resistance ? zoneRangeLabel(resistance.zone) : "-"}</strong></div>
          <div><span>Magnet</span><strong class="${magnetClass}">${escapeHtml(magnet.direction || "-")}</strong></div>
        </div>
      `;
    }).join("") : "<div class=\"empty-state\">Start the system to load current important levels.</div>";
  }

  const rows = importantLevelRows(status);
  body.innerHTML = rows.length ? rows.map((item) => {
    const sideClass = item.side === "support" ? "good" : "bad";
    const scoreClass = item.score >= 75 ? "good" : item.score >= 65 ? "warn" : "";
    const memory = Number(item.zone.memory_score ?? item.zone.strength ?? 0);
    return row([
      escapeHtml(item.symbol),
      fmt(item.mid),
      `<span class="level-pill ${sideClass}">${item.side}</span>`,
      zoneRangeLabel(item.zone),
      `<span class="${scoreClass}">${fmt(item.score, 2)}</span>`,
      item.distance == null ? "-" : `${fmt(item.distance, 2)} bps`,
      memory ? fmt(memory, 2) : "-",
      zoneSourceText(item.zone),
    ]);
  }).join("") : "<tr><td colspan=\"8\" class=\"empty-state\">No important levels available yet.</td></tr>";
}

function renderLiquidityMap(status) {
  const features = status.last_features || {};
  const symbols = Object.keys(features).sort();
  const select = q("#liquidity-symbol");
  if (!select) return;

  const previous = state.liquiditySymbol;
  if (!previous || !features[previous]) {
    state.liquiditySymbol = symbols[0] || null;
  }
  select.innerHTML = symbols.map((symbol) => (
    `<option value="${symbol}" ${symbol === state.liquiditySymbol ? "selected" : ""}>${symbol}</option>`
  )).join("");

  const symbol = state.liquiditySymbol;
  const f = symbol ? features[symbol] : null;
  const setup = symbol ? activeSetupFor(symbol, status) : null;
  if (!f) {
    q("#heatmap-panel").innerHTML = "<div class=\"empty-state\">Start the system to load live liquidity data.</div>";
    q("#magnet-panel").innerHTML = "";
    q("#delta-panel").innerHTML = "";
    q("#memory-panel").innerHTML = "";
    q("#profile-panel").innerHTML = "";
    q("#setup-panel").innerHTML = "";
    return;
  }

  const mid = Number(f.mid);
  const bucketRows = f.book_price_buckets || [];
  const maxBucketNotional = Math.max(1, ...bucketRows.flatMap((bucket) => [Number(bucket.bid_notional || 0), Number(bucket.ask_notional || 0)]));
  const maxMemoryNotional = Math.max(1, ...bucketRows.map((bucket) => Number(bucket.max_notional_seen || 0)));

  q("#heatmap-panel").innerHTML = bucketRows.length ? bucketRows.map((bucket) => {
    const center = (Number(bucket.price_low) + Number(bucket.price_high)) / 2;
    const classes = ["heat-row"];
    if (Number(bucket.price_low) <= mid && mid < Number(bucket.price_high)) classes.push("midline");
    const marker = bandClass(bucket, setup, mid);
    if (marker) classes.push(marker);
    if (Number(bucket.pull_rate || 0) > 0) classes.push("pull-warning");
    if (Number(bucket.traded_through_count || 0) > 0) classes.push("traded-through");
    const bidWidth = Math.min(100, (Number(bucket.bid_notional || 0) / maxBucketNotional) * 100);
    const askWidth = Math.min(100, (Number(bucket.ask_notional || 0) / maxBucketNotional) * 100);
    const memoryWidth = Math.min(100, (Number(bucket.max_notional_seen || 0) / maxMemoryNotional) * 100);
    const memoryBar = `<div class="bar memory" style="width:${memoryWidth}%"></div>`;
    const bidBar = `${memoryBar}<div class="bar bid" style="width:${bidWidth}%"></div>`;
    const askBar = `${memoryBar}<div class="bar ask" style="width:${askWidth}%"></div>`;
    const priceLabel = `${bucket.label}${Number(bucket.price_low) <= mid && mid < Number(bucket.price_high) ? " MID" : ""}`;
    const tags = [];
    if (bucket.memory_marker === "stacking") tags.push("STK");
    if (bucket.memory_marker === "pulling") tags.push("PULL");
    if (Number(bucket.traded_through_count || 0) > 0) tags.push("THRU");
    const memoryLabel = Number(bucket.memory_score || 0) > 0 ? `M${fmt(bucket.memory_score, 0)}` : "";
    const distanceLabel = `${fmt(bucket.distance_bps, 2)} bps ${memoryLabel}${tags.length ? ` ${tags.join("/")}` : ""}`;
    return `
      <div class="${classes.join(" ")}">
        <div class="bar-wrap">${bidBar}</div>
        <div class="heat-price">${priceLabel}</div>
        <div class="heat-distance">${distanceLabel}</div>
        <div class="bar-wrap">${askBar}</div>
      </div>
    `;
  }).join("") : "<div class=\"empty-state\">No price bucket liquidity yet.</div>";

  const magnet = f.liquidity_magnet || {};
  const magnetClass = magnet.direction === "up" ? "good" : magnet.direction === "down" ? "bad" : "warn";
  q("#magnet-panel").innerHTML = renderStatList([
    ["Attraction", magnet.direction || "- ", magnetClass],
    ["Target", fmt(magnet.target)],
    ["Confidence", `${fmt(magnet.confidence, 2)}%`],
    ["Strong Bid", fmt(f.strongest_bid)],
    ["Strong Ask", fmt(f.strongest_ask)],
    ["Reason", magnet.reason || "-"],
  ]);

  const deltaClass = Number(f.cumulative_delta) > 0 ? "good" : Number(f.cumulative_delta) < 0 ? "bad" : "";
  q("#delta-panel").innerHTML = renderStatList([
    ["Session VWAP", fmt(f.session_vwap ?? f.vwap)],
    ["VWAP Anchor", f.session_vwap_anchor || "-"],
    ["VWAP Source", f.session_vwap_source || "-"],
    ["VWAP Reconciled", f.session_vwap_reconciled_at || "-"],
    ["VWAP Distance", `${fmt(f.session_vwap_distance_bps ?? f.vwap_distance_bps, 2)} bps`],
    ["VWAP Std Dev", fmt(f.session_vwap_std)],
    ["VWAP +1 SD", fmt(f.session_vwap_upper_1)],
    ["VWAP -1 SD", fmt(f.session_vwap_lower_1)],
    ["VWAP +2 SD", fmt(f.session_vwap_upper_2)],
    ["VWAP -2 SD", fmt(f.session_vwap_lower_2)],
    ["Session Volume", fmt(f.session_volume, 6)],
    ["Session CVD", fmt(f.session_cumulative_delta, 6), Number(f.session_cumulative_delta) > 0 ? "good" : Number(f.session_cumulative_delta) < 0 ? "bad" : ""],
    ["Session Delta Imb.", fmt(f.session_delta_imbalance, 4)],
    ["Delta", fmt(f.delta, 6), deltaClass],
    ["Cumulative Delta", fmt(f.cumulative_delta, 6), deltaClass],
    ["Delta Imbalance", fmt(f.delta_imbalance, 4)],
    ["Divergence", f.delta_divergence || "none"],
    ["Open Interest", fmt(f.open_interest, 4)],
    ["OI Change", `${fmt(f.open_interest_change_pct, 4)}%`],
    ["OI Signal", f.open_interest_signal || "unknown"],
    ["OI Support", f.open_interest_support || "neutral"],
  ]);

  q("#far-liquidity-panel").innerHTML = bucketRows.length
    ? bucketRows.map((bucket) => {
        const bidWidth = Math.min(100, (Number(bucket.bid_notional || 0) / maxBucketNotional) * 100);
        const askWidth = Math.min(100, (Number(bucket.ask_notional || 0) / maxBucketNotional) * 100);
        const biasClass = Number(bucket.bias) > 0 ? "bad" : Number(bucket.bias) < 0 ? "good" : "";
        return `
          <div class="band-row">
            <span class="band-label">${bucket.label}</span>
            <div class="bar-wrap"><div class="bar bid" style="width:${bidWidth}%"></div></div>
            <div class="bar-wrap"><div class="bar ask" style="width:${askWidth}%"></div></div>
            <strong class="${biasClass}">${fmt(bucket.bias, 2)}</strong>
          </div>
        `;
      }).join("")
    : "<div class=\"empty-state\">No book liquidity buckets yet.</div>";

  const memoryTargets = f.memory_liquidity_targets || {};
  const recentPull = memoryTargets.recently_pulled;
  q("#memory-panel").innerHTML = renderStatList([
    ["Strong Above", memoryTargetLine(memoryTargets.strongest_above)],
    ["Strong Below", memoryTargetLine(memoryTargets.strongest_below)],
    ["Recently Pulled", recentPull ? `${recentPull.label} / ${fmt(recentPull.pull_rate, 2)} per sec` : "- ", recentPull ? "warn" : ""],
    ["Best Target", memoryTargetLine(memoryTargets.best_target), memoryTargets.best_target ? "good" : ""],
    ["Rows Remembered", f.liquidity_memory_summary?.rows ?? 0],
    ["Retention", fmtDuration(f.liquidity_memory_summary?.retention_seconds)],
  ]);

  const profile = f.session_volume_profile || [];
  q("#profile-panel").innerHTML = profile.length
    ? profile.map((bucket) => `
        <div class="profile-row">
          <span>${bucket.label || `${fmt(bucket.price_low)} - ${fmt(bucket.price_high)}`}</span>
          <div class="bar-wrap"><div class="profile-bar" style="width:${Math.min(100, bucket.strength)}%"></div></div>
          <strong class="${Number(bucket.delta) > 0 ? "good" : Number(bucket.delta) < 0 ? "bad" : ""}">${fmt(bucket.volume, 6)}</strong>
        </div>
      `).join("")
    : "<div class=\"empty-state\">No session volume profile yet.</div>";

  q("#setup-panel").innerHTML = setup ? renderStatList([
    ["State", `${setup.state || "WATCHING"} (${setup.confirmations || 0})`],
    ["Side", setup.side],
    ["Strategy", setup.strategy || "-"],
    ["Swept Level", fmt(setup.swept_level)],
    ["Sweep Extreme", fmt(setup.sweep_extreme)],
    ["Entry Sweep Seen", setup.entry_sweep_seen ? "Yes" : "No", setup.entry_sweep_seen ? "good" : "warn"],
    ["Entry Sweep Extreme", fmt(setup.entry_sweep_extreme)],
    ["Planned Entry", fmt(setup.planned_entry ?? setup.entry)],
    ["Entry Zone", `${fmt(setup.entry_zone_low)} - ${fmt(setup.entry_zone_high)}`],
    ["SL", fmt(setup.stop_loss)],
    ["TP", fmt(setup.take_profit)],
    ["Target Bucket", setup.target_label || "-"],
    ["Target Distance", `${fmt(setup.target_distance_bps, 2)} bps`],
    ["RR", fmt(setup.rr, 2)],
    ["Score", fmt(setup.score, 2)],
    ["Entry Reason", setup.entry_reason || "-"],
    ["SL Reason", setup.stop_reason || "-"],
    ["TP Reason", setup.tp_reason || "-"],
    ["OI", `${setup.open_interest_signal || "unknown"} / ${setup.open_interest_alignment || "neutral"}`],
    ["Invalidation", setup.invalidation || "-"],
  ]) : "<div class=\"empty-state\">No active setup waiting for this symbol.</div>";
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
  document.querySelectorAll(".view").forEach((panel) => panel.classList.toggle("active", panel.id === view));
  q("#page-title").textContent = titleForView(view);
}

async function refresh() {
  const [status, stats, config, trades, candidates, contracts, events, brokerPositions] = await Promise.all([
    api("/api/status"),
    api("/api/stats"),
    api("/api/config"),
    api("/api/trades"),
    api("/api/candidates"),
    api("/api/contracts"),
    api("/api/events"),
    api("/api/broker-positions"),
  ]);

  const effectiveStats = statsFromTrades(stats, trades);
  state.status = status;
  state.stats = effectiveStats;
  state.config = config;
  renderBacktestSymbols(config);

  q("#status-line").textContent = status.running
    ? `Running since ${status.started_at || "-"}`
    : `Stopped${status.last_error ? ` - ${status.last_error}` : ""}`;
  q("#m-running").textContent = status.running ? "Yes" : "No";
  q("#m-running").className = status.running ? "good" : "warn";
  q("#m-pnl").textContent = money.format(Number(effectiveStats.net_pnl || 0));
  q("#m-pnl").className = clsPnL(effectiveStats.net_pnl);
  q("#m-pf").textContent = effectiveStats.profit_factor ?? "-";
  q("#m-win-rate").textContent = effectiveStats.win_rate == null ? "-" : `${fmt(effectiveStats.win_rate, 2)}%`;
  q("#m-dd").textContent = money.format(Number(effectiveStats.max_drawdown || 0));
  q("#m-dd").className = Number(effectiveStats.max_drawdown || 0) > 0 ? "bad" : "";
  q("#m-best-trade").textContent = effectiveStats.max_profit_trade == null ? "-" : money.format(Number(effectiveStats.max_profit_trade || 0));
  q("#m-best-trade").className = clsPnL(effectiveStats.max_profit_trade);
  q("#m-worst-trade").textContent = effectiveStats.max_loss_trade == null ? "-" : money.format(Number(effectiveStats.max_loss_trade || 0));
  q("#m-worst-trade").className = clsPnL(effectiveStats.max_loss_trade);
  q("#m-open").textContent = effectiveStats.open_trades ?? 0;
  q("#m-total").textContent = effectiveStats.total_trades ?? 0;
  q("#m-mode").textContent = status.execution_mode || "-";
  const watchdog = status.watchdog || {};
  q("#m-watchdog").textContent = !watchdog.enabled ? "Off" : watchdog.live_entries_blocked ? "Blocked" : (!watchdog.market_ok || !watchdog.mt5_ok) ? "Watch" : "OK";
  q("#m-watchdog").className = !watchdog.enabled ? "warn" : watchdog.live_entries_blocked ? "bad" : (!watchdog.market_ok || !watchdog.mt5_ok) ? "warn" : "good";
  const feed = status.market_data || {};
  q("#m-feed").textContent = feed.connected ? "Live" : "Offline";
  q("#m-feed").className = feed.connected ? "good" : "warn";
  const awake = status.keep_awake || {};
  q("#m-awake").textContent = awake.active ? "On" : "Off";
  q("#m-awake").className = awake.active ? "good" : "warn";

  const hybrid = stats.hybrid_mt5 || {};
  const hybridFloatingPnl = hybridOpenPnl(trades, brokerPositions);
  q("#h-pnl").textContent = money.format(Number(hybrid.net_pnl || 0));
  q("#h-pnl").className = clsPnL(hybrid.net_pnl);
  q("#h-open-pnl").textContent = money.format(hybridFloatingPnl);
  q("#h-open-pnl").className = clsPnL(hybridFloatingPnl);
  q("#h-open").textContent = hybrid.open_trades ?? 0;
  q("#h-total").textContent = hybrid.total_trades ?? 0;
  q("#h-win-rate").textContent = hybrid.win_rate == null ? "-" : `${fmt(hybrid.win_rate, 2)}%`;
  q("#h-pf").textContent = hybrid.profit_factor ?? "-";
  q("#h-dd").textContent = money.format(Number(hybrid.max_drawdown || 0));
  q("#h-dd").className = Number(hybrid.max_drawdown || 0) > 0 ? "bad" : "";
  q("#h-best-trade").textContent = hybrid.max_profit_trade == null ? "-" : money.format(Number(hybrid.max_profit_trade || 0));
  q("#h-best-trade").className = clsPnL(hybrid.max_profit_trade);
  q("#h-worst-trade").textContent = hybrid.max_loss_trade == null ? "-" : money.format(Number(hybrid.max_loss_trade || 0));
  q("#h-worst-trade").className = clsPnL(hybrid.max_loss_trade);

  const features = status.last_features || {};
  const quality = status.last_quality || {};
  q("#features-body").innerHTML = Object.keys(features).sort().map((symbol) => {
    const f = features[symbol];
    const health = quality[symbol] || { ok: true, reason: "OK" };
    return row([
      symbol,
      `<span class="${health.ok ? "good" : "bad"}">${health.reason}</span>`,
      number.format(f.mid),
      number.format(f.spread_bps),
      number.format(f.book_imbalance),
      number.format(f.flow_imbalance),
      f.open_interest_signal || "-",
      number.format(f.bid_depth),
      number.format(f.ask_depth),
    ]);
  }).join("");

  const perSymbol = symbolPnlRows(trades, status, brokerPositions);
  q("#symbol-pnl-body").innerHTML = perSymbol.length ? perSymbol.map((s) => row([
    s.symbol,
    `<span class="${clsPnL(s.totalPnl)}">${money.format(s.totalPnl)}</span>`,
    `<span class="${clsPnL(s.closedPnl)}">${money.format(s.closedPnl)}</span>`,
    `<span class="${clsPnL(s.openPnl)}">${money.format(s.openPnl)}</span>`,
    s.closed,
    s.open,
    s.winRate == null ? "-" : `${fmt(s.winRate, 2)}%`,
    `<span class="${s.maxDrawdown > 0 ? "bad" : ""}">${money.format(s.maxDrawdown)}</span>`,
    s.maxProfitTrade == null ? "-" : `<span class="${clsPnL(s.maxProfitTrade)}">${money.format(s.maxProfitTrade)}</span>`,
    s.maxLossTrade == null ? "-" : `<span class="${clsPnL(s.maxLossTrade)}">${money.format(s.maxLossTrade)}</span>`,
  ])).join("") : "<tr><td colspan=\"10\" class=\"empty-state\">No trade PnL yet.</td></tr>";

  const perThesis = thesisPnlRows(trades, status, brokerPositions);
  q("#thesis-pnl-body").innerHTML = perThesis.length ? perThesis.map((s) => row([
    s.thesis,
    `<span class="${clsPnL(s.totalPnl)}">${money.format(s.totalPnl)}</span>`,
    `<span class="${clsPnL(s.closedPnl)}">${money.format(s.closedPnl)}</span>`,
    `<span class="${clsPnL(s.openPnl)}">${money.format(s.openPnl)}</span>`,
    s.closed,
    s.open,
    s.winRate == null ? "-" : `${fmt(s.winRate, 2)}%`,
    `<span class="${s.maxDrawdown > 0 ? "bad" : ""}">${money.format(s.maxDrawdown)}</span>`,
    s.maxProfitTrade == null ? "-" : `<span class="${clsPnL(s.maxProfitTrade)}">${money.format(s.maxProfitTrade)}</span>`,
    s.maxLossTrade == null ? "-" : `<span class="${clsPnL(s.maxLossTrade)}">${money.format(s.maxLossTrade)}</span>`,
  ])).join("") : "<tr><td colspan=\"10\" class=\"empty-state\">No thesis PnL yet.</td></tr>";

  renderLiquidityMap(status);
  renderImportantLevels(status);
  renderActiveThesis(status, trades, brokerPositions);

  q("#symbols-body").innerHTML = (config.symbols || []).map((s) => row([
    s.name,
    s.data_symbol,
    s.mt5_symbol,
    s.enabled ? "Yes" : "No",
    s.trade_enabled ? "Yes" : "No",
    s.notes || "",
  ])).join("");

  q("#trades-body").innerHTML = trades.map((t) => {
    const mark = markPriceForTrade(t, status, brokerPositions);
    const displayPnl = displayPnlForTrade(t, status, brokerPositions);
    const mt5Pnl = mt5PnlForTrade(t, status, brokerPositions);
    const pnlLabel = displayPnl == null ? "-" : money.format(Number(displayPnl || 0));
    const mt5PnlLabel = mt5Pnl == null ? "-" : money.format(Number(mt5Pnl || 0));
    return row([
    t.id,
    t.opened_at,
    t.symbol,
    t.side,
    t.strategy,
    setupTierText(t),
    number.format(t.entry),
    number.format(t.lots),
    number.format(t.broker_requested_price),
    number.format(t.broker_fill_price),
    number.format(t.stop_loss),
    number.format(t.take_profit),
    t.status,
    mark == null ? "-" : number.format(mark),
    `<span class="${clsPnL(displayPnl)}">${pnlLabel}${t.status === "OPEN" ? " live" : ""}</span>`,
    `<span class="${clsPnL(mt5Pnl)}">${mt5PnlLabel}${String(t.hybrid_mt5_status || "") === "OPEN" ? " live" : ""}</span>`,
    number.format(t.score),
    t.exit_reason || "",
    ], `id="trade-row-${t.id}" data-trade-id="${t.id}"`);
  }).join("");

  q("#positions-body").innerHTML = (brokerPositions.positions || []).map((p) => row([
    p.ticket,
    p.symbol,
    p.type,
    number.format(p.volume),
    number.format(p.price_open),
    number.format(p.sl),
    number.format(p.tp),
    `<span class="${clsPnL(p.profit)}">${money.format(Number(p.profit || 0))}</span>`,
  ])).join("");

  q("#candidates-body").innerHTML = candidates.map((c) => row([
    c.created_at,
    c.symbol,
    c.side,
    c.strategy,
    number.format(c.entry),
    number.format(c.stop_loss),
    number.format(c.take_profit),
    number.format(c.rr),
    number.format(c.planned_lots || 0),
    money.format(Number(c.risk_cash || 0)),
    number.format(c.score),
    c.decision,
    `${c.reason || ""}${c.sizing_reason ? ` Sizing: ${c.sizing_reason}` : ""}`,
  ])).join("");

  q("#contracts-body").innerHTML = contracts.map((c) => row([
    c.symbol,
    c.scanned_at,
    number.format(c.contract_size),
    number.format(c.tick_size),
    number.format(c.tick_value),
    number.format(c.volume_min),
    number.format(c.volume_step),
    number.format(c.volume_max),
  ])).join("");

  q("#events-body").innerHTML = events.map((e) => row([
    e.created_at,
    e.level,
    e.source,
    e.message,
  ])).join("");

  q("#config-json").textContent = JSON.stringify(config, null, 2);
}

document.querySelectorAll(".nav").forEach((btn) => {
  btn.addEventListener("click", () => setView(btn.dataset.view));
});

applyTheme(state.theme);
q("#theme-toggle").addEventListener("click", () => {
  applyTheme(state.theme === "dark" ? "light" : "dark");
});

document.addEventListener("click", (event) => {
  const button = event.target?.closest?.(".trade-link");
  if (!button) return;
  const tradeId = button.dataset.tradeId;
  setView("trades");
  setTimeout(() => {
    const target = q(`#trade-row-${tradeId}`);
    if (!target) return;
    target.classList.add("highlight-row");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => target.classList.remove("highlight-row"), 2400);
  }, 0);
});

q("#liquidity-symbol").addEventListener("change", (event) => {
  state.liquiditySymbol = event.target.value;
  if (state.status) renderLiquidityMap(state.status);
});

q("#start-btn").addEventListener("click", async () => {
  await api("/api/start", { method: "POST" });
  await refresh();
});

q("#stop-btn").addEventListener("click", async () => {
  await api("/api/stop", { method: "POST" });
  await refresh();
});

q("#reload-btn").addEventListener("click", async () => {
  await api("/api/reload", { method: "POST" });
  await refresh();
});

q("#scan-btn").addEventListener("click", async () => {
  await api("/api/scan-contracts", { method: "POST" });
  await refresh();
});

q("#backtest-run").addEventListener("click", () => {
  runBacktest();
});

refresh().catch((err) => {
  q("#status-line").textContent = err.message;
});
setInterval(() => refresh().catch(() => {}), 3000);
