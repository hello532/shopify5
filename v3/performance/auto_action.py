"""Auto-action engine — read ad_performance + decision.test_plan_json → emit auto_actions.

Rules (from config.test_plan):
  KILL if day 3 ROAS < kill_day3_roas
  KILL if day 7 ROAS < kill_day7_roas
  SCALE if day 7 ROAS >= scale_day7_roas and not yet scaled today
  HOLD otherwise

Action rows are written to auto_actions with applied=0. A separate executor
can pick them up and call meta_ads_api.pause_ad / update_adset_budget.
"""
from __future__ import annotations

import json
from typing import Any

from .. import config, db


def evaluate_all_active() -> dict[str, int]:
    """Walk every GO_TEST decision with performance data and emit actions."""
    tp_cfg = config.get("test_plan", {})
    kill_d3 = tp_cfg.get("kill_day3_roas", 1.5)
    kill_d7 = tp_cfg.get("kill_day7_roas", 2.0)
    scale_d7 = tp_cfg.get("scale_day7_roas", 3.0)
    scale_step = tp_cfg.get("scale_step_pct", 0.20)

    counters = {"emitted": 0, "kill": 0, "scale": 0, "hold": 0}
    with db.conn() as c:
        decisions = c.execute(
            """SELECT d.*, p.id AS pid FROM decisions d
               JOIN products p ON p.id = d.product_id
               WHERE d.decision='GO_TEST'
                 AND d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)"""
        ).fetchall()
        for d in decisions:
            perf = c.execute(
                """SELECT date_iso, day_index, ad_id, ad_set_id,
                          SUM(spend_usd) AS spend, SUM(revenue_usd) AS rev,
                          SUM(purchases) AS purchases,
                          AVG(roas) AS avg_roas
                   FROM ad_performance
                   WHERE decision_id = ? OR product_id = ?
                   GROUP BY date_iso, ad_id
                   ORDER BY date_iso ASC""",
                (d["id"], d["pid"]),
            ).fetchall()
            if not perf:
                continue
            # Aggregate by ad
            per_ad: dict[str, dict[str, Any]] = {}
            for row in perf:
                aid = row["ad_id"] or "_aggregate"
                slot = per_ad.setdefault(aid, {
                    "ad_set_id": row["ad_set_id"],
                    "spend": 0.0, "revenue": 0.0, "purchases": 0,
                    "days": [], "roas_per_day": [],
                })
                slot["spend"] += row["spend"] or 0
                slot["revenue"] += row["rev"] or 0
                slot["purchases"] += row["purchases"] or 0
                slot["days"].append((row["day_index"], row["date_iso"]))
                if row["avg_roas"] is not None:
                    slot["roas_per_day"].append((row["day_index"], row["avg_roas"]))
            for aid, s in per_ad.items():
                days_run = max([d[0] or 0 for d in s["days"]] or [0])
                cum_roas = (s["revenue"] / s["spend"]) if s["spend"] else 0
                action, reason, observed, threshold, suggested = _decide(
                    days_run, cum_roas, s, kill_d3, kill_d7, scale_d7, scale_step,
                )
                if action == "HOLD":
                    counters["hold"] += 1
                    continue
                _emit_action(c, d, aid, s, action, reason, observed, threshold, suggested)
                counters["emitted"] += 1
                counters[action.lower()] = counters.get(action.lower(), 0) + 1
    return counters


def _decide(days_run: int, cum_roas: float, slot: dict[str, Any],
            kill_d3: float, kill_d7: float, scale_d7: float, scale_step: float,
            ) -> tuple[str, str, str, str, float | None]:
    if days_run >= 7 and cum_roas < kill_d7:
        return ("KILL", "day7_roas_below_kill",
                f"roas={cum_roas:.2f} after {days_run}d",
                f"<{kill_d7}",
                None)
    if 3 <= days_run < 7 and cum_roas < kill_d3:
        return ("KILL", "day3_roas_below_kill",
                f"roas={cum_roas:.2f} after {days_run}d",
                f"<{kill_d3}",
                None)
    if days_run >= 7 and cum_roas >= scale_d7:
        # Suggested new daily budget = current * (1+step). We don't know current here,
        # so suggested_value is the *step multiplier*, executor reads current then multiplies.
        return ("SCALE", "day7_roas_above_scale",
                f"roas={cum_roas:.2f} sustained",
                f">={scale_d7}",
                1.0 + scale_step)
    return ("HOLD", "within_test_window",
            f"roas={cum_roas:.2f} d{days_run}",
            f"{kill_d3} <= roas (d3) / >={scale_d7} (d7)",
            None)


def _emit_action(c, decision_row, ad_id: str, slot: dict[str, Any],
                 action: str, reason: str, observed: str, threshold: str,
                 suggested: float | None) -> None:
    # De-dupe: don't emit same action twice in 24h for same ad
    existing = c.execute(
        """SELECT id FROM auto_actions
           WHERE product_id = ? AND ad_id = ? AND action = ?
             AND triggered_at >= datetime('now','-1 day')""",
        (decision_row["pid"], ad_id, action),
    ).fetchone()
    if existing:
        return
    c.execute(
        """INSERT INTO auto_actions(
               decision_id, product_id, ad_id, action, reason,
               metric_observed, metric_threshold, suggested_value, triggered_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (decision_row["id"], decision_row["pid"], ad_id if ad_id != "_aggregate" else None,
         action, reason, observed, threshold, suggested, db.now_iso()),
    )


def pending_actions(limit: int = 200) -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute(
            """SELECT aa.*, p.title, p.shop_domain
               FROM auto_actions aa JOIN products p ON p.id = aa.product_id
               WHERE aa.applied = 0
               ORDER BY aa.triggered_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def apply_action(action_id: int, dry_run: bool = True) -> dict[str, Any]:
    """Execute one pending action against Meta API (or simulate)."""
    with db.conn() as c:
        a = c.execute("SELECT * FROM auto_actions WHERE id = ?", (action_id,)).fetchone()
        if not a:
            return {"ok": False, "error": "not found"}
        if a["applied"]:
            return {"ok": False, "error": "already applied"}
    if dry_run:
        return {"ok": True, "dry_run": True, "would": dict(a)}

    from . import meta_ads_api
    try:
        if a["action"] in ("KILL", "PAUSE") and a["ad_id"]:
            meta_ads_api.pause_ad(a["ad_id"])
        elif a["action"] == "SCALE":
            # Need to fetch current budget first; placeholder pseudocode
            # current = meta_ads_api.get_adset_budget(a["ad_set_id"])
            # new_budget = int(current * (a["suggested_value"] or 1.2))
            # meta_ads_api.update_adset_budget(a["ad_set_id"], new_budget)
            raise RuntimeError("SCALE requires current adset budget — wire executor end")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    with db.conn() as c:
        c.execute(
            "UPDATE auto_actions SET applied = 1, applied_at = ? WHERE id = ?",
            (db.now_iso(), action_id),
        )
    return {"ok": True, "applied": True}
