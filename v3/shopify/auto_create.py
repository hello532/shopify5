"""Clone v3 GO_TEST product to your own Shopify store as DRAFT.

Pipeline:
  1. Read v3 product + latest decision/scores + creative_variants
  2. Scrape source product page for full detail (images / description / variants)
  3. Build productSet input — DRAFT status, source-attribution metafield
  4. POST GraphQL mutation, log result in shopify_clones table
  5. Refuse to re-clone the same source product to the same destination shop

Env required (or set in config.yaml under `shopify:`):
  SHOPIFY_SHOP                  e.g. "mystore.myshopify.com"
  SHOPIFY_ADMIN_ACCESS_TOKEN    shpat_...

Safety:
  - All products land in DRAFT — never go live without human review
  - Existing clone (same source → same destination) returns the prior result
  - dry_run=True returns the would-be payload without calling Shopify
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import requests

from .. import config, db
from . import source_scraper

API_VERSION = "2026-01"

PRODUCT_SET_MUTATION = """
mutation productSet($synchronous: Boolean = true, $productSet: ProductSetInput!) {
  productSet(synchronous: $synchronous, input: $productSet) {
    product {
      id
      handle
      status
      onlineStoreUrl
      onlineStorePreviewUrl
    }
    productSetOperation { id status }
    userErrors { field message }
  }
}
"""


# ───────────────────────── env / config ─────────────────────────

def _shop() -> str:
    s = os.environ.get("SHOPIFY_SHOP") or config.get("shopify.shop")
    if not s:
        raise RuntimeError("SHOPIFY_SHOP not set (env or config.shopify.shop)")
    return s.rstrip("/")


def _token() -> str:
    t = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or config.get("shopify.admin_token")
    if not t:
        raise RuntimeError("SHOPIFY_ADMIN_ACCESS_TOKEN not set (env or config.shopify.admin_token)")
    return t


def _endpoint() -> str:
    return f"https://{_shop()}/admin/api/{API_VERSION}/graphql.json"


def _admin_url(handle: str | None) -> str:
    if not handle:
        return f"https://{_shop()}/admin/products"
    return f"https://{_shop()}/admin/products?handle={handle}"


# ───────────────────────── existing-clone check ─────────────────────────

def existing_clone(source_product_id: int, shop_destination: str | None = None) -> dict[str, Any] | None:
    """If this source has already been cloned to the destination, return the prior row."""
    shop = shop_destination or (_shop() if (os.environ.get("SHOPIFY_SHOP") or config.get("shopify.shop")) else None)
    if not shop:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM shopify_clones WHERE source_product_id = ? AND shop_destination = ? ORDER BY id DESC LIMIT 1",
            (source_product_id, shop),
        ).fetchone()
    return dict(row) if row else None


# ───────────────────────── main clone function ─────────────────────────

def clone(
    v3_product_id: int,
    *,
    dry_run: bool = False,
    force: bool = False,
    price_override: float | None = None,
    status: str = "DRAFT",
) -> dict[str, Any]:
    """Clone one v3 product to the configured Shopify store.

    Args:
      v3_product_id: id from v3.products
      dry_run:    build payload but do not POST; return what would be sent
      force:      ignore prior clone and push again
      price_override: override the source price (USD)
      status:     "DRAFT" (default) or "ACTIVE"
    """
    # 1. Read v3 product
    with db.conn() as c:
        prow = c.execute("SELECT * FROM products WHERE id = ?", (v3_product_id,)).fetchone()
        if not prow:
            raise ValueError(f"product {v3_product_id} not found")
        product = dict(prow)
        drow = c.execute(
            "SELECT * FROM decisions WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (v3_product_id,),
        ).fetchone()
        variants_rows = c.execute(
            "SELECT * FROM creative_variants WHERE product_id = ? ORDER BY id DESC LIMIT 5",
            (v3_product_id,),
        ).fetchall()
        sc_row = c.execute(
            "SELECT * FROM scores WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (v3_product_id,),
        ).fetchone()
    decision = dict(drow) if drow else {}
    creative_variants = [dict(v) for v in variants_rows]
    score = dict(sc_row) if sc_row else {}

    # 2. Dedupe check unless force
    if not force and not dry_run:
        try:
            shop = _shop()
        except RuntimeError:
            shop = None
        if shop:
            prior = existing_clone(v3_product_id, shop)
            if prior and prior["status"] != "FAILED":
                return {
                    "ok": True,
                    "already_cloned": True,
                    "clone": prior,
                    "message": f"already cloned (clone_id={prior['id']}, status={prior['status']}); pass force=true to re-push",
                }

    # 3. Scrape source page for rich detail
    source_url = product.get("product_url") or product.get("landing_url")
    src = source_scraper.fetch_source_detail(source_url) if source_url else {}

    # 4. Build productSet input
    payload = _build_input(product, decision, creative_variants, score, src,
                           price_override=price_override, status=status)

    if dry_run:
        return {"ok": True, "dry_run": True, "would_send": payload, "source_scraped": bool(src)}

    # 5. POST to Shopify
    body = {"query": PRODUCT_SET_MUTATION, "variables": {"synchronous": True, "productSet": payload}}
    r = requests.post(
        _endpoint(),
        headers={"X-Shopify-Access-Token": _token(), "Content-Type": "application/json"},
        json=body,
        timeout=int(config.get("shopify.request_timeout_sec", 60)),
    )
    raw_response: dict[str, Any]
    try:
        raw_response = r.json()
    except Exception:
        raw_response = {"_raw_text": r.text[:1000], "_status": r.status_code}

    # 6. Parse + persist
    return _persist_result(v3_product_id, payload, raw_response, r.status_code)


def _build_input(
    product: dict[str, Any],
    decision: dict[str, Any],
    creative_variants: list[dict[str, Any]],
    score: dict[str, Any],
    src: dict[str, Any],
    *,
    price_override: float | None,
    status: str,
) -> dict[str, Any]:
    """Assemble the ProductSetInput payload."""
    title = (src.get("title") or product.get("title") or "Untitled v3 clone")[:255]
    src_html = src.get("descriptionHtml") or ""
    desc = _description_html(src_html, creative_variants, decision, score)
    vendor = src.get("vendor") or product.get("shop_domain") or "v3-clone"
    product_type = src.get("productType") or product.get("category") or "Other"

    # Tags carry decision metadata so user can filter in admin
    base_tags = src.get("tags") or []
    v3_tags = [
        "v3-clone",
        f"v3-decision:{decision.get('decision','?')}",
        f"v3-score:{int(score.get('composite_score') or 0)}",
        f"v3-source:{(product.get('shop_domain') or 'manual')[:40]}",
    ]
    if decision.get("kill_reason"):
        v3_tags.append(f"v3-kill:{decision['kill_reason']}")
    tags = sorted(set(base_tags + v3_tags))

    # Variants: prefer source variants; fall back to single default at v3 price
    src_variants = src.get("variants") or []
    price = price_override if price_override is not None else (
        product.get("price_usd")
        or (src_variants[0].get("price") if src_variants else None)
        or 0
    )
    options = src.get("options") or [{"name": "Title", "values": ["Default"]}]
    primary_option_name = options[0]["name"] if options and options[0].get("name") else "Title"

    if src_variants:
        variants_input = []
        for v in src_variants[:50]:  # cap variants
            opt_value = v.get("option1") or v.get("title") or "Default"
            variants_input.append({
                "optionValues": [{"optionName": primary_option_name, "name": str(opt_value)[:255]}],
                "price": str(price_override if price_override is not None else (v.get("price") or price or 0)),
                **({"sku": v["sku"]} if v.get("sku") else {}),
            })
    else:
        variants_input = [{
            "optionValues": [{"optionName": primary_option_name, "name": "Default"}],
            "price": str(price or 0),
        }]

    # Product options must list every value used in variants
    used_values: dict[str, list[str]] = {}
    for v in variants_input:
        for ov in v["optionValues"]:
            used_values.setdefault(ov["optionName"], [])
            if ov["name"] not in used_values[ov["optionName"]]:
                used_values[ov["optionName"]].append(ov["name"])
    product_options = [
        {"name": name, "values": [{"name": val} for val in vals]}
        for name, vals in used_values.items()
    ] or [{"name": "Title", "values": [{"name": "Default"}]}]

    # Files / media — Shopify productSet accepts external URLs via files block
    images = (src.get("images") or [])[:10]
    if not images and product.get("image_url"):
        images = [product["image_url"]]
    files_input = [
        {"originalSource": u, "contentType": "IMAGE", "alt": title[:150]}
        for u in images
    ]

    metafields = [
        {
            "namespace": "v3",
            "key": "source_product_id",
            "type": "single_line_text_field",
            "value": str(product["id"]),
        },
        {
            "namespace": "v3",
            "key": "source_url",
            "type": "url",
            "value": (product.get("product_url") or product.get("landing_url") or "")[:255],
        },
        {
            "namespace": "v3",
            "key": "decision",
            "type": "single_line_text_field",
            "value": str(decision.get("decision") or ""),
        },
        {
            "namespace": "v3",
            "key": "composite_score",
            "type": "number_decimal",
            "value": str(score.get("composite_score") or 0),
        },
    ]
    if decision.get("test_plan_json"):
        try:
            tp = json.loads(decision["test_plan_json"])
            metafields.append({
                "namespace": "v3", "key": "test_plan",
                "type": "json", "value": json.dumps(tp, ensure_ascii=False),
            })
        except Exception:
            pass

    return {
        "title": title,
        "descriptionHtml": desc,
        "status": status,
        "vendor": vendor[:255],
        "productType": product_type[:255],
        "tags": tags,
        "productOptions": product_options,
        "variants": variants_input,
        "files": files_input,
        "metafields": metafields,
    }


def _description_html(src_html: str, creative_variants: list[dict], decision: dict, score: dict) -> str:
    """Combine source description with v3 creative hooks for a richer LP."""
    parts = []
    if src_html:
        parts.append(src_html)
    if creative_variants:
        parts.append('<hr/><h3>Hooks (v3 generated)</h3>')
        for v in creative_variants:
            parts.append(
                f'<div><strong>{v.get("variant_type","")}</strong>: '
                f'{v.get("hook","")}</div><div>{v.get("body","")}</div>'
            )
    parts.append(
        f'<!-- v3-clone · decision={decision.get("decision")} · '
        f'composite={score.get("composite_score")} · scored_at={score.get("scored_at")} -->'
    )
    return "\n".join(parts)


# ───────────────────────── persistence ─────────────────────────

def _persist_result(
    source_product_id: int,
    payload: dict[str, Any],
    response: dict[str, Any],
    http_status: int,
) -> dict[str, Any]:
    shop = _shop()
    now = db.now_iso()
    err: str | None = None
    sg_gid: str | None = None
    sg_handle: str | None = None
    sg_store_url: str | None = None
    status = "FAILED"
    if http_status >= 400 or response.get("errors"):
        err = f"HTTP {http_status}: {json.dumps(response.get('errors') or response)[:600]}"
    else:
        data = (response.get("data") or {}).get("productSet") or {}
        ue = data.get("userErrors") or []
        if ue:
            err = "userErrors: " + json.dumps(ue, ensure_ascii=False)
        prod = data.get("product") or {}
        sg_gid = prod.get("id")
        sg_handle = prod.get("handle")
        sg_store_url = prod.get("onlineStoreUrl") or prod.get("onlineStorePreviewUrl")
        if sg_gid:
            status = (prod.get("status") or "DRAFT").upper()
            err = None  # successful even if userErrors empty
    admin_url = _admin_url(sg_handle) if sg_handle else None
    with db.conn() as c:
        # Upsert: replace prior failed/draft row for same (source, dest)
        c.execute(
            """INSERT INTO shopify_clones(
                   source_product_id, shop_destination, shopify_product_gid,
                   shopify_product_handle, shopify_admin_url, shopify_storefront_url,
                   status, pushed_at, error, payload_json, response_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_product_id, shop_destination) DO UPDATE SET
                   shopify_product_gid=excluded.shopify_product_gid,
                   shopify_product_handle=excluded.shopify_product_handle,
                   shopify_admin_url=excluded.shopify_admin_url,
                   shopify_storefront_url=excluded.shopify_storefront_url,
                   status=excluded.status,
                   pushed_at=excluded.pushed_at,
                   error=excluded.error,
                   payload_json=excluded.payload_json,
                   response_json=excluded.response_json""",
            (source_product_id, shop, sg_gid, sg_handle, admin_url, sg_store_url,
             status, now, err,
             json.dumps(payload, ensure_ascii=False)[:60000],
             json.dumps(response, ensure_ascii=False, default=str)[:60000]),
        )
        row = c.execute(
            "SELECT * FROM shopify_clones WHERE source_product_id = ? AND shop_destination = ?",
            (source_product_id, shop),
        ).fetchone()
    return {
        "ok": err is None,
        "clone": dict(row) if row else None,
        "error": err,
    }


# ───────────────────────── batch ─────────────────────────

def clone_batch(
    decision_filter: str = "GO_TEST",
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Clone all products matching the decision (default GO_TEST)."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT p.id FROM products p
               JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
               WHERE d.decision = ?
                 AND p.shop_domain IS NOT NULL
                 AND p.shop_domain NOT IN ('test.com','example.com')
               ORDER BY (SELECT composite_score FROM scores
                         WHERE product_id = p.id ORDER BY id DESC LIMIT 1) DESC""",
            (decision_filter,),
        ).fetchall()
    pids = [r["id"] for r in rows]
    if limit:
        pids = pids[:limit]
    results = []
    ok = 0
    skipped = 0
    failed = 0
    for pid in pids:
        try:
            res = clone(pid, dry_run=dry_run, force=force)
            results.append({"pid": pid, **res})
            if res.get("already_cloned"):
                skipped += 1
            elif res.get("ok"):
                ok += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append({"pid": pid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return {
        "attempted": len(pids),
        "cloned": ok,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
        "results": results,
    }


def list_clones(limit: int = 100) -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute(
            """SELECT sc.*, p.title AS source_title, p.shop_domain AS source_shop
               FROM shopify_clones sc JOIN products p ON p.id = sc.source_product_id
               ORDER BY sc.pushed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
