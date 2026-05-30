"""Pipeline internals — discover → enrich → score → decide → output."""
from __future__ import annotations

import concurrent.futures as cf
import json
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, db
from .signals import SignalResult


def _start_run(c: sqlite3.Connection, keyword_count: int) -> int:
    cur = c.execute(
        "INSERT INTO scan_runs(started_at, keyword_count) VALUES(?, ?)",
        (db.now_iso(), keyword_count),
    )
    return cur.lastrowid


def _finish_run(c: sqlite3.Connection, run_id: int, **stats: Any) -> None:
    c.execute(
        """UPDATE scan_runs SET
             finished_at = ?, products_found = ?, products_scored = ?,
             go_test_count = ?, watch_count = ?, kill_count = ?, error_log = ?
           WHERE id = ?""",
        (
            db.now_iso(),
            stats.get("products_found", 0),
            stats.get("products_scored", 0),
            stats.get("go_test_count", 0),
            stats.get("watch_count", 0),
            stats.get("kill_count", 0),
            stats.get("error_log", ""),
            run_id,
        ),
    )


# -------------- Stage A: discover --------------

def stage_discover(keywords: list[str]) -> list[int]:
    """Return product ids discovered for the given keywords."""
    from .signals import fb_ads as fb_signal

    product_ids: list[int] = []
    errors: list[str] = []
    with db.conn() as c:
        for kw in keywords:
            kw_id = db.upsert_keyword(c, kw, source="cli")
            try:
                ads = fb_signal.discover_keyword(kw)
            except Exception as e:
                errors.append(f"discover[{kw}]: {type(e).__name__}: {e}")
                ads = []
            for ad in ads:
                shop_domain = ad.get("shop_domain")
                handle = ad.get("handle") or ad.get("product_handle")
                if not shop_domain or not handle:
                    # If we can't pin product yet, persist by landing url + advertiser
                    handle = ad.get("landing_url", "")[-80:] or f"adv-{ad.get('advertiser_id','?')}"
                    shop_domain = ad.get("shop_domain") or ad.get("landing_domain", "")
                pid = db.upsert_product(c, {
                    "shop_domain": shop_domain,
                    "handle": handle,
                    "title": ad.get("title") or ad.get("ad_title"),
                    "price_usd": ad.get("price_usd"),
                    "image_url": ad.get("image_url"),
                    "product_url": ad.get("product_url"),
                    "landing_url": ad.get("landing_url"),
                    "category": ad.get("category"),
                    "advertiser_id": ad.get("advertiser_id"),
                    "keyword_id": kw_id,
                    "raw": ad,
                })
                product_ids.append(pid)
            db.mark_keyword_scanned(c, kw_id)
    if errors:
        Path(config.resolve_path("paths.logs") / "discover_errors.log").write_text(
            "\n".join(errors), encoding="utf-8"
        )
    return product_ids


# -------------- Stage B: enrich --------------

def _collect_signals_for_product(pid: int) -> dict[str, SignalResult]:
    from .signals import fb_ads, profit, trends, landing_page

    with db.conn() as c:
        prow = c.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if prow is None:
        return {}
    p = dict(prow)
    raw = json.loads(p["raw_json"] or "{}")

    workers = config.get("pipeline.parallel_signal_workers", 4)
    results: dict[str, SignalResult] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(fb_ads.collect, p, raw): "fb_ads",
            ex.submit(profit.collect, p, raw): "profit",
            ex.submit(trends.collect, p, raw): "trends",
            ex.submit(landing_page.collect, p, raw): "lp",
        }
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = SignalResult(signal=name, status="failed", error=f"{type(e).__name__}: {e}")
            results[name] = res
    # Persist signals
    with db.conn() as c:
        _persist_signals(c, pid, results)
    return results


def _persist_signals(c: sqlite3.Connection, pid: int, results: dict[str, SignalResult]) -> None:
    now = db.now_iso()
    fb = results.get("fb_ads")
    if fb:
        d = fb.data or {}
        c.execute(
            """INSERT INTO ad_signals
                 (product_id, days_active, impressions_total, distinct_entity_ids,
                  creative_count_raw, countries_running, advertiser_id, first_seen_at,
                  last_seen_at, homogeneity_flag, raw_json, checked_at, status, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                d.get("days_active"),
                d.get("impressions_total"),
                d.get("distinct_entity_ids"),
                d.get("creative_count_raw"),
                d.get("countries_running"),
                d.get("advertiser_id"),
                d.get("first_seen_at"),
                d.get("last_seen_at"),
                d.get("homogeneity_flag"),
                json.dumps(d, ensure_ascii=False),
                now,
                fb.status,
                fb.error,
            ),
        )
    pr = results.get("profit")
    if pr:
        d = pr.data or {}
        c.execute(
            """INSERT INTO profit_signals
                 (product_id, selling_price_usd, source_cost_usd, cost_method,
                  payment_fee_pct, refund_rate_pct, shipping_cost_usd, gross_margin_pct,
                  markup_multiplier, beroas, target_roas, price_band, checked_at, status, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                d.get("selling_price_usd"),
                d.get("source_cost_usd"),
                d.get("cost_method"),
                d.get("payment_fee_pct"),
                d.get("refund_rate_pct"),
                d.get("shipping_cost_usd"),
                d.get("gross_margin_pct"),
                d.get("markup_multiplier"),
                d.get("beroas"),
                d.get("target_roas"),
                d.get("price_band"),
                now,
                pr.status,
                pr.error,
            ),
        )
    tr = results.get("trends")
    if tr:
        d = tr.data or {}
        c.execute(
            """INSERT INTO trend_signals
                 (product_id, keyword, score_7d, score_30d, score_90d, yoy_growth, slope_90d,
                  seasonality_phase, related_queries_json, checked_at, status, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                d.get("keyword"),
                d.get("score_7d"),
                d.get("score_30d"),
                d.get("score_90d"),
                d.get("yoy_growth"),
                d.get("slope_90d"),
                d.get("seasonality_phase"),
                json.dumps(d.get("related_queries", []), ensure_ascii=False),
                now,
                tr.status,
                tr.error,
            ),
        )
    lp = results.get("lp")
    if lp:
        d = lp.data or {}
        c.execute(
            """INSERT INTO lp_signals
                 (product_id, has_shopify, has_klaviyo, has_reviews_app, has_pixel, has_capi,
                  payment_methods_count, has_video_hero, has_comparison_chart, has_ugc_block,
                  awareness_signals_json, awareness_match_layer, checked_at, status, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                int(bool(d.get("has_shopify"))),
                int(bool(d.get("has_klaviyo"))),
                int(bool(d.get("has_reviews_app"))),
                int(bool(d.get("has_pixel"))),
                int(bool(d.get("has_capi"))) if d.get("has_capi") is not None else None,
                d.get("payment_methods_count"),
                int(bool(d.get("has_video_hero"))),
                int(bool(d.get("has_comparison_chart"))),
                int(bool(d.get("has_ugc_block"))),
                json.dumps(d.get("awareness_signals", []), ensure_ascii=False),
                d.get("awareness_match_layer"),
                now,
                lp.status,
                lp.error,
            ),
        )


