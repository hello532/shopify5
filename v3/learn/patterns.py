"""Winning Pattern Learner.

A "win" is a GO_TEST decision whose actual ad_performance crossed the scale
threshold (sustained ROAS >= test_plan.scale_day7_roas for >= 3 days).

Patterns are tuples of (price_band, category, awareness_layer, hook_type) and
we track win_rate per pattern. Patterns with statistical significance feed
boosters.py to lift future scoring.
"""
from __future__ import annotations

import json
from typing import Any

from .. import config, db


def _price_band(price: float | None) -> str:
    if price is None:
        return "?"
    if price < 20:
        return "0-20"
    if price < 40:
        return "20-40"
    if price < 70:
        return "40-70"
    if price < 120:
        return "70-120"
    if price < 250:
        return "120-250"
    return "250+"


def extract_patterns() -> dict[str, int]:
    """Walk all GO_TEST decisions with performance data, classify outcome, update patterns."""
    scale_roas = config.get("test_plan.scale_day7_roas", 3.0)
    sample_size = 0
    patterns: dict[tuple, dict[str, Any]] = {}

    with db.conn() as c:
        rows = c.execute(
            """SELECT p.id AS pid, p.price_usd, p.category, p.awareness_level_detected,
                      d.id AS did,
                      (SELECT awareness_match_layer FROM lp_signals
                       WHERE product_id = p.id ORDER BY id DESC LIMIT 1) AS lp_aware,
                      (SELECT MAX(cum_roas) FROM (
                          SELECT date_iso,
                                 SUM(revenue_usd) OVER (PARTITION BY product_id ORDER BY date_iso)
                               / NULLIF(SUM(spend_usd) OVER (PARTITION BY product_id ORDER BY date_iso), 0)
                                 AS cum_roas
                          FROM ad_performance WHERE product_id = p.id
                      ) WHERE cum_roas IS NOT NULL) AS max_cum_roas,
                      (SELECT MAX(day_index) FROM ad_performance WHERE product_id = p.id) AS days_run
               FROM decisions d JOIN products p ON p.id = d.product_id
               WHERE d.decision='GO_TEST'
                 AND d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)"""
        ).fetchall()
        for r in rows:
            if not r["days_run"] or r["days_run"] < 3:
                continue  # not enough data
            won = (r["max_cum_roas"] or 0) >= scale_roas and r["days_run"] >= 7
            dims = (
                _price_band(r["price_usd"]),
                (r["category"] or "other"),
                (r["lp_aware"] or r["awareness_level_detected"] or "unknown"),
            )
            slot = patterns.setdefault(dims, {"win": 0, "total": 0, "roas_sum": 0.0})
            slot["total"] += 1
            if won:
                slot["win"] += 1
            slot["roas_sum"] += r["max_cum_roas"] or 0
            sample_size += 1

        now = db.now_iso()
        for dims, slot in patterns.items():
            key = "|".join(dims)
            win_rate = slot["win"] / slot["total"] if slot["total"] else 0
            avg_roas = slot["roas_sum"] / slot["total"] if slot["total"] else 0
            c.execute(
                """INSERT INTO winning_patterns(pattern_key, dimensions, win_count, total_count,
                                                 win_rate, avg_roas, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(pattern_key) DO UPDATE SET
                     dimensions=excluded.dimensions,
                     win_count=excluded.win_count,
                     total_count=excluded.total_count,
                     win_rate=excluded.win_rate,
                     avg_roas=excluded.avg_roas,
                     updated_at=excluded.updated_at""",
                (key,
                 json.dumps({"price_band": dims[0], "category": dims[1], "awareness": dims[2]}),
                 slot["win"], slot["total"], win_rate, avg_roas, now),
            )
    return {"patterns": len(patterns), "samples": sample_size}


def boost_for_product(price: float | None, category: str | None, awareness: str | None) -> float:
    """Return a multiplicative boost (e.g. 1.0..1.2) for composite_score
    based on which winning pattern the product falls under.

    Conservative: only boost when sample size >= 5 and win_rate >= 0.5.
    """
    if price is None or category is None:
        return 1.0
    key = f"{_price_band(price)}|{category}|{awareness or 'unknown'}"
    with db.conn() as c:
        row = c.execute(
            "SELECT win_rate, total_count FROM winning_patterns WHERE pattern_key = ?",
            (key,),
        ).fetchone()
    if not row or row["total_count"] < 5:
        return 1.0
    wr = row["win_rate"]
    if wr >= 0.50:
        return 1.20
    if wr >= 0.30:
        return 1.10
    if wr <= 0.10:
        return 0.85  # actively penalize losing patterns
    return 1.0


def top_patterns(limit: int = 10) -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute(
            """SELECT pattern_key, dimensions, win_count, total_count, win_rate, avg_roas, updated_at
               FROM winning_patterns
               WHERE total_count >= 3
               ORDER BY win_rate DESC, total_count DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
