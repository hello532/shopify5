"""Landing kit generator — runs only for GO_TEST products.

Per spec, this produces:
  brief.md (paste-ready Meta Ads brief)
  creatives/hook_problem.md hook_solution.md hook_product.md
  lp_blocks/hero.md bullets.md faq.md comparison.md reviews_prompts.md
  ads_manager_paste.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config, db


def generate_for_go_test_today() -> int:
    """Generate landing kits for all GO_TEST products decided since 24h ago."""
    base = config.resolve_path("paths.landing_kits")
    base.mkdir(parents=True, exist_ok=True)
    count = 0
    with db.conn() as c:
        rows = c.execute(
            """SELECT p.*, d.test_plan_json, d.decision, d.decided_at
               FROM products p JOIN decisions d ON d.product_id = p.id
               WHERE d.decision = 'GO_TEST'
                 AND d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
                 AND d.decided_at >= datetime('now','-1 day')"""
        ).fetchall()
        for row in rows:
            generate_for_product(dict(row))
            count += 1
    return count


def generate_for_product(product: dict[str, Any]) -> Path:
    base = config.resolve_path("paths.landing_kits")
    pdir = base / f"product_{product['id']}"
    (pdir / "creatives").mkdir(parents=True, exist_ok=True)
    (pdir / "lp_blocks").mkdir(parents=True, exist_ok=True)
    tp = json.loads(product["test_plan_json"]) if product.get("test_plan_json") else {}

    title = product.get("title") or "Product"
    landing = product.get("landing_url") or product.get("product_url") or ""
    price = product.get("price_usd")

    # PR7: Creative AI Factory — generate Andromeda-diverse variants
    try:
        from ..creative import factory as _factory
        variants = _factory.factory_for_product(product["id"], count=3, generator="auto")
        audit = _factory.diversity_audit(product["id"])
    except Exception as e:
        variants = []
        audit = {"error": f"{type(e).__name__}: {e}"}

    # PR9: Klaviyo flow JSON kit
    try:
        from ..klaviyo import flow_kit as _kv
        _kv.generate_kit_for_product(product)
    except Exception:
        pass

    # PR9: attribution survey template
    try:
        from .. import attribution as _attr
        (pdir / "attribution_survey.json").write_text(
            json.dumps(_attr.survey_template(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # brief.md
    (pdir / "brief.md").write_text(_brief_md(product, tp, audit), encoding="utf-8")

    # creatives — three awareness layers (Mark's 7-layer compressed to 3 practical hooks)
    (pdir / "creatives" / "hook_problem.md").write_text(_hook("problem", title), encoding="utf-8")
    (pdir / "creatives" / "hook_solution.md").write_text(_hook("solution", title), encoding="utf-8")
    (pdir / "creatives" / "hook_product.md").write_text(_hook("product", title), encoding="utf-8")

    # lp_blocks
    (pdir / "lp_blocks" / "hero.md").write_text(_hero_md(title, price), encoding="utf-8")
    (pdir / "lp_blocks" / "bullets.md").write_text(_bullets_md(title), encoding="utf-8")
    (pdir / "lp_blocks" / "faq.md").write_text(_faq_md(title), encoding="utf-8")
    (pdir / "lp_blocks" / "comparison.md").write_text(_comparison_md(title), encoding="utf-8")
    (pdir / "lp_blocks" / "reviews_prompts.md").write_text(_reviews_md(title), encoding="utf-8")

    # ads_manager_paste.json — ASC default, broad targeting, 3 creatives
    paste = _ads_manager_paste(product, tp)
    (pdir / "ads_manager_paste.json").write_text(json.dumps(paste, indent=2, ensure_ascii=False), encoding="utf-8")

    return pdir


def _brief_md(p: dict[str, Any], tp: dict[str, Any], audit: dict[str, Any] | None = None) -> str:
    audit_block = ""
    if audit:
        if audit.get("is_andromeda_safe"):
            audit_block = f"\n## ✅ Andromeda diversity: {audit['distinct_signatures']} distinct entity signatures (>=3 safe)"
        elif "error" in audit:
            audit_block = f"\n## ⚠ Creative factory: {audit['error']}"
        else:
            audit_block = (
                f"\n## ⚠ Andromeda risk: only {audit.get('distinct_signatures')} distinct signatures\n"
                f"   Meta will likely collapse to 1 Entity ID. Add more diverse variants."
            )
    lines = [
        f"# Meta Ads Brief — {p.get('title')}",
        f"",
        f"- Landing URL: {p.get('landing_url') or p.get('product_url')}",
        f"- Price: ${p.get('price_usd')}",
        f"- Test budget: ${tp.get('test_budget_usd', 50)}/day CBO",
        f"- Required creatives: {tp.get('test_creatives_required', 3)} (must be visually distinct → Andromeda Entity ID)",
        f"- Target ROAS: {tp.get('target_roas')}",
        f"",
        "## Kill rules",
        f"- Day 3: if ROAS < {tp.get('kill_rule_day3', 1.5)} → pause",
        f"- Day 7: if ROAS < {tp.get('kill_rule_day7', 2.0)} → close",
        f"",
        "## Scale rule",
        f"- Day 7 ROAS ≥ {tp.get('scale_rule', 3.0)} sustained → +{int((tp.get('scale_step_pct',0.20))*100)}% budget/day",
        f"",
        "## Campaign structure (ASC default)",
        "- Campaign type: Advantage+ Shopping (ASC)",
        "- Countries: US (or your shipping region — keep narrow)",
        "- Creative mix: 1 product-focused + 1 UGC + 1 problem/solution hook",
        "- Pixel & CAPI: verify dedup before scaling",
        "",
        "## Hooks (pick one per creative)",
        "See creatives/hook_*.md",
        "",
        "## Landing page blocks",
        "See lp_blocks/*.md",
        audit_block,
    ]
    return "\n".join(lines)


def _hook(layer: str, title: str) -> str:
    if layer == "problem":
        return f"""# Hook — Problem-aware

