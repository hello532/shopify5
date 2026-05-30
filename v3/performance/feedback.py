"""Persist Meta Ads API insights into v3 SQLite.

Match strategy: ad_name encodes v3 product_id (we emit ads_manager_paste.json
with name pattern `v3-test-{product_id}-...`).
Falls back to fuzzy domain match against landing_url for ads named differently.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .. import db

V3_NAME_PATTERN = re.compile(r"v3-test-(\d+)", re.I)


def ingest_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert ad_performance rows. Returns counters."""
    inserted = 0
    matched_pid = 0
    unmatched = 0
    now = db.now_iso()
    with db.conn() as c:
        for row in rows:
            pid, did = _resolve_product_and_decision(c, row)
            if pid:
                matched_pid += 1
            else:
                unmatched += 1
                continue  # cannot store without product anchor
            day_index = _day_index_for_decision(c, did, row.get("date_iso"))
            c.execute(
                """INSERT INTO ad_performance(
                       decision_id, product_id, campaign_id, ad_set_id, ad_id, creative_id,
                       day_index, date_iso, spend_usd, impressions, clicks,
                       add_to_cart, purchases, revenue_usd, roas, cpa, ctr, cpc, cpm,
                       captured_at, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    did, pid,
                    row.get("campaign_id"), row.get("ad_set_id"), row.get("ad_id"),
                    None,  # creative_id not in default field set; can be added
                    day_index,
                    row.get("date_iso"),
                    row.get("spend_usd"),
                    row.get("impressions"),
                    row.get("clicks"),
                    row.get("add_to_cart"),
                    row.get("purchases"),
                    row.get("revenue_usd"),
                    row.get("roas"),
                    row.get("cpa"),
                    row.get("ctr"),
                    row.get("cpc"),
                    row.get("cpm"),
                    now,
                    json.dumps(row.get("raw"), ensure_ascii=False, default=str),
                ),
            )
            inserted += 1
    return {"inserted": inserted, "matched": matched_pid, "unmatched": unmatched}


def _resolve_product_and_decision(c, row: dict[str, Any]) -> tuple[int | None, int | None]:
    ad_name = row.get("ad_name") or ""
    m = V3_NAME_PATTERN.search(ad_name)
    if m:
        pid = int(m.group(1))
        prow = c.execute("SELECT id FROM products WHERE id = ?", (pid,)).fetchone()
        if prow:
            drow = c.execute(
                "SELECT id FROM decisions WHERE product_id = ? AND decision='GO_TEST' ORDER BY id DESC LIMIT 1",
                (pid,),
            ).fetchone()
            return pid, (drow["id"] if drow else None)
    # Fallback: by landing_url substring match against ad_name (e.g. "shop.example.com/products/foo")
    candidates = c.execute(
        """SELECT id, landing_url, product_url FROM products
           WHERE landing_url IS NOT NULL OR product_url IS NOT NULL"""
    ).fetchall()
    name_lc = ad_name.lower()
    for p in candidates:
        for url in (p["landing_url"], p["product_url"]):
            if url and url.split("/")[-1].lower() in name_lc:
                return p["id"], None
    return None, None


def _day_index_for_decision(c, did: int | None, date_iso: str | None) -> int | None:
    if not did or not date_iso:
        return None
    row = c.execute("SELECT decided_at FROM decisions WHERE id = ?", (did,)).fetchone()
    if not row:
        return None
    try:
        decided = datetime.fromisoformat(row["decided_at"].replace("Z", "+00:00"))
        cur = datetime.fromisoformat(date_iso + "T00:00:00+00:00")
        return max(1, (cur - decided.replace(tzinfo=cur.tzinfo)).days + 1)
    except Exception:
        return None
