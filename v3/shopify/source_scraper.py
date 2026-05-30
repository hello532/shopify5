"""Source-page scraper for product cloning.

Pulls full product detail from a Shopify storefront's public endpoints:
  - {url}.json — Shopify exposes raw product JSON at product_url + ".json"
  - HTML fallback if .json blocked (parse og:image, h1, description)

All images are referenced by URL; we don't re-host. Shopify productSet accepts
remote image URLs directly via the `files` / `media` field.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import requests

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 v3-clone"


def fetch_source_detail(product_url: str, timeout: int = 20) -> dict[str, Any]:
    """Return {title, descriptionHtml, images, variants, tags, vendor, productType, options}."""
    if not product_url:
        return {}
    # 1) try /<url>.json (Shopify storefront REST)
    json_url = product_url.split("?")[0].rstrip("/") + ".json"
    try:
        r = requests.get(json_url, timeout=timeout, headers={"User-Agent": _UA})
        if r.status_code < 400 and r.headers.get("content-type", "").startswith("application/json"):
            payload = r.json()
            return _from_storefront_json(payload, product_url)
    except Exception:
        pass
    # 2) HTML fallback
    try:
        r = requests.get(product_url, timeout=timeout, headers={"User-Agent": _UA})
        if r.status_code < 400:
            return _from_html(r.text, product_url)
    except Exception:
        pass
    return {}


def _from_storefront_json(payload: dict[str, Any], product_url: str) -> dict[str, Any]:
    p = payload.get("product") or payload
    images = []
    for img in (p.get("images") or []):
        src = img.get("src") if isinstance(img, dict) else img
        if src and src not in images:
            images.append(src)
    variants = []
    for v in (p.get("variants") or []):
        if not isinstance(v, dict):
            continue
        variants.append({
            "title": v.get("title") or "Default",
            "price": _safe_price(v.get("price")),
            "sku": v.get("sku") or "",
            "inventory_quantity": v.get("inventory_quantity"),
            "option1": v.get("option1"),
            "option2": v.get("option2"),
            "option3": v.get("option3"),
        })
    options = []
    for o in (p.get("options") or []):
        if isinstance(o, str):
            options.append({"name": o, "values": []})
        elif isinstance(o, dict):
            options.append({"name": o.get("name") or "Title", "values": o.get("values") or []})
    return {
        "title": p.get("title"),
        "descriptionHtml": p.get("body_html") or "",
        "vendor": p.get("vendor"),
        "productType": p.get("product_type"),
        "tags": _normalize_tags(p.get("tags")),
        "handle": p.get("handle"),
        "images": images,
        "variants": variants,
        "options": options or [{"name": "Title", "values": ["Default"]}],
        "source_url": product_url,
        "source": "storefront_json",
    }


def _from_html(html: str, product_url: str) -> dict[str, Any]:
    title = _meta(html, "og:title") or _re_first(html, r"<title>(.*?)</title>") or ""
    desc = _meta(html, "og:description") or _meta(html, "description") or ""
    images = [u for u in _meta_all(html, "og:image") if u]
    # Try harder: scan all <img src> on the page with product-like paths
    for m in re.finditer(r'<img[^>]+src="([^"]+products[^"]+\.(?:jpg|jpeg|png|webp))"', html, re.I):
        u = m.group(1)
        if u not in images:
            images.append(u)
    images = images[:10]
    return {
        "title": title.strip(),
        "descriptionHtml": f"<p>{desc.strip()}</p>" if desc else "",
        "vendor": urlparse(product_url).netloc.replace("www.", ""),
        "productType": "",
        "tags": [],
        "handle": product_url.rstrip("/").split("/")[-1],
        "images": images,
        "variants": [{"title": "Default", "price": None, "sku": "", "option1": "Default"}],
        "options": [{"name": "Title", "values": ["Default"]}],
        "source_url": product_url,
        "source": "html_parse",
    }


def _meta(html: str, name: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
        html, re.I,
    )
    return m.group(1) if m else None


def _meta_all(html: str, name: str) -> list[str]:
    return [m.group(1) for m in re.finditer(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
        html, re.I,
    )]


def _re_first(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html, re.I | re.S)
    return m.group(1).strip() if m else None


def _safe_price(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if t]
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return []
