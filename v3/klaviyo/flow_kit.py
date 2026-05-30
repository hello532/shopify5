"""Klaviyo AI flow kit — generates importable JSON for 3 must-have flows.

Output: JSON files that the user can import into Klaviyo (or use as
documentation for human setup). We don't auto-push because Klaviyo's flow
import is the place a small spec error can break revenue.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config


def _abandoned_cart_flow(product_title: str) -> dict[str, Any]:
    return {
        "name": f"v3 · Abandoned Cart · {product_title}",
        "trigger": {"event": "Checkout Started", "filter": "no Placed Order in last 24h"},
        "steps": [
            {"wait_minutes": 30, "type": "email", "subject": f"Forgot about {product_title}?",
             "body_outline": "1) Hook from creative · 2) 3 reviews · 3) free shipping reminder · 4) CTA"},
            {"wait_hours": 6, "type": "email", "subject": "Still on the fence? Here's what others say",
             "body_outline": "5-star review carousel + 30-day guarantee"},
            {"wait_hours": 24, "type": "sms", "subject": None,
             "body_outline": f"Last chance — your {product_title} is waiting. 10% off code: CART10"},
        ],
    }


def _welcome_flow(brand: str) -> dict[str, Any]:
    return {
        "name": f"v3 · Welcome Series · {brand}",
        "trigger": {"event": "Subscribed to List"},
        "steps": [
            {"wait_minutes": 0, "type": "email", "subject": f"Welcome to {brand} — here's 10% off",
             "body_outline": "Founder story · brand promise · discount code WELCOME10"},
            {"wait_days": 2, "type": "email", "subject": "Best-sellers our community swears by",
             "body_outline": "Top 3 products with social proof"},
            {"wait_days": 5, "type": "email", "subject": "Quick question",
             "body_outline": "Survey: what brought you here? (feeds attribution.py)"},
        ],
    }


def _post_purchase_flow(product_title: str) -> dict[str, Any]:
    return {
        "name": f"v3 · Post-Purchase · {product_title}",
        "trigger": {"event": "Placed Order"},
        "steps": [
            {"wait_minutes": 5, "type": "email", "subject": "Thanks — here's what to expect",
             "body_outline": "ETA · how-to-use guide · contact for help"},
            {"wait_days": 3, "type": "email", "subject": "Should arrive soon — quick tip",
             "body_outline": "First-use tutorial video"},
            {"wait_days": 10, "type": "email", "subject": "How's it going?",
             "body_outline": "Ask for review (Loox/Judge.me trigger) + ATTRIBUTION SURVEY EMBED"},
            {"wait_days": 30, "type": "email", "subject": "Refill / upgrade time",
             "body_outline": "Cross-sell related products from same brand"},
        ],
    }


def generate_kit_for_product(product: dict[str, Any]) -> Path:
    base = config.resolve_path("paths.landing_kits") / f"product_{product['id']}" / "klaviyo"
    base.mkdir(parents=True, exist_ok=True)
    title = product.get("title") or "Product"
    brand = (product.get("shop_domain") or "").split(".")[0].title() or "Brand"
    flows = {
        "abandoned_cart": _abandoned_cart_flow(title),
        "welcome_series": _welcome_flow(brand),
        "post_purchase": _post_purchase_flow(title),
    }
    for name, payload in flows.items():
        (base / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return base
