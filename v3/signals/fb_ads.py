"""FB Ad Library signal — proxies to the 127.0.0.1:8000 service.

Discovery: given a keyword, list candidate ads/products.
Collect:   given a product, fetch its ad-level signals (days_active, impressions, distinct entities).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .. import config
from .._base_local_helpers import explain_writer
from ._base import SignalResult

try:
    import requests
except ImportError as e:
    raise SystemExit("requests required. pip install requests") from e


def _service_url() -> str:
    return config.get("fb_ads.service_url", "http://127.0.0.1:8000").rstrip("/")


def _timeout() -> int:
    return int(config.get("fb_ads.request_timeout_sec", 30))


def _blacklisted(advertiser_id: str | None, advertiser_name: str | None) -> bool:
    bl = [b.lower() for b in config.get("fb_ads.exclude_advertisers", [])]
    target = " ".join(filter(None, [
        (advertiser_id or "").lower(),
        (advertiser_name or "").lower(),
    ]))
    return any(b in target for b in bl)


# -------- discovery --------

def discover_keyword(keyword: str) -> list[dict[str, Any]]:
    """Return a list of normalized product candidates.

    Tries several known endpoints on the FB Ad Library service:
      /v2/scrape?q=...        (fb-ad-scrape-fast style)
      /search?q=...           (generic)
      /api/ads?q=...
    Returns [] on failure — never raises (failures logged downstream).
    """
    base = _service_url()
    timeout = _timeout()
    paths = ["/v2/scrape", "/v3/scrape", "/search", "/api/ads", "/ads"]
    last_error: str | None = None
    for path in paths:
        url = f"{base}{path}?{urlencode({'q': keyword, 'limit': 50})}"
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code >= 400:
                last_error = f"{path}:{r.status_code}"
                continue
            payload = r.json()
            ads = _extract_ads(payload)
            if ads is not None:
                return [_normalize_ad(a, keyword) for a in ads if _filter_ad(a)]
        except Exception as e:
            last_error = f"{path}:{type(e).__name__}"
            continue
    # Fallback: pull from existing shopify_monitor JSON snapshots so the pipeline is testable offline
    return _fallback_from_snapshots(keyword)


def _extract_ads(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("ads", "results", "data", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return None


def _filter_ad(ad: dict[str, Any]) -> bool:
    return not _blacklisted(ad.get("advertiser_id") or ad.get("page_id"),
                            ad.get("advertiser") or ad.get("page_name"))


def _normalize_ad(ad: dict[str, Any], keyword: str) -> dict[str, Any]:
    landing_url = ad.get("landing_url") or ad.get("link_url") or ad.get("destination_url")
    shop_domain = _domain_of(landing_url) if landing_url else (ad.get("shop_domain") or "")
    handle = ad.get("handle") or _handle_of(landing_url)
    return {
        "shop_domain": shop_domain,
        "handle": handle,
        "title": ad.get("title") or ad.get("ad_title") or ad.get("page_name"),
        "price_usd": _coerce_price(ad.get("price") or ad.get("price_usd")),
        "image_url": ad.get("image_url") or ad.get("thumbnail"),
        "product_url": ad.get("product_url") or landing_url,
        "landing_url": landing_url,
        "category": ad.get("category"),
        "advertiser_id": str(ad.get("advertiser_id") or ad.get("page_id") or ""),
        "raw": ad,
        "source_keyword": keyword,
    }


def _coerce_price(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _handle_of(url: str | None) -> str | None:
    if not url:
        return None
    m = url.rstrip("/").split("/products/")
    if len(m) > 1:
        return m[-1].split("?")[0]
    return None


def _fallback_from_snapshots(keyword: str) -> list[dict[str, Any]]:
    """When fb service is offline, gather candidate products from shopify snapshots.

    Treat keyword as a substring match against title/body_html.
    This keeps the pipeline runnable for development; in production fb service must be up.
    """
    out: list[dict[str, Any]] = []
    try:
        snap_dir = config.resolve_path("paths.shopify_snapshots")
    except KeyError:
        return out
    if not snap_dir.exists():
        return out
    kw_lc = keyword.lower()
    seen = 0
    for f in snap_dir.glob("*_snapshot.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        domain = data.get("domain") or f.stem.replace("_snapshot", "")
        for pid, p in (data.get("products") or {}).items():
            title = (p.get("title") or "").lower()
            ptype = (p.get("product_type") or "").lower()
            if kw_lc not in title and kw_lc not in ptype:
                continue
            handle = p.get("handle") or str(pid)
            out.append({
                "shop_domain": domain,
                "handle": handle,
                "title": p.get("title"),
                "price_usd": _coerce_price(p.get("price")),
                "image_url": (p.get("image") or {}).get("src") if isinstance(p.get("image"), dict) else None,
                "product_url": f"https://{domain}/products/{handle}",
                "landing_url": f"https://{domain}/products/{handle}",
                "category": p.get("product_type"),
                "advertiser_id": None,
                "raw": p,
                "source_keyword": keyword,
                "_fallback": "shopify_snapshot",
            })
            seen += 1
            if seen >= 30:
                return out
    return out


# -------- per-product collection --------

def collect(p: dict[str, Any], raw: dict[str, Any]) -> SignalResult:
    """Fetch product-level ad signals from the FB service.

    Returns SignalResult.failed cleanly on any error — never returns fake numbers.
    """
    pid = p["id"]
    base = _service_url()
    timeout = _timeout()
    advertiser_id = p.get("advertiser_id") or raw.get("advertiser_id")
    landing_url = p.get("landing_url") or raw.get("landing_url")

    payload: dict[str, Any] | None = None
    candidates = []
    if advertiser_id:
        candidates.append(f"{base}/v2/advertiser/{advertiser_id}")
        candidates.append(f"{base}/api/advertiser?id={advertiser_id}")
    if landing_url:
        candidates.append(f"{base}/v2/lookup?{urlencode({'landing_url': landing_url})}")
        candidates.append(f"{base}/api/ads?{urlencode({'landing_url': landing_url})}")

    last_error: str | None = None
    for url in candidates:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code >= 400:
                last_error = f"{url}:{r.status_code}"
                continue
            payload = r.json()
            break
        except Exception as e:
            last_error = f"{url}:{type(e).__name__}"
            continue

    if payload is None:
        # Fallback: extract from product raw snapshot if it included some ad data
        if raw.get("active_ads") or raw.get("ad_details"):
            payload = {"ads": raw.get("ad_details", []), "active_ads": raw.get("active_ads", 0)}
        else:
            return SignalResult(
                signal="fb_ads",
                status="failed",
                error=last_error or "no advertiser_id and no landing_url to look up",
            )

    ads = payload.get("ads") or payload.get("results") or payload.get("data") or []
    if not isinstance(ads, list):
        ads = []

    if not ads:
        explain_writer.queue(pid, "fb_ads", "ads_found", ">=1", "0", False, "no ads in library")
        return SignalResult(signal="fb_ads", status="ok", score=0.0,
                            data={"days_active": None, "impressions_total": 0, "distinct_entity_ids": 0,
                                  "creative_count_raw": 0, "countries_running": 0})

    # Aggregate signals
    creative_count_raw = len(ads)
    distinct_entities = {a.get("entity_id") or a.get("snapshot_id") or a.get("id") for a in ads if a.get("entity_id") or a.get("snapshot_id") or a.get("id")}
    distinct_entity_ids = len(distinct_entities) or creative_count_raw  # if no entity_id given, treat each as distinct

    impressions_total = 0
    countries = set()
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    for a in ads:
        imp = a.get("impressions") or a.get("impressions_total") or 0
        if isinstance(imp, dict):
            # FB Ad Library sometimes returns {"lower_bound": "10000", "upper_bound": "100000"}
            try:
                lower = float(imp.get("lower_bound", 0))
                upper = float(imp.get("upper_bound", lower))
                imp = (lower + upper) / 2
            except Exception:
                imp = 0
        try:
            impressions_total += int(float(imp))
        except (TypeError, ValueError):
            pass
        for cc in (a.get("countries") or a.get("delivery_countries") or []):
            countries.add(cc)
        for k in ("ad_creation_time", "start_date", "first_seen"):
            v = a.get(k)
            if v:
                dt = _parse_dt(v)
                if dt and (first_seen is None or dt < first_seen):
                    first_seen = dt
        for k in ("ad_delivery_stop_time", "end_date", "last_seen"):
            v = a.get(k)
            if v:
                dt = _parse_dt(v)
                if dt and (last_seen is None or dt > last_seen):
                    last_seen = dt

    if first_seen is None:
        days_active = None
    else:
        ref = last_seen or datetime.now(timezone.utc)
        days_active = max(1, (ref - first_seen).days)

    advertiser_name = ads[0].get("advertiser") or ads[0].get("page_name")
    if _blacklisted(advertiser_id, advertiser_name):
        explain_writer.queue(pid, "fb_ads", "advertiser_blacklist", "not in blacklist",
                             advertiser_name or advertiser_id or "?", False,
                             "blacklisted advertiser (Amazon/Temu/Shein/etc)")
        return SignalResult(signal="fb_ads", status="ok", score=0.0,
                            data={"days_active": days_active, "impressions_total": impressions_total,
                                  "distinct_entity_ids": distinct_entity_ids,
                                  "creative_count_raw": creative_count_raw,
                                  "countries_running": len(countries),
                                  "advertiser_id": advertiser_id})

    # Score per spec
    min_days = config.get("fb_ads.min_days_active", 14)
    min_imp = config.get("fb_ads.min_impressions", 100000)

    if (days_active or 0) < min_days:
        explain_writer.queue(pid, "fb_ads", "min_days_active", f">={min_days}", days_active, False, None)
        return SignalResult(signal="fb_ads", status="ok", score=0.0,
                            data=_pack(days_active, impressions_total, distinct_entity_ids,
                                       creative_count_raw, len(countries), advertiser_id,
                                       first_seen, last_seen))
    explain_writer.queue(pid, "fb_ads", "min_days_active", f">={min_days}", days_active, True, None)

    if impressions_total < min_imp:
        explain_writer.queue(pid, "fb_ads", "min_impressions", f">={min_imp}", impressions_total, False, None)
        return SignalResult(signal="fb_ads", status="ok", score=0.0,
                            data=_pack(days_active, impressions_total, distinct_entity_ids,
                                       creative_count_raw, len(countries), advertiser_id,
                                       first_seen, last_seen))
    explain_writer.queue(pid, "fb_ads", "min_impressions", f">={min_imp}", impressions_total, True, None)

    import math
    persist = min(40.0, (days_active - min_days) * 1.5) + min(40.0, max(0, days_active - min_days + 1))  # smooth
    persist = min(40.0, (days_active - min_days + 1) * 1.5)
    diversity = min(30.0, distinct_entity_ids * 6.0)
    impression = min(20.0, max(0.0, math.log10(impressions_total / min_imp + 1e-9)) * 10.0) if impressions_total > 0 else 0.0
    country = min(10.0, len(countries) * 2.0)
    score = round(persist + diversity + impression + country, 2)
    score = min(score, 100.0)

    homog_ratio = creative_count_raw / max(distinct_entity_ids, 1)
    homog_flag = "creative_homogeneity_high" if homog_ratio >= config.get("fb_ads.entity_homogeneity_warn_ratio", 3.0) else None

    return SignalResult(
        signal="fb_ads",
        status="ok",
        score=score,
        data=_pack(days_active, impressions_total, distinct_entity_ids,
                   creative_count_raw, len(countries), advertiser_id,
                   first_seen, last_seen, homog_flag),
    )


def _pack(days_active, impressions_total, distinct_entity_ids, creative_count_raw,
          countries_running, advertiser_id, first_seen, last_seen, homog_flag=None):
    return {
        "days_active": days_active,
        "impressions_total": impressions_total,
        "distinct_entity_ids": distinct_entity_ids,
        "creative_count_raw": creative_count_raw,
        "countries_running": countries_running,
        "advertiser_id": advertiser_id,
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
        "homogeneity_flag": homog_flag,
    }


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v[: len(fmt) + 5], fmt) if "%z" in fmt else datetime.strptime(v[:19], fmt[:19] if fmt != "%Y-%m-%d" else fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
