"""Landing-page signal — fingerprint detection + Mark 7-layer Awareness match.

No mock. If fetch fails, returns SignalResult.failed.
"""
from __future__ import annotations

import re
from typing import Any

from .. import config
from .._base_local_helpers import explain_writer
from ._base import SignalResult

try:
    import requests
except ImportError as e:
    raise SystemExit("requests required") from e


# Mark's 7 Awareness layers (Most → Least aware), with detection cues.
AWARENESS_LAYERS = [
    ("most_aware",          ["buy now", "order now", "free shipping", "discount code", "%"]),
    ("product_aware",       ["our product", "introducing", "this is", "patented", "limited"]),
    ("solution_aware",      ["solution", "fix", "relieves", "improves", "boost", "stop"]),
    ("problem_aware",       ["tired of", "struggle", "do you suffer", "frustrated", "annoying"]),
    ("pain_aware",          ["pain", "ache", "stress", "fatigue", "embarrassing"]),
    ("desire_aware",        ["love", "wish", "dream", "imagine", "transform"]),
    ("unaware",             ["did you know", "studies show", "research"]),
]


def collect(p: dict[str, Any], raw: dict[str, Any]) -> SignalResult:
    pid = p["id"]
    url = p.get("landing_url") or p.get("product_url") or raw.get("landing_url")
    if not url:
        return SignalResult(signal="lp", status="failed", error="no landing_url")

    timeout = int(config.get("landing_page.request_timeout_sec", 30))
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 v3-lp"})
    except Exception as e:
        return SignalResult(signal="lp", status="failed", error=f"{type(e).__name__}: {e}")
    if r.status_code >= 400:
        return SignalResult(signal="lp", status="failed", error=f"HTTP {r.status_code}")
    html = r.text or ""
    html_lc = html.lower()

    has_shopify = (
        "cdn.shopify.com" in html_lc
        or "shopify.theme" in html_lc
        or "shopify.routes" in html_lc
        or "x-shopify" in (r.headers.get("x-shopify-stage", "") + " ").lower()
    )
    has_klaviyo = "klaviyo" in html_lc or "kl_id" in html_lc
    has_reviews_app = any(k in html_lc for k in ("loox", "judge.me", "judgeme", "stamped", "yotpo", "okendo", "reviews.io"))
    has_pixel = "fbq(" in html_lc or "facebook.net/en_us/fbevents.js" in html_lc or '"meta_pixel"' in html_lc
    has_capi = "conversion-api" in html_lc or "capi" in html_lc  # weak signal; mostly server-side

    payment_methods = sum(
        bool(re.search(p, html_lc))
        for p in [
            r"shop[\- _]?pay", r"apple[\- _]?pay", r"google[\- _]?pay",
            r"paypal", r"klarna", r"afterpay", r"sezzle", r"amazon[\- _]?pay",
        ]
    )

    has_video_hero = bool(re.search(r"<video[^>]*(autoplay|class=['\"][^'\"]*hero)", html, re.I))
    has_comparison_chart = any(k in html_lc for k in ("comparison", "vs. competitors", "vs others", "compare"))
    has_ugc_block = any(k in html_lc for k in ("ugc", "customer photos", "verified buyer", "real customer", "@customer"))

    # Awareness signals (headlines/heroes typically in <h1>/<h2>)
    headlines = " ".join(re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.I | re.S))
    headlines_lc = re.sub(r"<[^>]+>", " ", headlines).lower()
    layer_scores: dict[str, int] = {}
    for layer, cues in AWARENESS_LAYERS:
        hits = sum(headlines_lc.count(c) for c in cues)
        if hits > 0:
            layer_scores[layer] = hits
    match_layer = max(layer_scores, key=lambda k: layer_scores[k]) if layer_scores else None
    awareness_signals = [{"layer": k, "hits": v} for k, v in sorted(layer_scores.items(), key=lambda kv: -kv[1])]

    # Required gates
    required = config.get("landing_page.required", ["shopify", "pixel"])
    flag_map = {"shopify": has_shopify, "klaviyo": has_klaviyo, "reviews_app": has_reviews_app,
                "pixel": has_pixel, "capi": has_capi}
    for req in required:
        passed = bool(flag_map.get(req, False))
        explain_writer.queue(pid, "lp", f"requires_{req}", "True", str(passed), passed, None)
        if not passed:
            return SignalResult(
                signal="lp",
                status="ok",
                score=0.0,
                data={
                    "has_shopify": has_shopify, "has_klaviyo": has_klaviyo,
                    "has_reviews_app": has_reviews_app, "has_pixel": has_pixel,
                    "has_capi": has_capi, "payment_methods_count": payment_methods,
                    "has_video_hero": has_video_hero, "has_comparison_chart": has_comparison_chart,
                    "has_ugc_block": has_ugc_block, "awareness_match_layer": match_layer,
                    "awareness_signals": awareness_signals,
                },
            )

    # Scoring: required (30) + bonus (30) + awareness (30) + extras (10)
    base = 30.0
    bonus_keys = config.get("landing_page.professional_bonus", [])
    bonus_map = {
        "klaviyo": has_klaviyo, "reviews_app": has_reviews_app,
        "video_hero": has_video_hero, "comparison_chart": has_comparison_chart,
        "ugc_block": has_ugc_block,
    }
    bonus = sum(6.0 for k in bonus_keys if bonus_map.get(k))
    bonus = min(30.0, bonus)
    awareness = 30.0 if match_layer in ("problem_aware", "solution_aware", "product_aware") else (15.0 if match_layer else 0.0)
    extras = min(10.0, payment_methods * 2.5)
    score = round(base + bonus + awareness + extras, 2)

    return SignalResult(
        signal="lp",
        status="ok",
        score=min(100.0, score),
        data={
            "has_shopify": has_shopify, "has_klaviyo": has_klaviyo,
            "has_reviews_app": has_reviews_app, "has_pixel": has_pixel,
            "has_capi": has_capi, "payment_methods_count": payment_methods,
            "has_video_hero": has_video_hero, "has_comparison_chart": has_comparison_chart,
            "has_ugc_block": has_ugc_block, "awareness_match_layer": match_layer,
            "awareness_signals": awareness_signals,
        },
    )
