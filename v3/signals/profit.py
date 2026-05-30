"""Profit signal — BEROAS math, Marcus's Rule, 1688 markup tiers.

This signal is deterministic and depends on no external service.
"""
from __future__ import annotations

import math
from typing import Any

from .. import config
from .._base_local_helpers import classify_category, estimate_source_cost, explain_writer
from ._base import SignalResult


def collect(p: dict[str, Any], raw: dict[str, Any]) -> SignalResult:
    cfg = config.load()["profit"]
    selling_price = _coerce_price(p.get("price_usd")) or _coerce_price(raw.get("price_usd")) or _coerce_price(raw.get("price"))
    if selling_price is None or selling_price <= 0:
        return SignalResult(
            signal="profit",
            status="failed",
            error="missing selling_price",
        )

    category = (p.get("category") or classify_category(p.get("title"), raw)).lower()
    shipping = cfg["default_shipping_usd_by_category"].get(category, cfg["default_shipping_usd_by_category"]["other"])
    refund_rate = cfg["default_refund_rate_by_category"].get(category, cfg["default_refund_rate_by_category"]["other"])
    fee_pct = cfg["fee_pct"]

    cost, cost_method = estimate_source_cost(p, raw, selling_price)
    if cost is None or cost <= 0:
        return SignalResult(
            signal="profit",
            status="failed",
            error="cannot estimate source cost",
        )

    # margin uses ALL variable costs including fees & refund expectation
    expected_loss_per_sale = cost + shipping + selling_price * fee_pct + selling_price * refund_rate
    gross_margin_pct = max(0.0, (selling_price - expected_loss_per_sale) / selling_price * 100.0)
    markup = selling_price / cost
    if gross_margin_pct > 0:
        beroas = 100.0 / gross_margin_pct
    else:
        beroas = float("inf")
    target_roas = beroas * config.get("test_plan.target_roas_multiplier", 1.2)

    # Score buckets per spec
    if gross_margin_pct < 15.0:
        score = 0.0
        explain_writer.queue(p["id"], "profit", "marcus_rule",
                             ">=15% margin", f"{gross_margin_pct:.1f}%", False,
                             "Marcus's Rule violated — paid ads infeasible")
    elif gross_margin_pct < 25.0:
        score = 30.0
    elif gross_margin_pct < 40.0:
        score = 60.0
    elif gross_margin_pct < 60.0:
        score = 85.0
    else:
        score = 100.0

    if markup < cfg["min_markup"]:
        score = min(score, 40.0)
        explain_writer.queue(p["id"], "profit", "min_markup_3x",
                             ">=3.0", f"{markup:.2f}", False,
                             "markup<3x caps profit_score at 40")
    else:
        explain_writer.queue(p["id"], "profit", "min_markup_3x",
                             ">=3.0", f"{markup:.2f}", True, None)

    explain_writer.queue(p["id"], "profit", "gross_margin",
                         ">=15% (paid ads) / >=40% (scale)",
                         f"{gross_margin_pct:.1f}%",
                         gross_margin_pct >= 15.0,
                         f"score={score}")

    if beroas < 2.0:
        band = "healthy"
    elif beroas < 3.5:
        band = "marginal"
    elif beroas < 6.0:
        band = "tight"
    else:
        band = "infeasible"

    return SignalResult(
        signal="profit",
        status="ok",
        score=score,
        data={
            "selling_price_usd": round(selling_price, 2),
            "source_cost_usd": round(cost, 2),
            "cost_method": cost_method,
            "payment_fee_pct": fee_pct,
            "refund_rate_pct": refund_rate,
            "shipping_cost_usd": shipping,
            "gross_margin_pct": round(gross_margin_pct, 2),
            "markup_multiplier": round(markup, 2),
            "beroas": round(beroas, 2) if math.isfinite(beroas) else None,
            "target_roas": round(target_roas, 2) if math.isfinite(target_roas) else None,
            "price_band": band,
        },
    )


def _coerce_price(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
