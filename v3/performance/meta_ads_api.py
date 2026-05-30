"""Meta Marketing API v25 client — fetch ad-level insights.

Reads token + ad_account_id from env or config.yaml:
  META_ACCESS_TOKEN
  META_AD_ACCOUNT_ID  (e.g. "act_1234567890")

Endpoint: GET /v25.0/{AD_OBJECT_ID}/insights
Field set chosen to match v3's kill/scale decision needs.

Source: https://developers.facebook.com/docs/marketing-api/insights/
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from .. import config


API_VERSION = "v25.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

INSIGHTS_FIELDS = [
    "ad_id", "ad_name",
    "adset_id", "adset_name",
    "campaign_id", "campaign_name",
    "date_start", "date_stop",
    "spend", "impressions", "clicks", "reach", "frequency",
    "ctr", "cpc", "cpm",
    "actions", "action_values",
    "cost_per_action_type",
    "purchase_roas",
    "website_purchase_roas",
]


def _token() -> str:
    t = os.environ.get("META_ACCESS_TOKEN") or config.get("meta_ads.access_token")
    if not t:
        raise RuntimeError("META_ACCESS_TOKEN not set (env or config.meta_ads.access_token)")
    return t


def _account() -> str:
    a = os.environ.get("META_AD_ACCOUNT_ID") or config.get("meta_ads.ad_account_id")
    if not a:
        raise RuntimeError("META_AD_ACCOUNT_ID not set")
    if not a.startswith("act_"):
        a = "act_" + a
    return a


def fetch_ad_insights(
    since: datetime | None = None,
    until: datetime | None = None,
    level: str = "ad",
) -> list[dict[str, Any]]:
    """Pull ad-level insights for the configured account.

    Returns normalized rows (one per ad per day) with derived ROAS/CPA.
    Raises RuntimeError on auth issues; returns [] on empty windows.
    """
    until = until or datetime.utcnow()
    since = since or (until - timedelta(days=7))

    params = {
        "access_token": _token(),
        "level": level,
        "fields": ",".join(INSIGHTS_FIELDS),
        "time_increment": 1,  # daily breakdown
        "time_range": f'{{"since":"{since.strftime("%Y-%m-%d")}","until":"{until.strftime("%Y-%m-%d")}"}}',
        "limit": 500,
    }
    url = f"{BASE_URL}/{_account()}/insights?{urlencode(params)}"

    rows: list[dict[str, Any]] = []
    timeout = int(config.get("meta_ads.request_timeout_sec", 30))
    while url:
        r = requests.get(url, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Meta API error {r.status_code}: {r.text[:300]}")
        payload = r.json()
        for raw in payload.get("data", []):
            rows.append(_normalize_insight(raw))
        url = (payload.get("paging") or {}).get("next")
    return rows


def _normalize_insight(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract a flat row from a single insights line item."""
    spend = _f(raw.get("spend"))
    impressions = _i(raw.get("impressions"))
    clicks = _i(raw.get("clicks"))
    actions = {a["action_type"]: _i(a.get("value")) for a in (raw.get("actions") or [])}
    action_values = {a["action_type"]: _f(a.get("value")) for a in (raw.get("action_values") or [])}

    purchases = (
        actions.get("purchase")
        or actions.get("offsite_conversion.fb_pixel_purchase")
        or actions.get("omni_purchase")
        or 0
    )
    revenue = (
        action_values.get("purchase")
        or action_values.get("offsite_conversion.fb_pixel_purchase")
        or action_values.get("omni_purchase")
        or 0.0
    )
    add_to_cart = actions.get("add_to_cart") or actions.get("offsite_conversion.fb_pixel_add_to_cart") or 0
    roas_field = raw.get("purchase_roas") or raw.get("website_purchase_roas") or []
    roas = _f(roas_field[0].get("value")) if isinstance(roas_field, list) and roas_field else None
    if roas is None and spend and revenue:
        roas = round(revenue / spend, 3)
    cpa = round(spend / purchases, 2) if purchases and spend else None
    ctr = _f(raw.get("ctr"))
    cpc = _f(raw.get("cpc"))
    cpm = _f(raw.get("cpm"))

    return {
        "ad_id": raw.get("ad_id"),
        "ad_name": raw.get("ad_name"),
        "ad_set_id": raw.get("adset_id"),
        "ad_set_name": raw.get("adset_name"),
        "campaign_id": raw.get("campaign_id"),
        "campaign_name": raw.get("campaign_name"),
        "date_iso": raw.get("date_start"),
        "spend_usd": spend,
        "impressions": impressions,
        "clicks": clicks,
        "add_to_cart": add_to_cart,
        "purchases": purchases,
        "revenue_usd": revenue,
        "roas": roas,
        "cpa": cpa,
        "ctr": ctr,
        "cpc": cpc,
        "cpm": cpm,
        "raw": raw,
    }


def pause_ad(ad_id: str) -> dict[str, Any]:
    """POST /{ad_id} with status=PAUSED. Returns API response."""
    url = f"{BASE_URL}/{ad_id}?access_token={_token()}"
    r = requests.post(url, data={"status": "PAUSED"}, timeout=int(config.get("meta_ads.request_timeout_sec", 30)))
    if r.status_code >= 400:
        raise RuntimeError(f"pause_ad {ad_id} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def update_adset_budget(ad_set_id: str, daily_budget_cents: int) -> dict[str, Any]:
    """Increase or decrease daily_budget on an ad set (cents)."""
    url = f"{BASE_URL}/{ad_set_id}?access_token={_token()}"
    r = requests.post(url, data={"daily_budget": str(daily_budget_cents)}, timeout=int(config.get("meta_ads.request_timeout_sec", 30)))
    if r.status_code >= 400:
        raise RuntimeError(f"update_adset_budget {ad_set_id} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int:
    f = _f(v)
    return int(f) if f is not None else 0
