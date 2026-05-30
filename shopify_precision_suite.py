#!/usr/bin/env python3
"""Precision audit helpers for Shopify monitor snapshots.

This module stays independent from the FastAPI server so scraping quality
checks can be tested without starting uvicorn.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PRECISION_AUDIT_VERSION = "scrape-precision-v1"
PRECISION_AUDIT_CACHE_NAME = "scrape_precision_audit_cache.json"


def _text(value: Any, default: str = "") -> str:
    if value in (None, ""):
        return default
    if isinstance(value, (list, tuple, set)):
        value = " ".join(str(v) for v in value if v not in (None, ""))
    elif isinstance(value, dict):
        value = " ".join(str(v) for v in value.values() if v not in (None, ""))
    return re.sub(r"\s+", " ", str(value)).strip() or default


def _clean_html(value: Any, limit: int = 1800) -> str:
    text = _text(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _money(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group(0)) if match else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_money(value, default)))
    except Exception:
        return default


def _domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0].split(":", 1)[0].strip(".")
    return text[4:] if text.startswith("www.") else text


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _age_days(value: Any) -> float:
    parsed = _parse_dt(value)
    if parsed is None:
        return 9999.0
    return max(0.0, (datetime.now() - parsed).total_seconds() / 86400)


def _products_from_snapshot(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("products", {})
    if isinstance(raw, dict):
        return [p for p in raw.values() if isinstance(p, dict)]
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    return []


def _product_images(product: dict[str, Any]) -> list[str]:
    images = product.get("images")
    out: list[str] = []
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict):
                src = item.get("src") or item.get("url")
            else:
                src = item
            src = _text(src)
            if src:
                out.append(src)
    for key in ("image", "image_url", "main_image", "thumbnail"):
        src = _text(product.get(key))
        if src and src not in out:
            out.append(src)
    return out


def _product_image_count(product: dict[str, Any]) -> int:
    images = _product_images(product)
    explicit = max(
        _safe_int(product.get("image_count"), 0),
        _safe_int(product.get("images_count"), 0),
        _safe_int(product.get("图片数"), 0),
    )
    return max(len(images), explicit)


def _product_price(product: dict[str, Any]) -> float:
    price = _money(product.get("price"), 0)
    if price > 0:
        return price
    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                price = _money(variant.get("price"), 0)
                if price > 0:
                    return price
    return 0.0


def product_quality(product: dict[str, Any]) -> dict[str, Any]:
    """Return field-level quality signals for one scraped product."""

    title = _text(product.get("title") or product.get("product_title"))
    handle = _text(product.get("handle"))
    price = _product_price(product)
    image_count = _product_image_count(product)
    description = _clean_html(
        product.get("body_html")
        or product.get("description")
        or product.get("描述摘要")
        or product.get("summary")
    )
    variants = product.get("variants") if isinstance(product.get("variants"), list) else []
    variant_count = max(
        len(variants),
        _safe_int(product.get("variant_count"), 0),
        _safe_int(product.get("variants_count"), 0),
    )
    tags = product.get("tags")
    tag_count = len(tags) if isinstance(tags, list) else len([t for t in str(tags or "").split(",") if t.strip()])
    errors: list[str] = []
    warnings: list[str] = []
    score = 100

    if len(title) < 8:
        errors.append("title_missing_or_too_short")
        score -= 28
    elif len(title) < 24:
        warnings.append("title_thin")
        score -= 8
    if price <= 0:
        errors.append("price_missing")
        score -= 22
    if image_count <= 0:
        errors.append("image_missing")
        score -= 24
    elif image_count < 3:
        warnings.append("image_count_low")
        score -= 8
    if len(description) < 80:
        warnings.append("description_thin")
        score -= 10
    if not handle:
        warnings.append("handle_missing")
        score -= 5
    if variant_count <= 0:
        warnings.append("variant_data_missing")
        score -= 6
    if tag_count == 0:
        warnings.append("tags_missing")
        score -= 4

    return {
        "title": title,
        "handle": handle,
        "price": round(price, 2),
        "image_count": image_count,
        "description_length": len(description),
        "variant_count": variant_count,
        "tag_count": tag_count,
        "score": max(0, min(100, score)),
        "errors": errors,
        "warnings": warnings,
    }


def _precision_cache_path(root: Path) -> Path:
    return root / PRECISION_AUDIT_CACHE_NAME


def _cached_audit_is_fresh(path: Path, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0 or not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) <= max_age_seconds


def read_cached_scrape_precision(
    monitor_dir: Path | str,
    limit: int = 30,
    max_age_seconds: int = 900,
) -> dict[str, Any]:
    """Return a recent precision audit without scanning all snapshots."""

    root = Path(monitor_dir)
    cache_path = _precision_cache_path(root)
    if not _cached_audit_is_fresh(cache_path, max_age_seconds):
        return {
            "ok": False,
            "cached": False,
            "stale": cache_path.exists(),
            "path": str(cache_path),
            "message": "No fresh precision audit cache",
            "summary": {},
            "focus_domains": [],
        }
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "cached": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "path": str(cache_path),
            "summary": {},
            "focus_domains": [],
        }
    cached["cached"] = True
    cached["cache_path"] = str(cache_path)
    cached["cache_age_seconds"] = round(time.time() - cache_path.stat().st_mtime, 1)
    cached["focus_domains"] = (cached.get("focus_domains") or [])[: max(1, min(int(limit or 30), 200))]
    return cached


def audit_snapshot_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    domain = _domain(data.get("domain") or path.stem.replace("_snapshot", ""))
    products = _products_from_snapshot(data)
    qualities = [product_quality(p) for p in products]
    product_count = max(_safe_int(data.get("product_count"), len(products)), len(products))
    duplicate_keys: Counter[str] = Counter()
    for quality in qualities:
        key = quality["handle"] or re.sub(r"[^a-z0-9]+", " ", quality["title"].lower()).strip()
        if key:
            duplicate_keys[key] += 1
    duplicate_count = sum(count - 1 for count in duplicate_keys.values() if count > 1)
    missing_images = sum(1 for q in qualities if "image_missing" in q["errors"])
    thin_images = sum(1 for q in qualities if q["image_count"] < 3)
    missing_prices = sum(1 for q in qualities if "price_missing" in q["errors"])
    thin_descriptions = sum(1 for q in qualities if q["description_length"] < 80)
    quality_scores = [q["score"] for q in qualities]
    avg_quality = round(sum(quality_scores) / len(quality_scores), 1) if quality_scores else 0.0
    last_check = data.get("last_check") or data.get("generated_at") or data.get("scan_time") or ""
    age = _age_days(last_check)
    coverage = {
        "price": round((len(qualities) - missing_prices) / max(len(qualities), 1) * 100, 1),
        "image": round((len(qualities) - missing_images) / max(len(qualities), 1) * 100, 1),
        "multi_image": round((len(qualities) - thin_images) / max(len(qualities), 1) * 100, 1),
        "description": round((len(qualities) - thin_descriptions) / max(len(qualities), 1) * 100, 1),
    }
    penalty = 0
    if age > 3:
        penalty += min(18, int(age // 2))
    if duplicate_count:
        penalty += min(12, duplicate_count * 2)
    if product_count and len(products) < product_count * 0.6:
        penalty += 10
    precision_score = max(0, min(100, round(avg_quality - penalty, 1)))
    reasons: list[str] = []
    if age > 3:
        reasons.append(f"snapshot_stale_{age:.1f}d")
    if missing_images or thin_images:
        reasons.append("image_coverage_low")
    if thin_descriptions:
        reasons.append("description_coverage_low")
    if missing_prices:
        reasons.append("price_coverage_low")
    if duplicate_count:
        reasons.append("duplicate_products")
    if product_count and len(products) < product_count * 0.6:
        reasons.append("partial_snapshot")
    return {
        "domain": domain,
        "path": str(path),
        "product_count": product_count,
        "loaded_products": len(products),
        "last_check": str(last_check),
        "age_days": round(age, 2),
        "precision_score": precision_score,
        "coverage": coverage,
        "issues": {
            "missing_images": missing_images,
            "thin_images": thin_images,
            "missing_prices": missing_prices,
            "thin_descriptions": thin_descriptions,
            "duplicates": duplicate_count,
        },
        "refresh_reasons": reasons,
        "sample_gaps": [q for q in qualities if q["errors"] or q["warnings"]][:5],
    }


def audit_scrape_precision(monitor_dir: Path | str, limit: int = 30) -> dict[str, Any]:
    """Audit local snapshot quality and return precision-first refresh targets."""

    root = Path(monitor_dir)
    rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for path in sorted(root.glob("*_snapshot.json")):
        try:
            rows.append(audit_snapshot_file(path))
        except Exception as exc:
            failed.append({"path": str(path), "error": f"{exc.__class__.__name__}: {exc}"})

    if not rows:
        return {
            "ok": False,
            "version": PRECISION_AUDIT_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "message": "No snapshot files found",
            "summary": {},
            "focus_domains": [],
            "failed_files": failed[:20],
        }

    total_products = sum(int(r["loaded_products"]) for r in rows)
    avg_precision = round(sum(float(r["precision_score"]) for r in rows) / len(rows), 1)
    issue_totals = Counter()
    for row in rows:
        issue_totals.update(row.get("issues", {}))
    stale_count = sum(1 for r in rows if float(r.get("age_days") or 0) > 3)
    low_precision = sum(1 for r in rows if float(r.get("precision_score") or 0) < 76)
    rows.sort(
        key=lambda r: (
            float(r.get("precision_score") or 0),
            -len(r.get("refresh_reasons") or []),
            -float(r.get("age_days") or 0),
            str(r.get("domain") or ""),
        )
    )
    focus_limit = max(1, min(int(limit or 30), 200))
    result = {
        "ok": True,
        "cached": False,
        "version": PRECISION_AUDIT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "snapshot_files": len(rows),
            "loaded_products": total_products,
            "avg_precision_score": avg_precision,
            "low_precision_domains": low_precision,
            "stale_domains": stale_count,
            "failed_files": len(failed),
            "issue_totals": dict(issue_totals),
        },
        "strategy": [
            "优先补刷低 precision_score 域名，而不是盲目全站重扫。",
            "刷新后保留历史快照基线，避免把反爬临时空结果误判成商品下架。",
            "每个产品最少要拿到 title、price、3+ images、description、variants，才能进入 AI 跟品判断。",
        ],
        "focus_domains": rows[:focus_limit],
        "failed_files": failed[:20],
    }
    cache_path = _precision_cache_path(root)
    try:
        cache_payload = {**result, "focus_domains": rows}
        cache_path.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result["cache_path"] = str(cache_path)
    except Exception as exc:
        result["cache_error"] = f"{exc.__class__.__name__}: {exc}"
    return result
