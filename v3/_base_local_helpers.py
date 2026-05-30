"""Local helpers shared across signal modules.

- classify_category: keyword-based category guess
- estimate_source_cost: cheap heuristic for 1688/AliExpress source cost
- explain_writer: queues explain_log entries flushed by pipeline
"""
from __future__ import annotations

import re
import threading
from typing import Any

# -------- explain queue --------

class _ExplainQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[tuple] = []

    def queue(
        self,
        product_id: int,
        signal: str,
        rule: str,
        threshold: Any,
        observed: Any,
        passed: bool,
        note: str | None,
    ) -> None:
        with self._lock:
            self._items.append((product_id, signal, rule, threshold, observed, passed, note))

    def drain(self) -> list[tuple]:
        with self._lock:
            items = self._items[:]
            self._items.clear()
            return items


explain_writer = _ExplainQueue()


# -------- category classification --------

_CATEGORY_KEYWORDS = {
    "beauty": [
        "skin", "face", "lip", "hair", "mask", "serum", "beauty", "cosmetic",
        "makeup", "moisturizer", "cleanser", "anti-aging", "led face",
    ],
    "home": [
        "kitchen", "kettle", "lamp", "decor", "cleaning", "sofa", "pillow",
        "blanket", "storage", "organizer", "vacuum",
    ],
    "pet": ["pet", "dog", "cat", "puppy", "kitten", "fish", "hamster"],
    "fitness": ["fitness", "workout", "gym", "yoga", "posture", "resistance"],
    "electronics": [
        "speaker", "headphone", "earbud", "phone", "tablet", "laptop", "charger",
        "camera", "smart", "bluetooth",
    ],
    "accessories": ["watch", "bag", "wallet", "belt", "sunglass", "necklace"],
    "apparel": ["shirt", "dress", "jacket", "pants", "shoe", "sneaker", "hat"],
}


def classify_category(title: str | None, raw: dict[str, Any]) -> str:
    text = " ".join(filter(None, [
        title or "",
        raw.get("ad_text") or "",
        raw.get("body_html") or "",
    ])).lower()
    for cat, words in _CATEGORY_KEYWORDS.items():
        if any(w in text for w in words):
            return cat
    return "other"


# -------- source-cost estimation --------
# Heuristic: 1688 dropshipping typically lands at 3x-5x markup.
# We invert from selling price by category sensitivity, then verify with raw hints.

_BASE_INVERSE_MARKUP = {
    "beauty": 0.22,        # ~4.5x
    "home": 0.25,           # ~4x
    "pet": 0.28,            # ~3.5x
    "fitness": 0.26,
    "electronics": 0.45,    # ~2.2x (electronics have thinner cost ratios)
    "accessories": 0.20,
    "apparel": 0.30,
    "other": 0.28,
}


def estimate_source_cost(p: dict[str, Any], raw: dict[str, Any], selling_price: float) -> tuple[float | None, str]:
    """Return (cost_usd, method). Prefer explicit hints before heuristic."""
    # Direct hint from upstream
    for k in ("source_cost_usd", "cost_usd", "supplier_cost"):
        if raw.get(k):
            try:
                return float(raw[k]), "upstream_hint"
            except (TypeError, ValueError):
                pass
    # 1688 link present → use category-aware heuristic but mark different method
    has_1688 = any(
        "1688" in str(raw.get(k, "")) for k in ("source_url", "supplier_url", "notes")
    )
    cat = (p.get("category") or classify_category(p.get("title"), raw)).lower()
    inv = _BASE_INVERSE_MARKUP.get(cat, _BASE_INVERSE_MARKUP["other"])
    cost = round(selling_price * inv, 2)
    return cost, ("1688_linked_heuristic" if has_1688 else "category_heuristic")
