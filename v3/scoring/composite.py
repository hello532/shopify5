"""Composite score = weighted sum of 4 signal scores."""
from __future__ import annotations

from typing import Any

from .. import config, db
from .._base_local_helpers import explain_writer
from ..signals import SignalResult


def compute(pid: int, signals: dict[str, SignalResult]) -> dict[str, Any]:
    weights = config.get("scoring.weights", {"fb": 0.35, "profit": 0.30, "trend": 0.20, "lp": 0.15})
    # Failed signals contribute 0 to score (not None — but we explicitly flag failures elsewhere)
    fb = _score_or_zero(signals.get("fb_ads"))
    profit = _score_or_zero(signals.get("profit"))
    trend = _score_or_zero(signals.get("trends"))
    lp = _score_or_zero(signals.get("lp"))

    composite = (
        weights.get("fb", 0.35) * fb
        + weights.get("profit", 0.30) * profit
        + weights.get("trend", 0.20) * trend
        + weights.get("lp", 0.15) * lp
    )

    # PR8: apply winning-pattern boost (learned from history)
    boost = 1.0
    try:
        from ..learn import patterns as _patterns
        from .. import db as _db
        with _db.conn() as _c:
            p = _c.execute("SELECT price_usd, category FROM products WHERE id = ?", (pid,)).fetchone()
            lp_row = _c.execute(
                "SELECT awareness_match_layer FROM lp_signals WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                (pid,),
            ).fetchone()
        if p:
            boost = _patterns.boost_for_product(
                p["price_usd"],
                p["category"],
                lp_row["awareness_match_layer"] if lp_row else None,
            )
    except Exception:
        boost = 1.0

    composite = round(min(100.0, composite * boost), 2)
    if abs(boost - 1.0) > 0.001:
        explain_writer.queue(pid, "composite", "winning_pattern_boost",
                             "1.0 (baseline)", f"{boost:.2f}", True,
                             f"learned-pattern multiplier applied")

    explain_writer.queue(pid, "composite", "weighted_sum",
                         f"weights={weights}",
                         f"fb={fb} profit={profit} trend={trend} lp={lp}",
                         True,
                         f"composite={composite}")

    # Flush queued explain entries to DB
    items = explain_writer.drain()
    if items:
        scored_at = db.now_iso()
        with db.conn() as c:
            for (product_id, signal, rule, threshold, observed, passed, note) in items:
                c.execute(
                    """INSERT INTO explain_log
                         (product_id, scored_at, signal, rule, threshold, observed, passed, note)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (product_id, scored_at, signal, rule,
                     str(threshold) if threshold is not None else None,
                     str(observed) if observed is not None else None,
                     1 if passed else 0,
                     note),
                )

    return {
        "fb_score": fb,
        "profit_score": profit,
        "trend_score": trend,
        "lp_score": lp,
        "composite": composite,
        "weights": weights,
    }


def _score_or_zero(r: SignalResult | None) -> float:
    if r is None or r.score is None:
        return 0.0
    return float(r.score)
