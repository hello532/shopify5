"""FastAPI thin wrapper — preserves /api/v3/* compatibility, reads from new SQLite.

Mount this from shopify_api_server.py via `app.include_router(v3_router())`
or run standalone via uvicorn for testing.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, db


def _attach(app, mount_page: bool = True) -> None:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    router = APIRouter()

    @router.get("/api/v3/followable-products")
    def followable_products(min_score: float = 60.0, limit: int = 50):
        with db.conn() as c:
            rows = c.execute(
                """SELECT p.id, p.title, p.shop_domain, p.handle, p.price_usd, p.image_url,
                          p.product_url, p.landing_url,
                          sc.composite_score AS ai_score, sc.fb_score, sc.profit_score,
                          sc.trend_score, sc.lp_score,
                          pr.gross_margin_pct, pr.beroas, pr.target_roas,
                          d.decision, d.test_plan_json
                   FROM products p
                   JOIN scores sc ON sc.id = (SELECT MAX(id) FROM scores WHERE product_id = p.id)
                   JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
                   LEFT JOIN profit_signals pr ON pr.id = (SELECT MAX(id) FROM profit_signals WHERE product_id = p.id)
                   WHERE d.decision = 'GO_TEST' AND sc.composite_score >= ?
                   ORDER BY sc.composite_score DESC LIMIT ?""",
                (min_score, limit),
            ).fetchall()
        items = [dict(r) for r in rows]
        return {"ok": True, "count": len(items), "products": items}

    @router.get("/api/v3/today-fast")
    def today_fast(max_snapshot_age_days: int = 3):
        cutoff = (datetime.utcnow() - timedelta(days=max_snapshot_age_days)).isoformat(timespec="seconds") + "Z"
        with db.conn() as c:
            rows = c.execute(
                """SELECT p.*, sc.composite_score AS ai_score, d.decision
                   FROM products p
                   LEFT JOIN scores sc ON sc.id = (SELECT MAX(id) FROM scores WHERE product_id = p.id)
                   LEFT JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
                   WHERE p.last_seen_at >= ?
                   ORDER BY sc.composite_score DESC NULLS LAST LIMIT 200""",
                (cutoff,),
            ).fetchall()
        return {"ok": True, "count": len(rows), "products": [dict(r) for r in rows]}

    @router.post("/api/v3/today-scan")
    def today_scan(keywords: list[str] | None = None, top: int = 30):
        from . import pipeline_impl
        with db.conn() as c:
            kws = keywords or [r["keyword"] for r in c.execute("SELECT keyword FROM keywords").fetchall()]
        if not kws:
            raise HTTPException(400, "no keywords provided and DB has none")
        summary = pipeline_impl.run_pipeline(kws, top=top, dry_run=False)
        return {"ok": True, **summary}

    @router.get("/api/v3/decisions")
    def decisions(limit: int = 200, dedupe: bool = True):
        """Return summary counts + fully-joined recent decisions for card UI.

        dedupe=True (default) collapses same-product-different-market listings:
        - Multiple shop suffixes like .com / .co / -au / -uk
        - Brand root is the longest leftmost label shared (e.g. "muravai" for
          both "muravai.co" and "muravai.com")
        - Composite_score winner becomes the representative; others go into
          `duplicates` array on that card.
        """
        sql = """
        SELECT
            p.id, p.title, p.shop_domain, p.handle, p.price_usd, p.image_url,
            p.product_url, p.landing_url, p.category,
            sc.fb_score, sc.profit_score, sc.trend_score, sc.lp_score,
            sc.composite_score,
            ad.days_active, ad.impressions_total, ad.distinct_entity_ids,
            ad.creative_count_raw, ad.countries_running, ad.homogeneity_flag,
            pr.gross_margin_pct, pr.markup_multiplier, pr.beroas, pr.target_roas,
            pr.source_cost_usd, pr.price_band,
            tr.score_7d, tr.score_90d, tr.yoy_growth, tr.seasonality_phase,
            lp.has_shopify, lp.has_klaviyo, lp.has_reviews_app, lp.has_pixel,
            lp.payment_methods_count, lp.awareness_match_layer,
            d.decision, d.kill_reason, d.watch_reason, d.watch_recheck_at,
            d.test_plan_json, d.decided_at
        FROM products p
        JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
        LEFT JOIN scores sc ON sc.id = (SELECT MAX(id) FROM scores WHERE product_id = p.id)
        LEFT JOIN ad_signals ad ON ad.id = (SELECT MAX(id) FROM ad_signals WHERE product_id = p.id)
        LEFT JOIN profit_signals pr ON pr.id = (SELECT MAX(id) FROM profit_signals WHERE product_id = p.id)
        LEFT JOIN trend_signals tr ON tr.id = (SELECT MAX(id) FROM trend_signals WHERE product_id = p.id)
        LEFT JOIN lp_signals lp ON lp.id = (SELECT MAX(id) FROM lp_signals WHERE product_id = p.id)
        WHERE p.shop_domain IS NOT NULL
          AND p.shop_domain NOT IN ('test.com', 'example.com')
          AND p.title IS NOT NULL AND TRIM(p.title) != ''
        ORDER BY CASE d.decision
                   WHEN 'GO_TEST' THEN 0
                   WHEN 'WATCH'   THEN 1
                   ELSE 2 END,
                 sc.composite_score DESC
        LIMIT ?
        """
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(sql, (limit,)).fetchall()]
            counts_rows = c.execute(
                """SELECT decision, COUNT(*) AS n FROM decisions d
                   JOIN products p ON p.id = d.product_id
                   WHERE d.id = (SELECT MAX(id) FROM decisions WHERE product_id = d.product_id)
                     AND p.shop_domain IS NOT NULL
                     AND p.shop_domain NOT IN ('test.com', 'example.com')
                     AND p.title IS NOT NULL AND TRIM(p.title) != ''
                   GROUP BY d.decision"""
            ).fetchall()
        for r in rows:
            if r.get("test_plan_json"):
                try:
                    r["test_plan"] = json.loads(r["test_plan_json"])
                except Exception:
                    r["test_plan"] = None
            else:
                r["test_plan"] = None
            r.pop("test_plan_json", None)

        if dedupe:
            rows = _dedupe_same_product(rows)

        # Counts AFTER dedupe (so UI badges match what's shown)
        if dedupe:
            counts = {"GO_TEST": 0, "WATCH": 0, "KILL": 0}
            for r in rows:
                counts[r["decision"]] = counts.get(r["decision"], 0) + 1
        else:
            counts = {r["decision"]: r["n"] for r in counts_rows}

        return {"ok": True, "summary": counts, "rows": rows}

    @router.post("/api/v3/generate-research-prompt")
    def gen_prompt(product_url: str):
        # Look up product, then generate the 8-dim research prompt (reuses old format)
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM products WHERE product_url = ? OR landing_url = ?",
                (product_url, product_url),
            ).fetchone()
        title = row["title"] if row else product_url.split("/")[-1]
        prompt = _research_prompt(title, product_url)
        return {"ok": True, "prompt": prompt}

    @router.get("/v3-radar", response_class=HTMLResponse)
    def radar_page():
        """爆款雷达页面 — full SPA card UI."""
        return _render_page()

    # Only mount /v3 directly when running standalone (not when attached to another app
    # which is expected to provide its own /v3 wrapper).
    if mount_page:
        @router.get("/v3", response_class=HTMLResponse)
        def page():
            return _render_page()

    @router.get("/api/v3/explain/{product_id}")
    def explain_route(product_id: int):
        from . import explain
        text = explain.explain_product(product_id)
        return JSONResponse({"ok": True, "trace": text})

    # ─── PR10: Shopify clone ────────────────────────────────────────────
    @router.post("/api/v3/clone/{product_id}")
    def clone_route(product_id: int, dry_run: bool = False, force: bool = False,
                    price_override: float | None = None, status: str = "DRAFT"):
        from .shopify import auto_create
        try:
            res = auto_create.clone(
                product_id, dry_run=dry_run, force=force,
                price_override=price_override, status=status,
            )
            return res
        except RuntimeError as e:
            # Missing token / shop = clean 400 not 500
            return JSONResponse({"ok": False, "error": str(e), "hint":
                "Set SHOPIFY_SHOP + SHOPIFY_ADMIN_ACCESS_TOKEN env or config.shopify.{shop,admin_token}"},
                status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)

    @router.post("/api/v3/clone-batch")
    def clone_batch_route(decision: str = "GO_TEST", limit: int | None = None,
                          dry_run: bool = False, force: bool = False):
        from .shopify import auto_create
        try:
            return auto_create.clone_batch(decision_filter=decision, limit=limit,
                                           dry_run=dry_run, force=force)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @router.get("/api/v3/clones")
    def list_clones_route(limit: int = 100):
        from .shopify import auto_create
        return {"ok": True, "clones": auto_create.list_clones(limit=limit)}

    # ─── PR11: one-click launch + human-language reasoning ─────────────
    @router.get("/api/v3/why/{product_id}")
    def why_route(product_id: int):
        from . import launch
        return launch.why_card(product_id)

    @router.post("/api/v3/launch/{product_id}")
    def launch_route(product_id: int, dry_run: bool = False, status: str = "DRAFT"):
        from . import launch as _launch
        try:
            return _launch.launch(product_id, dry_run=dry_run, status=status)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)

    app.include_router(router)


# ─────────────────────────────────────────────────────────────────────────
# Dedupe helpers — collapse same-product-different-listing duplicates
# ─────────────────────────────────────────────────────────────────────────

import re as _re

# Market suffixes on Shopify handles (e.g. "super-max-red-light-au")
_MARKET_SUFFIXES = ("-au", "-us", "-uk", "-eu", "-ca", "-nz", "-de", "-fr", "-jp", "-cn")
# Words that should be stripped from titles for canonicalization
_TITLE_NOISE = _re.compile(
    r"(\b(20\d{2}|new|upgraded|version|edition|model|pro|max|plus|mini|lite)\b"
    r"|⏳|💥|🔥|✅|★|\bv\d+\b|\bgen\d+\b)", _re.I)


def _brand_root(shop_domain: str) -> str:
    """Strip TLD + common suffixes to get a brand identifier.

    muravai.co     → muravai
    muravai.com    → muravai
    boncharge.com  → boncharge
    boncharge.com.au → boncharge
    """
    if not shop_domain:
        return ""
    d = shop_domain.lower().strip().lstrip("www.")
    # Drop progressively from the right: .au, .com, .co etc.
    parts = d.split(".")
    if len(parts) > 1:
        # boncharge.com.au → ["boncharge","com","au"] → root = "boncharge"
        # muravai.co       → ["muravai","co"]        → root = "muravai"
        return parts[0]
    return d


def _canon_handle(handle: str) -> str:
    """Strip market suffix from handle: 'super-max-light-au' → 'super-max-light'."""
    if not handle:
        return ""
    h = handle.lower().rstrip("/")
    for suf in _MARKET_SUFFIXES:
        if h.endswith(suf):
            return h[: -len(suf)]
    return h


def _canon_title(title: str) -> str:
    """Normalize a product title for fuzzy equality."""
    if not title:
        return ""
    t = title.lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = _re.sub(r"[^\w\s]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t


def _dedupe_key(row: dict) -> tuple:
    """Soft-dedupe key. Two rows with the same key are treated as duplicates."""
    return (_brand_root(row.get("shop_domain", "")), _canon_handle(row.get("handle", "")) or _canon_title(row.get("title", "")))


def _dedupe_same_product(rows: list[dict]) -> list[dict]:
    """Collapse same-product-different-listing rows into a single representative.

    Strategy:
      1. Group by (brand_root, canon_handle-or-canon_title)
      2. Within each group, pick the row with the best composite_score
      3. Append all other variants to that row's `duplicates` list
      4. Preserve original decision-tier ordering
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = _dedupe_key(r)
        groups.setdefault(key, []).append(r)

    out: list[dict] = []
    for key, members in groups.items():
        if len(members) == 1:
            rep = members[0]
            rep["duplicates"] = []
            out.append(rep)
            continue
        # Best decision tier wins; tie-break by composite_score
        members.sort(key=lambda m: (
            {"GO_TEST": 0, "WATCH": 1, "KILL": 2}.get(m.get("decision"), 9),
            -(m.get("composite_score") or 0),
        ))
        rep = members[0]
        rep["duplicates"] = [
            {
                "id": m["id"],
                "shop_domain": m.get("shop_domain"),
                "handle": m.get("handle"),
                "decision": m.get("decision"),
                "composite_score": m.get("composite_score"),
                "product_url": m.get("product_url"),
            }
            for m in members[1:]
        ]
        out.append(rep)

    # Re-sort by decision tier then composite (preserve original semantics)
    out.sort(key=lambda m: (
        {"GO_TEST": 0, "WATCH": 1, "KILL": 2}.get(m.get("decision"), 9),
        -(m.get("composite_score") or 0),
    ))
    return out


def _research_prompt(title: str, url: str) -> str:
    return f"""你是跨境电商选品专家。请用 8 个维度分析以下产品:

产品: {title}
URL: {url}

请分析:
1. Google Trends 趋势 (7d/30d/90d/12m + YoY)
2. FB Ad Library 广告投放情况 (days_active, distinct creatives, advertiser)
3. Shopify 同款竞品 (数量/价格分布/差异化机会)
4. 1688 / AliExpress 成本估算 + 毛利率 + BEROAS
5. Awareness Level (Mark 七层) 落地页 hook 匹配
6. 风险点 (政策/物流/竞争/季节性)
7. 三档决策: 🟢立即跟 / 🟡观察 / 🔴不碰
8. 如果跟,给出测试方案 (预算/创意数/Kill/Scale 阈值)
"""


def _render_page() -> str:
    """Return v3.html. Reads file directly — no Jinja2 dep needed (page is static SPA)."""
    tpl_path = Path(__file__).parent / "web" / "templates" / "v3.html"
    try:
        return tpl_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"<h1>v3 page error</h1><pre>{e}</pre><p>missing: {tpl_path}</p>"
