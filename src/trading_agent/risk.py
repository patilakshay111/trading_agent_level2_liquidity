from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class RiskDecision:
    approved: bool
    reason: str


class RiskEngine:
    def __init__(self, config: dict[str, Any], store):
        self.config = config
        self.store = store

    def approve(self, candidate: dict[str, Any], symbol_cfg: dict[str, Any]) -> RiskDecision:
        symbol = candidate["symbol"]
        risk = self.config.get("risk", {})
        strategy = self.config.get("strategy", {})

        if not symbol_cfg.get("trade_enabled", False):
            return RiskDecision(False, "Symbol trading disabled in config.")
        if candidate["score"] < float(strategy.get("min_total_score", 75)):
            return RiskDecision(False, "Score below configured minimum.")
        if candidate["rr"] < float(risk.get("min_rr", 2.0)):
            return RiskDecision(False, "RR below configured minimum.")
        if candidate.get("session_sweep_allowed") is False:
            return RiskDecision(False, str(candidate.get("session_sweep_reason") or "Session sweep guard blocked new entry."))
        if candidate.get("a_plus_allowed") is False:
            return RiskDecision(False, str(candidate.get("a_plus_reason") or "Candidate failed A+ pullback/liquidity filter."))
        if candidate.get("regime_side_allowed") is False:
            return RiskDecision(False, str(candidate.get("regime_reason") or "Side blocked by market regime filter."))
        if candidate.get("structure_side_allowed") is False:
            return RiskDecision(False, str(candidate.get("structure_reason") or "Side blocked by active structure bias."))
        if self.store.count_open_trades() >= int(risk.get("max_open_positions", 2)):
            return RiskDecision(False, "Max open positions reached.")
        side = str(candidate["side"]).upper()
        if self.store.count_open_trades(symbol, side) >= int(risk.get("max_open_positions_per_symbol", 1)):
            return RiskDecision(False, f"Symbol already has an open {side} position.")
        cooldown = self._recent_stop_cooldown(candidate)
        if cooldown:
            return RiskDecision(False, cooldown)

        stats = self.store.stats()
        paper_equity = float(self.config.get("engine", {}).get("paper_initial_equity", 10000.0))
        max_daily_loss_cash = paper_equity * (float(risk.get("max_daily_loss_pct", 1.5)) / 100.0)
        if float(stats.get("net_pnl") or 0) <= -max_daily_loss_cash:
            return RiskDecision(False, "Max daily loss reached.")

        return RiskDecision(True, "Risk checks passed.")

    def _recent_stop_cooldown(self, candidate: dict[str, Any]) -> str | None:
        risk = self.config.get("risk", {})
        cooldown_seconds = float(risk.get("same_symbol_side_stop_cooldown_seconds", 0.0) or 0.0)
        if cooldown_seconds <= 0:
            return None

        symbol = str(candidate["symbol"])
        side = str(candidate["side"]).upper()
        rows = self.store.query(
            """
            select id, closed_at, exit_reason
            from trades
            where status = 'CLOSED'
              and symbol = ?
              and side = ?
              and coalesce(exit_reason, '') like '%SL%'
            order by closed_at desc, id desc
            limit 1
            """,
            (symbol, side),
        )
        if not rows or not rows[0].get("closed_at"):
            return None

        closed_at = self._parse_time(str(rows[0]["closed_at"]))
        if not closed_at:
            return None
        elapsed = (datetime.now(timezone.utc) - closed_at).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = cooldown_seconds - elapsed
            return (
                f"Same {symbol} {side} direction is cooling down after SL on trade #{rows[0]['id']}; "
                f"{remaining:.0f}s remaining."
            )
        return None

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
