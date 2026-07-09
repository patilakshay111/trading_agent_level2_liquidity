from __future__ import annotations

from time import time
from typing import Any

from ..state import StateStore


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class LiquidityMemoryEngine:
    def __init__(self, store: StateStore):
        self.store = store

    def update(
        self,
        symbol: str,
        buckets: list[dict[str, Any]],
        mid: float,
        config: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        memory_cfg = config.get("liquidity_memory", {})
        if not memory_cfg.get("enabled", True):
            return {
                "book_price_buckets": buckets,
                "liquidity_memory_buckets": [],
                "memory_liquidity_targets": {},
                "liquidity_memory_summary": {"enabled": False},
            }

        now = float(now if now is not None else time())
        retention_seconds = float(memory_cfg.get("retention_minutes", 180.0)) * 60.0
        ema_alpha = float(memory_cfg.get("ema_alpha", 0.18))
        min_notional = float((memory_cfg.get("min_bucket_notional") or {}).get(symbol, 100000.0))

        previous_rows = {
            (str(row["symbol"]), round(float(row["price_low"]), 8)): row
            for row in self.store.load_liquidity_memory(symbol)
        }
        output_rows: list[dict[str, Any]] = []
        enriched_buckets: list[dict[str, Any]] = []

        for bucket in buckets:
            row = self._update_bucket(
                symbol=symbol,
                bucket=bucket,
                previous=previous_rows.get((symbol, round(float(bucket["price_low"]), 8))),
                mid=mid,
                now=now,
                ema_alpha=ema_alpha,
                min_notional=min_notional,
            )
            output_rows.append(row)
            enriched_buckets.append({**bucket, **self._public_fields(row, mid)})

        self.store.save_liquidity_memory(output_rows)
        self.store.prune_liquidity_memory(now - retention_seconds)

        remembered = self._remembered_rows(symbol, now, retention_seconds, output_rows)
        targets = self._targets(remembered, mid, memory_cfg, now)
        return {
            "book_price_buckets": enriched_buckets,
            "liquidity_memory_buckets": [self._public_fields(row, mid) for row in remembered],
            "memory_liquidity_targets": targets,
            "liquidity_memory_summary": {
                "enabled": True,
                "rows": len(remembered),
                "min_bucket_notional": min_notional,
                "retention_seconds": retention_seconds,
            },
        }

    def _update_bucket(
        self,
        symbol: str,
        bucket: dict[str, Any],
        previous: dict[str, Any] | None,
        mid: float,
        now: float,
        ema_alpha: float,
        min_notional: float,
    ) -> dict[str, Any]:
        price_low = float(bucket["price_low"])
        price_high = float(bucket["price_high"])
        bid_notional = float(bucket.get("bid_notional", 0.0))
        ask_notional = float(bucket.get("ask_notional", 0.0))
        current = bid_notional + ask_notional

        if previous:
            first_seen = float(previous["first_seen"])
            last_seen = float(previous["last_seen"])
            sample_count = int(previous["sample_count"]) + 1
            old_current = float(previous.get("current_notional", previous.get("previous_notional", 0.0)))
            old_ema = float(previous.get("ema_notional", 0.0))
            old_max = float(previous.get("max_notional_seen", 0.0))
            old_pull_count = int(previous.get("pull_count", 0))
            old_last_pull_ts = previous.get("last_pull_ts")
            old_traded_through = int(previous.get("traded_through_count", 0))
            last_mid = previous.get("last_mid")
        else:
            first_seen = now
            last_seen = now
            sample_count = 1
            old_current = 0.0
            old_ema = current
            old_max = current
            old_pull_count = 0
            old_last_pull_ts = None
            old_traded_through = 0
            last_mid = None

        dt = max(1.0, now - last_seen)
        stack_rate = max(0.0, current - old_current) / dt
        pull_rate = max(0.0, old_current - current) / dt
        pulled = old_current >= min_notional and current <= max(min_notional * 0.25, old_current * 0.55)
        pull_count = old_pull_count + (1 if pulled else 0)
        last_pull_ts = now if pulled else old_last_pull_ts
        traded_through = old_traded_through + (1 if self._crossed_bucket(last_mid, mid, price_low, price_high) else 0)
        ema_notional = (ema_alpha * current) + ((1.0 - ema_alpha) * old_ema)
        max_seen = max(old_max, current)
        persistence_seconds = max(0.0, now - first_seen)
        absorption_score = self._absorption_score(mid, price_low, price_high, current, min_notional, traded_through)
        memory_score = self._memory_score(
            current=current,
            max_seen=max_seen,
            ema=ema_notional,
            persistence_seconds=persistence_seconds,
            min_notional=min_notional,
            pull_count=pull_count,
            pull_rate=pull_rate,
            traded_through=traded_through,
            absorption_score=absorption_score,
        )

        return {
            "symbol": symbol,
            "price_low": price_low,
            "price_high": price_high,
            "label": str(bucket.get("label") or f"{price_low:.0f}-{price_high:.0f}"),
            "first_seen": first_seen,
            "last_seen": now,
            "sample_count": sample_count,
            "current_bid_notional": bid_notional,
            "current_ask_notional": ask_notional,
            "current_notional": current,
            "max_notional_seen": max_seen,
            "ema_notional": ema_notional,
            "previous_notional": old_current,
            "stack_rate": stack_rate,
            "pull_rate": pull_rate,
            "pull_count": pull_count,
            "last_pull_ts": last_pull_ts,
            "traded_through_count": traded_through,
            "last_mid": mid,
            "absorption_score": absorption_score,
            "memory_score": memory_score,
        }

    @staticmethod
    def _crossed_bucket(last_mid: Any, mid: float, low: float, high: float) -> bool:
        if last_mid is None:
            return False
        previous = float(last_mid)
        crossed_up = previous < low and mid > high
        crossed_down = previous > high and mid < low
        entered_from_outside = (previous < low or previous > high) and low <= mid <= high
        return crossed_up or crossed_down or entered_from_outside

    @staticmethod
    def _absorption_score(mid: float, low: float, high: float, current: float, min_notional: float, traded_through: int) -> float:
        if not (low <= mid <= high) or current <= 0:
            return 0.0
        size_score = _clamp((current / max(min_notional, 1.0)) * 55.0)
        hold_score = 20.0 if traded_through == 0 else 8.0
        return _clamp(size_score + hold_score)

    @staticmethod
    def _memory_score(
        current: float,
        max_seen: float,
        ema: float,
        persistence_seconds: float,
        min_notional: float,
        pull_count: int,
        pull_rate: float,
        traded_through: int,
        absorption_score: float,
    ) -> float:
        threshold = max(min_notional, 1.0)
        size_score = _clamp((max_seen / threshold) * 30.0, 0.0, 30.0)
        ema_score = _clamp((ema / threshold) * 22.0, 0.0, 22.0)
        current_score = _clamp((current / threshold) * 12.0, 0.0, 12.0)
        persistence_score = _clamp((persistence_seconds / 900.0) * 28.0, 0.0, 28.0)
        traded_bonus = _clamp(traded_through * 2.0, 0.0, 6.0)
        absorption_bonus = _clamp(absorption_score * 0.08, 0.0, 6.0)
        pull_penalty = _clamp((pull_count * 7.0) + ((pull_rate / threshold) * 12.0), 0.0, 35.0)
        return round(_clamp(size_score + ema_score + current_score + persistence_score + traded_bonus + absorption_bonus - pull_penalty), 2)

    @staticmethod
    def _public_fields(row: dict[str, Any], mid: float) -> dict[str, Any]:
        low = float(row["price_low"])
        high = float(row["price_high"])
        distance_bps = abs(((low + high) / 2.0) - mid) / mid * 10000.0 if mid else 0.0
        persistence = max(0.0, float(row["last_seen"]) - float(row["first_seen"]))
        marker = "stacking" if float(row["stack_rate"]) > float(row["pull_rate"]) and float(row["stack_rate"]) > 0 else ""
        if float(row["pull_rate"]) > float(row["stack_rate"]) and float(row["pull_rate"]) > 0:
            marker = "pulling"
        return {
            "price_low": round(low, 4),
            "price_high": round(high, 4),
            "label": row["label"],
            "current_notional": round(float(row["current_notional"]), 4),
            "max_notional_seen": round(float(row["max_notional_seen"]), 4),
            "ema_notional": round(float(row["ema_notional"]), 4),
            "memory_score": round(float(row["memory_score"]), 2),
            "persistence_seconds": round(persistence, 1),
            "sample_count": int(row["sample_count"]),
            "stack_rate": round(float(row["stack_rate"]), 4),
            "pull_rate": round(float(row["pull_rate"]), 4),
            "pull_count": int(row["pull_count"]),
            "last_pull_ts": row.get("last_pull_ts"),
            "traded_through_count": int(row["traded_through_count"]),
            "absorption_score": round(float(row["absorption_score"]), 2),
            "distance_bps": round(distance_bps, 2),
            "memory_marker": marker,
        }

    def _remembered_rows(
        self,
        symbol: str,
        now: float,
        retention_seconds: float,
        current_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_key = {(symbol, round(float(row["price_low"]), 8)): row for row in current_rows}
        for row in self.store.load_liquidity_memory(symbol):
            if now - float(row["last_seen"]) <= retention_seconds:
                by_key.setdefault((symbol, round(float(row["price_low"]), 8)), row)
        return sorted(by_key.values(), key=lambda row: float(row["price_low"]), reverse=True)

    def _targets(self, rows: list[dict[str, Any]], mid: float, memory_cfg: dict[str, Any], now: float) -> dict[str, Any]:
        pull_decay = float(memory_cfg.get("pull_warning_decay_seconds", 120.0))
        target_weight = float(memory_cfg.get("target_memory_weight", 0.35))
        above = [row for row in rows if float(row["price_low"]) > mid and float(row["memory_score"]) > 0]
        below = [row for row in rows if float(row["price_high"]) < mid and float(row["memory_score"]) > 0]
        recently_pulled = [
            row
            for row in rows
            if row.get("last_pull_ts") is not None and (now - float(row["last_pull_ts"])) <= pull_decay
        ]
        best_above = self._best_target(above, mid, target_weight)
        best_below = self._best_target(below, mid, target_weight)
        best_candidates = [item for item in (best_above, best_below) if item]
        best_target = max(best_candidates, key=lambda item: float(item["target_score"])) if best_candidates else None
        recent_pull = self._public_fields(max(recently_pulled, key=lambda row: float(row["pull_rate"])), mid) if recently_pulled else None
        return {
            "strongest_above": best_above,
            "strongest_below": best_below,
            "recently_pulled": recent_pull,
            "best_target": best_target,
        }

    def _best_target(self, rows: list[dict[str, Any]], mid: float, target_weight: float) -> dict[str, Any] | None:
        if not rows:
            return None

        def score(row: dict[str, Any]) -> float:
            memory = float(row["memory_score"])
            current_strength = _clamp((float(row["current_notional"]) / max(float(row["max_notional_seen"]), 1.0)) * 100.0)
            pull_penalty = min(30.0, float(row["pull_count"]) * 5.0 + float(row["pull_rate"]) / max(float(row["max_notional_seen"]), 1.0) * 20.0)
            return (memory * target_weight) + (current_strength * (1.0 - target_weight)) - pull_penalty

        row = max(rows, key=score)
        payload = self._public_fields(row, mid)
        payload["target_price"] = payload["price_low"] if float(row["price_low"]) > mid else payload["price_high"]
        payload["target_score"] = round(score(row), 2)
        payload["side"] = "above" if float(row["price_low"]) > mid else "below"
        return payload
