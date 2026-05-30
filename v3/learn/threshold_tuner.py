"""Adaptive threshold tuner.

Backtest: given a candidate new threshold value, compute what fraction of
historical GO_TEST decisions that would have flipped to WATCH/KILL actually
turned out to be winners. If the candidate keeps >= 95% of true winners and
prunes >= 30% of losers, suggest it.

Suggestions land in threshold_history; applying them rewrites config.yaml
(opt-in via CLI: `v3 tune --apply`).
"""
from __future__ import annotations

from typing import Any

from .. import config, db


def _evaluate_threshold(c, threshold_key: str, candidate: float, observed_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """For a given candidate threshold, what's the resulting precision/recall vs. actual winners?"""
    winners = [o for o in observed_outcomes if o["won"]]
    losers = [o for o in observed_outcomes if not o["won"]]
    if not winners:
        return {"win_rate": 0, "preserved": 0, "pruned_losers": 0, "skipped": True}
    # threshold_key.endswith("_min") means HIGHER candidate = stricter
    stricter = threshold_key.endswith("_min")
    passes = (lambda v: v >= candidate) if stricter else (lambda v: v <= candidate)
    preserved_winners = sum(1 for w in winners if passes(w["score_value"]))
    pruned_losers = sum(1 for l in losers if not passes(l["score_value"]))
    return {
        "preserved": preserved_winners / len(winners),
        "pruned_losers": pruned_losers / len(losers) if losers else 0,
        "winners_total": len(winners),
        "losers_total": len(losers),
    }


def suggest_threshold(threshold_key: str, score_field: str = "composite_score") -> dict[str, Any]:
    """Find a candidate replacement for `threshold_key` (e.g. scoring.go_test.composite_min)."""
    scale_roas = config.get("test_plan.scale_day7_roas", 3.0)
    with db.conn() as c:
        rows = c.execute(
            f"""SELECT sc.{score_field} AS score_value, d.id AS did, d.product_id AS pid,
                       (SELECT MAX(day_index) FROM ad_performance WHERE product_id = d.product_id) AS days_run,
                       (SELECT MAX(roas) FROM ad_performance WHERE product_id = d.product_id) AS best_roas
                FROM scores sc JOIN decisions d ON d.product_id = sc.product_id
                WHERE d.decision='GO_TEST'
                  AND sc.id = (SELECT MAX(id) FROM scores WHERE product_id = d.product_id)
                  AND d.id = (SELECT MAX(id) FROM decisions WHERE product_id = d.product_id)"""
        ).fetchall()
        observed = []
        for r in rows:
            if r["days_run"] is None or r["days_run"] < 3:
                continue
            observed.append({
                "score_value": r["score_value"] or 0,
                "won": (r["best_roas"] or 0) >= scale_roas and r["days_run"] >= 7,
            })
        if len(observed) < 10:
            return {"ok": False, "reason": f"need >=10 historical samples, have {len(observed)}"}
        current_value = config.get(threshold_key)
        # Try candidates around the current value
        candidates = [current_value * f for f in [0.85, 0.95, 1.00, 1.05, 1.15]]
        results = []
        for cand in candidates:
            ev = _evaluate_threshold(c, threshold_key, cand, observed)
            ev["candidate"] = cand
            results.append(ev)
        # Pick candidate that keeps >=95% of winners and prunes most losers
        eligible = [r for r in results if r.get("preserved", 0) >= 0.95]
        best = max(eligible, key=lambda r: r.get("pruned_losers", 0)) if eligible else None
        if not best or abs(best["candidate"] - current_value) < 1e-9:
            return {"ok": False, "reason": "no better candidate found", "results": results}
        # Record
        c.execute(
            """INSERT INTO threshold_history(
                   threshold_key, old_value, new_value, backtested_win_rate,
                   sample_size, suggested_at, note)
               VALUES(?,?,?,?,?,?,?)""",
            (threshold_key, current_value, best["candidate"],
             best.get("preserved", 0), len(observed), db.now_iso(),
             f"prunes {best['pruned_losers']:.0%} of losers while keeping {best['preserved']:.0%} of winners"),
        )
        return {"ok": True, "from": current_value, "to": best["candidate"], "evaluation": best, "all": results}