# -------------- Stage C+D: score + decide + persist --------------

def stage_score_and_decide(pid: int, signals: dict[str, SignalResult]) -> dict[str, Any]:
    from .scoring import composite, decision

    res = composite.compute(pid, signals)
    dec = decision.compute(pid, signals, res)
    with db.conn() as c:
        c.execute(
            """INSERT INTO scores
                 (product_id, fb_score, profit_score, trend_score, lp_score,
                  composite_score, weights_json, scored_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                pid,
                res["fb_score"],
                res["profit_score"],
                res["trend_score"],
                res["lp_score"],
                res["composite"],
                json.dumps(res["weights"]),
                db.now_iso(),
            ),
        )
        c.execute(
            """INSERT INTO decisions
                 (product_id, decision, kill_reason, watch_reason, watch_recheck_at,
                  test_plan_json, composite_score, decided_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                pid,
                dec["decision"],
                dec.get("kill_reason"),
                dec.get("watch_reason"),
                dec.get("watch_recheck_at"),
                json.dumps(dec.get("test_plan"), ensure_ascii=False) if dec.get("test_plan") else None,
                res["composite"],
                db.now_iso(),
            ),
        )
    return {"score": res, "decision": dec}


# -------------- Driver --------------

def run_pipeline(keywords: list[str], top: int | None, dry_run: bool) -> dict[str, Any]:
    top = top or config.get("pipeline.scan_default_top", 30)
    db.init_db()
    errors: list[str] = []

    with db.conn() as c:
        run_id = _start_run(c, len(keywords))

    print(f"[1/4] discover — {len(keywords)} keywords")
    try:
        product_ids = stage_discover(keywords)
    except Exception as e:
        errors.append(f"stage_discover fatal: {e}\n{traceback.format_exc()}")
        product_ids = []
    product_ids = list(dict.fromkeys(product_ids))  # dedupe preserve order
    print(f"      → {len(product_ids)} products")

    print(f"[2/4] enrich — collect 4 signals × {len(product_ids)} products")
    enriched: dict[int, dict[str, SignalResult]] = {}
    for i, pid in enumerate(product_ids, 1):
        try:
            enriched[pid] = _collect_signals_for_product(pid)
        except Exception as e:
            errors.append(f"enrich[{pid}]: {e}")
            enriched[pid] = {}
        if i % 5 == 0 or i == len(product_ids):
            print(f"      {i}/{len(product_ids)}")

    print(f"[3/4] score + decide")
    decisions_count = {"GO_TEST": 0, "WATCH": 0, "KILL": 0}
    for pid, signals in enriched.items():
        try:
            r = stage_score_and_decide(pid, signals)
            decisions_count[r["decision"]["decision"]] = decisions_count.get(r["decision"]["decision"], 0) + 1
        except Exception as e:
            errors.append(f"score[{pid}]: {e}")

    print(f"[4/4] output")
    report_path = None
    if not dry_run:
        try:
            from .outputs import decision_table
            report_path = str(decision_table.generate(fmt="xlsx"))
            from .outputs import landing_kit
            landing_kit.generate_for_go_test_today()
        except Exception as e:
            errors.append(f"output: {e}")
            traceback.print_exc()

    with db.conn() as c:
        _finish_run(
            c,
            run_id,
            products_found=len(product_ids),
            products_scored=len(enriched),
            go_test_count=decisions_count.get("GO_TEST", 0),
            watch_count=decisions_count.get("WATCH", 0),
            kill_count=decisions_count.get("KILL", 0),
            error_log="\n".join(errors)[-4000:],
        )

    return {
        "run_id": run_id,
        "keyword_count": len(keywords),
        "products_found": len(product_ids),
        "products_scored": len(enriched),
        "go_test_count": decisions_count.get("GO_TEST", 0),
        "watch_count": decisions_count.get("WATCH", 0),
        "kill_count": decisions_count.get("KILL", 0),
        "report_path": report_path,
        "errors": errors,
    }
