"""Three-layer attribution: Pixel(平台层) + CAPI(服务层) + Post-purchase Survey(自报层).

This module generates the survey template + ingests responses, matching them
against Pixel-tracked sessions to compute Total-Impact-style channel credit
(à la Triple Whale's PPS model).
"""
from __future__ import annotations

import json
from typing import Any

from .. import db


DEFAULT_SURVEY = {
    "questions": [
        {
            "id": "where_heard",
            "text": "Where did you first hear about us?",
            "type": "single_select",
            "options": [
                "Facebook ad",
                "Instagram ad",
                "TikTok",
                "YouTube",
                "Friend / Family",
                "Google",
                "ChatGPT / AI assistant",
                "Email",
                "Other",
            ],
            "shuffle": True,  # avoid order bias
        },
        {
            "id": "main_motivation",
            "text": "What made you decide to buy today?",
            "type": "single_select",
            "options": [
                "Solved my [problem]",
                "Price / discount",
                "Reviews / social proof",
                "Friend recommendation",
                "Just felt right",
                "Other",
            ],
        },
    ],
    "settings": {
        "show_after_checkout": True,
        "max_questions": 2,
        "skip_threshold": 0.85,  # show on 85% of orders to keep response rate up
    },
}


def survey_template() -> dict[str, Any]:
    return DEFAULT_SURVEY


def ingest_response(order_id: str, customer_email: str | None,
                    reported_source: str, revenue_usd: float,
                    matched_pixel_source: str | None = None,
                    product_id: int | None = None,
                    raw: dict[str, Any] | None = None) -> int:
    with db.conn() as c:
        cur = c.execute(
            """INSERT INTO attribution_survey(
                   product_id, order_id, customer_email, reported_source,
                   matched_pixel_source, revenue_usd, submitted_at, raw_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (product_id, order_id, customer_email, reported_source,
             matched_pixel_source, revenue_usd, db.now_iso(),
             json.dumps(raw or {}, ensure_ascii=False)),
        )
        return cur.lastrowid


def channel_credit_total_impact(days: int = 14) -> dict[str, dict[str, float]]:
    """Triple-Whale-style Total Impact: weight survey + pixel together.

    For each channel, return revenue credit, share, and survey vs pixel deltas.
    """
    with db.conn() as c:
        rows = c.execute(
            f"""SELECT reported_source, matched_pixel_source, revenue_usd
                FROM attribution_survey
                WHERE submitted_at >= datetime('now','-{int(days)} days')"""
        ).fetchall()
    survey_rev: dict[str, float] = {}
    pixel_rev: dict[str, float] = {}
    for r in rows:
        survey_rev[r["reported_source"] or "?"] = survey_rev.get(r["reported_source"] or "?", 0) + (r["revenue_usd"] or 0)
        if r["matched_pixel_source"]:
            pixel_rev[r["matched_pixel_source"]] = pixel_rev.get(r["matched_pixel_source"], 0) + (r["revenue_usd"] or 0)
    total_survey = sum(survey_rev.values()) or 1
    total_pixel = sum(pixel_rev.values()) or 1
    channels = set(survey_rev) | set(pixel_rev)
    out: dict[str, dict[str, float]] = {}
    for ch in channels:
        sv = survey_rev.get(ch, 0)
        px = pixel_rev.get(ch, 0)
        # Total Impact: average of share-of-survey and share-of-pixel
        share = (sv / total_survey + px / total_pixel) / 2
        out[ch] = {
            "survey_revenue": sv,
            "pixel_revenue": px,
            "share_total_impact": share,
            "dark_social_index": (sv - px) / max(sv, 1),  # positive = pixel under-attributes
        }
    return out
