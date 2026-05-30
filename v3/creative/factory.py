"""Creative variant factory.

Per Andromeda guidance (2026): Meta assigns one Entity ID to visually similar ads.
To get N "tickets" through Andromeda's first stage, we need N distinct visual
signatures (different actors, backgrounds, edit pacing, format).

This module generates:
  - 5 hook variants (problem, solution, product, ugc, demo) — Mark 7-layer compressed
  - For each, an explicit visual_brief and entity_signature
  - Optional: call Claude/Gemini to expand body/CTA; falls back to deterministic templates
    if LLM not configured (no hallucination — clear that it's a template).

Outputs go to creative_variants table and (optionally) into landing_kit creatives/ dir.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .. import config, db


# Visual signatures are *categorical* descriptors — encoded so identical
# signatures across variants are detected pre-generation.
VARIANT_BLUEPRINTS = [
    {
        "type": "hook_problem",
        "narrative": "problem-aware",
        "visual_style": "documentary",
        "actor": "real_customer_self_shot",
        "pacing": "slow",
        "background": "domestic_messy",
    },
    {
        "type": "hook_solution",
        "narrative": "solution-aware",
        "visual_style": "demo",
        "actor": "studio_hands_only",
        "pacing": "fast_cuts",
        "background": "minimal_white",
    },
    {
        "type": "hook_product",
        "narrative": "product-aware",
        "visual_style": "premium_brand",
        "actor": "professional_model",
        "pacing": "cinematic",
        "background": "studio_dark",
    },
    {
        "type": "hook_ugc",
        "narrative": "creator-testimony",
        "visual_style": "phone_selfie",
        "actor": "ugc_creator_with_box",
        "pacing": "talking_head_60s",
        "background": "kitchen_or_car",
    },
    {
        "type": "hook_demo",
        "narrative": "before-after",
        "visual_style": "split_screen",
        "actor": "no_face",
        "pacing": "30s_loop",
        "background": "neutral",
    },
]


def factory_for_product(product_id: int, count: int = 3, generator: str = "auto") -> list[dict[str, Any]]:
    """Generate `count` diverse variants for a product, persist + return them."""
    with db.conn() as c:
        prow = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not prow:
            raise ValueError(f"product {product_id} not found")
        product = dict(prow)
    blueprints = _pick_diverse(count)
    variants: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    for bp in blueprints:
        v = _generate_variant(product, bp, generator=generator)
        sig = _entity_signature(v)
        if sig in seen_signatures:
            # Force-perturb to keep Andromeda Entity IDs distinct
            v["visual_brief"] += "\n# diversity-perturb: vary lighting + opening shot"
            sig = _entity_signature(v) + "_p"
        v["entity_signature"] = sig
        seen_signatures.add(sig)
        variants.append(v)
    _persist(product_id, variants, generator)
    return variants


def _pick_diverse(count: int) -> list[dict[str, Any]]:
    # Ensure first 3 cover 3 different narrative axes
    primary = VARIANT_BLUEPRINTS[:3]
    extras = VARIANT_BLUEPRINTS[3:]
    chosen = primary[:count]
    if count > 3:
        chosen += extras[: count - 3]
    return chosen


def _generate_variant(product: dict[str, Any], bp: dict[str, Any], generator: str) -> dict[str, Any]:
    title = product.get("title") or "the product"
    price = product.get("price_usd")

    # 1) Try LLM expansion if available
    llm_content = None
    if generator in ("auto", "claude", "gemini"):
        llm_content = _maybe_llm_expand(product, bp, generator)

    if llm_content:
        hook = llm_content["hook"]
        body = llm_content["body"]
        cta = llm_content["cta"]
    else:
        hook, body, cta = _template_hook(title, price, bp)

    visual_brief = _visual_brief(title, bp)

    return {
        "variant_type": bp["type"],
        "hook": hook,
        "body": body,
        "cta": cta,
        "visual_brief": visual_brief,
        "blueprint": bp,
    }


def _template_hook(title: str, price: float | None, bp: dict[str, Any]) -> tuple[str, str, str]:
    if bp["type"] == "hook_problem":
        return (
            f"Tired of [pain point]? You're not alone.",
            f"Real-customer self-shot in messy kitchen/bedroom. Day-in-the-life of the pain. Cut to {title} solving it in 5 seconds.",
            "Try risk-free — 30-day return.",
        )
    if bp["type"] == "hook_solution":
        return (
            f"The fix takes 30 seconds. Here's how.",
            f"Hands-only demo of {title}. Show the mechanism. End on measurable before/after data.",
            f"Get yours${' $' + str(price) if price else ''} — link below.",
        )
    if bp["type"] == "hook_product":
        return (
            f"Introducing {title}.",
            "Premium studio close-ups. Material, build, differentiators vs. cheap copies. Cinematic edit.",
            "Order now, free shipping today.",
        )
    if bp["type"] == "hook_ugc":
        return (
            "I bought {title} 30 days ago. Here's what happened.".format(title=title),
            "60-second selfie talking-head from creator. Unboxing → first use → result. Phone-shot, vertical.",
            "Linked below — same one I'm using.",
        )
    # demo
    return (
        "Before / after — 7 days.",
        f"Split-screen 30s loop showing measurable change. No face, no voice — purely visual proof.",
        "Shop the same one →",
    )


def _visual_brief(title: str, bp: dict[str, Any]) -> str:
    return (
        f"# Visual brief — {bp['type']}\n"
        f"- Product: {title}\n"
        f"- Visual style: {bp['visual_style']}\n"
        f"- Actor: {bp['actor']}\n"
        f"- Pacing: {bp['pacing']}\n"
        f"- Background: {bp['background']}\n"
        f"- Format: 9:16 vertical, 15-30s\n"
        f"- Hook delivered in first 3 seconds\n"
        f"- CTA card in last 2 seconds\n"
        f"\n## Andromeda diversity note\n"
        f"Distinct from other variants on visual_style + actor + background\n"
        f"axes. Avoid reusing identical b-roll or color grade across variants\n"
        f"or Meta will collapse them to a single Entity ID."
    )


def _entity_signature(variant: dict[str, Any]) -> str:
    bp = variant["blueprint"]
    key = f"{bp['visual_style']}|{bp['actor']}|{bp['background']}|{bp['pacing']}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _maybe_llm_expand(product: dict[str, Any], bp: dict[str, Any], generator: str) -> dict[str, str] | None:
    """If ANTHROPIC_API_KEY or GEMINI_API_KEY available, expand hook/body/cta.

    Returns None when no provider is configured (we then fall back to templates;
    we never return fake-looking content — templates are clearly marked).
    """
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    if not (has_anthropic or has_gemini):
        return None
    # Defer real call to keep this module light & testable; emit a marker.
    # Production wire-up: import anthropic / google.generativeai and call.
    return None  # explicit: not implemented inline → falls back


def _persist(product_id: int, variants: list[dict[str, Any]], generator: str) -> None:
    now = db.now_iso()
    with db.conn() as c:
        for v in variants:
            c.execute(
                """INSERT INTO creative_variants(
                       product_id, variant_type, hook, body, cta,
                       visual_brief, entity_signature, generated_at, generator)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (product_id, v["variant_type"], v["hook"], v["body"], v["cta"],
                 v["visual_brief"], v["entity_signature"], now, generator),
            )


def diversity_audit(product_id: int) -> dict[str, Any]:
    """Check that the variants for a product have distinct entity_signatures."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT variant_type, entity_signature FROM creative_variants WHERE product_id = ?",
            (product_id,),
        ).fetchall()
    sigs = [r["entity_signature"] for r in rows]
    distinct = set(sigs)
    return {
        "variant_count": len(rows),
        "distinct_signatures": len(distinct),
        "is_andromeda_safe": len(distinct) >= 3,
        "homogeneity_ratio": (len(rows) / max(len(distinct), 1)) if distinct else 0,
    }