**Opening (3s):** "Tired of [common pain point related to {title}]? You're not alone."
**Body:** Show 2 frustrating before-moments. Cut to {title} doing the work easily.
**CTA:** "Try {title} risk-free — 30-day return."
"""
    if layer == "solution":
        return f"""# Hook — Solution-aware

**Opening (3s):** "The fix for [problem] takes 30 seconds. Here's how."
**Body:** Demo {title} in use. Show measurable change.
**CTA:** "Get yours — link below."
"""
    return f"""# Hook — Product-aware

**Opening (3s):** "Introducing {title} — built for [audience]."
**Body:** Product close-ups, materials, key differentiators vs. cheap copies.
**CTA:** "Order now, free shipping today."
"""


def _hero_md(title: str, price: float | None) -> str:
    return f"""# Hero block — 3 variants

## A — Outcome-led
**H1:** Stop [pain]. Start [outcome].
**Sub:** {title} delivers measurable change in 7 days, or your money back.
**CTA:** Shop now — ${price or 'XX'}

## B — Authority-led
**H1:** {title} — engineered for [target audience].
**Sub:** Loved by 12,000+ customers. 4.8★ verified reviews.
**CTA:** Get yours

## C — Urgency-led
**H1:** {title} — back in stock, going fast.
**Sub:** Free shipping for the next 24 hours. ${price or 'XX'} flat.
**CTA:** Claim mine
"""


def _bullets_md(title: str) -> str:
    return f"""# Bullets — 5 sales points for {title}

1. **Solves [the #1 pain]** — backed by data/testimonials.
2. **30-second setup / use** — no learning curve.
3. **Built to last** — premium materials, 12-month warranty.
4. **Loved by 12,000+ customers** — verified reviews.
5. **Risk-free trial** — 30-day money-back guarantee.
"""


def _faq_md(title: str) -> str:
    return f"""# FAQ — 6 questions for {title}

**Q: How does it work?**
A: Plain-English explanation. Avoid jargon.

**Q: Does it really do X?**
A: Yes — here's a 10-second demo / proof.

**Q: How long until I see results?**
A: Most customers report change in 7 days.

**Q: Is it safe for [edge case]?**
A: Yes — explain why, cite source.

**Q: What if it doesn't work for me?**
A: 30-day money-back, no questions.

**Q: When does it ship?**
A: Within 24h. Tracking emailed.
"""


def _comparison_md(title: str) -> str:
    return f"""# Comparison chart — {title} vs alternatives

| Feature              | {title} | Generic alt | DIY |
|----------------------|---------|-------------|-----|
| Solves [pain]        | ✅      | ⚠️ partial  | ❌  |
| Setup time           | 30s     | 5min        | 30min |
| Warranty             | 12mo    | none        | n/a |
| Verified reviews     | 12,000+ | <500        | n/a |
| Money-back guarantee | 30 days | 14 days     | n/a |
"""


def _reviews_md(title: str) -> str:
    return f"""# UGC / review prompts for {title}

Target 3 review angles:

1. **Before/after** — visible change. Encourage photos.
2. **Speed** — "How fast did you see results?"
3. **Comparison** — "What were you using before {title}?"

Outreach: target verified buyers via Klaviyo flow at order day 14.
Offer: discount on next purchase for a 60-second video review.
"""


def _ads_manager_paste(p: dict[str, Any], tp: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign": {
            "name": f"v3-test-{p['id']}-{datetime.utcnow().strftime('%Y%m%d')}",
            "objective": "OUTCOME_SALES",
            "buying_type": "AUCTION",
            "smart_promotion_type": "ADVANTAGE_SHOPPING_CAMPAIGN",  # ASC
            "daily_budget_usd": tp.get("test_budget_usd", 50),
            "target_roas": tp.get("target_roas"),
        },
        "ad_set": {
            "countries": ["US"],
            "age_min": 18,
            "age_max": 65,
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "billing_event": "IMPRESSIONS",
            "advantage_audience": True,
            "advantage_placements": True,
        },
        "ads": [
            {"name": "ad_problem", "creative_brief": "creatives/hook_problem.md", "landing_url": p.get("landing_url") or p.get("product_url")},
            {"name": "ad_solution", "creative_brief": "creatives/hook_solution.md", "landing_url": p.get("landing_url") or p.get("product_url")},
            {"name": "ad_product", "creative_brief": "creatives/hook_product.md", "landing_url": p.get("landing_url") or p.get("product_url")},
        ],
        "kill_rules": {
            "day3_roas_below": tp.get("kill_rule_day3", 1.5),
            "day7_roas_below": tp.get("kill_rule_day7", 2.0),
        },
        "scale_rule": {
            "day7_roas_at_or_above": tp.get("scale_rule", 3.0),
            "step_pct": tp.get("scale_step_pct", 0.20),
        },
        "tracking_audit_required": [
            "Pixel firing on Purchase",
            "CAPI deduplication (event_id match)",
            "Attribution window 7d-click 1d-view",
        ],
    }
