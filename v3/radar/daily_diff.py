"""Shopify store daily-diff radar.

Hits the public products.json on watched stores (a public Shopify endpoint
exposed by every theme by default) and detects newly-added products.

New products are auto-inserted into v3 with source='radar' and queued for
evaluation on next scan.

Usage:
  v3 radar add <domain>      # add to watch list
  v3 radar check             # diff all enabled competitors now
  v3 radar list              # show watched stores
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import requests

from .. import db


def add_competitor(domain: str, tags: str = "") -> int:
    domain = domain.lower().lstrip("https://").lstrip("http://").rstrip("/")
    with db.conn() as c:
        row = c.execute("SELECT id FROM competitor_watch WHERE shop_domain = ?", (domain,)).fetchone()
        if row:
            c.execute("UPDATE competitor_watch SET enabled = 1, tags = ? WHERE id = ?", (tags, row["id"]))
            return row["id"]
        cur = c.execute(
            "INSERT INTO competitor_watch(shop_domain, tags, added_at) VALUES(?,?,?)",
            (domain, tags, db.now_iso()),
        )
        return cur.lastrowid


def list_competitors() -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM competitor_watch WHERE enabled = 1 ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_store_products(domain: str, limit: int = 250) -> list[dict[str, Any]]:
    """Hit https://{domain}/products.json (public Shopify endpoint)."""
    url = f"https://{domain}/products.json?limit={limit}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 v3-radar"})
    if r.status_code >= 400:
        raise RuntimeError(f"{domain}: HTTP {r.status_code}")
    payload = r.json()
    return payload.get("products", [])


def diff_and_enqueue(domain: str) -> dict[str, int]:
    """Detect new products on this store; insert them into competitor_new_products."""
    products = fetch_store_products(domain)
    new_count = 0
    with db.conn() as c:
        comp = c.execute(
            "SELECT id, last_checked_at FROM competitor_watch WHERE shop_domain = ?",
            (domain,),
        ).fetchone()
        if not comp:
            raise ValueError(f"{domain} not in watch list — run `v3 radar add {domain}` first")
        last_checked = comp["last_checked_at"]
        for p in products:
            handle = p.get("handle")
            if not handle:
                continue
            created = p.get("created_at") or p.get("published_at") or ""
            is_new = (not last_checked) or (created > last_checked)
            if not is_new:
                continue
            existing = c.execute(
                "SELECT id FROM competitor_new_products WHERE competitor_id = ? AND product_handle = ?",
                (comp["id"], handle),
            ).fetchone()
            if existing:
                continue
            price = None
            variants = p.get("variants") or []
            if variants:
                try:
                    price = float(variants[0].get("price") or 0)
                except (TypeError, ValueError):
                    price = None
            c.execute(
                """INSERT INTO competitor_new_products(
                       competitor_id, product_handle, title, price_usd, detected_at)
                   VALUES(?,?,?,?,?)""",
                (comp["id"], handle, p.get("title"), price, db.now_iso()),
            )
            new_count += 1
        c.execute(
            "UPDATE competitor_watch SET last_checked_at = ?, products_seen_count = ? WHERE id = ?",
            (db.now_iso(), len(products), comp["id"]),
        )
    return {"domain": domain, "store_total": len(products), "new_detected": new_count}


def check_all() -> list[dict[str, int]]:
    """Diff every enabled competitor; return per-store summary."""
    summaries = []
    for comp in list_competitors():
        try:
            summaries.append(diff_and_enqueue(comp["shop_domain"]))
        except Exception as e:
            summaries.append({"domain": comp["shop_domain"], "error": f"{type(e).__name__}: {e}"})
    return summaries


def pending_new_products(limit: int = 100) -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute(
            """SELECT np.*, cw.shop_domain
               FROM competitor_new_products np JOIN competitor_watch cw ON cw.id = np.competitor_id
               WHERE np.processed = 0
               ORDER BY np.detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
