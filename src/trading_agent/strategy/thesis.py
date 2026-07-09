from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any


@dataclass
class ThesisOutcome:
    candidate: dict[str, Any] | None
    decision: str
    reason: str
    executable: bool = False


class LiquidityThesisEngine:
    def __init__(self) -> None:
        self.theses: dict[str, dict[str, Any]] = {}
        self.structures: dict[str, dict[str, float]] = {}
        self.zone_observations: dict[str, dict[str, dict[str, Any]]] = {}
        self.drift_observations: dict[str, dict[str, Any]] = {}
        self.major_observations: dict[str, dict[str, Any]] = {}
        self._last_logged_state: dict[str, str] = {}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {symbol: dict(thesis) for symbol, thesis in self.theses.items()}

    def clear_symbol(self, symbol: str) -> None:
        self.theses.pop(symbol, None)
        self._last_logged_state.pop(symbol, None)

    def evaluate(
        self,
        symbol: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float | None = None,
    ) -> ThesisOutcome:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("enabled", True):
            return ThesisOutcome(None, "THESIS_DISABLED", "Locked liquidity thesis is disabled.")

        now = now or time()
        previous = self.structures.get(symbol)
        active = self.theses.get(symbol)

        if active:
            outcome = self._update_existing(symbol, active, features, config, now)
            self._remember_structure(symbol, features)
            return outcome

        if not previous:
            self._remember_structure(symbol, features)
            return ThesisOutcome(None, "SCANNING", "Building the first structure reference from live data.")

        outcome = self._try_lock_new(symbol, previous, features, config, now)
        self._remember_structure(symbol, features)
        return outcome

    def _remember_structure(self, symbol: str, features: dict[str, Any]) -> None:
        self.structures[symbol] = {
            "recent_high": float(features.get("recent_high", features.get("mid", 0.0))),
            "recent_low": float(features.get("recent_low", features.get("mid", 0.0))),
            "mid": float(features.get("mid", 0.0)),
        }

    def _try_lock_new(
        self,
        symbol: str,
        previous: dict[str, float],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome:
        mid = float(features["mid"])
        sweep_buffer = self._bps_value(config, "thesis", "sweep_buffer_bps", symbol, 4.0, mid)
        reclaim_buffer = self._bps_value(config, "thesis", "entry_reclaim_buffer_bps", symbol, 2.0, mid)
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        reference_low = float(previous["recent_low"])
        reference_high = float(previous["recent_high"])

        swept_down = recent_low <= reference_low - sweep_buffer and mid >= reference_low + reclaim_buffer
        swept_up = recent_high >= reference_high + sweep_buffer and mid <= reference_high - reclaim_buffer
        attempts: list[dict[str, Any]] = []
        if swept_down:
            attempts.append(
                self._build_thesis(
                    symbol=symbol,
                    side="BUY",
                    features=features,
                    config=config,
                    now=now,
                    swept_level=reference_low,
                    sweep_extreme=recent_low,
                )
            )
        if swept_up:
            attempts.append(
                self._build_thesis(
                    symbol=symbol,
                    side="SELL",
                    features=features,
                    config=config,
                    now=now,
                    swept_level=reference_high,
                    sweep_extreme=recent_high,
                )
            )

        attempts = [attempt for attempt in attempts if attempt]
        if not attempts:
            major_outcome = self._try_major_liquidity_thesis(symbol, features, config, now)
            if major_outcome:
                return major_outcome
            zone_outcome = self._try_zone_thesis(symbol, features, config, now)
            if zone_outcome:
                return zone_outcome
            drift_outcome = self._try_drift_thesis(symbol, previous, features, config, now)
            if drift_outcome:
                return drift_outcome
            sharp_outcome = self._try_sharp_flow_thesis(symbol, features, config, now)
            if sharp_outcome:
                return sharp_outcome
            return ThesisOutcome(None, "SCANNING", "No confirmed liquidity sweep/reclaim, defended zone, drift exhaustion, or sharp-flow impulse yet.")

        thesis = max(attempts, key=lambda item: float(item["score"]))
        min_score = float(config.get("thesis", {}).get("min_score", 72.0))
        if float(thesis["score"]) < min_score:
            candidate = self._candidate_from_thesis(thesis, "locked_liquidity_thesis")
            return ThesisOutcome(
                candidate,
                "THESIS_REJECTED",
                f"Sweep found but confluence score {thesis['score']:.2f} is below thesis minimum {min_score:.2f}.",
            )

        thesis["state"] = "WAITING_FOR_ENTRY_SWEEP"
        self.theses[symbol] = thesis
        return ThesisOutcome(
            self._candidate_from_thesis(thesis, "locked_liquidity_thesis"),
            "THESIS_LOCKED",
            (
                f"{thesis['side']} thesis locked after liquidity sweep at {thesis['swept_level']:.4f}; "
                f"target remains {thesis['target_label']} / {thesis['planned_tp']:.4f}. "
                "No order will be sent until the planned SL zone is swept and reclaimed."
            ),
        )

    def _try_major_liquidity_thesis(
        self,
        symbol: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome | None:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("major_liquidity_thesis_enabled", True):
            return None

        mid = float(features["mid"])
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        max_age = float(thesis_cfg.get("major_liquidity_max_age_seconds", 1800.0))
        displacement_bps = self._bps_value(config, "thesis", "major_liquidity_displacement_bps", symbol, 12.0, mid)
        retest_tolerance = self._bps_value(config, "thesis", "major_liquidity_retest_tolerance_bps", symbol, 8.0, mid)
        min_zone_score = float(thesis_cfg.get("major_liquidity_min_zone_score", 70.0))
        min_score = float(thesis_cfg.get("major_liquidity_min_score", 82.0))

        observation = self.major_observations.get(symbol)
        if observation and now - float(observation.get("created_ts", now)) > max_age:
            self.major_observations.pop(symbol, None)
            observation = None

        if observation:
            side = str(observation["side"])
            zone = dict(observation["zone"])
            if side == "BUY":
                observation["sweep_extreme"] = min(float(observation["sweep_extreme"]), recent_low)
                if mid >= float(zone["price_high"]) + displacement_bps:
                    observation["displacement_seen"] = True
                retested = recent_low <= float(zone["price_high"]) + retest_tolerance and mid >= float(zone["price_high"])
            else:
                observation["sweep_extreme"] = max(float(observation["sweep_extreme"]), recent_high)
                if mid <= float(zone["price_low"]) - displacement_bps:
                    observation["displacement_seen"] = True
                retested = recent_high >= float(zone["price_low"]) - retest_tolerance and mid <= float(zone["price_low"])
            observation["updated_ts"] = now
            if not observation.get("displacement_seen") or not retested:
                return None
            thesis = self._build_major_liquidity_thesis(symbol, side, zone, observation, features, config, now)
            if not thesis:
                return None
            candidate = self._candidate_from_thesis(thesis, "major_liquidity_sweep_retest_thesis")
            if float(thesis["score"]) < min_score:
                return ThesisOutcome(
                    candidate,
                    "MAJOR_LIQUIDITY_REJECTED",
                    f"Major liquidity retest found but score {thesis['score']:.2f} is below minimum {min_score:.2f}.",
                )
            self.major_observations.pop(symbol, None)
            return ThesisOutcome(
                candidate,
                "MAJOR_LIQUIDITY_TRIGGERED",
                (
                    f"{side} major liquidity sweep/retest triggered at {zone['label']}; "
                    f"target {thesis['target_label']}."
                ),
                executable=True,
            )

        sweep_buffer = self._bps_value(config, "thesis", "major_liquidity_sweep_buffer_bps", symbol, 4.0, mid)
        reclaim_buffer = self._bps_value(config, "thesis", "entry_reclaim_buffer_bps", symbol, 2.0, mid)
        zones = sorted(
            [zone for zone in features.get("major_liquidity_zones", []) if float(zone.get("score", 0.0)) >= min_zone_score],
            key=lambda zone: float(zone.get("score", 0.0)),
            reverse=True,
        )
        for zone in zones:
            side = None
            if zone.get("side") == "support":
                low = float(zone["price_low"])
                high = float(zone["price_high"])
                if recent_low <= low - sweep_buffer and mid >= high + reclaim_buffer:
                    side = "BUY"
                    extreme = recent_low
            elif zone.get("side") == "resistance":
                low = float(zone["price_low"])
                high = float(zone["price_high"])
                if recent_high >= high + sweep_buffer and mid <= low - reclaim_buffer:
                    side = "SELL"
                    extreme = recent_high
            if not side:
                continue
            self.major_observations[symbol] = {
                "symbol": symbol,
                "side": side,
                "zone": dict(zone),
                "sweep_extreme": extreme,
                "created_ts": now,
                "updated_ts": now,
                "displacement_seen": False,
            }
            return None
        return None

    def _build_major_liquidity_thesis(
        self,
        symbol: str,
        side: str,
        zone: dict[str, Any],
        observation: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        target = self._select_target(side, features, config)
        if not target:
            return {}
        mid = float(features["mid"])
        min_target_distance = float(config.get("thesis", {}).get("major_liquidity_min_target_distance_bps", {}).get(symbol, 50.0))
        if float(target.get("distance_bps", 0.0)) < min_target_distance:
            return {}
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, mid)
        min_stop = self._bps_value(config, "strategy", "min_structural_stop_bps", symbol, 10.0, mid)
        sweep_extreme = float(observation.get("sweep_extreme", mid))
        if side == "BUY":
            planned_sl = min(sweep_extreme - sl_buffer, float(zone["price_low"]) - sl_buffer, mid - min_stop)
            zone_extreme = float(zone["price_low"])
        else:
            planned_sl = max(sweep_extreme + sl_buffer, float(zone["price_high"]) + sl_buffer, mid + min_stop)
            zone_extreme = float(zone["price_high"])
        wick_confirmation = self._wick_absorption_confirmation(symbol, side, zone_extreme, features, config, mid, "major_liquidity", "major liquidity zone")
        if not wick_confirmation["ok"]:
            return {}
        flow_confirmation = self._flow_confirmation(side, features, config, wick_confirmation, float(zone.get("score", 0.0)))
        if not flow_confirmation["ok"]:
            return {}
        planned_tp = float(target["price"])
        rr = self._rr(side, mid, planned_sl, planned_tp)
        if not self._valid_trade_plan(side, mid, planned_sl, planned_tp, rr):
            return {}
        score, scores, reasons = self._score_confluence(side, features, target, config)
        zone_bonus = min(10.0, float(zone.get("score", 0.0)) * 0.10)
        score = min(100.0, score + zone_bonus + float(wick_confirmation.get("score_bonus", 0.0)) + float(flow_confirmation["score_bonus"]))
        reasons.append(
            f"major {zone.get('side')} liquidity {zone.get('label')} swept and retested; "
            f"zone score {float(zone.get('score', 0.0)):.1f}; {wick_confirmation['reason']}"
        )
        reasons.append(flow_confirmation["reason"])
        return {
            "id": f"{symbol}:MAJOR:{side}:{int(now)}",
            "symbol": symbol,
            "side": side,
            "state": "MAJOR_LIQUIDITY_CANDIDATE",
            "created_ts": now,
            "updated_ts": now,
            "planned_entry": round(mid, 4),
            "planned_sl": round(planned_sl, 4),
            "planned_tp": round(planned_tp, 4),
            "target_label": str(target["label"]),
            "target_distance_bps": round(float(target["distance_bps"]), 2),
            "target_notional": round(float(target["notional"]), 4),
            "target_memory_score": round(float(target.get("memory_score", 0.0)), 2),
            "target_pull_count": int(target.get("pull_count", 0)),
            "major_zone_label": str(zone.get("label")),
            "major_zone_score": round(float(zone.get("score", 0.0)), 2),
            "major_zone_sources": ",".join(str(item) for item in zone.get("sources", [])),
            "sweep_extreme": round(sweep_extreme, 4),
            "absorption_candle_time": wick_confirmation.get("open_time"),
            "absorption_wick_pct": round(float(wick_confirmation.get("wick_pct", 0.0)), 4),
            "absorption_close_position": round(float(wick_confirmation.get("close_position", 0.0)), 4),
            "absorption_delta": round(float(wick_confirmation.get("delta", 0.0)), 8),
            "flow_confirmation_passes": int(flow_confirmation["passes"]),
            "rr": round(rr, 2),
            "score": round(score, 2),
            "scores": scores,
            "reasons": reasons,
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "entry_reason": (
                f"{side} after major liquidity at {zone.get('label')} was swept, price displaced, "
                "then retested/reclaimed with M1 absorption."
            ),
            "stop_reason": f"SL is beyond the major liquidity sweep extreme {sweep_extreme:.4f}, outside the obvious stop pool.",
            "tp_reason": f"TP targets opposing major/resting liquidity at {target['label']}.",
            "invalidation": "Invalid if price accepts back through the retested major liquidity zone.",
        }

    def _try_zone_thesis(
        self,
        symbol: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome | None:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("zone_thesis_enabled", True):
            return None

        mid = float(features["mid"])
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        buckets = features.get("book_price_buckets") or []
        required_touches = int(thesis_cfg.get("zone_required_touches", 3))
        max_age = float(thesis_cfg.get("zone_max_age_seconds", 1200.0))
        min_score = float(thesis_cfg.get("zone_min_score", thesis_cfg.get("min_score", 72.0)))
        candidates: list[dict[str, Any]] = []

        support_bucket = self._bucket_for_price(buckets, recent_low)
        if support_bucket and mid >= float(support_bucket["price_high"]):
            observation = self._observe_zone(symbol, "BUY", support_bucket, now, max_age)
            if int(observation["touches"]) >= required_touches:
                candidates.append(self._build_zone_thesis(symbol, "BUY", support_bucket, observation, features, config, now))

        resistance_bucket = self._bucket_for_price(buckets, recent_high)
        if resistance_bucket and mid <= float(resistance_bucket["price_low"]):
            observation = self._observe_zone(symbol, "SELL", resistance_bucket, now, max_age)
            if int(observation["touches"]) >= required_touches:
                candidates.append(self._build_zone_thesis(symbol, "SELL", resistance_bucket, observation, features, config, now))

        candidates = [candidate for candidate in candidates if candidate]
        if not candidates:
            return None

        thesis = max(candidates, key=lambda item: float(item["score"]))
        candidate = self._candidate_from_thesis(thesis, "repeated_liquidity_zone_thesis")
        if float(thesis["score"]) < min_score:
            return ThesisOutcome(
                candidate,
                "ZONE_THESIS_REJECTED",
                f"Repeated zone found but confluence score {thesis['score']:.2f} is below zone minimum {min_score:.2f}.",
            )

        thesis["state"] = "READY_FOR_RISK"
        return ThesisOutcome(
            candidate,
            "ZONE_THESIS_TRIGGERED",
            (
                f"{thesis['side']} defended-zone thesis triggered after {thesis['zone_touches']} touches at "
                f"{thesis['zone_label']}; target {thesis['target_label']}."
            ),
            executable=True,
        )

    def _try_drift_thesis(
        self,
        symbol: str,
        previous: dict[str, float],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome | None:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("drift_thesis_enabled", True):
            return None

        mid = float(features["mid"])
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        required_pushes = int(thesis_cfg.get("drift_required_pushes", 3))
        max_age = float(thesis_cfg.get("drift_max_age_seconds", 900.0))
        min_score = float(thesis_cfg.get("drift_min_score", thesis_cfg.get("min_score", 72.0)))
        obs = self.drift_observations.setdefault(symbol, {"started_ts": now, "down_pushes": 0, "up_pushes": 0})
        if now - float(obs.get("started_ts", now)) > max_age:
            obs.clear()
            obs.update({"started_ts": now, "down_pushes": 0, "up_pushes": 0})

        reclaim_buffer = self._bps_value(config, "thesis", "entry_reclaim_buffer_bps", symbol, 2.0, mid)
        if recent_low < float(previous["recent_low"]) and mid < float(previous["mid"]):
            obs["down_pushes"] = int(obs.get("down_pushes", 0)) + 1
            obs["last_down_low"] = recent_low
        if recent_high > float(previous["recent_high"]) and mid > float(previous["mid"]):
            obs["up_pushes"] = int(obs.get("up_pushes", 0)) + 1
            obs["last_up_high"] = recent_high

        attempts: list[dict[str, Any]] = []
        if int(obs.get("down_pushes", 0)) >= required_pushes and mid >= recent_low + reclaim_buffer:
            attempts.append(self._build_drift_thesis(symbol, "BUY", obs, features, config, now))
        if int(obs.get("up_pushes", 0)) >= required_pushes and mid <= recent_high - reclaim_buffer:
            attempts.append(self._build_drift_thesis(symbol, "SELL", obs, features, config, now))

        attempts = [attempt for attempt in attempts if attempt]
        if not attempts:
            return None

        thesis = max(attempts, key=lambda item: float(item["score"]))
        candidate = self._candidate_from_thesis(thesis, "drift_sweep_retrace_thesis")
        if float(thesis["score"]) < min_score:
            return ThesisOutcome(
                candidate,
                "DRIFT_THESIS_REJECTED",
                f"Drift exhaustion found but confluence score {thesis['score']:.2f} is below drift minimum {min_score:.2f}.",
            )
        self.drift_observations.pop(symbol, None)
        thesis["state"] = "READY_FOR_RISK"
        return ThesisOutcome(
            candidate,
            "DRIFT_THESIS_TRIGGERED",
            f"{thesis['side']} drift exhaustion thesis triggered after {thesis.get('drift_pushes', 0)} pushes; target {thesis['target_label']}.",
            executable=True,
        )

    def _build_zone_thesis(
        self,
        symbol: str,
        side: str,
        zone: dict[str, Any],
        observation: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        target = self._select_target(side, features, config)
        if not target:
            return {}
        mid = float(features["mid"])
        low = float(zone["price_low"])
        high = float(zone["price_high"])
        extreme = high if side == "BUY" else low
        wick_confirmation = self._wick_absorption_confirmation(symbol, side, extreme, features, config, mid, "zone", "defended zone")
        if not wick_confirmation["ok"]:
            return {}
        rejection_guard = self._opposite_wick_rejection_guard(symbol, side, features, config, mid, "zone thesis")
        if not rejection_guard["ok"]:
            return {}
        zone_absorption_score = float(zone.get("absorption_score", 0.0))
        flow_confirmation = self._flow_confirmation(side, features, config, wick_confirmation, zone_absorption_score)
        if not flow_confirmation["ok"]:
            return {}
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, mid)
        if side == "BUY":
            planned_sl = low - sl_buffer
            planned_tp = float(target["price"])
        else:
            planned_sl = high + sl_buffer
            planned_tp = float(target["price"])
        rr = self._rr(side, mid, planned_sl, planned_tp)
        if not self._valid_trade_plan(side, mid, planned_sl, planned_tp, rr):
            return {}
        score, scores, reasons = self._score_confluence(side, features, target, config)
        touch_bonus = min(8.0, (int(observation["touches"]) - 2) * 3.0)
        memory = float(zone.get("memory_score", 0.0))
        wick_bonus = float(wick_confirmation.get("score_bonus", 0.0))
        score = min(100.0, score + touch_bonus + min(6.0, zone_absorption_score * 0.06) + min(6.0, memory * 0.05) + wick_bonus + float(flow_confirmation["score_bonus"]))
        reasons.append(
            f"zone {zone.get('label')} defended {int(observation['touches'])} times; memory {memory:.1f}, "
            f"book absorption {zone_absorption_score:.1f}; {wick_confirmation['reason']}"
        )
        reasons.append(flow_confirmation["reason"])
        return {
            "id": f"{symbol}:ZONE:{side}:{int(now)}",
            "symbol": symbol,
            "side": side,
            "state": "ZONE_CANDIDATE",
            "created_ts": now,
            "updated_ts": now,
            "planned_entry": round(mid, 4),
            "planned_sl": round(planned_sl, 4),
            "planned_tp": round(planned_tp, 4),
            "target_label": str(target["label"]),
            "target_distance_bps": round(float(target["distance_bps"]), 2),
            "target_notional": round(float(target["notional"]), 4),
            "target_memory_score": round(float(target.get("memory_score", 0.0)), 2),
            "target_pull_count": int(target.get("pull_count", 0)),
            "zone_label": str(zone.get("label")),
            "zone_touches": int(observation["touches"]),
            "absorption_candle_time": wick_confirmation.get("open_time"),
            "absorption_wick_pct": round(float(wick_confirmation.get("wick_pct", 0.0)), 4),
            "absorption_close_position": round(float(wick_confirmation.get("close_position", 0.0)), 4),
            "absorption_delta": round(float(wick_confirmation.get("delta", 0.0)), 8),
            "flow_confirmation_passes": int(flow_confirmation["passes"]),
            "rr": round(rr, 2),
            "score": round(score, 2),
            "scores": scores,
            "reasons": reasons,
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "entry_reason": f"{side} after repeated defended liquidity zone {zone.get('label')} held with M1 wick absorption and reclaimed.",
            "stop_reason": f"SL is beyond the full defended zone {zone.get('label')} plus buffer, not inside the touched area.",
            "tp_reason": f"TP targets opposing memory/current liquidity at {target['label']}.",
            "invalidation": "Invalid if price accepts beyond the defended zone or target liquidity pulls.",
        }

    def _build_drift_thesis(
        self,
        symbol: str,
        side: str,
        observation: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        target = self._select_target(side, features, config)
        if not target:
            return {}
        mid = float(features["mid"])
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, mid)
        min_stop = self._bps_value(config, "strategy", "min_structural_stop_bps", symbol, 10.0, mid)
        if side == "BUY":
            extreme = float(observation.get("last_down_low", features.get("recent_low", mid)))
            planned_sl = min(extreme - sl_buffer, mid - min_stop)
            pushes = int(observation.get("down_pushes", 0))
        else:
            extreme = float(observation.get("last_up_high", features.get("recent_high", mid)))
            planned_sl = max(extreme + sl_buffer, mid + min_stop)
            pushes = int(observation.get("up_pushes", 0))
        absorption = self._wick_absorption_confirmation(symbol, side, extreme, features, config, mid, "drift", "drift extreme")
        if not absorption["ok"]:
            return {}
        rejection_guard = self._opposite_wick_rejection_guard(symbol, side, features, config, mid, "drift thesis")
        if not rejection_guard["ok"]:
            return {}
        flow_confirmation = self._flow_confirmation(side, features, config, absorption, 0.0)
        if not flow_confirmation["ok"]:
            return {}
        planned_tp = float(target["price"])
        rr = self._rr(side, mid, planned_sl, planned_tp)
        if not self._valid_trade_plan(side, mid, planned_sl, planned_tp, rr):
            return {}
        score, scores, reasons = self._score_confluence(side, features, target, config)
        delta_divergence = str(features.get("delta_divergence", "none"))
        if (side == "BUY" and delta_divergence == "bullish_delta_divergence") or (side == "SELL" and delta_divergence == "bearish_delta_divergence"):
            score += 6.0
        score = min(100.0, score + min(8.0, max(0, pushes - 2) * 3.0) + float(absorption.get("score_bonus", 0.0)) + float(flow_confirmation["score_bonus"]))
        reasons.append(
            f"drift exhaustion after {pushes} directional pushes; delta divergence {delta_divergence}; "
            f"{absorption['reason']}"
        )
        reasons.append(flow_confirmation["reason"])
        return {
            "id": f"{symbol}:DRIFT:{side}:{int(now)}",
            "symbol": symbol,
            "side": side,
            "state": "DRIFT_CANDIDATE",
            "created_ts": now,
            "updated_ts": now,
            "planned_entry": round(mid, 4),
            "planned_sl": round(planned_sl, 4),
            "planned_tp": round(planned_tp, 4),
            "target_label": str(target["label"]),
            "target_distance_bps": round(float(target["distance_bps"]), 2),
            "target_notional": round(float(target["notional"]), 4),
            "target_memory_score": round(float(target.get("memory_score", 0.0)), 2),
            "target_pull_count": int(target.get("pull_count", 0)),
            "drift_pushes": pushes,
            "absorption_candle_time": absorption.get("open_time"),
            "absorption_wick_pct": round(float(absorption.get("wick_pct", 0.0)), 4),
            "absorption_close_position": round(float(absorption.get("close_position", 0.0)), 4),
            "absorption_delta": round(float(absorption.get("delta", 0.0)), 8),
            "flow_confirmation_passes": int(flow_confirmation["passes"]),
            "rr": round(rr, 2),
            "score": round(score, 2),
            "scores": scores,
            "reasons": reasons,
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "entry_reason": f"{side} after slow drift exhaustion, M1 wick absorption, and reclaim from marginal sweep structure.",
            "stop_reason": f"SL is beyond the drift extreme {extreme:.4f} plus buffer.",
            "tp_reason": f"TP targets opposing memory/current liquidity at {target['label']}.",
            "invalidation": "Invalid if drift resumes through the exhaustion extreme or target liquidity pulls.",
        }

    def _wick_absorption_confirmation(
        self,
        symbol: str,
        side: str,
        extreme: float,
        features: dict[str, Any],
        config: dict[str, Any],
        mid: float,
        prefix: str,
        label: str,
    ) -> dict[str, Any]:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get(f"{prefix}_absorption_wick_enabled", True):
            return {"ok": True, "reason": f"M1 {prefix} wick absorption filter disabled.", "score_bonus": 0.0}

        candles = list(features.get("trade_m1_candles") or [])
        lookback = int(self._symbol_value(config, "thesis", f"{prefix}_absorption_lookback_candles", symbol, 3))
        min_wick = float(self._symbol_value(config, "thesis", f"{prefix}_absorption_min_wick_pct", symbol, 0.35))
        min_reclaim = float(self._symbol_value(config, "thesis", f"{prefix}_absorption_min_close_reclaim_pct", symbol, 0.55))
        tolerance = self._bps_value(config, "thesis", f"{prefix}_absorption_extreme_tolerance_bps", symbol, 8.0, mid)
        recent = candles[-max(1, lookback) :]
        if not recent:
            return {"ok": False, "reason": f"No M1 trade candle available for {prefix} absorption confirmation."}

        for candle in reversed(recent):
            high = float(candle.get("high", 0.0))
            low = float(candle.get("low", 0.0))
            close_position = float(candle.get("close_position", 0.5))
            delta = float(candle.get("delta", 0.0))
            if high <= low:
                continue
            if side == "BUY":
                wick_pct = float(candle.get("lower_wick_pct", 0.0))
                touched_extreme = low <= extreme + tolerance
                reclaimed = close_position >= min_reclaim
                direction = "lower"
            else:
                wick_pct = float(candle.get("upper_wick_pct", 0.0))
                touched_extreme = high >= extreme - tolerance
                reclaimed = close_position <= 1.0 - min_reclaim
                direction = "upper"
            if touched_extreme and wick_pct >= min_wick and reclaimed:
                return {
                    "ok": True,
                    "open_time": candle.get("open_time"),
                    "wick_pct": wick_pct,
                    "close_position": close_position,
                    "delta": delta,
                    "score_bonus": min(5.0, wick_pct * 8.0),
                    "reason": (
                        f"M1 {direction}-wick absorption confirmed near {label} {extreme:.4f}: "
                        f"wick={wick_pct:.2f}, close_position={close_position:.2f}, delta={delta:.4f}."
                    ),
                }

        return {
            "ok": False,
            "reason": (
                f"No recent M1 absorption wick near {label} {extreme:.4f}; "
                f"required wick>={min_wick:.2f} and reclaim close position {min_reclaim:.2f}."
            ),
        }

    def _opposite_wick_rejection_guard(
        self,
        symbol: str,
        side: str,
        features: dict[str, Any],
        config: dict[str, Any],
        mid: float,
        context: str,
    ) -> dict[str, Any]:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("opposite_wick_rejection_enabled", True):
            return {"ok": True, "reason": "Opposite wick rejection guard disabled."}

        candles = list(features.get("trade_m1_candles") or [])
        lookback = int(self._symbol_value(config, "thesis", "opposite_wick_rejection_lookback_candles", symbol, 3))
        min_wick = float(self._symbol_value(config, "thesis", "opposite_wick_rejection_min_wick_pct", symbol, 0.40))
        min_reclaim = float(self._symbol_value(config, "thesis", "opposite_wick_rejection_min_close_reclaim_pct", symbol, 0.60))
        tolerance = self._bps_value(config, "thesis", "opposite_wick_rejection_tolerance_bps", symbol, 8.0, mid)
        recent = candles[-max(1, lookback) :]
        if not recent:
            return {"ok": True, "reason": "No M1 candle available for opposite wick rejection guard."}

        if side == "BUY":
            wick_key = "upper_wick_pct"
            extreme_key = "high"
            reference = float(features.get("recent_high", mid))
            rejected_close = lambda close_position: close_position <= 1.0 - min_reclaim
            near_reference = lambda extreme: extreme >= reference - tolerance
            label = "upper-wick resistance"
        else:
            wick_key = "lower_wick_pct"
            extreme_key = "low"
            reference = float(features.get("recent_low", mid))
            rejected_close = lambda close_position: close_position >= min_reclaim
            near_reference = lambda extreme: extreme <= reference + tolerance
            label = "lower-wick support"

        rejections: list[dict[str, float | int | None]] = []
        double_enabled = bool(thesis_cfg.get("double_wick_rejection_enabled", True))
        double_min_wick = float(self._symbol_value(config, "thesis", "double_wick_rejection_min_wick_pct", symbol, 0.30))
        double_tolerance = self._bps_value(config, "thesis", "double_wick_rejection_tolerance_bps", symbol, 8.0, mid)

        for candle in reversed(recent):
            if candle.get("is_closed") is False:
                continue
            high = float(candle.get("high", 0.0))
            low = float(candle.get("low", 0.0))
            if high <= low:
                continue
            wick = float(candle.get(wick_key, 0.0))
            close_position = float(candle.get("close_position", 0.5))
            extreme = float(candle.get(extreme_key, 0.0))
            if wick >= double_min_wick and rejected_close(close_position):
                rejections.append(
                    {
                        "open_time": candle.get("open_time"),
                        "extreme": extreme,
                        "wick": wick,
                        "close_position": close_position,
                    }
                )
            if wick >= min_wick and rejected_close(close_position) and near_reference(extreme):
                return {
                    "ok": False,
                    "reason": (
                        f"{side} {context} blocked: fresh {label} rejection at {extreme:.4f}; "
                        f"wick={wick:.2f}, close_position={close_position:.2f}."
                    ),
                }

        if double_enabled and len(rejections) >= 2:
            for index, first in enumerate(rejections):
                for second in rejections[index + 1 :]:
                    if abs(float(first["extreme"]) - float(second["extreme"])) <= double_tolerance:
                        return {
                            "ok": False,
                            "reason": (
                                f"{side} {context} blocked: double {label} rejection near "
                                f"{float(first['extreme']):.4f}/{float(second['extreme']):.4f}."
                            ),
                        }

        return {"ok": True, "reason": f"No opposite {label} rejection blocking {context}."}

    def _flow_confirmation(
        self,
        side: str,
        features: dict[str, Any],
        config: dict[str, Any],
        wick_confirmation: dict[str, Any],
        book_absorption_score: float,
    ) -> dict[str, Any]:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("flow_confirmation_enabled", True):
            return {"ok": True, "passes": 0, "score_bonus": 0.0, "reason": "Flow confirmation filter disabled."}

        symbol = str(features["symbol"])
        required = int(thesis_cfg.get("flow_confirmation_min_passes", 3))
        min_delta = float(self._symbol_value(config, "thesis", "flow_confirmation_min_delta_imbalance", symbol, 0.06))
        min_recent_cvd = float(self._symbol_value(config, "thesis", "flow_confirmation_min_recent_cvd", symbol, 0.25))
        min_absorption = float(thesis_cfg.get("flow_confirmation_min_absorption_score", 20.0))
        allow_neutral_oi = bool(thesis_cfg.get("flow_confirmation_allow_neutral_oi", True))
        require_core_flow = bool(thesis_cfg.get("flow_confirmation_require_core_flow", True))
        require_session_not_opposed = bool(thesis_cfg.get("flow_confirmation_require_session_not_opposed", True))
        block_oi_divergence = bool(thesis_cfg.get("flow_confirmation_block_oi_divergence", True))

        delta_imbalance = float(features.get("delta_imbalance", features.get("flow_imbalance", 0.0)))
        recent_cvd = float(features.get("cumulative_delta", features.get("delta", 0.0)))
        session_cvd = float(features.get("session_cumulative_delta", 0.0))
        session_delta_imbalance = float(features.get("session_delta_imbalance", 0.0))
        divergence = str(features.get("delta_divergence", "none"))
        oi_alignment = self._oi_alignment(side, features)

        if side == "BUY":
            delta_pass = delta_imbalance >= min_delta or divergence == "bullish_delta_divergence"
            recent_cvd_pass = recent_cvd >= min_recent_cvd
            session_cvd_pass = session_cvd > 0 and session_delta_imbalance >= 0
        else:
            delta_pass = delta_imbalance <= -min_delta or divergence == "bearish_delta_divergence"
            recent_cvd_pass = recent_cvd <= -min_recent_cvd
            session_cvd_pass = session_cvd < 0 and session_delta_imbalance <= 0

        oi_pass = oi_alignment == "supporting" or (allow_neutral_oi and oi_alignment == "neutral")
        oi_strong = oi_alignment == "supporting"
        absorption_pass = bool(wick_confirmation.get("ok")) and (
            float(wick_confirmation.get("wick_pct", 0.0)) > 0 or book_absorption_score >= min_absorption
        )

        checks = {
            "delta": delta_pass,
            "recent_cvd": recent_cvd_pass,
            "session_cvd": session_cvd_pass,
            "oi": oi_pass,
            "absorption": absorption_pass,
        }
        passes = sum(1 for passed in checks.values() if passed)
        blockers: list[str] = []
        if require_core_flow and not (delta_pass and recent_cvd_pass):
            blockers.append("core delta/recent CVD not aligned")
        if require_session_not_opposed and not session_cvd_pass:
            blockers.append("session CVD opposes entry")
        if block_oi_divergence and oi_alignment == "diverging":
            blockers.append("OI diverges from entry side")

        ok = passes >= required and not blockers
        score_bonus = min(5.0, max(0, passes - required + 1) * 1.5 + (1.0 if oi_strong else 0.0))
        blocker_text = f" blockers: {', '.join(blockers)}." if blockers else ""
        reason = (
            f"flow confirmation {passes}/{required}: "
            f"delta={delta_imbalance:.3f} {'OK' if delta_pass else 'NO'}, "
            f"recent CVD={recent_cvd:.4f} {'OK' if recent_cvd_pass else 'NO'}, "
            f"session CVD={session_cvd:.4f}/{session_delta_imbalance:.3f} {'OK' if session_cvd_pass else 'NO'}, "
            f"OI={oi_alignment} {'OK' if oi_pass else 'NO'}, "
            f"absorption wick={float(wick_confirmation.get('wick_pct', 0.0)):.2f}/book={book_absorption_score:.1f} "
            f"{'OK' if absorption_pass else 'NO'}."
            f"{blocker_text}"
        )
        return {"ok": ok, "passes": passes, "score_bonus": score_bonus, "reason": reason, "checks": checks, "blockers": blockers}

    def _try_sharp_flow_thesis(
        self,
        symbol: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome | None:
        thesis_cfg = config.get("thesis", {})
        if not thesis_cfg.get("sharp_flow_enabled", True):
            return None

        mid = float(features["mid"])
        delta = float(features.get("delta_imbalance", features.get("flow_imbalance", 0.0)))
        drift_bps = float(features.get("trade_drift_bps", 0.0))
        min_delta = float(thesis_cfg.get("sharp_flow_min_delta", {}).get(symbol, 0.28))
        min_drift = float(thesis_cfg.get("sharp_flow_min_drift_bps", {}).get(symbol, 6.0))
        side = None
        if delta >= min_delta and drift_bps >= min_drift:
            side = "BUY"
        elif delta <= -min_delta and drift_bps <= -min_drift:
            side = "SELL"
        if not side:
            return None

        thesis = self._build_sharp_flow_thesis(symbol, side, features, config, now, delta, drift_bps)
        if not thesis:
            return None

        min_score = float(thesis_cfg.get("sharp_flow_min_score", thesis_cfg.get("min_score", 72.0)))
        candidate = self._candidate_from_thesis(thesis, "sharp_flow_impulse_thesis")
        if float(thesis["score"]) < min_score:
            return ThesisOutcome(
                candidate,
                "SHARP_FLOW_REJECTED",
                f"Sharp {side} flow found but confluence score {thesis['score']:.2f} is below sharp-flow minimum {min_score:.2f}.",
            )
        return ThesisOutcome(
            candidate,
            "SHARP_FLOW_TRIGGERED",
            f"Sharp {side} flow thesis triggered: delta {delta:.3f}, drift {drift_bps:.2f} bps, target {thesis['target_label']}.",
            executable=True,
        )

    def _build_sharp_flow_thesis(
        self,
        symbol: str,
        side: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
        delta: float,
        drift_bps: float,
    ) -> dict[str, Any]:
        target = self._select_target(side, features, config)
        if not target:
            return {}
        mid = float(features["mid"])
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, mid)
        min_stop = self._bps_value(config, "strategy", "min_structural_stop_bps", symbol, 10.0, mid)
        if side == "BUY":
            planned_sl = min(recent_low - sl_buffer, mid - min_stop)
        else:
            planned_sl = max(recent_high + sl_buffer, mid + min_stop)
        planned_tp = float(target["price"])
        rr = self._rr(side, mid, planned_sl, planned_tp)
        if not self._valid_trade_plan(side, mid, planned_sl, planned_tp, rr):
            return {}
        score, scores, reasons = self._score_confluence(side, features, target, config)
        impulse_bonus = min(10.0, abs(delta) * 18.0 + abs(drift_bps) * 0.25)
        score = min(100.0, score + impulse_bonus)
        reasons.append(f"sharp flow impulse delta {delta:.3f}, trade drift {drift_bps:.2f} bps")
        return {
            "id": f"{symbol}:SHARP:{side}:{int(now)}",
            "symbol": symbol,
            "side": side,
            "state": "SHARP_FLOW_CANDIDATE",
            "created_ts": now,
            "updated_ts": now,
            "planned_entry": round(mid, 4),
            "planned_sl": round(planned_sl, 4),
            "planned_tp": round(planned_tp, 4),
            "target_label": str(target["label"]),
            "target_distance_bps": round(float(target["distance_bps"]), 2),
            "target_notional": round(float(target["notional"]), 4),
            "target_memory_score": round(float(target.get("memory_score", 0.0)), 2),
            "target_pull_count": int(target.get("pull_count", 0)),
            "rr": round(rr, 2),
            "score": round(score, 2),
            "scores": scores,
            "reasons": reasons,
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "entry_reason": f"{side} on sharp tape impulse: aggressive delta and fast price displacement aligned.",
            "stop_reason": "SL is beyond the impulse origin/recent extreme plus structural buffer.",
            "tp_reason": f"TP targets next opposing liquidity/memory bucket at {target['label']}.",
            "invalidation": "Invalid if impulse immediately mean-reverts through the recent extreme or target liquidity pulls.",
        }

    def _build_thesis(
        self,
        symbol: str,
        side: str,
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
        swept_level: float,
        sweep_extreme: float,
    ) -> dict[str, Any]:
        target = self._select_target(side, features, config)
        if not target:
            return {}

        mid = float(features["mid"])
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, mid)
        min_stop = self._bps_value(config, "strategy", "min_structural_stop_bps", symbol, 10.0, mid)
        if side == "BUY":
            planned_entry = mid
            planned_sl = min(sweep_extreme - sl_buffer, mid - min_stop)
            planned_tp = float(target["price"])
        else:
            planned_entry = mid
            planned_sl = max(sweep_extreme + sl_buffer, mid + min_stop)
            planned_tp = float(target["price"])

        rr = self._rr(side, planned_entry, planned_sl, planned_tp)
        if not self._valid_trade_plan(side, planned_entry, planned_sl, planned_tp, rr):
            return {}
        score, scores, reasons = self._score_confluence(side, features, target, config)
        return {
            "id": f"{symbol}:{side}:{int(now)}",
            "symbol": symbol,
            "side": side,
            "state": "THESIS_CANDIDATE",
            "created_ts": now,
            "updated_ts": now,
            "swept_level": round(swept_level, 4),
            "sweep_extreme": round(sweep_extreme, 4),
            "planned_entry": round(planned_entry, 4),
            "planned_sl": round(planned_sl, 4),
            "planned_tp": round(planned_tp, 4),
            "target_label": str(target["label"]),
            "target_distance_bps": round(float(target["distance_bps"]), 2),
            "target_notional": round(float(target["notional"]), 4),
            "target_memory_score": round(float(target.get("memory_score", 0.0)), 2),
            "target_pull_count": int(target.get("pull_count", 0)),
            "entry_sweep_seen": False,
            "entry_sweep_extreme": None,
            "rr": round(rr, 2),
            "score": round(score, 2),
            "scores": scores,
            "reasons": reasons,
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "entry_reason": (
                f"{side} thesis begins only after the first liquidity sweep/reclaim. "
                "This is a pre-plan, not an immediate entry."
            ),
            "stop_reason": (
                f"Initial planned SL sits beyond the swept extreme {sweep_extreme:.4f} with volatility/structure buffer. "
                "The system waits for that level to be swept before any real entry."
            ),
            "tp_reason": f"TP is locked to opposing resting liquidity at {target['label']} near {planned_tp:.4f}.",
            "invalidation": "Invalid if target is reached before entry, thesis expires, or the retest never reclaims.",
        }

    def _update_existing(
        self,
        symbol: str,
        thesis: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        now: float,
    ) -> ThesisOutcome:
        side = thesis["side"]
        mid = float(features["mid"])
        max_age = float(config.get("thesis", {}).get("max_age_seconds", 900.0))
        if now - float(thesis["created_ts"]) > max_age:
            self.clear_symbol(symbol)
            return ThesisOutcome(
                self._candidate_from_thesis(thesis, "locked_liquidity_thesis"),
                "THESIS_EXPIRED",
                f"{side} thesis expired after {max_age:.0f}s without entry confirmation.",
            )

        planned_tp = float(thesis["planned_tp"])
        if (side == "BUY" and mid >= planned_tp) or (side == "SELL" and mid <= planned_tp):
            self.clear_symbol(symbol)
            return ThesisOutcome(
                self._candidate_from_thesis(thesis, "locked_liquidity_thesis"),
                "THESIS_INVALIDATED",
                "Locked target was reached before the entry sweep; no chase entry.",
            )

        thesis["updated_ts"] = now
        planned_sl = float(thesis["planned_sl"])
        recent_low = float(features.get("recent_low", mid))
        recent_high = float(features.get("recent_high", mid))
        reclaim_buffer = self._bps_value(config, "thesis", "entry_reclaim_buffer_bps", symbol, 2.0, mid)

        if side == "BUY" and not thesis.get("entry_sweep_seen") and recent_low <= planned_sl:
            thesis["entry_sweep_seen"] = True
            thesis["entry_sweep_extreme"] = round(recent_low, 4)
            thesis["state"] = "WAITING_FOR_RECLAIM"
            return self._state_change(symbol, thesis, "ENTRY_SWEEP_SEEN", "Planned BUY SL zone was swept; waiting for reclaim.")

        if side == "SELL" and not thesis.get("entry_sweep_seen") and recent_high >= planned_sl:
            thesis["entry_sweep_seen"] = True
            thesis["entry_sweep_extreme"] = round(recent_high, 4)
            thesis["state"] = "WAITING_FOR_RECLAIM"
            return self._state_change(symbol, thesis, "ENTRY_SWEEP_SEEN", "Planned SELL SL zone was swept; waiting for reclaim.")

        if not thesis.get("entry_sweep_seen"):
            thesis["state"] = "WAITING_FOR_ENTRY_SWEEP"
            return self._state_change(symbol, thesis, "WAITING_FOR_ENTRY_SWEEP", "Thesis is locked; waiting for planned SL zone to be swept.")

        reclaimed = (
            mid >= float(thesis["swept_level"]) + reclaim_buffer
            if side == "BUY"
            else mid <= float(thesis["swept_level"]) - reclaim_buffer
        )
        if not reclaimed:
            thesis["state"] = "WAITING_FOR_RECLAIM"
            return self._state_change(symbol, thesis, "WAITING_FOR_RECLAIM", "Entry sweep happened; waiting for price to reclaim the swept level.")

        entry_candle = self._entry_candle_confirmation(symbol, side, thesis, features, config, mid)
        if not entry_candle["ok"]:
            thesis["state"] = "WAITING_FOR_ENTRY_CANDLE"
            thesis["entry_candle_reason"] = entry_candle["reason"]
            return self._state_change(symbol, thesis, "WAITING_FOR_ENTRY_CANDLE", entry_candle["reason"])

        target = {
            "price": float(thesis["planned_tp"]),
            "label": thesis["target_label"],
            "distance_bps": abs(float(thesis["planned_tp"]) - mid) / mid * 10000.0 if mid else 0.0,
            "notional": float(thesis.get("target_notional", 0.0)),
            "memory_score": float(thesis.get("target_memory_score", 0.0)),
            "pull_count": int(thesis.get("target_pull_count", 0)),
            "pull_rate": 0.0,
            "strength": 100.0,
        }
        score, scores, reasons = self._score_confluence(side, features, target, config)
        trigger_score = float(config.get("thesis", {}).get("entry_trigger_score", 75.0))
        if score < trigger_score:
            thesis["state"] = "WAITING_FOR_CONFLUENCE"
            thesis["score"] = round(score, 2)
            thesis["scores"] = scores
            thesis["reasons"] = reasons
            return self._state_change(
                symbol,
                thesis,
                "WAITING_FOR_CONFLUENCE",
                f"Reclaim confirmed, but confluence score {score:.2f} is below trigger {trigger_score:.2f}.",
            )

        reasons = [*reasons, entry_candle["reason"]]
        candidate = self._trigger_candidate(thesis, features, config, score, scores, reasons)
        if not candidate:
            thesis["state"] = "WAITING_FOR_CONFLUENCE"
            return self._state_change(symbol, thesis, "WAITING_FOR_CONFLUENCE", "Reclaim confirmed but RR/stop constraints are not acceptable.")

        self.clear_symbol(symbol)
        return ThesisOutcome(
            candidate,
            "ENTRY_TRIGGERED",
            (
                "Locked thesis entry confirmed: original liquidity sweep, opposing target lock, planned SL sweep, "
                "reclaim, and closed M1 entry candle all aligned. Candidate is ready for risk checks."
            ),
            executable=True,
        )

    def _entry_candle_confirmation(
        self,
        symbol: str,
        side: str,
        thesis: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        mid: float,
    ) -> dict[str, Any]:
        cfg = config.get("entry_candle_confirmation", {})
        if not cfg.get("enabled", True):
            return {"ok": True, "reason": "Entry M1 candle confirmation disabled."}

        use_closed = bool(cfg.get("use_closed_m1", True))
        lookback = int(self._symbol_value(config, "entry_candle_confirmation", "lookback_candles", symbol, 2))
        min_wick = float(self._symbol_value(config, "entry_candle_confirmation", "min_wick_pct", symbol, 0.40))
        min_reclaim = float(self._symbol_value(config, "entry_candle_confirmation", "min_close_reclaim_pct", symbol, 0.60))
        tolerance = self._bps_value(config, "entry_candle_confirmation", "extreme_tolerance_bps", symbol, 8.0, mid)
        require_delta = bool(cfg.get("require_delta_alignment", True))
        min_delta = float(self._symbol_value(config, "entry_candle_confirmation", "min_delta_imbalance", symbol, 0.0))
        candles = self._recent_m1_candles(features, max(1, lookback), use_closed)
        if not candles:
            candle_type = "closed M1" if use_closed else "M1"
            return {"ok": False, "reason": f"Reclaim confirmed, waiting for a {candle_type} entry candle."}

        extreme = float(thesis.get("entry_sweep_extreme") or thesis.get("planned_sl") or mid)
        for candle in reversed(candles):
            high = float(candle.get("high", 0.0))
            low = float(candle.get("low", 0.0))
            if high <= low:
                continue
            close_position = float(candle.get("close_position", 0.5))
            delta = float(candle.get("delta", 0.0))
            if side == "BUY":
                wick = float(candle.get("lower_wick_pct", 0.0))
                touched = low <= extreme + tolerance
                reclaimed = close_position >= min_reclaim
                delta_ok = (not require_delta) or delta >= min_delta
                direction = "lower-wick support"
                delta_phrase = f"delta>={min_delta:.4f}"
            else:
                wick = float(candle.get("upper_wick_pct", 0.0))
                touched = high >= extreme - tolerance
                reclaimed = close_position <= 1.0 - min_reclaim
                delta_ok = (not require_delta) or delta <= -min_delta
                direction = "upper-wick resistance"
                delta_phrase = f"delta<={-min_delta:.4f}"
            if touched and wick >= min_wick and reclaimed and delta_ok:
                return {
                    "ok": True,
                    "reason": (
                        f"Closed M1 entry candle confirmed {direction} absorption near {extreme:.4f}: "
                        f"wick={wick:.2f}, close_position={close_position:.2f}, delta={delta:.4f}."
                    ),
                }

        return {
            "ok": False,
            "reason": (
                f"Reclaim confirmed, but entry candle is not ready: need {side} M1 absorption near {extreme:.4f} "
                f"with wick>={min_wick:.2f}, reclaim>={min_reclaim:.2f}, {delta_phrase}; "
                f"checked {len(candles)} candle(s)."
            ),
        }

    def _recent_m1_candles(self, features: dict[str, Any], lookback: int, use_closed: bool) -> list[dict[str, Any]]:
        candles = list(features.get("trade_m1_candles") or [])
        if use_closed:
            candles = [candle for candle in candles if self._m1_candle_closed(candle)]
        return candles[-lookback:]

    @staticmethod
    def _m1_candle_closed(candle: dict[str, Any]) -> bool:
        if "is_closed" in candle:
            return bool(candle.get("is_closed"))
        open_time = candle.get("open_time")
        if open_time is None:
            return False
        try:
            return int(open_time) + 60_000 <= int(time() * 1000)
        except (TypeError, ValueError):
            return False

    def _state_change(self, symbol: str, thesis: dict[str, Any], decision: str, reason: str) -> ThesisOutcome:
        key = f"{decision}:{thesis.get('state')}:{thesis.get('entry_sweep_seen')}:{thesis.get('score')}"
        if self._last_logged_state.get(symbol) == key:
            return ThesisOutcome(None, decision, reason)
        self._last_logged_state[symbol] = key
        return ThesisOutcome(self._candidate_from_thesis(thesis, "locked_liquidity_thesis"), decision, reason)

    def _trigger_candidate(
        self,
        thesis: dict[str, Any],
        features: dict[str, Any],
        config: dict[str, Any],
        score: float,
        scores: dict[str, float],
        reasons: list[str],
    ) -> dict[str, Any] | None:
        symbol = thesis["symbol"]
        side = thesis["side"]
        entry = float(features["mid"])
        sl_extreme = float(thesis.get("entry_sweep_extreme") or thesis["planned_sl"])
        sl_buffer = self._bps_value(config, "strategy", "sl_buffer_bps", symbol, 5.0, entry)
        min_stop = self._bps_value(config, "strategy", "min_structural_stop_bps", symbol, 10.0, entry)
        max_stop = self._bps_value(config, "strategy", "max_structural_stop_bps", symbol, 60.0, entry)
        if side == "BUY":
            stop_loss = min(sl_extreme - sl_buffer, entry - min_stop)
            stop_distance = entry - stop_loss
        else:
            stop_loss = max(sl_extreme + sl_buffer, entry + min_stop)
            stop_distance = stop_loss - entry
        if stop_distance <= 0:
            return None
        stop_bps = stop_distance / entry * 10000.0 if entry else 0.0
        if stop_distance > max_stop:
            return None
        take_profit = float(thesis["planned_tp"])
        rr = self._rr(side, entry, stop_loss, take_profit)
        min_rr = float(config.get("risk", {}).get("min_rr", 2.0))
        if not self._valid_trade_plan(side, entry, stop_loss, take_profit, rr):
            return None
        if rr < min_rr:
            return None

        liquidity_score = scores.get("target_liquidity", 0.0) + scores.get("sweep", 0.0)
        flow_score = scores.get("delta", 0.0) + scores.get("open_interest", 0.0)
        return {
            "symbol": symbol,
            "side": side,
            "strategy": "locked_liquidity_thesis_entry",
            "entry": round(entry, 4),
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
            "rr": round(rr, 2),
            "score": round(score, 2),
            "liquidity_score": round(min(100.0, liquidity_score * 2.0), 2),
            "flow_score": round(min(100.0, flow_score * 2.5), 2),
            "regime_score": round(min(100.0, scores.get("vwap", 0.0) * 6.0 + scores.get("volume_profile", 0.0) * 4.0), 2),
            "entry_zone_low": round(entry, 4),
            "entry_zone_high": round(entry, 4),
            "stop_distance_bps": round(stop_bps, 2),
            "entry_reason": (
                f"Entry after {side} thesis: first sweep locked the direction, then the planned SL zone was swept, "
                "and price reclaimed with confirmation."
            ),
            "stop_reason": f"Fresh SL is beyond the second sweep extreme {sl_extreme:.4f}, not at the obvious first stop.",
            "tp_reason": f"TP remains locked at the original opposing liquidity bucket {thesis['target_label']}.",
            "open_interest_signal": features.get("open_interest_signal", "unknown"),
            "open_interest_alignment": self._oi_alignment(side, features),
            "invalidation": "Risk engine may still reject; otherwise manage from MT5/paper execution.",
            "reason": " ".join(reasons),
        }

    def _candidate_from_thesis(self, thesis: dict[str, Any], strategy: str) -> dict[str, Any]:
        scores = thesis.get("scores", {})
        return {
            "symbol": thesis["symbol"],
            "side": thesis["side"],
            "strategy": strategy,
            "entry": float(thesis["planned_entry"]),
            "stop_loss": float(thesis["planned_sl"]),
            "take_profit": float(thesis["planned_tp"]),
            "rr": float(thesis.get("rr", 0.0)),
            "score": float(thesis.get("score", 0.0)),
            "liquidity_score": round(min(100.0, (scores.get("target_liquidity", 0.0) + scores.get("sweep", 0.0)) * 2.0), 2),
            "flow_score": round(min(100.0, (scores.get("delta", 0.0) + scores.get("open_interest", 0.0)) * 2.5), 2),
            "regime_score": round(min(100.0, scores.get("vwap", 0.0) * 6.0 + scores.get("volume_profile", 0.0) * 4.0), 2),
            "entry_zone_low": float(thesis["planned_entry"]),
            "entry_zone_high": float(thesis["planned_entry"]),
            "entry_reason": thesis.get("entry_reason", ""),
            "stop_reason": thesis.get("stop_reason", ""),
            "tp_reason": thesis.get("tp_reason", ""),
            "invalidation": thesis.get("invalidation", ""),
            "reason": " ".join(thesis.get("reasons", [])),
            "state": thesis.get("state"),
            "confirmations": 0,
            "target_label": thesis.get("target_label"),
            "target_memory_score": thesis.get("target_memory_score"),
            "entry_sweep_seen": thesis.get("entry_sweep_seen", False),
            "swept_level": thesis.get("swept_level"),
            "sweep_extreme": thesis.get("sweep_extreme"),
            "entry_sweep_extreme": thesis.get("entry_sweep_extreme"),
        }

    def _bucket_for_price(self, buckets: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
        if price <= 0:
            return None
        for bucket in buckets:
            low = float(bucket.get("price_low", 0.0))
            high = float(bucket.get("price_high", 0.0))
            if low <= price < high:
                return bucket
        return None

    def _observe_zone(
        self,
        symbol: str,
        side: str,
        bucket: dict[str, Any],
        now: float,
        max_age: float,
    ) -> dict[str, Any]:
        key = f"{side}:{bucket.get('label')}"
        zones = self.zone_observations.setdefault(symbol, {})
        existing = zones.get(key)
        if not existing or now - float(existing.get("first_ts", now)) > max_age:
            existing = {
                "first_ts": now,
                "last_ts": now,
                "touches": 0,
                "label": bucket.get("label"),
                "price_low": float(bucket.get("price_low", 0.0)),
                "price_high": float(bucket.get("price_high", 0.0)),
            }
        min_gap = 8.0
        if now - float(existing.get("last_touch_ts", 0.0)) >= min_gap:
            existing["touches"] = int(existing.get("touches", 0)) + 1
            existing["last_touch_ts"] = now
        existing["last_ts"] = now
        zones[key] = existing

        for old_key, old_value in list(zones.items()):
            if now - float(old_value.get("last_ts", now)) > max_age:
                zones.pop(old_key, None)
        return existing

    def _select_target(self, side: str, features: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(features["symbol"])
        mid = float(features["mid"])
        min_distance_bps = float(config.get("thesis", {}).get("min_target_distance_bps", {}).get(symbol, 30.0))
        memory_cfg = config.get("liquidity_memory", {})
        memory_weight = float(memory_cfg.get("target_memory_weight", 0.35)) if memory_cfg.get("enabled", True) else 0.0
        candidates = []
        for zone in features.get("major_liquidity_zones") or []:
            low = float(zone.get("price_low", 0.0))
            high = float(zone.get("price_high", 0.0))
            zone_side = str(zone.get("side", ""))
            if side == "BUY":
                valid_side = zone_side == "resistance" and low > mid
                target_price = low
            else:
                valid_side = zone_side == "support" and high < mid
                target_price = high
            if not valid_side:
                continue
            distance_bps = abs(target_price - mid) / mid * 10000.0 if mid else 0.0
            if distance_bps < min_distance_bps:
                continue
            candidates.append(
                {
                    "price": target_price,
                    "label": zone.get("label", f"{low:.4f}-{high:.4f}"),
                    "distance_bps": distance_bps,
                    "notional": float(zone.get("notional", 0.0)),
                    "strength": float(zone.get("strength", zone.get("score", 0.0))),
                    "memory_score": float(zone.get("memory_score", 0.0)),
                    "persistence_seconds": 0.0,
                    "pull_rate": 0.0,
                    "pull_count": 0,
                    "traded_through_count": 0,
                    "target_score": float(zone.get("score", 0.0)) + min(10.0, distance_bps * 0.03),
                    "major_zone": True,
                }
            )

        buckets = features.get("book_price_buckets") or []
        for bucket in buckets:
            low = float(bucket["price_low"])
            high = float(bucket["price_high"])
            if side == "BUY":
                target_price = low
                notional = float(bucket.get("ask_notional", 0.0))
                valid_side = low > mid
            else:
                target_price = high
                notional = float(bucket.get("bid_notional", 0.0))
                valid_side = high < mid
            memory_score = float(bucket.get("memory_score", 0.0))
            pull_count = int(bucket.get("pull_count", 0))
            pull_rate = float(bucket.get("pull_rate", 0.0))
            if not valid_side or (notional <= 0 and memory_score < 45.0):
                continue
            distance_bps = abs(target_price - mid) / mid * 10000.0 if mid else 0.0
            if distance_bps < min_distance_bps:
                continue
            pull_penalty = min(35.0, (pull_count * 5.0) + min(20.0, pull_rate / max(notional, 1.0) * 20.0))
            current_strength = float(bucket.get("strength", 0.0))
            target_score = (
                (current_strength * (1.0 - memory_weight))
                + (memory_score * memory_weight)
                - pull_penalty
            )
            candidates.append(
                {
                    "price": target_price,
                    "label": bucket.get("label", f"{low:.4f}-{high:.4f}"),
                    "distance_bps": distance_bps,
                    "notional": notional,
                    "strength": current_strength,
                    "memory_score": memory_score,
                    "persistence_seconds": float(bucket.get("persistence_seconds", 0.0)),
                    "pull_rate": pull_rate,
                    "pull_count": pull_count,
                    "traded_through_count": int(bucket.get("traded_through_count", 0)),
                    "target_score": target_score,
                }
            )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (float(item["target_score"]), float(item["memory_score"]), -float(item["distance_bps"])))

    def _score_confluence(
        self,
        side: str,
        features: dict[str, Any],
        target: dict[str, Any],
        config: dict[str, Any],
    ) -> tuple[float, dict[str, float], list[str]]:
        symbol = str(features["symbol"])
        max_spread = float(config.get("strategy", {}).get("max_spread_bps", {}).get(symbol, 6.0))
        spread_bps = abs(float(features.get("spread_bps", 0.0)))
        memory_score = float(target.get("memory_score", 0.0))
        pull_penalty = min(8.0, (float(target.get("pull_count", 0)) * 2.0) + min(5.0, float(target.get("pull_rate", 0.0)) / max(float(target.get("notional", 1.0)), 1.0) * 5.0))
        target_liquidity_score = min(
            25.0,
            8.0 + (float(target.get("strength", 0.0)) / 100.0 * 10.0) + (memory_score / 100.0 * 9.0) - pull_penalty,
        )
        scores = {
            "sweep": 20.0,
            "target_liquidity": max(0.0, target_liquidity_score),
            "delta": self._delta_score(side, features),
            "open_interest": self._open_interest_score(side, features),
            "vwap": self._vwap_score(side, features),
            "volume_profile": self._volume_profile_score(side, features, float(target["price"])),
            "spread": max(0.0, 5.0 * (1.0 - (spread_bps / max(max_spread, 0.01)))),
        }
        total = sum(scores.values())
        reasons = [
            f"sweep score {scores['sweep']:.1f}",
            (
                f"target liquidity {target['label']} at {target['distance_bps']:.2f} bps scored "
                f"{scores['target_liquidity']:.1f}; memory {memory_score:.1f}, pulls {int(target.get('pull_count', 0))}"
            ),
            f"delta/OI {scores['delta']:.1f}/{scores['open_interest']:.1f}",
            f"VWAP/profile {scores['vwap']:.1f}/{scores['volume_profile']:.1f}",
            f"spread {spread_bps:.2f} bps scored {scores['spread']:.1f}",
        ]
        return max(0.0, min(100.0, total)), scores, reasons

    def _delta_score(self, side: str, features: dict[str, Any]) -> float:
        imbalance = float(features.get("delta_imbalance", features.get("flow_imbalance", 0.0)))
        divergence = str(features.get("delta_divergence", "none"))
        if side == "BUY":
            score = 15.0 if imbalance > 0.08 else 8.0 if imbalance > -0.08 else 2.0
            if divergence == "bullish_delta_divergence":
                score += 4.0
            if divergence == "bearish_delta_divergence":
                score -= 5.0
        else:
            score = 15.0 if imbalance < -0.08 else 8.0 if imbalance < 0.08 else 2.0
            if divergence == "bearish_delta_divergence":
                score += 4.0
            if divergence == "bullish_delta_divergence":
                score -= 5.0
        return max(0.0, min(15.0, score))

    def _open_interest_score(self, side: str, features: dict[str, Any]) -> float:
        alignment = self._oi_alignment(side, features)
        if alignment == "supporting":
            return 15.0
        if alignment == "diverging":
            return 2.0
        return 8.0

    def _oi_alignment(self, side: str, features: dict[str, Any]) -> str:
        support = str(features.get("open_interest_support", "neutral"))
        if side == "BUY":
            if support.startswith("supports_up"):
                return "supporting"
            if support.startswith("supports_down"):
                return "diverging"
        else:
            if support.startswith("supports_down"):
                return "supporting"
            if support.startswith("supports_up"):
                return "diverging"
        return "neutral"

    def _vwap_score(self, side: str, features: dict[str, Any]) -> float:
        mid = float(features.get("mid", 0.0))
        lower_2 = float(features.get("session_vwap_lower_2", mid))
        upper_2 = float(features.get("session_vwap_upper_2", mid))
        vwap = float(features.get("session_vwap", features.get("vwap", mid)))
        if side == "BUY":
            if lower_2 <= mid <= upper_2:
                return 10.0 if mid >= vwap else 7.0
            return 4.0
        if lower_2 <= mid <= upper_2:
            return 10.0 if mid <= vwap else 7.0
        return 4.0

    def _volume_profile_score(self, side: str, features: dict[str, Any], target_price: float) -> float:
        profile = features.get("session_volume_profile") or features.get("volume_profile") or []
        if not profile:
            return 5.0
        mid = float(features.get("mid", 0.0))
        blockers = []
        for row in profile:
            low = float(row.get("price_low", 0.0))
            high = float(row.get("price_high", 0.0))
            strength = float(row.get("strength", 0.0))
            if side == "BUY" and mid < low < target_price and strength > 80.0:
                blockers.append(row)
            if side == "SELL" and target_price < high < mid and strength > 80.0:
                blockers.append(row)
        return 5.0 if blockers else 10.0

    def _rr(self, side: str, entry: float, stop_loss: float, take_profit: float) -> float:
        risk = entry - stop_loss if side == "BUY" else stop_loss - entry
        reward = take_profit - entry if side == "BUY" else entry - take_profit
        if risk <= 0 or reward <= 0:
            return 0.0
        return reward / risk

    @staticmethod
    def _valid_trade_plan(side: str, entry: float, stop_loss: float, take_profit: float, rr: float) -> bool:
        if entry <= 0 or stop_loss <= 0 or take_profit <= 0 or rr <= 0:
            return False
        if side == "BUY":
            return stop_loss < entry < take_profit
        return take_profit < entry < stop_loss

    def _bps_value(
        self,
        config: dict[str, Any],
        section: str,
        key: str,
        symbol: str,
        default_bps: float,
        price: float,
    ) -> float:
        bps = float(self._symbol_value(config, section, key, symbol, default_bps))
        return price * (bps / 10000.0)

    @staticmethod
    def _symbol_value(config: dict[str, Any], section: str, key: str, symbol: str, default: float) -> float:
        value = config.get(section, {}).get(key, default)
        if isinstance(value, dict):
            return float(value.get(symbol, default))
        return float(value)
