#!/usr/bin/env python3
"""Auto intelligence loop for FB Ads signal + Shopify product draft generation.

This module is intentionally independent from ``shopify_api_server.py`` so the
8001 API can call it without inheriting another large block of business logic.
It reads local Shopify monitor snapshots, enriches products with 8000-side FB
signals, and writes review-ready artifacts. It never publishes products or
changes a Shopify store.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MONITOR_DIR = BASE_DIR / "output" / "shopify_monitor"
DEFAULT_OUTPUT_ROOT = BASE_DIR / "output" / "auto_intelligence"
DEFAULT_VALIDATION_DIR = BASE_DIR / "output" / "ecommerce_rank_monitor"
DEFAULT_GTRENDS_DIR = Path("/Users/doi/Desktop/Selection/gtrends")
DEFAULT_GTRENDS_BULK_DIR = Path("/Users/doi/.claude/skills/gtrends-bulk")
MONEY_DECISION_PROMPT_VERSION = "money-decision-v3-platform-ai"
SEVEN_DAY_METHOD_VERSION = "seven-day-new-products-v4-platform-ai"
PROFIT_PIPELINE_VERSION = "profit-pipeline-v3-launch-observe-avoid"
_PLATFORM_VALIDATION_CACHE: JsonDict | None = None

# Local FB pipeline (127.0.0.1:8000) opener that bypasses the macOS system
# proxy. Without an empty ProxyHandler, urllib reads the host's SOCKS/HTTP
# proxy from System Preferences and routes localhost calls through it, which
# yields HTTP 503 for every brand lookup. Built once at import time.
_FB_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


JsonDict = dict[str, Any]
SignalMap = dict[str, JsonDict]
SignalProvider = Callable[["AutoIntelConfig"], tuple[SignalMap, list[str]]]
TrendProductProvider = Callable[[list[JsonDict], str, int], tuple[list[JsonDict | None], JsonDict, list[str]]]


@dataclass
class AutoIntelConfig:
    """Runtime configuration for one auto-intelligence run."""

    monitor_dir: Path = field(default_factory=lambda: DEFAULT_MONITOR_DIR)
    output_root: Path = field(default_factory=lambda: DEFAULT_OUTPUT_ROOT)
    fb_api_base: str = "http://127.0.0.1:8000"
    limit: int = 20
    min_score: int = 60
    fb_limit: int = 5000
    lookback_days: int = 2
    max_products: int = 900
    max_per_domain: int = 3
    max_per_family: int = 1
    research_min_score: int = 45
    watchlist_limit: int = 30
    own_vendor: str = "AUTO-INTEL"
    as_of_date: str = ""
    fb_timeout_seconds: float = 8.0
    fb_exact_verify_limit: int = 300
    enable_trends: bool = False
    trend_top_n: int = 50
    trend_geo: str = "US"
    trend_max_keywords: int = 150
    lock_stale_seconds: int = 1800


class AutoIntelRunLocked(RuntimeError):
    """Raised when another auto-intelligence run is already writing artifacts."""


def normalize_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0].split(":")[0]
    return domain[4:] if domain.startswith("www.") else domain


def slugify(text: str, fallback: str = "product") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or fallback


def product_family_key(item: JsonDict) -> str:
    """Build a stable family key so offer variants do not crowd the shortlist."""

    domain = normalize_domain(item.get("domain", ""))
    title = clean_product_title(str(item.get("title", ""))).lower()
    title = re.sub(r"\b\d+\s*x\b", " ", title)
    title = re.sub(r"\b\d+\s*(pack|set|pcs?|pieces?|ct|count)\b", " ", title)
    title = re.sub(r"\b(buy|free|offer|sale|secret|spring|summer|winter|limited|edition)\b", " ", title)
    words = re.findall(r"[a-z0-9]+", title)
    core = [w for w in words if len(w) > 2 and w not in FAMILY_STOPWORDS]
    return f"{domain}::{ '-'.join(core[:10]) or slugify(title) }"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_recent_products(config: AutoIntelConfig) -> tuple[list[JsonDict], JsonDict, list[str]]:
    """Load recent products from local ``*_snapshot.json`` files."""

    warnings: list[str] = []
    as_of = _parse_date(config.as_of_date) or datetime.now()
    valid_dates = {
        (as_of - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(max(1, config.lookback_days))
    }
    products: list[JsonDict] = []
    scanned_snapshots = 0

    for fp in sorted(Path(config.monitor_dir).glob("*_snapshot.json")):
        scanned_snapshots += 1
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"snapshot_read_failed:{fp.name}:{exc.__class__.__name__}")
            continue

        domain = normalize_domain(data.get("domain") or fp.stem.replace("_snapshot", ""))
        raw_products = data.get("products", {})
        if isinstance(raw_products, dict):
            iterable = list(raw_products.items())
            store_product_count = len(raw_products)
        elif isinstance(raw_products, list):
            iterable = [(str(i), p) for i, p in enumerate(raw_products)]
            store_product_count = len(raw_products)
        else:
            continue

        for key, raw in iterable:
            if not isinstance(raw, dict):
                continue
            entry = _product_from_snapshot(domain, key, raw)
            if not entry:
                continue
            if not _is_recent(entry, valid_dates):
                continue
            entry["store_product_count"] = store_product_count
            products.append(entry)

    products.sort(key=lambda p: (_recent_sort_key(p), safe_float(p.get("price"))), reverse=True)
    if config.max_products > 0:
        products = products[: config.max_products]

    stats = {
        "snapshot_files": scanned_snapshots,
        "recent_products": len(products),
        "lookback_dates": sorted(valid_dates, reverse=True),
    }
    return products, stats, warnings


def load_newly_listed_products(
    config: AutoIntelConfig,
    days: int = 7,
) -> tuple[list[JsonDict], JsonDict, list[str]]:
    """Load products whose created/published date is inside the listing window."""

    warnings: list[str] = []
    as_of = _parse_date(config.as_of_date) or datetime.now()
    valid_dates = {
        (as_of - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(max(1, int(days)))
    }
    products: list[JsonDict] = []
    skipped_old_snapshots = 0
    failed_snapshots = 0
    snapshot_files: list[Path] = []

    for fp in sorted(Path(config.monitor_dir).glob("*_snapshot.json")):
        if not config.as_of_date:
            try:
                snapshot_date = datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d")
            except OSError as exc:
                failed_snapshots += 1
                warnings.append(f"snapshot_stat_failed:{fp.name}:{exc.__class__.__name__}")
                continue
            if snapshot_date not in valid_dates:
                skipped_old_snapshots += 1
                continue
        snapshot_files.append(fp)

    max_workers = min(12, max(1, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_load_new_products_from_snapshot, fp, valid_dates)
            for fp in snapshot_files
        ]
        for future in as_completed(futures):
            loaded, warning = future.result()
            products.extend(loaded)
            if warning:
                failed_snapshots += 1
                warnings.append(warning)

    products.sort(
        key=lambda p: (_new_product_date(p), safe_float(p.get("price"))),
        reverse=True,
    )
    if config.max_products > 0:
        products = products[: config.max_products]

    stats = {
        "snapshot_files": len(snapshot_files),
        "snapshot_files_skipped_old": skipped_old_snapshots,
        "snapshot_files_failed": failed_snapshots,
        "recent_products": len(products),
        "lookback_dates": sorted(valid_dates, reverse=True),
    }
    return products, stats, warnings


def _load_new_products_from_snapshot(fp: Path, valid_dates: set[str]) -> tuple[list[JsonDict], str]:
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], f"snapshot_read_failed:{fp.name}:{exc.__class__.__name__}"

    domain = normalize_domain(data.get("domain") or fp.stem.replace("_snapshot", ""))
    raw_products = data.get("products", {})
    if isinstance(raw_products, dict):
        iterable = list(raw_products.items())
        store_product_count = len(raw_products)
    elif isinstance(raw_products, list):
        iterable = [(str(i), p) for i, p in enumerate(raw_products)]
        store_product_count = len(raw_products)
    else:
        return [], ""

    products: list[JsonDict] = []
    for key, raw in iterable:
        if not isinstance(raw, dict):
            continue
        entry = _product_from_snapshot(domain, key, raw)
        if not entry:
            continue
        entry["store_product_count"] = store_product_count
        if _new_product_date(entry) in valid_dates:
            products.append(entry)
    return products, ""


def fetch_fb_signals(config: AutoIntelConfig) -> tuple[SignalMap, list[str]]:
    """Fetch FB-side brand signals from the 8000 pipeline."""

    warnings: list[str] = []
    if not config.fb_api_base:
        return {}, ["fb_api_disabled"]

    url = f"{config.fb_api_base.rstrip('/')}/brands?limit={max(1, config.fb_limit)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "auto-intelligence-loop/1.0"})
        timeout = max(1.0, min(float(config.fb_timeout_seconds), 30.0))
        with _FB_LOCAL_OPENER.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        return {}, [f"fb_signal_fetch_failed:{exc.__class__.__name__}:{exc}"]

    signals: SignalMap = {}
    if not isinstance(payload, list):
        return signals, ["fb_signal_payload_not_list"]

    for row in payload:
        if not isinstance(row, dict):
            continue
        domain = normalize_domain(row.get("domain", ""))
        if not domain:
            continue
        signals[domain] = _fb_signal_from_8000_row(row, source="bulk_brands")

    return signals, warnings


def verify_missing_fb_signals(
    config: AutoIntelConfig,
    products: list[JsonDict],
    signals: SignalMap,
) -> tuple[JsonDict, list[str]]:
    """Use 8000's exact brand endpoint for domains missing from the bulk window."""

    stats = {"checked": 0, "matched": 0, "not_found": 0, "failed": 0, "skipped": 0}
    warnings: list[str] = []
    if not config.fb_api_base:
        stats["skipped"] = 1
        return stats, ["fb_exact_verify_disabled"]
    if config.fb_exact_verify_limit <= 0:
        stats["skipped"] = 1
        return stats, []

    missing_domains: list[str] = []
    seen: set[str] = set()
    for product in products:
        domain = normalize_domain(product.get("domain", ""))
        if not domain or domain in signals or domain in seen:
            continue
        seen.add(domain)
        missing_domains.append(domain)
        if len(missing_domains) >= config.fb_exact_verify_limit:
            break

    for domain in missing_domains:
        signal, warning = fetch_exact_fb_signal(config, domain)
        signals[domain] = signal
        stats["checked"] += 1
        status = signal.get("fb_verification_status")
        if status == "matched":
            stats["matched"] += 1
        elif status == "not_found":
            stats["not_found"] += 1
        else:
            stats["failed"] += 1
        if warning:
            warnings.append(warning)

    return stats, warnings


def enrich_top_products_with_trends(
    products: list[JsonDict],
    geo: str = "US",
    max_keywords: int = 18,
) -> tuple[list[JsonDict | None], JsonDict, list[str]]:
    """Validate the top products with the shared gtrends engine.

    The provider can return true Google Trends time-series records, Google
    Suggest proxy records, or no result. Downstream code labels those sources
    differently so we do not pretend proxy data is real trend proof.
    """

    warnings: list[str] = []
    if not products:
        return [], _empty_trend_stats(), warnings

    gtrends_dir = Path(os.environ.get("AUTO_INTEL_GTRENDS_DIR", str(DEFAULT_GTRENDS_DIR)))
    if not gtrends_dir.exists():
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "provider": "missing",
        }, [f"gtrends_dir_missing:{gtrends_dir}"]

    if str(gtrends_dir) not in sys.path:
        sys.path.insert(0, str(gtrends_dir))

    try:
        from gtrends_router import _validate_keywords
    except Exception as exc:
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "provider": "import_failed",
        }, [f"gtrends_import_failed:{exc.__class__.__name__}:{exc}"]

    product_keywords, keywords = _trend_keyword_index(products, max_keywords)
    if not keywords:
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "provider": "no_keywords",
        }, ["gtrends_no_keywords"]

    try:
        raw_trends = _validate_keywords(keywords, (geo or "US").upper())
    except Exception as exc:
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "requested_keywords": len(keywords),
            "provider": "validate_failed",
        }, [f"gtrends_validate_failed:{exc.__class__.__name__}:{exc}"]

    enriched: list[JsonDict | None] = []
    for keywords_for_product in product_keywords:
        best = _best_trend_record(keywords_for_product, raw_trends)
        enriched.append(best)

    stats = _trend_stats_from_records(enriched, len(products), len(keywords), raw_trends)
    return enriched, stats, warnings


def enrich_top_products_with_gtrends_bulk(
    products: list[JsonDict],
    geo: str = "US",
    max_keywords: int = 18,
    timeframe: str = "today 12-m",
    workers: int = 2,
    ttl_days: int = 7,
    timeout_seconds: int = 180,
) -> tuple[list[JsonDict | None], JsonDict, list[str]]:
    """Validate top products with the local gtrends-bulk skill.

    This path reads the skill's SQLite cache first, fetches only missing/stale
    product keywords with ``bulk_scraper.py``, then normalizes real time-series
    rows back into auto-intelligence evidence.
    """

    warnings: list[str] = []
    if not products:
        return [], _empty_trend_stats(), warnings

    gtrends_dir = Path(
        os.environ.get("AUTO_INTEL_GTRENDS_BULK_DIR", str(DEFAULT_GTRENDS_BULK_DIR))
    )
    product_keywords, keywords = _trend_keyword_index(products, max_keywords)
    if not keywords:
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "provider": "gtrends_bulk",
        }, ["gtrends_bulk_no_keywords"]

    if not gtrends_dir.exists():
        return [None for _ in products], {
            **_empty_trend_stats(),
            "requested_products": len(products),
            "requested_keywords": len(keywords),
            "provider": "gtrends_bulk_missing",
            "bulk_dir": str(gtrends_dir),
        }, [f"gtrends_bulk_dir_missing:{gtrends_dir}"]

    records, cache_info, cache_warnings = _read_gtrends_bulk_cache(
        gtrends_dir,
        keywords,
        (geo or "US").upper(),
        timeframe,
        ttl_days,
    )
    warnings.extend(cache_warnings)

    missing_keywords = [keyword for keyword in keywords if keyword not in records]
    fetch_info: JsonDict = {
        "attempted": False,
        "attempted_keywords": 0,
        "returncode": None,
        "seconds": 0.0,
    }
    if missing_keywords:
        fetch_info, fetch_warnings = _run_gtrends_bulk_fetch(
            gtrends_dir,
            missing_keywords,
            (geo or "US").upper(),
            timeframe,
            workers,
            ttl_days,
            timeout_seconds,
        )
        warnings.extend(fetch_warnings)
        refreshed, refreshed_info, refreshed_warnings = _read_gtrends_bulk_cache(
            gtrends_dir,
            keywords,
            (geo or "US").upper(),
            timeframe,
            ttl_days,
        )
        records.update(refreshed)
        warnings.extend(refreshed_warnings)
        cache_info = {
            **cache_info,
            "post_fetch_cache_hits": refreshed_info.get("cache_hits", 0),
            "post_fetch_stale_keywords": refreshed_info.get("stale_keywords", []),
        }

    raw_trends = {keyword: records[keyword] for keyword in keywords if keyword in records}
    enriched: list[JsonDict | None] = []
    for keywords_for_product in product_keywords:
        enriched.append(_best_trend_record(keywords_for_product, raw_trends))

    stats = _trend_stats_from_records(enriched, len(products), len(keywords), raw_trends)
    stats.update({
        "provider": "gtrends_bulk",
        "bulk_dir": str(gtrends_dir),
        "geo": (geo or "US").upper(),
        "timeframe": timeframe,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "cache_hits": cache_info.get("cache_hits", 0),
        "stale_keywords": cache_info.get("stale_keywords", []),
        "missing_before_fetch": missing_keywords,
        "fetch": fetch_info,
    })
    return enriched, stats, warnings


def _trend_keyword_index(products: list[JsonDict], max_keywords: int) -> tuple[list[list[str]], list[str]]:
    product_keywords: list[list[str]] = []
    for product in products:
        keywords = build_trend_keywords(product)
        product_keywords.append(keywords)

    limit = max(1, int(max_keywords))
    ordered: list[str] = []
    seen: set[str] = set()
    for slot in range(3):
        for keywords in product_keywords:
            if len(ordered) >= limit:
                return product_keywords, ordered
            if slot >= len(keywords):
                continue
            keyword = keywords[slot]
            if keyword in seen:
                continue
            seen.add(keyword)
            ordered.append(keyword)
    return product_keywords, ordered


def _read_gtrends_bulk_cache(
    gtrends_dir: Path,
    keywords: list[str],
    geo: str,
    timeframe: str,
    ttl_days: int,
) -> tuple[JsonDict, JsonDict, list[str]]:
    db_path = Path(gtrends_dir) / "cache.db"
    if not db_path.exists():
        return {}, {"cache_hits": 0, "stale_keywords": []}, [f"gtrends_bulk_cache_missing:{db_path}"]

    cutoff = time.time() - max(1, int(ttl_days)) * 86400 if int(ttl_days) > 0 else None
    records: JsonDict = {}
    stale_keywords: list[str] = []
    warnings: list[str] = []
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            for keyword in keywords:
                row = conn.execute(
                    """
                    SELECT keyword, geo, timeframe, avg_score, median, last_30d,
                           peak, peak_date, trend_dir, confidence, volatility,
                           seasonal_peak_month, yoy_change, data_points, source, ts
                    FROM trends_v3
                    WHERE keyword=? AND geo=? AND timeframe=?
                    """,
                    (keyword, geo, timeframe),
                ).fetchone()
                if not row:
                    continue
                ts = safe_float(row["ts"], 0)
                if cutoff and ts and ts < cutoff:
                    stale_keywords.append(keyword)
                    continue
                records[keyword] = _gtrends_bulk_row_to_record(row)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        warnings.append(f"gtrends_bulk_cache_read_failed:{exc.__class__.__name__}:{exc}")

    return records, {"cache_hits": len(records), "stale_keywords": stale_keywords}, warnings


def _gtrends_bulk_row_to_record(row: sqlite3.Row) -> JsonDict:
    cached_at = ""
    ts = safe_float(row["ts"], 0)
    if ts:
        cached_at = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    return {
        "keyword": row["keyword"],
        "avg_score": safe_float(row["avg_score"], 0),
        "median": safe_float(row["median"], 0),
        "last_30d": safe_float(row["last_30d"], 0),
        "peak": safe_float(row["peak"], 0),
        "peak_date": row["peak_date"],
        "trend_dir": row["trend_dir"] or "flat",
        "confidence": row["confidence"] or "unknown",
        "volatility": safe_float(row["volatility"], 0),
        "seasonal_peak_month": row["seasonal_peak_month"],
        "yoy_change": row["yoy_change"],
        "data_points": int(safe_float(row["data_points"], 0)),
        "source": row["source"] or "pytrends",
        "cached_at": cached_at,
    }


def _run_gtrends_bulk_fetch(
    gtrends_dir: Path,
    keywords: list[str],
    geo: str,
    timeframe: str,
    workers: int,
    ttl_days: int,
    timeout_seconds: int,
) -> tuple[JsonDict, list[str]]:
    scraper = Path(gtrends_dir) / "bulk_scraper.py"
    info: JsonDict = {
        "attempted": False,
        "attempted_keywords": len(keywords),
        "returncode": None,
        "seconds": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
        "proxies": "",
    }
    if not scraper.exists():
        return info, [f"gtrends_bulk_scraper_missing:{scraper}"]

    input_path = Path(gtrends_dir) / f"auto_intel_trends_{os.getpid()}_{time.time_ns()}.txt"
    _write_atomic(input_path, "\n".join(keywords) + "\n")
    cmd = [
        sys.executable or "python3",
        str(scraper),
        "--input",
        str(input_path),
        "--geo",
        geo,
        "--timeframe",
        timeframe,
        "--workers",
        str(max(1, min(int(workers), 4))),
        "--ttl",
        str(max(0, int(ttl_days))),
    ]
    proxies_path = Path(
        os.environ.get("AUTO_INTEL_GTRENDS_BULK_PROXIES", str(Path(gtrends_dir) / "proxies.txt"))
    )
    if _has_proxy_entries(proxies_path):
        cmd.extend(["--proxies", str(proxies_path)])
        info["proxies"] = str(proxies_path)

    started = time.time()
    info["attempted"] = True
    try:
        result = subprocess.run(
            cmd,
            cwd=str(gtrends_dir),
            text=True,
            capture_output=True,
            timeout=max(30, min(int(timeout_seconds), 900)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        info.update({
            "returncode": "timeout",
            "seconds": round(time.time() - started, 2),
            "stdout_tail": _tail_text(exc.stdout),
            "stderr_tail": _tail_text(exc.stderr),
        })
        return info, [f"gtrends_bulk_timeout:{len(keywords)}keywords:{timeout_seconds}s"]
    finally:
        input_path.unlink(missing_ok=True)

    info.update({
        "returncode": result.returncode,
        "seconds": round(time.time() - started, 2),
        "stdout_tail": _tail_text(result.stdout),
        "stderr_tail": _tail_text(result.stderr),
    })
    if result.returncode != 0:
        return info, [f"gtrends_bulk_failed:code{result.returncode}:{_tail_text(result.stderr, 240)}"]
    return info, []


def _tail_text(value: Any, limit: int = 1200) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return text[-limit:]


def _has_proxy_entries(path: Path) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text and not text.startswith("#"):
                return True
    except OSError:
        return False
    return False


def build_trend_keywords(product: JsonDict) -> list[str]:
    """Extract commercial trend queries from product facts."""

    title = clean_product_title(
        str(product.get("title") or product.get("product_title") or "")
    )
    keywords: list[str] = []
    try:
        gtrends_dir = Path(os.environ.get("AUTO_INTEL_GTRENDS_DIR", str(DEFAULT_GTRENDS_DIR)))
        if str(gtrends_dir) not in sys.path:
            sys.path.insert(0, str(gtrends_dir))
        from radar_bridge import _extract_keywords_from_title

        keywords.extend(_extract_keywords_from_title(title))
    except Exception:
        keywords.extend(_fallback_trend_keywords(title))
    if not keywords:
        keywords.extend(_fallback_trend_keywords(title))

    product_type = str(product.get("product_type") or "").strip()
    if product_type and product_type != "未分类":
        keywords.append(product_type)

    cleaned: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        text = re.sub(r"\s+", " ", str(keyword or "").strip().lower())
        text = re.sub(r"[^a-z0-9 ]+", "", text).strip()
        if len(text) < 4 or text in seen:
            continue
        seen.add(text)
        cleaned.append(text[:80])
        if len(cleaned) >= 5:
            break
    return cleaned


def _fallback_trend_keywords(title: str) -> list[str]:
    words = [
        w for w in re.findall(r"[a-z0-9]+", title.lower())
        if len(w) >= 3 and w not in FAMILY_STOPWORDS
    ]
    keywords: list[str] = []
    if len(words) >= 3:
        keywords.append(" ".join(words[:3]))
    if len(words) >= 2:
        keywords.append(" ".join(words[:2]))
    for idx in range(min(4, len(words) - 1)):
        keywords.append(f"{words[idx]} {words[idx + 1]}")
    return keywords


def _best_trend_record(keywords: list[str], raw_trends: JsonDict) -> JsonDict | None:
    best: tuple[float, JsonDict] | None = None
    for keyword in keywords:
        raw = raw_trends.get(keyword)
        if not isinstance(raw, dict):
            continue
        record = normalize_trend_record(keyword, raw)
        score = (
            safe_float(record.get("trend_score"), 0)
            + safe_float(record.get("trend_current"), 0) * 0.3
            + max(safe_float(record.get("trend_momentum_pct"), 0), 0) * 0.5
            + (8 if record.get("trend_verified") else 0)
        )
        if best is None or score > best[0]:
            best = (score, record)
    return best[1] if best else None


def normalize_trend_record(keyword: str, raw: JsonDict | None = None) -> JsonDict:
    """Normalize a gtrends record into product-selection evidence."""

    raw = raw or {}
    avg = safe_float(raw.get("avg_score", raw.get("score", 0)), 0)
    current = safe_float(raw.get("last_30d", raw.get("current", avg)), avg)
    median = safe_float(raw.get("median", 0), 0)
    peak = safe_float(raw.get("peak", 0), 0)
    yoy = raw.get("yoy_change")
    yoy_value = safe_float(yoy, 0) if yoy is not None else None
    trend_dir = str(raw.get("trend_dir") or raw.get("direction") or "").lower()
    if not trend_dir:
        if current >= avg * 1.15 and current >= 15:
            trend_dir = "up"
        elif avg and current <= avg * 0.75:
            trend_dir = "down"
        else:
            trend_dir = "flat"
    momentum = _trend_momentum(avg, current, yoy_value)
    source = str(raw.get("source") or "unknown")
    confidence = str(raw.get("confidence") or "")
    data_points = int(safe_float(raw.get("data_points"), 0))
    is_proxy = source in {"google-suggest", "suggest"} or confidence == "suggest"
    real_sources = {"pytrends", "google-trends", "google_trends", "gtrends", "gtrends_bulk", "trends"}
    has_timeseries = bool(not is_proxy and (data_points >= 8 or raw.get("last_30d") is not None))
    fetched_no_volume = (
        not is_proxy
        and source in real_sources
        and confidence in {"", "none", "unknown"}
        and avg <= 0
        and current <= 0
        and peak <= 0
        and bool(raw.get("cached_at") or raw.get("ts") or raw.get("source"))
    )
    verified = bool(has_timeseries or fetched_no_volume)
    status = _trend_status(avg, current, momentum, trend_dir, verified, is_proxy)
    quality = _trend_data_quality(avg, current, data_points, confidence, source, verified, is_proxy)

    return {
        "trend_query": str(raw.get("keyword") or keyword),
        "trend_score": round(avg, 2),
        "trend_current": round(current, 2),
        "trend_median": round(median, 2),
        "trend_peak": round(peak, 2),
        "trend_direction": trend_dir or "flat",
        "trend_momentum_pct": round(momentum, 2),
        "trend_yoy_change": round(yoy_value, 2) if yoy_value is not None else None,
        "trend_status": status,
        "trend_verified": verified,
        "trend_source": source,
        "trend_confidence": confidence or ("suggest" if is_proxy else "unknown"),
        "trend_data_points": data_points,
        "trend_data_quality": quality,
        "trend_decision": _trend_decision(status, quality, verified, is_proxy),
        "trend_risk": _trend_risk(status, quality, trend_dir, is_proxy),
        "trend_cached_at": raw.get("cached_at") or raw.get("ts"),
        "trend_raw": {
            key: raw.get(key)
            for key in ("avg_score", "last_30d", "median", "peak", "trend_dir", "confidence", "data_points", "source")
            if key in raw
        },
    }


def empty_trend_signal(reason: str = "not_checked") -> JsonDict:
    return {
        "trend_query": "",
        "trend_score": 0.0,
        "trend_current": 0.0,
        "trend_median": 0.0,
        "trend_peak": 0.0,
        "trend_direction": "unverified",
        "trend_momentum_pct": 0.0,
        "trend_yoy_change": None,
        "trend_status": "unverified",
        "trend_verified": False,
        "trend_source": reason,
        "trend_confidence": "none",
        "trend_data_points": 0,
        "trend_data_quality": "未验证",
        "trend_decision": "Google Trends 未验证；不能把趋势当作跟品依据。",
        "trend_risk": "缺少可靠 Trends 时间序列，需补跑后再判断需求热度。",
        "trend_cached_at": None,
        "trend_raw": {},
    }


def _apply_trend_signal(candidate: JsonDict, trend: JsonDict | None) -> JsonDict:
    signal = trend or empty_trend_signal("cache_missing")
    candidate["trend_signal"] = signal
    candidate["platform_ai"] = build_platform_ai_profit_engine(candidate, expert_for_candidate(candidate))
    candidate["money_decision"] = build_money_decision(candidate, expert_for_candidate(candidate))
    return candidate


def _trend_momentum(avg: float, current: float, yoy_value: float | None) -> float:
    if yoy_value is not None:
        return yoy_value
    if avg > 0:
        return ((current - avg) / avg) * 100
    return 0.0


def _trend_status(avg: float, current: float, momentum: float, trend_dir: str, verified: bool, is_proxy: bool) -> str:
    if is_proxy:
        return "proxy"
    if not verified:
        return "unverified" if avg <= 0 else "weak"
    if avg >= 45 and current >= 50 and (momentum >= 8 or trend_dir == "up"):
        return "hot"
    if avg >= 28 and current >= 25 and momentum >= -10:
        return "watch"
    if avg >= 18 or current >= 25:
        return "emerging"
    return "weak"


def _trend_data_quality(
    avg: float,
    current: float,
    data_points: int,
    confidence: str,
    source: str,
    verified: bool,
    is_proxy: bool,
) -> str:
    if is_proxy:
        return "代理信号"
    if not verified:
        return "未验证"
    if confidence == "high" and data_points >= 40 and avg >= 25:
        return "高"
    if confidence in {"high", "medium"} and data_points >= 20 and (avg >= 18 or current >= 25):
        return "中"
    if source and avg > 0:
        return "低"
    if source:
        return "已验证无明显搜索量"
    return "未验证"


def _trend_decision(status: str, quality: str, verified: bool, is_proxy: bool) -> str:
    if status == "hot" and quality in {"高", "中"}:
        return "趋势强，支持优先测款，但仍需 FB 素材和毛利一起确认。"
    if status == "watch":
        return "趋势可用，适合进入素材拆解或轻测。"
    if status == "emerging":
        return "有早期需求苗头，适合观察或小样本验证。"
    if is_proxy:
        return "只有搜索建议代理信号，不能当作真实 Trends 结论。"
    if status == "weak":
        return "趋势弱，除非 FB/Shopify 信号很强，否则不优先。"
    return "Google Trends 未验证；先补跑趋势再决定是否跟。"


def _trend_risk(status: str, quality: str, trend_dir: str, is_proxy: bool) -> str:
    if is_proxy:
        return "搜索建议不等于需求趋势，容易高估真实购买热度。"
    if status == "unverified":
        return "没有可靠 Trends 时间序列，时间点判断不足。"
    if status == "weak":
        return "搜索热度偏弱，可能是窄众品或短期素材驱动。"
    if trend_dir == "down":
        return "近 30 天走弱，需确认是否错过窗口。"
    if quality == "低":
        return "数据质量偏低，结论只可作辅助。"
    return ""


def _trend_reason_line(trend: JsonDict) -> str:
    query = str(trend.get("trend_query") or "").strip()
    score = safe_float(trend.get("trend_score"), 0)
    current = safe_float(trend.get("trend_current"), 0)
    direction = str(trend.get("trend_direction") or "flat")
    quality = str(trend.get("trend_data_quality") or "未验证")
    source = str(trend.get("trend_source") or "")
    if not query:
        return ""
    return (
        f"Google Trends: {query} 均值 {score:.0f}/近30天 {current:.0f}/方向 {direction}/"
        f"质量 {quality}/来源 {source}"
    )


def _platform_validation_reason_line(evidence: JsonDict) -> str:
    if not isinstance(evidence, dict):
        return ""
    score = int(safe_float(evidence.get("score"), 0))
    sources = evidence.get("sources", [])
    if not score or not isinstance(sources, list) or not sources:
        return ""
    labels = [
        f"{source.get('platform')}:{source.get('signal')}"
        for source in sources[:3]
        if isinstance(source, dict) and source.get("platform")
    ]
    return f"外部验证 {score}/100: " + " · ".join(labels)


def _trend_stats_from_records(
    records: list[JsonDict | None],
    requested_products: int,
    requested_keywords: int,
    raw_trends: JsonDict,
) -> JsonDict:
    normalized = [record for record in records if isinstance(record, dict)]
    verified = [record for record in normalized if record.get("trend_verified")]
    proxy = [record for record in normalized if record.get("trend_status") == "proxy"]
    hot = [record for record in normalized if record.get("trend_status") == "hot"]
    status_counts: dict[str, int] = {}
    for record in normalized:
        status = str(record.get("trend_status") or "unverified")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "requested_products": requested_products,
        "requested_keywords": requested_keywords,
        "matched_products": len(normalized),
        "verified_products": len(verified),
        "proxy_products": len(proxy),
        "hot_products": len(hot),
        "status_breakdown": status_counts,
        "raw_found_keywords": len(raw_trends or {}),
        "provider": "gtrends_router",
    }


def _empty_trend_stats() -> JsonDict:
    return {
        "requested_products": 0,
        "requested_keywords": 0,
        "matched_products": 0,
        "verified_products": 0,
        "proxy_products": 0,
        "hot_products": 0,
        "status_breakdown": {},
        "raw_found_keywords": 0,
        "provider": "none",
    }


def build_platform_validation_evidence(candidate: JsonDict) -> JsonDict:
    """Build cross-platform proof from cached validation files plus live search tasks."""

    keywords = build_trend_keywords(candidate)[:5]
    if not keywords:
        keywords = _fallback_trend_keywords(clean_product_title(str(candidate.get("title", ""))))
    keywords = keywords[:5]
    primary_keyword = keywords[0] if keywords else clean_product_title(str(candidate.get("title", ""))).lower()
    cache = _load_platform_validation_cache()
    sources: list[JsonDict] = []
    score = 0

    reddit_hits = _cached_keyword_hits(cache.get("reddit", {}), keywords)
    if reddit_hits:
        thread_count = sum(len(hit.get("items", [])) for hit in reddit_hits)
        comment_count = sum(int(safe_float(item.get("comments_found") or item.get("comment_count"), 0)) for hit in reddit_hits for item in hit.get("items", []))
        score += min(26, 8 + thread_count * 4 + min(comment_count // 20, 8))
        top = reddit_hits[0]["items"][0] if reddit_hits[0].get("items") else {}
        sources.append({
            "platform": "Reddit",
            "status": "cached_verified",
            "keyword": reddit_hits[0].get("keyword", ""),
            "signal": f"{thread_count} threads / {comment_count} comments",
            "url": top.get("url") or top.get("redlib_url") or "",
            "title": top.get("title", ""),
        })

    rigorous_hits = _cached_keyword_hits(cache.get("rigorous", {}), keywords)
    if rigorous_hits:
        best = rigorous_hits[0]["items"][0] if rigorous_hits[0].get("items") else {}
        rigorous_score = safe_float(best.get("rigorous_score"), 0)
        score += min(24, int(rigorous_score / 5)) if rigorous_score else 10
        sources.append({
            "platform": "Rigorous validation",
            "status": "cached_merged",
            "keyword": best.get("kw") or rigorous_hits[0].get("keyword", ""),
            "signal": best.get("final_decision") or best.get("decision_hint") or "",
            "url": best.get("amazon") or "",
            "title": best.get("product") or "",
        })
        gt = best.get("gt") if isinstance(best.get("gt"), dict) else {}
        if gt:
            sources.append({
                "platform": "Google Trends cache",
                "status": gt.get("status", "cached"),
                "keyword": best.get("kw") or rigorous_hits[0].get("keyword", ""),
                "signal": f"avg12={gt.get('avg12', gt.get('avg_12m', ''))}, latest={gt.get('latest_complete', '')}",
                "url": "",
                "title": "",
            })

    store_product_count = int(safe_float(candidate.get("store_product_count"), 0))
    is_micro_vertical = 5 <= store_product_count <= 15
    if is_micro_vertical:
        score += 12
        sources.append({
            "platform": "Vertical micro-site",
            "status": "shopify_snapshot",
            "keyword": str(candidate.get("product_type") or "niche store"),
            "signal": f"{store_product_count} products on one focused site",
            "url": f"https://{normalize_domain(candidate.get('domain', ''))}",
            "title": normalize_domain(candidate.get("domain", "")),
        })

    required_checks = [
        "Reddit: 找真实求推荐/吐槽/使用场景，优先看评论数和痛点原话",
        "YouTube: 找 review/unboxing/demo，确认是否能拍出 3 秒演示素材",
        "TikTok/Instagram: 找 UGC 展示密度，判断素材是否容易冷启动",
        "Google Trends: 必须是真实时间序列，proxy/suggest 只能辅助",
        "Amazon/Google Shopping: 看同类价格、评论痛点、供应商可替代性",
        "独立站: 优先看 5-15 个产品的小垂直站，判断是否是新奇特单品线",
    ]
    if not reddit_hits:
        required_checks.insert(0, "补 Reddit 验证：当前没有命中本地缓存，不能把社媒需求当已验证")

    status = "strong" if score >= 48 else "cached" if score >= 22 else "needs_platform_proof"
    return {
        "score": max(0, min(100, int(score))),
        "status": status,
        "primary_keyword": primary_keyword,
        "keywords": keywords,
        "sources": sources[:6],
        "required_checks": required_checks,
        "search_links": _platform_search_links(primary_keyword),
        "store_context": {
            "store_product_count": store_product_count,
            "is_micro_vertical": is_micro_vertical,
            "micro_vertical_rule": "5-15 products, ideally around 10, with a focused niche assortment",
        },
    }


def _load_platform_validation_cache() -> JsonDict:
    global _PLATFORM_VALIDATION_CACHE
    if _PLATFORM_VALIDATION_CACHE is not None:
        return _PLATFORM_VALIDATION_CACHE

    cache: JsonDict = {"reddit": {}, "rigorous": {}}
    validation_dir = Path(os.environ.get("AUTO_INTEL_VALIDATION_DIR", str(DEFAULT_VALIDATION_DIR)))
    if not validation_dir.exists():
        _PLATFORM_VALIDATION_CACHE = cache
        return cache

    for path in sorted(validation_dir.glob("reddit_validated_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
        payload, error = _read_json_file(path)
        if error or not isinstance(payload, dict):
            continue
        for keyword, items in payload.items():
            if isinstance(items, list):
                cache["reddit"].setdefault(_normalize_validation_keyword(keyword), []).extend(items[:5])

    for path in sorted(validation_dir.glob("rigorous_validation*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
        payload, error = _read_json_file(path)
        if error or not isinstance(payload, dict):
            continue
        for item in payload.get("records", []) if isinstance(payload.get("records"), list) else []:
            if not isinstance(item, dict):
                continue
            keyword = _normalize_validation_keyword(item.get("kw") or item.get("keyword") or "")
            if keyword:
                cache["rigorous"].setdefault(keyword, []).append(item)

    _PLATFORM_VALIDATION_CACHE = cache
    return cache


def _cached_keyword_hits(index: JsonDict, keywords: list[str]) -> list[JsonDict]:
    hits: list[JsonDict] = []
    normalized_keywords = [_normalize_validation_keyword(keyword) for keyword in keywords if keyword]
    for query in normalized_keywords:
        for key, items in index.items():
            if not isinstance(items, list) or not key:
                continue
            if query == key or query in key or key in query:
                hits.append({"keyword": key, "items": items})
    return hits


def _normalize_validation_keyword(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _platform_search_links(keyword: str) -> list[JsonDict]:
    query = _normalize_validation_keyword(keyword)
    encoded = urllib.parse.quote_plus(query)
    return [
        {"platform": "Reddit", "url": f"https://www.reddit.com/search/?q={encoded}%20recommendations%20review", "purpose": "真实需求/推荐/吐槽"},
        {"platform": "X", "url": f"https://x.com/search?q={encoded}%20review%20ad%20product&src=typed_query", "purpose": "实时讨论/吐槽/达人或品牌投放线索"},
        {"platform": "YouTube", "url": f"https://www.youtube.com/results?search_query={encoded}%20review%20unboxing%20demo", "purpose": "开箱/演示/评测素材"},
        {"platform": "TikTok", "url": f"https://www.tiktok.com/search?q={encoded}%20review", "purpose": "UGC密度/视频钩子"},
        {"platform": "Meta Ad Library", "url": f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&is_targeted_country=false&media_type=all&q={encoded}&search_type=keyword_unordered", "purpose": "FB/IG 竞品广告是否真的在跑"},
        {"platform": "Google Ads Transparency", "url": f"https://adstransparency.google.com/?region=US&query={encoded}", "purpose": "Search/Shopping/YouTube 广告透明度线索"},
        {"platform": "Google Shopping", "url": f"https://www.google.com/search?tbm=shop&q={encoded}", "purpose": "竞价带/同类卖家"},
        {"platform": "Amazon", "url": f"https://www.amazon.com/s?k={encoded}", "purpose": "评论痛点/价格锚点"},
    ]


def fetch_exact_fb_signal(config: AutoIntelConfig, domain: str) -> tuple[JsonDict, str]:
    """Fetch one exact domain signal from 8000 /brands/{domain}."""

    normalized = normalize_domain(domain)
    if not normalized:
        return _empty_fb_signal(domain, "lookup_failed", "empty_domain"), "fb_exact_verify_failed:empty_domain"

    url = f"{config.fb_api_base.rstrip('/')}/brands/{urllib.parse.quote(normalized, safe='')}"
    timeout = max(1.0, min(float(config.fb_timeout_seconds), 30.0))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "auto-intelligence-loop/1.0"})
        with _FB_LOCAL_OPENER.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _empty_fb_signal(normalized, "not_found", "exact_brand_lookup"), ""
        return (
            _empty_fb_signal(normalized, "lookup_failed", "exact_brand_lookup"),
            f"fb_exact_verify_failed:{normalized}:HTTP{exc.code}",
        )
    except Exception as exc:
        return (
            _empty_fb_signal(normalized, "lookup_failed", "exact_brand_lookup"),
            f"fb_exact_verify_failed:{normalized}:{exc.__class__.__name__}:{exc}",
        )

    if not isinstance(payload, dict):
        return (
            _empty_fb_signal(normalized, "lookup_failed", "exact_brand_lookup"),
            f"fb_exact_verify_failed:{normalized}:payload_not_object",
        )
    return _fb_signal_from_8000_row(payload, source="exact_brand_lookup"), ""


def _fb_signal_from_8000_row(row: JsonDict, source: str) -> JsonDict:
    domain = normalize_domain(row.get("domain", ""))
    appearances = int(max(
        safe_float(row.get("appearance_count"), 0),
        safe_float(row.get("total_ads_across_scans"), 0),
    ))
    return {
        "domain": domain,
        "brand": row.get("canonical_name") or row.get("brand_name") or domain,
        "ad_creative_count": appearances,
        "appearance_count": appearances,
        "orbit_score": safe_float(row.get("orbit_score"), 0),
        "ai_score": safe_float(row.get("ai_score"), 0),
        "grade": row.get("grade", ""),
        "fb_verified_by_8000": True,
        "fb_verification_status": "matched",
        "fb_verification_source": source,
        "fb_verification_endpoint": "/brands" if source == "bulk_brands" else "/brands/{domain}",
    }


def _empty_fb_signal(domain: str, status: str, source: str) -> JsonDict:
    normalized = normalize_domain(domain)
    return {
        "domain": normalized,
        "brand": normalized,
        "ad_creative_count": 0,
        "appearance_count": 0,
        "orbit_score": 0,
        "ai_score": 0,
        "grade": "",
        "fb_verified_by_8000": status in {"not_found", "lookup_failed"},
        "fb_verification_status": status,
        "fb_verification_source": source,
        "fb_verification_endpoint": "/brands/{domain}" if source == "exact_brand_lookup" else "",
    }


def score_candidate(product: JsonDict, signal: JsonDict | None = None) -> JsonDict:
    """Score one product using Shopify product facts plus FB Ads evidence."""

    signal = signal or {}
    price = safe_float(product.get("price"))
    text = _text_blob(product)
    has_pain = _has_any(text, PAIN_WORDS)
    has_visual = _has_any(text, VISUAL_WORDS)
    has_gift = _has_any(text, GIFT_WORDS)
    has_bundle = _has_any(text, BUNDLE_WORDS)
    score = 12
    reasons = ["本地快照近期更新/上新 (+12)"]
    risks: list[str] = []
    angles: list[str] = []

    if 29 <= price <= 89:
        score += 18
        reasons.append(f"Meta 冷启动甜蜜价格带 ${price:.0f} (+18)")
    elif 90 <= price <= 180:
        score += 14
        reasons.append(f"AOV 可投放区间 ${price:.0f} (+14)")
    elif 18 <= price < 29 or 180 < price <= 250:
        score += 8
        reasons.append(f"价格可测但 CAC 容错较低 ${price:.0f} (+8)")
    elif price < 12:
        score -= 8
        risks.append("客单价过低，Meta 广告难打平")
    elif price > 300:
        score -= 10
        risks.append("高客单决策链长，冷启动慢")

    appearances = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    if 5 <= appearances <= 80:
        score += 22
        reasons.append(f"FB 素材数 {appearances}，验证充分且未过饱和 (+22)")
    elif 1 <= appearances < 5:
        score += 12
        reasons.append(f"FB 已有初测素材 {appearances} 条 (+12)")
    elif 80 < appearances <= 300:
        score += 12
        reasons.append(f"FB 放量明显 {appearances} 条 (+12)")
        risks.append("广告侧可能进入拥挤期，需要差异化素材")
    elif appearances > 300:
        score += 4
        reasons.append(f"FB 大规模放量 {appearances} 条，需求强但拥挤 (+4)")
        risks.append("素材/受众可能过热，不能直接复制")

    orbit = safe_float(signal.get("orbit_score"), 0)
    ai_score = safe_float(signal.get("ai_score"), 0)
    if orbit >= 80 or ai_score >= 80:
        score += 12
        reasons.append("8000 管线 Orbit/AI 高分验证 (+12)")
    elif orbit >= 60 or ai_score >= 60:
        score += 7
        reasons.append("8000 管线中高分验证 (+7)")

    grade = str(signal.get("grade", "") or "").upper()
    if grade in {"HOT", "SOLID", "WATCH"}:
        bonus = {"HOT": 10, "SOLID": 7, "WATCH": 5}[grade]
        score += bonus
        reasons.append(f"FB 管线评级 {grade} (+{bonus})")

    if has_pain:
        score += 10
        reasons.append("痛点/问题解决型产品，广告钩子清晰 (+10)")
        angles.append("痛点前置：问题 -> 演示 -> 结果")
    if has_visual:
        score += 8
        reasons.append("一眼可演示，适合短视频首 3 秒 (+8)")
        angles.append("短视频演示：变化/效果/使用场景")
    if has_gift:
        score += 6
        reasons.append("礼品属性，适合季节节点和 Bundle (+6)")
        angles.append("礼品场景：送礼对象 + 节日/纪念日")
    if has_bundle:
        score += 4
        reasons.append("组合/多件装信号，适合 AOV 提升 (+4)")
        angles.append("Offer：单件 vs 多件装对比")
    if appearances == 0 and 29 <= price <= 180 and sum([has_pain, has_visual, has_gift, has_bundle]) >= 2:
        score += 8
        reasons.append("Shopify 侧价格/场景/Offer 信号完整，先入观察池 (+8)")

    platform_validation = build_platform_validation_evidence(product)
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    if platform_score >= 48:
        score += 12
        reasons.append("Reddit/GT/小垂直站等外部验证较强 (+12)")
    elif platform_score >= 22:
        score += 7
        reasons.append("已有部分外部验证缓存，可先轻量跟进 (+7)")
    elif platform_validation.get("status") == "needs_platform_proof":
        risks.append("Reddit/YouTube/TikTok 等外部验证不足，先补证据")
    if (platform_validation.get("store_context") or {}).get("is_micro_vertical"):
        score += 6
        reasons.append("约10个产品的小垂直站，适合作为新奇特跟品来源 (+6)")

    expert = build_expert_assessment(product, price, appearances, text)
    score += int(expert.get("score_adjustment", 0))
    if expert.get("score_adjustment"):
        reasons.append(expert.get("score_reason", "专家经验修正"))
    risks.extend(expert.get("risk_flags", []))
    for angle in expert.get("conversion_angles", []):
        if angle not in angles:
            angles.append(angle)

    if _has_licensed_ip_risk(text):
        score -= 40
        risks.append("授权/IP/艺人周边风险高，未拿授权前不可直接跟")
    if _has_any(text, RESTRICTED_WORDS):
        score -= 28
        risks.append("合规/IP/平台审核风险高")
    if _has_any(text, LOW_QUALITY_WORDS):
        score -= 35
        risks.append("疑似捐赠、App 占位或不可售商品")
    if _has_any(text, FRAGILE_WORDS):
        score -= 8
        risks.append("物流破损或大件履约风险")

    risks = _unique_list(risks)
    score = max(0, min(100, int(round(score))))
    decision = _decision_for_score(score, risks)
    if not angles:
        angles.append("先拆竞品素材：找首 3 秒钩子、证明点和 CTA")

    scored = {
        "score": score,
        "decision": decision,
        "product_url": _product_url(product),
        "domain": product.get("domain", ""),
        "handle": product.get("handle", ""),
        "title": product.get("title", ""),
        "price": round(price, 2),
        "product_type": product.get("product_type") or "未分类",
        "vendor": product.get("vendor", ""),
        "created_at": product.get("created_at", ""),
        "published_at": product.get("published_at", ""),
        "updated_at": product.get("updated_at", ""),
        "image_count": product.get("image_count", 0),
        "variant_count": product.get("variant_count", 0),
        "fb_signal": {
            "ad_creative_count": appearances,
            "appearance_count": appearances,
            "orbit_score": orbit,
            "ai_score": ai_score,
            "grade": grade,
            "brand": signal.get("brand", ""),
            "fb_verified_by_8000": bool(signal.get("fb_verified_by_8000")),
            "fb_verification_status": signal.get("fb_verification_status", "not_checked"),
            "fb_verification_source": signal.get("fb_verification_source", ""),
            "fb_verification_endpoint": signal.get("fb_verification_endpoint", ""),
        },
        "reasons": reasons,
        "risk_flags": risks,
        "conversion_angles": angles,
        "platform_validation": platform_validation,
        "expert_assessment": _public_expert_assessment(expert),
        "family_key": product_family_key(product),
        "shopify_fit": "高" if score >= 75 and 29 <= price <= 180 else "中" if score >= 55 else "低",
        "fb_ads_fit": "高" if appearances >= 5 or _has_any(text, PAIN_WORDS + VISUAL_WORDS) else "待验证",
        "ad_heat": "过热" if appearances > 300 else "放量" if appearances > 80 else "验证中" if appearances >= 5 else "初测" if appearances > 0 else "未知",
    }
    scored["platform_ai"] = build_platform_ai_profit_engine(scored, _public_expert_assessment(expert))
    scored["money_decision"] = build_money_decision(scored, _public_expert_assessment(expert))
    return scored


def build_shopify_draft(candidate: JsonDict, own_vendor: str = "AUTO-INTEL") -> JsonDict:
    """Build a review-only Shopify product draft payload."""

    title = clean_product_title(candidate.get("title", ""))
    price = suggested_price(safe_float(candidate.get("price")))
    compare_at = round(max(price * 1.35, safe_float(candidate.get("price")) * 1.2), 2)
    handle = slugify(title)
    expert = expert_for_candidate(candidate)
    money = money_decision_for_candidate(candidate)
    tags = [
        "auto-intel",
        "status-draft",
        f"decision-{tag_value(candidate.get('decision', 'watch'), 'watch')}",
        f"source-{normalize_domain(candidate.get('domain', ''))}",
        f"score-{candidate.get('score', 0)}",
    ]
    if candidate.get("product_type"):
        tags.append(f"type-{tag_value(candidate['product_type'], 'uncategorized')}")

    return {
        "title": title,
        "handle": handle,
        "vendor": own_vendor,
        "productType": candidate.get("product_type") or "General",
        "status": "DRAFT",
        "tags": tags,
        "descriptionHtml": build_description_html(candidate),
        "variants": [
            {
                "option1": "Default Title",
                "price": f"{price:.2f}",
                "compareAtPrice": f"{compare_at:.2f}",
                "inventoryPolicy": "DENY",
            }
        ],
        "metafields": [
            {"namespace": "auto_intel", "key": "score", "type": "number_integer", "value": str(candidate.get("score", 0))},
            {"namespace": "auto_intel", "key": "decision", "type": "single_line_text_field", "value": candidate.get("decision", "")},
            {"namespace": "auto_intel", "key": "source_domain", "type": "single_line_text_field", "value": candidate.get("domain", "")},
            {"namespace": "auto_intel", "key": "source_product_url", "type": "url", "value": candidate.get("product_url", "")},
            {"namespace": "auto_intel", "key": "fb_ad_count", "type": "number_integer", "value": str(candidate.get("fb_signal", {}).get("ad_creative_count", 0))},
            {"namespace": "auto_intel", "key": "expert_archetype", "type": "single_line_text_field", "value": expert.get("archetype", "")},
            {"namespace": "auto_intel", "key": "meta_launch_tier", "type": "single_line_text_field", "value": expert.get("meta_launch_tier", "")},
            {"namespace": "auto_intel", "key": "follow_level", "type": "single_line_text_field", "value": money.get("follow_level", "")},
            {"namespace": "auto_intel", "key": "timing", "type": "single_line_text_field", "value": money.get("timing", "")},
        ],
        "source": {
            "domain": candidate.get("domain"),
            "product_url": candidate.get("product_url"),
            "risk_flags": candidate.get("risk_flags", []),
            "money_decision": money,
        },
    }


def build_creative_brief(candidate: JsonDict) -> JsonDict:
    """Create a concise Meta creative brief from one scored candidate."""

    title = clean_product_title(candidate.get("title", ""))
    price = safe_float(candidate.get("price"))
    angles = candidate.get("conversion_angles") or []
    risks = candidate.get("risk_flags") or []
    expert = expert_for_candidate(candidate)
    money = money_decision_for_candidate(candidate)
    hooks = [
        {
            "name": "Problem-first demo",
            "opening": f"Show the exact annoying problem this solves before showing {title}.",
            "shot_plan": ["0-3s: close-up of the problem", "3-8s: product enters and fixes it", "8-15s: proof/result", "15-20s: offer and CTA"],
        },
        {
            "name": "Before-after contrast",
            "opening": "Split screen: messy/painful/slow vs clean/easy/fast.",
            "shot_plan": ["Before state", "One simple use motion", "After state", "Social proof overlay"],
        },
        {
            "name": "Offer stack",
            "opening": f"Anchor the ${price:.0f} value, then show bundle/savings and guarantee.",
            "shot_plan": ["Hero product", "What is included", "Savings math", "CTA with urgency"],
        },
    ]
    if "礼品场景：送礼对象 + 节日/纪念日" in angles:
        hooks[2]["opening"] = "Frame it as a gift people can understand in three seconds."
    if risks:
        hooks.append({
            "name": "Risk-control note",
            "opening": "Do not make medical, IP, or unrealistic transformation claims.",
            "shot_plan": risks[:3],
        })

    return {
        "product_title": title,
        "domain": candidate.get("domain"),
        "score": candidate.get("score"),
        "decision": candidate.get("decision"),
        "primary_angles": angles,
        "platform_ai": candidate.get("platform_ai") or build_platform_ai_profit_engine(candidate, expert),
        "money_decision": money,
        "expert_assessment": expert,
        "hooks": hooks,
        "expert_creative_testing_plan": expert.get("creative_testing_plan", []),
        "landing_page_angle": build_landing_page_angle(candidate),
        "landing_page_must_haves": expert.get("landing_page_must_haves", []),
        "offer_strategy": expert.get("offer_strategy", ""),
        "operator_note": expert.get("operator_note", ""),
        "go_kill_rule": {
            "go": "48h 内 CTR > 1.5%、ATC > 4%、CPA 低于售价 35%，进入下一轮素材扩量。",
            "kill": "花费达到售价 2 倍仍无 ATC，或 CPA 高于售价 50%，停止该角度。",
        },
    }


def build_platform_ai_profit_engine(candidate: JsonDict, expert: JsonDict | None = None) -> JsonDict:
    """Score whether a product can feed modern platform AI bidding/creative systems."""

    expert = expert or expert_for_candidate(candidate)
    price = safe_float(candidate.get("price"))
    text = _text_blob(candidate)
    signal = candidate.get("fb_signal") if isinstance(candidate.get("fb_signal"), dict) else {}
    appearances = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    platform_validation = candidate.get("platform_validation") if isinstance(candidate.get("platform_validation"), dict) else build_platform_validation_evidence(candidate)
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    trend = candidate.get("trend_signal") if isinstance(candidate.get("trend_signal"), dict) else empty_trend_signal("not_checked")
    trend_verified = bool(trend.get("trend_verified"))
    trend_status = str(trend.get("trend_status") or "unverified")
    risks = list(candidate.get("risk_flags") or [])
    hard_risk = _has_hard_risk(risks)
    guardrails = expert.get("break_even_guardrails", {}) or _break_even_guardrails(price)

    axes = {
        "pain": _has_any(text, PAIN_WORDS),
        "visual_demo": _has_any(text, VISUAL_WORDS),
        "gift": _has_any(text, GIFT_WORDS + GIFT_IDENTITY_WORDS),
        "bundle": _has_any(text, BUNDLE_WORDS),
        "comfort_tool": _has_any(text, COMFORT_WORDS + TOOL_WORDS),
    }
    creative_axis_count = sum(1 for value in axes.values() if value)
    image_count = int(safe_float(candidate.get("image_count"), 0))
    variant_count = int(safe_float(candidate.get("variant_count"), 0))

    meta_score = 12
    if 29 <= price <= 180:
        meta_score += 18
    elif 18 <= price < 29 or 180 < price <= 250:
        meta_score += 8
    if 5 <= appearances <= 80:
        meta_score += 22
    elif 1 <= appearances < 5:
        meta_score += 12
    elif 80 < appearances <= 300:
        meta_score += 10
    elif appearances > 300:
        meta_score += 4
    meta_score += min(22, creative_axis_count * 6)
    if platform_score >= 22:
        meta_score += 8

    google_score = 10
    if trend_verified and trend_status in {"hot", "watch", "emerging"}:
        google_score += 22
    elif trend_verified:
        google_score += 10
    if 29 <= price <= 250:
        google_score += 12
    if image_count >= 2:
        google_score += 8
    if platform_score >= 22:
        google_score += 10
    if creative_axis_count >= 2:
        google_score += 6

    tiktok_score = 8
    if axes["visual_demo"]:
        tiktok_score += 22
    if axes["pain"] or axes["gift"]:
        tiktok_score += 12
    if 18 <= price <= 120:
        tiktok_score += 12
    if platform_score >= 22:
        tiktok_score += 8
    if image_count >= 2:
        tiktok_score += 5

    shopify_score = 12
    if 29 <= price <= 180:
        shopify_score += 16
    if image_count >= 2:
        shopify_score += 12
    if 1 <= variant_count <= 8:
        shopify_score += 8
    if creative_axis_count >= 2:
        shopify_score += 12
    if platform_score >= 22:
        shopify_score += 10
    if hard_risk:
        meta_score -= 30
        google_score -= 30
        tiktok_score -= 30
        shopify_score -= 20

    channel_scores = {
        "meta_advantage_plus": max(0, min(100, int(meta_score))),
        "google_pmax_ai_max": max(0, min(100, int(google_score))),
        "tiktok_smart_plus": max(0, min(100, int(tiktok_score))),
        "shopify_magic_sidekick": max(0, min(100, int(shopify_score))),
    }
    automation_score = int(round(sum(channel_scores.values()) / len(channel_scores)))

    if hard_risk:
        readiness = "blocked"
        launch_mode = "不要自动投放，先处理合规/IP/授权"
    elif automation_score >= 78 and min(channel_scores.values()) >= 55:
        readiness = "ai_ready"
        launch_mode = "可进入平台AI轻量测试"
    elif automation_score >= 62:
        readiness = "asset_gap"
        launch_mode = "先补素材/PDP/趋势证据，再开自动化"
    else:
        readiness = "research_first"
        launch_mode = "只做研究和观察，不进广告自动化"

    asset_gaps = []
    if creative_axis_count < 2:
        asset_gaps.append("至少补2个可被平台AI组合的素材角度：痛点演示/前后对比/礼品场景/Offer堆叠")
    if image_count < 2:
        asset_gaps.append("补PDP图片/GIF/短视频素材，否则PMax和Shopify页面信任不足")
    if platform_score < 22:
        asset_gaps.append("补Reddit/YouTube/TikTok/Amazon真实验证，避免只凭站内新品判断")
    if not trend_verified:
        asset_gaps.append("补Google Trends真实时间序列，尤其是PMax/搜索意图判断")
    if price < 18:
        asset_gaps.append("客单价过低，先做Bundle或多件装提高AOV")
    if risks:
        asset_gaps.extend(_top_unique(risks, 2))

    creative_angles = [item.get("angle", "") for item in (expert.get("creative_testing_plan", []) or []) if isinstance(item, dict)]
    if not creative_angles:
        creative_angles = list(candidate.get("conversion_angles") or [])[:3]
    creative_angles = _top_unique(creative_angles + [
        "UGC native demo" if axes["visual_demo"] else "",
        "Problem-solution proof" if axes["pain"] else "",
        "Gift recipient hook" if axes["gift"] else "",
        "Bundle value comparison" if axes["bundle"] else "",
    ], 4)

    return {
        "version": "platform-ai-profit-v1",
        "automation_score": automation_score,
        "readiness": readiness,
        "launch_mode": launch_mode,
        "channel_scores": channel_scores,
        "best_channels": sorted(channel_scores, key=channel_scores.get, reverse=True)[:2],
        "creative_axes": [key for key, value in axes.items() if value],
        "asset_gaps": _top_unique(asset_gaps, 6),
        "creative_input_pack": {
            "minimum_assets": "6 creatives: 3 UGC demos + 2 problem/benefit cuts + 1 offer/bundle cut",
            "angles": creative_angles,
            "negative_controls": _top_unique([
                "不要让平台自动生成医疗/夸大前后对比承诺" if _has_any(text, CLAIM_SENSITIVE_WORDS) else "",
                "不要照搬竞品品牌词、Logo、包装或达人素材",
                "保留人工审核：AI增强素材上线前检查首屏承诺、价格、合规词",
            ], 4),
        },
        "launch_stack": {
            "meta": "Advantage+ / broad conversion test; feed 6+ creative variants, keep CAPI/Pixels clean, 48h only看CTR/ATC/CPA",
            "google": "PMax/AI Max needs clean feed titles, PDP copy, images, search-intent keywords, and Trends proof",
            "tiktok": "Smart+ style test only if native UGC demo is strong; use Spark/creator-style hooks before polished ads",
            "shopify": "Use Magic/Sidekick-like workflow to generate PDP copy, FAQ, SEO fields, bundle copy, and review checklist",
        },
        "auto_rules": {
            "first_budget": f"${guardrails.get('first_test_budget', 50)}",
            "target_cpa": f"${guardrails.get('target_cpa', round(price * 0.35, 2))}",
            "kill": f"花费达到 ${guardrails.get('kill_spend_without_atc', round(max(price * 2, 50), 2))} 仍无ATC，或CPA高于售价50%，停止该角度",
            "scale": guardrails.get("scale_signal", "CTR > 1.5%, ATC > 4%, CPA <= 35% of selling price within 48h"),
        },
    }


def build_selection_board(
    top: list[JsonDict],
    watchlist: list[JsonDict],
    rejected: list[JsonDict],
    duplicate_suppressed: int = 0,
) -> JsonDict:
    """Build an operator-friendly action board from scored products."""

    go_queue = [_queue_item(c, "建 1 个 Shopify 草稿 + 3 条原创素材脚本，先小预算测 48h") for c in top if c.get("decision") == "立即测款"]
    creative_queue = [_queue_item(c, "先拆竞品素材结构，再决定是否建草稿") for c in top if c.get("decision") == "素材拆解"]
    review_queue = [_queue_item(c, "确认供应链、毛利和素材可拍性后再进入草稿") for c in top if c.get("decision") == "加入观察"]
    watch_queue = [_queue_item(c, "先补 FB/TikTok/Google Trends 验证，不进草稿") for c in watchlist]
    kill_list = [
        {
            "title": item.get("title", ""),
            "domain": item.get("domain", ""),
            "score": item.get("score", 0),
            "decision": item.get("decision", "放弃"),
            "money_decision": money_decision_for_candidate(item),
            "risk_flags": item.get("risk_flags", []),
            "why": " | ".join(str(r) for r in item.get("risk_flags", [])[:2] or item.get("reasons", [])[:2]),
        }
        for item in rejected
        if item.get("decision") == "放弃" or item.get("risk_flags")
    ][:50]

    return {
        "summary": {
            "go_queue_count": len(go_queue),
            "creative_queue_count": len(creative_queue),
            "review_queue_count": len(review_queue),
            "watchlist_count": len(watch_queue),
            "kill_list_count": len(kill_list),
            "duplicate_suppressed_count": duplicate_suppressed,
        },
        "go_queue": go_queue,
        "creative_research_queue": creative_queue,
        "review_queue": review_queue,
        "watchlist": watch_queue,
        "kill_list": kill_list,
    }


def build_expert_assessment(product: JsonDict, price: float, appearances: int, text: str) -> JsonDict:
    """Encode senior DTC product-selection and Meta launch heuristics."""

    has_pain = _has_any(text, PAIN_WORDS)
    has_visual = _has_any(text, VISUAL_WORDS)
    has_gift = _has_any(text, GIFT_WORDS + GIFT_IDENTITY_WORDS)
    has_health_safety = _has_any(text, CLAIM_SENSITIVE_WORDS)
    has_licensed_ip = _has_licensed_ip_risk(text)
    has_decor = _has_any(text, DECOR_WORDS)
    has_comfort = _has_any(text, COMFORT_WORDS)
    has_tool = _has_any(text, TOOL_WORDS)
    score_adjustment = 0
    risk_flags: list[str] = []
    conversion_angles: list[str] = []

    if has_licensed_ip:
        archetype = "licensed_ip_merch_risk"
        meta_launch_tier = "reject_without_authorization"
        score_adjustment = -30
        score_reason = "专家经验：艺人/专辑/IP周边没有授权不能跟，热度越高侵权和封户风险越高 (-30)"
        risk_flags.append("授权/IP/艺人周边风险高，未拿授权前不可直接跟")
        creative_plan = [
            _creative_plan("授权核验", "先确认品牌、艺人、角色、专辑或官方周边授权链路", "No unlicensed merch"),
            _creative_plan("替代方向", "只保留非侵权的通用场景、材质、功能或礼品结构", "Generic angle only"),
        ]
        landing = ["Authorization proof", "Generic non-infringing positioning", "Supplier license check", "No artist/album claims"]
        offer = "Do not follow unless authorization and supplier rights are verified"
        note = "有些新品看起来热，但本质是粉丝经济/IP热度，不是可复制的独立站机会；没授权不要碰。"
    elif has_health_safety:
        archetype = "claim_sensitive_health_safety"
        meta_launch_tier = "compliance_review"
        score_adjustment = -14
        score_reason = "专家经验：健康/安全声明在 Meta 审核和信任链路上摩擦高 (-14)"
        risk_flags.append("健康/安全声明合规风险，需要先做合规审查")
        creative_plan = [
            _creative_plan("Use-case education", "Show normal daily context without medical claims", "No cure/safety promises"),
            _creative_plan("Comfort routine", "Frame as comfort/support routine, not a guaranteed outcome", "Lifestyle UGC"),
        ]
        landing = ["Compliance-safe copy", "Evidence/FAQ", "Clear disclaimers", "No medical before/after claims"]
        offer = "Research only until compliance copy, supplier proof, and claim boundaries are reviewed"
        note = "美国 Meta 冷启动里，健康、安全、孕期、医疗暗示会显著增加拒登和信任成本，先审词再测。"
    elif has_pain and has_visual:
        archetype = "problem_solver_demo"
        meta_launch_tier = "test_now" if 29 <= price <= 180 and appearances >= 5 else "creative_research"
        score_adjustment = 6
        score_reason = "专家经验：问题解决 + 可视化演示是 Meta 冷启动最容易被看懂的结构 (+6)"
        conversion_angles.append("专家打法：首 3 秒必须展示痛点瞬间和解决动作")
        creative_plan = [
            _creative_plan("Problem-first demo", "Open on the exact annoying problem", "Close-up before/after"),
            _creative_plan("UGC proof", "Creator shows one use motion and result", "Native phone video"),
            _creative_plan("Offer stack", "Show single vs bundle savings", "Simple price anchor"),
        ]
        landing = ["Before/after demo", "3-bullet benefit stack", "UGC/review block", "Bundle offer", "Shipping/returns reassurance"]
        offer = "Start with single item + 2-pack bundle; keep first test below broad-audience impulse AOV"
        note = "这类品不要先写长文案，先拍得懂；看不懂的产品，算法没有耐心教育用户。"
    elif has_gift:
        archetype = "gift_identity"
        meta_launch_tier = "test_now" if appearances >= 5 else "creative_research"
        score_adjustment = 4
        score_reason = "专家经验：礼品/身份表达有天然受众切分和节日放大空间 (+4)"
        conversion_angles.append("专家打法：送给谁 + 为什么现在送 + 打开礼物反应")
        creative_plan = [
            _creative_plan("Gift-recipient angle", "Name the recipient in the first line", "Gift reveal moment"),
            _creative_plan("Personal identity", "Show how it reflects the buyer/recipient", "Close-up detail shots"),
            _creative_plan("Deadline urgency", "Tie offer to occasion and shipping cutoff", "Gift box visual"),
        ]
        landing = ["Gift-recipient hero", "Personalization/value proof", "Gift-box visual", "Shipping cutoff", "Reviews with recipient context"]
        offer = "Bundle or gift-box offer; test occasion-specific copy before discounting too hard"
        note = "礼品型产品最怕泛泛地卖产品，要明确收礼对象和场景，否则 CPM 便宜也难转化。"
    elif has_comfort:
        archetype = "comfort_mobility"
        meta_launch_tier = "creative_research"
        score_adjustment = 3
        score_reason = "专家经验：舒适/行动力产品需求强，但必须避开医疗化承诺 (+3)"
        conversion_angles.append("专家打法：生活场景对比，不承诺治疗")
        creative_plan = [
            _creative_plan("Daily comfort", "Show a common daily friction", "Walking/standing scene"),
            _creative_plan("Feature proof", "Demonstrate sole/support/material", "Macro product shots"),
        ]
        landing = ["Fit/sizing guide", "Comfort proof", "Material explanation", "Returns policy", "Non-medical wording"]
        offer = "Test size/fit reassurance before aggressive discount; returns risk is the hidden CAC"
        note = "鞋服舒适品能跑，但退货和尺码会吃利润，PDP 必须先解决 fit anxiety。"
    elif has_tool:
        archetype = "utility_tool"
        meta_launch_tier = "creative_research"
        score_adjustment = 4
        score_reason = "专家经验：工具型产品适合演示，但要证明使用频率和替代价值 (+4)"
        conversion_angles.append("专家打法：展示旧方法有多麻烦，再展示工具节省时间")
        creative_plan = [
            _creative_plan("Old way vs new way", "Make the inefficient old method obvious", "Side-by-side demo"),
            _creative_plan("One-job proof", "Complete one concrete task on camera", "Uncut proof shot"),
        ]
        landing = ["Use-case demo", "What is included", "Durability proof", "FAQ", "Bundle offer"]
        offer = "Bundle accessories only after proving the core job-to-be-done"
        note = "工具品的广告不是卖参数，是卖‘少费劲’；素材一定要让人立刻懂它替代了什么。"
    elif has_decor:
        archetype = "taste_identity_decor"
        meta_launch_tier = "research_first"
        score_adjustment = -6
        score_reason = "专家经验：装饰审美型产品依赖品牌/风格命中，冷启动泛投不占优 (-6)"
        conversion_angles.append("专家打法：先验证人群审美和空间场景，不急建草稿")
        creative_plan = [
            _creative_plan("Room transformation", "Show the room before and after the piece", "Room-context video"),
            _creative_plan("Identity statement", "Tie design to a specific taste/community", "Lifestyle stills"),
        ]
        landing = ["Room-context hero", "Size guide", "Material/print proof", "Style collection", "Shipping protection"]
        offer = "Test collection/theme positioning before SKU expansion"
        note = "墙画和装饰不是不能做，是不能当普通爆品做；没有风格人群和内容资产，会被 CAC 拖死。"
    else:
        archetype = "generic_merchandise"
        meta_launch_tier = "research_first" if appearances == 0 else "creative_research"
        score_reason = "专家经验：信号不完整，先补素材和需求验证"
        creative_plan = [
            _creative_plan("Competitor teardown", "Map the first 3 seconds, proof, and CTA from competitors", "Swipe-file notes"),
        ]
        landing = ["Clear hero", "Benefit proof", "Reviews", "FAQ", "Offer clarity"]
        offer = "Do not discount before the core buying reason is clear"
        note = "信号不完整时不要用预算硬撞，先找到一个人群、一个痛点、一个证明点。"

    return {
        "archetype": archetype,
        "meta_launch_tier": meta_launch_tier,
        "score_adjustment": score_adjustment,
        "score_reason": score_reason,
        "risk_flags": risk_flags,
        "conversion_angles": conversion_angles,
        "creative_testing_plan": creative_plan,
        "landing_page_must_haves": landing,
        "offer_strategy": offer,
        "operator_note": note,
        "break_even_guardrails": _break_even_guardrails(price),
    }


def build_money_decision(candidate: JsonDict, expert: JsonDict | None = None) -> JsonDict:
    """Turn product/ad evidence into an operator-ready money decision."""

    expert = expert or expert_for_candidate(candidate)
    score = int(safe_float(candidate.get("score"), 0))
    price = safe_float(candidate.get("price"))
    signal = candidate.get("fb_signal", {})
    appearances = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    risks = list(candidate.get("risk_flags") or [])
    reasons = list(candidate.get("reasons") or [])
    angles = list(candidate.get("conversion_angles") or [])
    trend = candidate.get("trend_signal") if isinstance(candidate.get("trend_signal"), dict) else empty_trend_signal("not_checked")
    trend_status = str(trend.get("trend_status") or "unverified")
    trend_verified = bool(trend.get("trend_verified"))
    trend_decision = str(trend.get("trend_decision") or "")
    trend_risk = str(trend.get("trend_risk") or "")
    trend_line = _trend_reason_line(trend)
    platform_validation = candidate.get("platform_validation") if isinstance(candidate.get("platform_validation"), dict) else build_platform_validation_evidence(candidate)
    platform_line = _platform_validation_reason_line(platform_validation)
    platform_ai = candidate.get("platform_ai") if isinstance(candidate.get("platform_ai"), dict) else build_platform_ai_profit_engine(candidate, expert)
    hard_risk = _has_hard_risk(risks)
    timing = _timing_for_ads(appearances, expert.get("meta_launch_tier", ""))
    follow_level = _follow_level(candidate.get("decision", ""), score, appearances, hard_risk)
    can_follow = follow_level in {"强跟", "轻跟"} and not hard_risk
    guardrails = expert.get("break_even_guardrails", {}) or _break_even_guardrails(price)
    creative_plan = expert.get("creative_testing_plan", []) or []
    meta_angles = [
        {
            "angle": item.get("angle", ""),
            "opening": item.get("opening", ""),
            "asset": item.get("asset", ""),
        }
        for item in creative_plan[:3]
        if isinstance(item, dict)
    ]
    if not meta_angles:
        meta_angles = [{"angle": str(a), "opening": str(a), "asset": "Original UGC"} for a in angles[:3]]

    timing_reasons = [
        r for r in reasons
        if any(token in r for token in ("近期", "FB", "8000", "价格", "放量", "素材", "Orbit", "评级"))
    ] or reasons[:3]
    why_now = _top_unique([
        platform_line if platform_validation.get("score", 0) else "",
        trend_line if trend_status in {"hot", "watch", "emerging"} and trend_verified else "",
        *timing_reasons,
    ], 4)
    why_will_sell = _top_unique([
        trend_decision if trend_status in {"hot", "watch", "emerging"} else "",
        *angles[:3],
        expert.get("operator_note", ""),
        f"价格 ${price:.0f} 处于可测区间" if 29 <= price <= 180 else "",
    ], 4)
    why_may_fail = _top_unique([
        *(risks or _default_failure_reasons(candidate, expert)),
        trend_risk if trend_status not in {"hot", "watch"} else "",
    ], 4)

    return {
        "prompt_version": MONEY_DECISION_PROMPT_VERSION,
        "can_follow": can_follow,
        "follow_level": follow_level,
        "timing": timing,
        "why_now": why_now,
        "why_will_sell": why_will_sell,
        "why_may_fail": why_may_fail,
        "meta_ads_angle": meta_angles,
        "landing_page_angle": build_landing_page_angle(candidate),
        "landing_page_must_haves": expert.get("landing_page_must_haves", []),
        "offer_strategy": expert.get("offer_strategy", ""),
        "platform_ai": platform_ai,
        "risk_flags": risks,
        "differentiation_angle": _differentiation_angle(candidate, expert),
        "what_not_to_copy": _what_not_to_copy(candidate, expert),
        "first_48h_test_plan": {
            "budget": f"${guardrails.get('first_test_budget', 50)}",
            "creative_count": max(3, len(meta_angles)),
            "angles": [a.get("angle", "") for a in meta_angles[:3]],
            "kill_rule": f"花费达到 ${guardrails.get('kill_spend_without_atc', round(max(price * 2, 50), 2))} 仍无 ATC，或 CPA 高于售价 50%，停止该角度",
            "scale_rule": guardrails.get("scale_signal", "CTR > 1.5%, ATC > 4%, CPA <= 35% of selling price within 48h"),
        },
        "next_action": _money_next_action(follow_level, timing, hard_risk),
        "evidence": {
            "score": score,
            "price": round(price, 2),
            "fb_ad_creative_count": appearances,
            "orbit_score": signal.get("orbit_score", 0),
            "ai_score": signal.get("ai_score", 0),
            "fb_verified_by_8000": signal.get("fb_verified_by_8000", False),
            "fb_verification_status": signal.get("fb_verification_status", "not_checked"),
            "fb_verification_source": signal.get("fb_verification_source", ""),
            "fb_verification_endpoint": signal.get("fb_verification_endpoint", ""),
            "google_trends": {
                "query": trend.get("trend_query", ""),
                "status": trend_status,
                "verified": trend_verified,
                "score": trend.get("trend_score", 0),
                "current": trend.get("trend_current", 0),
                "direction": trend.get("trend_direction", ""),
                "momentum_pct": trend.get("trend_momentum_pct", 0),
                "data_quality": trend.get("trend_data_quality", "未验证"),
                "confidence": trend.get("trend_confidence", ""),
                "source": trend.get("trend_source", ""),
                "data_points": trend.get("trend_data_points", 0),
                "decision": trend_decision,
                "risk": trend_risk,
            },
            "platform_validation": platform_validation,
            "platform_ai": {
                "automation_score": platform_ai.get("automation_score", 0),
                "readiness": platform_ai.get("readiness", ""),
                "best_channels": platform_ai.get("best_channels", []),
            },
            "decision": candidate.get("decision", ""),
        },
    }


def money_decision_for_candidate(candidate: JsonDict) -> JsonDict:
    existing = candidate.get("money_decision")
    if isinstance(existing, dict) and existing:
        return existing
    return build_money_decision(candidate, expert_for_candidate(candidate))


def build_selection_prompt_blueprint() -> JsonDict:
    """The deterministic prompt/rubric that the system applies to each product."""

    return {
        "version": MONEY_DECISION_PROMPT_VERSION,
        "role": "美国独立站 DTC 选品总监 + Meta/Facebook Ads 增长负责人 + Shopify 转化率负责人",
        "goal": "选出未来 48 小时内最可能用原创素材、简单 Shopify PDP、小预算 Meta 测试产生 ATC/CPA 信号的产品",
        "data_boundary": [
            "只能使用 8000 FB Ads 信号、8001 Shopify 快照、自动智能规则输出",
            "不能虚构销量、利润、供应链成本、广告花费或趋势数据",
            "所有自动输出只用于 review，不自动发布 Shopify 产品，不自动改主题，不自动花广告费",
        ],
        "scoring_rubric": [
            "$29-$89 冷启动甜蜜价格带加分；$90-$180 可测但要求信任组件；低客单和高客单扣分",
            "FB 素材 5-80 条为验证充分且未过饱和；80-300 条要差异化；300+ 视为拥挤/过热",
            "前 7 名产品必须补 Google Trends：hot/watch 才能强化时机判断；proxy/unverified 不能当真实趋势证据",
            "痛点解决、可视化演示、礼品身份、工具省力类加分",
            "医疗、孕妇、安全、治疗、IP、品牌词、大件易碎、不可售商品强风险",
            "最近 1-3 天新增/更新优先，因为时间窗口更接近实战跟品",
        ],
        "output_schema": {
            "score": "0-100",
            "decision": "立即测款 / 素材拆解 / 加入观察 / 放弃",
            "money_decision": [
                "can_follow",
                "follow_level",
                "timing",
                "why_now",
                "why_will_sell",
                "why_may_fail",
                "meta_ads_angle",
                "landing_page_angle",
                "offer_strategy",
                "first_48h_test_plan",
                "next_action",
                "evidence.google_trends",
            ],
        },
    }


def expert_for_candidate(candidate: JsonDict) -> JsonDict:
    existing = candidate.get("expert_assessment")
    if isinstance(existing, dict) and existing:
        return existing
    return _public_expert_assessment(
        build_expert_assessment(
            candidate,
            safe_float(candidate.get("price")),
            int(safe_float(candidate.get("fb_signal", {}).get("ad_creative_count"), 0)),
            _text_blob(candidate),
        )
    )


def run_auto_intelligence(
    config: AutoIntelConfig | None = None,
    fb_signals: SignalMap | None = None,
    signal_provider: SignalProvider | None = None,
    trend_provider: TrendProductProvider | None = None,
) -> JsonDict:
    """Run one complete auto-intelligence pass and write artifacts."""

    config = config or AutoIntelConfig()
    _normalize_config(config)
    with _auto_intel_run_lock(config):
        return _run_auto_intelligence_unlocked(config, fb_signals, signal_provider, trend_provider)


def _run_auto_intelligence_unlocked(
    config: AutoIntelConfig,
    fb_signals: SignalMap | None = None,
    signal_provider: SignalProvider | None = None,
    trend_provider: TrendProductProvider | None = None,
) -> JsonDict:
    """Run one complete pass after the caller has acquired the artifact lock."""

    Path(config.output_root).mkdir(parents=True, exist_ok=True)
    products, product_stats, warnings = load_recent_products(config)

    if fb_signals is None:
        provider = signal_provider or fetch_fb_signals
        fb_signals, signal_warnings = provider(config)
        warnings.extend(signal_warnings)
        verify_stats, verify_warnings = verify_missing_fb_signals(config, products, fb_signals)
        warnings.extend(verify_warnings)
    else:
        verify_stats = {"checked": 0, "matched": 0, "not_found": 0, "failed": 0, "skipped": 1}

    candidates: list[JsonDict] = []
    watch_source: list[JsonDict] = []
    rejected: list[JsonDict] = []
    for product in products:
        signal = fb_signals.get(normalize_domain(product.get("domain", "")), {})
        scored = score_candidate(product, signal)
        if scored["score"] >= config.min_score and scored["decision"] != "放弃":
            candidates.append(scored)
        elif scored["score"] >= config.research_min_score and not _has_hard_risk(scored["risk_flags"]):
            scored["decision"] = "观察池"
            scored["money_decision"] = build_money_decision(scored, expert_for_candidate(scored))
            watch_source.append(scored)
        else:
            rejected.append({
                "title": scored["title"],
                "domain": scored["domain"],
                "score": scored["score"],
                "decision": scored["decision"],
                "price": scored["price"],
                "fb_signal": scored["fb_signal"],
                "risk_flags": scored["risk_flags"],
                "reasons": scored["reasons"][:4],
                "money_decision": scored["money_decision"],
            })

    candidates = rank_candidates(candidates)
    portfolio, suppressed = apply_portfolio_limits(candidates, config)
    top = portfolio[: config.limit]
    top_ids = {_candidate_identity(c) for c in top}
    watchlist_raw = rank_candidates([
        c for c in [*portfolio[config.limit:], *suppressed, *watch_source]
        if _candidate_identity(c) not in top_ids
    ])
    diversified_watchlist, watchlist_suppressed = apply_portfolio_limits(watchlist_raw, config)
    watchlist = diversified_watchlist[: config.watchlist_limit]

    trend_stats = _empty_trend_stats()
    if config.enable_trends and top:
        trend_limit = min(max(0, int(config.trend_top_n)), len(top))
        if trend_limit > 0:
            provider = trend_provider or enrich_top_products_with_trends
            trend_records, trend_stats, trend_warnings = provider(
                top[:trend_limit],
                config.trend_geo,
                config.trend_max_keywords,
            )
            warnings.extend(trend_warnings)
            for candidate, trend in zip(top[:trend_limit], trend_records):
                _apply_trend_signal(candidate, trend)

    drafts = [build_shopify_draft(c, own_vendor=config.own_vendor) for c in top]
    briefs = [build_creative_brief(c) for c in top]

    run_date = (config.as_of_date or datetime.now().strftime("%Y-%m-%d"))[:10]
    output_dir = Path(config.output_root) / run_date
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_date": run_date,
        "output_dir": str(output_dir),
        "config": {
            "limit": config.limit,
            "min_score": config.min_score,
            "lookback_days": config.lookback_days,
            "fb_limit": config.fb_limit,
            "fb_exact_verify_limit": config.fb_exact_verify_limit,
            "enable_trends": config.enable_trends,
            "trend_top_n": config.trend_top_n,
            "trend_geo": config.trend_geo,
            "trend_max_keywords": config.trend_max_keywords,
            "max_per_domain": config.max_per_domain,
            "max_per_family": config.max_per_family,
            "research_min_score": config.research_min_score,
            "watchlist_limit": config.watchlist_limit,
            "money_decision_prompt_version": MONEY_DECISION_PROMPT_VERSION,
        },
        "stats": {
            **product_stats,
            "fb_signal_domains": len(fb_signals),
            "fb_exact_verify_checked": verify_stats.get("checked", 0),
            "fb_exact_verify_matched": verify_stats.get("matched", 0),
            "fb_exact_verify_not_found": verify_stats.get("not_found", 0),
            "fb_exact_verify_failed": verify_stats.get("failed", 0),
            "trend_checked_products": trend_stats.get("requested_products", 0),
            "trend_matched_products": trend_stats.get("matched_products", 0),
            "trend_verified_products": trend_stats.get("verified_products", 0),
            "trend_proxy_products": trend_stats.get("proxy_products", 0),
            "trend_hot_products": trend_stats.get("hot_products", 0),
            "qualified_candidates": len(candidates),
            "top_count": len(top),
            "watchlist_count": len(watchlist),
            "duplicate_suppressed_count": sum(1 for c in suppressed if c.get("portfolio_suppression") == "duplicate_family"),
            "domain_limited_count": sum(1 for c in suppressed if c.get("portfolio_suppression") == "domain_limit"),
            "watchlist_suppressed_count": len(watchlist_suppressed),
            "rejected_count": len(rejected),
        },
        "trend_summary": trend_stats,
        "warnings": warnings,
        "artifacts": {
            "top20_action_report": str(output_dir / "top20_action_report.md"),
            "shopify_draft_products": str(output_dir / "shopify_draft_products.json"),
            "creative_briefs": str(output_dir / "creative_briefs.json"),
            "selection_board": str(output_dir / "selection_board.json"),
            "selection_prompt_blueprint": str(output_dir / "selection_prompt_blueprint.json"),
            "rejected_products": str(output_dir / "rejected_products.json"),
            "run_summary": str(output_dir / "run_summary.json"),
        },
        "selection_prompt_blueprint": build_selection_prompt_blueprint(),
        "top_products": top,
        "watchlist": watchlist,
    }
    selection_board = build_selection_board(top, watchlist, rejected, duplicate_suppressed=len(suppressed))

    _write_json(output_dir / "shopify_draft_products.json", {"products": drafts})
    _write_json(output_dir / "creative_briefs.json", {"briefs": briefs})
    _write_json(output_dir / "selection_board.json", selection_board)
    _write_json(output_dir / "selection_prompt_blueprint.json", build_selection_prompt_blueprint())
    _write_json(output_dir / "rejected_products.json", {"products": rejected[:300]})
    _write_text(output_dir / "top20_action_report.md", build_action_report(summary, top, briefs, selection_board))
    _write_json(output_dir / "run_summary.json", summary)
    Path(config.output_root).mkdir(parents=True, exist_ok=True)
    _write_json(Path(config.output_root) / "latest_run.json", summary)
    return summary


def _normalize_config(config: AutoIntelConfig) -> None:
    config.limit = max(1, min(int(config.limit), 100))
    config.min_score = max(0, min(int(config.min_score), 100))
    config.max_per_domain = max(1, min(int(config.max_per_domain), 20))
    config.max_per_family = max(1, min(int(config.max_per_family), 5))
    config.research_min_score = max(0, min(int(config.research_min_score), config.min_score))
    config.watchlist_limit = max(0, min(int(config.watchlist_limit), 200))
    config.fb_limit = max(1, min(int(config.fb_limit), 20000))
    config.fb_exact_verify_limit = max(0, min(int(config.fb_exact_verify_limit), 1000))
    config.enable_trends = bool(config.enable_trends)
    config.trend_top_n = max(0, min(int(config.trend_top_n), 100))
    config.trend_geo = (str(config.trend_geo or "US").upper())[:8]
    config.trend_max_keywords = max(1, min(int(config.trend_max_keywords), 150))
    config.max_products = max(1, min(int(config.max_products), 5000))
    config.fb_timeout_seconds = max(1.0, min(float(config.fb_timeout_seconds), 30.0))
    config.lock_stale_seconds = max(60, min(int(config.lock_stale_seconds), 86400))


def get_latest_run_summary(output_root: Path = DEFAULT_OUTPUT_ROOT) -> JsonDict:
    latest = Path(output_root) / "latest_run.json"
    if not latest.exists():
        return {"ok": False, "error": "No auto intelligence run found"}
    payload, error = _read_json_file(latest)
    if error:
        return {"ok": False, "error": error, "path": str(latest)}
    return payload


def read_latest_artifact(name: str, output_root: Path = DEFAULT_OUTPUT_ROOT) -> JsonDict:
    summary = get_latest_run_summary(output_root)
    if not summary.get("ok"):
        return summary
    artifacts = summary.get("artifacts", {})
    if name not in artifacts:
        return {"ok": False, "error": f"Unknown artifact: {name}", "available": sorted(artifacts)}
    path = Path(artifacts[name])
    if not path.exists():
        return {"ok": False, "error": f"Artifact missing: {path}"}
    if path.suffix == ".md":
        content, error = _read_text_file(path)
        if error:
            return {"ok": False, "error": error, "path": str(path)}
        return {"ok": True, "name": name, "path": str(path), "content": content}
    content, error = _read_json_file(path)
    if error:
        return {"ok": False, "error": error, "path": str(path)}
    return {"ok": True, "name": name, "path": str(path), "content": content}


def run_latest_auto_intel_trends(
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    geo: str = "US",
    top_n: int = 7,
    max_keywords: int = 18,
    timeframe: str = "today 12-m",
    workers: int = 2,
    ttl_days: int = 7,
    timeout_seconds: int = 180,
    trend_provider: TrendProductProvider | None = None,
) -> JsonDict:
    """Run Google Trends only for the latest auto-intelligence Top products."""

    config = AutoIntelConfig(
        output_root=Path(output_root),
        enable_trends=True,
        trend_top_n=top_n,
        trend_geo=(geo or "US").upper(),
        trend_max_keywords=max_keywords,
    )
    _normalize_config(config)
    with _auto_intel_run_lock(config):
        summary = get_latest_run_summary(config.output_root)
        if not summary.get("ok"):
            return summary

        top = summary.get("top_products", [])
        if not isinstance(top, list) or not top:
            return {**summary, "ok": False, "error": "No top_products found in latest auto-intel run"}

        trend_limit = min(max(0, int(top_n)), len(top))
        if trend_limit <= 0:
            return {**summary, "ok": False, "error": "trend_top_n must be greater than 0"}

        provider = trend_provider
        if provider:
            trend_records, trend_stats, trend_warnings = provider(
                top[:trend_limit],
                config.trend_geo,
                config.trend_max_keywords,
            )
        else:
            trend_records, trend_stats, trend_warnings = enrich_top_products_with_gtrends_bulk(
                top[:trend_limit],
                config.trend_geo,
                config.trend_max_keywords,
                timeframe=timeframe,
                workers=workers,
                ttl_days=ttl_days,
                timeout_seconds=timeout_seconds,
            )

        for candidate, trend in zip(top[:trend_limit], trend_records):
            _apply_trend_signal(candidate, trend)

        stats = dict(summary.get("stats") or {})
        stats.update({
            "trend_checked_products": trend_stats.get("requested_products", 0),
            "trend_matched_products": trend_stats.get("matched_products", 0),
            "trend_verified_products": trend_stats.get("verified_products", 0),
            "trend_proxy_products": trend_stats.get("proxy_products", 0),
            "trend_hot_products": trend_stats.get("hot_products", 0),
        })
        summary["stats"] = stats
        summary["trend_summary"] = trend_stats
        summary["top_products"] = top
        summary["trend_checked_at"] = datetime.now().isoformat(timespec="seconds")
        existing_warnings = [
            str(w) for w in summary.get("warnings", [])
            if not str(w).startswith("gtrends_bulk_")
        ]
        summary["warnings"] = _unique_list([
            *existing_warnings,
            *[str(w) for w in trend_warnings],
        ])

        output_dir = Path(summary.get("output_dir") or (config.output_root / str(summary.get("run_date") or "")))
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = dict(summary.get("artifacts") or {})
        artifact_defaults = {
            "top20_action_report": output_dir / "top20_action_report.md",
            "shopify_draft_products": output_dir / "shopify_draft_products.json",
            "creative_briefs": output_dir / "creative_briefs.json",
            "selection_board": output_dir / "selection_board.json",
            "selection_prompt_blueprint": output_dir / "selection_prompt_blueprint.json",
            "rejected_products": output_dir / "rejected_products.json",
            "run_summary": output_dir / "run_summary.json",
        }
        for name, default_path in artifact_defaults.items():
            artifacts[name] = str(artifacts.get(name) or default_path)
        summary["artifacts"] = artifacts

        rejected = _read_rejected_products(summary)
        watchlist = summary.get("watchlist", [])
        if not isinstance(watchlist, list):
            watchlist = []
        duplicate_suppressed = int(stats.get("duplicate_suppressed_count", 0)) + int(stats.get("domain_limited_count", 0))
        selection_board = build_selection_board(top, watchlist, rejected, duplicate_suppressed=duplicate_suppressed)
        drafts = [build_shopify_draft(c) for c in top]
        briefs = [build_creative_brief(c) for c in top]

        _write_json(Path(artifacts["shopify_draft_products"]), {"products": drafts})
        _write_json(Path(artifacts["creative_briefs"]), {"briefs": briefs})
        _write_json(Path(artifacts["selection_board"]), selection_board)
        _write_json(Path(artifacts["selection_prompt_blueprint"]), summary.get("selection_prompt_blueprint") or build_selection_prompt_blueprint())
        _write_text(Path(artifacts["top20_action_report"]), build_action_report(summary, top, briefs, selection_board))
        _write_json(Path(artifacts["run_summary"]), summary)
        _write_json(config.output_root / "latest_run.json", summary)
        return summary


def run_seven_day_new_product_judgement(
    config: AutoIntelConfig | None = None,
    limit: int = 120,
    trend_top_n: int = 50,
    trend_provider: TrendProductProvider | None = None,
    signal_provider: SignalProvider | None = None,
    fb_signals: SignalMap | None = None,
) -> JsonDict:
    """Rank newly listed products from the last seven days for follow-up."""

    config = config or AutoIntelConfig()
    config.lookback_days = 7
    config.max_products = max(config.max_products, 2000)
    config.fb_exact_verify_limit = max(config.fb_exact_verify_limit, 300)
    config.trend_geo = (config.trend_geo or "US").upper()
    config.trend_top_n = max(0, min(int(trend_top_n), 100))
    _normalize_config(config)

    products, product_stats, warnings = load_newly_listed_products(config, days=7)
    as_of = _parse_date(config.as_of_date) or datetime.now()
    new_products = products

    if fb_signals is None:
        provider = signal_provider or fetch_fb_signals
        fb_signals, signal_warnings = provider(config)
        warnings.extend(signal_warnings)
        verify_stats, verify_warnings = verify_missing_fb_signals(config, new_products, fb_signals)
        warnings.extend(verify_warnings)
    else:
        verify_stats = {"checked": 0, "matched": 0, "not_found": 0, "failed": 0, "skipped": 1}

    scored: list[JsonDict] = []
    for product in new_products:
        signal = fb_signals.get(normalize_domain(product.get("domain", "")), {})
        candidate = score_candidate(product, signal)
        candidate["new_product_date"] = _new_product_date(product)
        candidate["days_since_new"] = _days_since_new(product, as_of)
        candidate["seven_day_judgement"] = build_seven_day_judgement(candidate)
        scored.append(candidate)

    scored = rank_seven_day_candidates(scored)
    top = scored[: max(1, min(int(limit), 300))]

    trend_stats = _empty_trend_stats()
    if config.trend_top_n > 0 and top:
        trend_scope = top[: min(config.trend_top_n, len(top))]
        provider = trend_provider
        if provider:
            trend_records, trend_stats, trend_warnings = provider(
                trend_scope,
                config.trend_geo,
                config.trend_max_keywords,
            )
        else:
            trend_records, trend_stats, trend_warnings = enrich_top_products_with_gtrends_bulk(
                trend_scope,
                config.trend_geo,
                config.trend_max_keywords,
                workers=2,
                timeout_seconds=900,
            )
        warnings.extend(trend_warnings)
        for candidate, trend in zip(trend_scope, trend_records):
            _apply_trend_signal(candidate, trend)
            candidate["seven_day_judgement"] = build_seven_day_judgement(candidate)
        top = rank_seven_day_candidates(top)

    priority_counts: dict[str, int] = {}
    for item in top:
        decision = str((item.get("seven_day_judgement") or {}).get("decision") or "暂不跟进")
        priority_counts[decision] = priority_counts.get(decision, 0) + 1
    platform_verified = sum(1 for item in top if int(safe_float((item.get("platform_validation") or {}).get("score"), 0)) >= 22)
    micro_vertical_count = sum(1 for item in top if ((item.get("platform_validation") or {}).get("store_context") or {}).get("is_micro_vertical"))
    platform_ai_ready = sum(1 for item in top if ((item.get("platform_ai") or (item.get("seven_day_judgement") or {}).get("platform_ai") or {}).get("readiness") == "ai_ready"))
    platform_ai_asset_gap = sum(1 for item in top if ((item.get("platform_ai") or (item.get("seven_day_judgement") or {}).get("platform_ai") or {}).get("readiness") == "asset_gap"))

    stats = {
        **product_stats,
        "window_days": 7,
        "new_products": len(new_products),
        "scored_products": len(scored),
        "returned_products": len(top),
        "fb_signal_domains": len(fb_signals),
        "fb_exact_verify_checked": verify_stats.get("checked", 0),
        "fb_exact_verify_matched": verify_stats.get("matched", 0),
        "fb_exact_verify_not_found": verify_stats.get("not_found", 0),
        "fb_exact_verify_failed": verify_stats.get("failed", 0),
        "trend_checked_products": trend_stats.get("requested_products", 0),
        "trend_verified_products": trend_stats.get("verified_products", 0),
        "trend_hot_products": trend_stats.get("hot_products", 0),
        "platform_verified_products": platform_verified,
        "micro_vertical_products": micro_vertical_count,
        "platform_ai_ready_products": platform_ai_ready,
        "platform_ai_asset_gap_products": platform_ai_asset_gap,
        "priority_counts": priority_counts,
        "duplicate_suppressed_count": 0,
    }
    summary = {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method_version": SEVEN_DAY_METHOD_VERSION,
        "as_of_date": as_of.strftime("%Y-%m-%d"),
        "window_days": 7,
        "config": {
            "limit": limit,
            "trend_top_n": config.trend_top_n,
            "trend_geo": config.trend_geo,
            "fb_limit": config.fb_limit,
            "fb_exact_verify_limit": config.fb_exact_verify_limit,
        },
        "stats": stats,
        "trend_summary": trend_stats,
        "warnings": _unique_list([str(w) for w in warnings]),
        "method_blueprint": build_seven_day_method_blueprint(),
        "products": [public_seven_day_product(item) for item in top],
    }
    try:
        _write_json(Path(config.output_root) / "seven_day_new_products_latest.json", summary)
    except OSError:
        pass
    return summary


def run_profit_pipeline(
    config: AutoIntelConfig | None = None,
    limit: int = 30,
    trend_top_n: int = 50,
    source: str = "seven_day",
) -> JsonDict:
    """Run the closed-loop product-to-test pipeline without publishing or spending."""

    config = config or AutoIntelConfig()
    config.lookback_days = 7
    config.trend_top_n = max(0, min(int(trend_top_n), 100))
    config.trend_max_keywords = max(1, min(max(config.trend_top_n or 1, limit or 1) * 3, 150))
    limit = max(1, min(int(limit), 100))

    if source not in {"seven_day", "7d", "new_products"}:
        source = "seven_day"

    judgement = run_seven_day_new_product_judgement(
        config,
        limit=max(limit, config.trend_top_n, 1),
        trend_top_n=config.trend_top_n,
    )
    if not judgement.get("ok"):
        return judgement

    products = judgement.get("products", [])
    if not isinstance(products, list):
        products = []
    actions = [
        build_profit_pipeline_item(product, rank=index + 1)
        for index, product in enumerate(products[:limit])
        if isinstance(product, dict)
    ]
    triage_order = {"launch_now": 0, "observe": 1, "avoid": 2}
    actions.sort(key=lambda item: (
        triage_order.get(((item.get("pareto") or {}).get("launch_triage") or {}).get("level"), 9),
        -int(safe_float((item.get("pareto") or {}).get("priority"), 0)),
        int(safe_float(item.get("rank"), 9999)),
    ))
    board = build_profit_pipeline_board(actions)
    stats = {
        "source_products": len(products),
        "returned_actions": len(actions),
        "ready_to_test": len(board["ready_to_test"]),
        "build_asset_pack": len(board["build_asset_pack"]),
        "validation_queue": len(board["validation_queue"]),
        "hold": len(board["hold"]),
        "trend_checked_products": (judgement.get("stats") or {}).get("trend_checked_products", 0),
        "trend_verified_products": (judgement.get("stats") or {}).get("trend_verified_products", 0),
        "platform_ai_ready_products": sum(1 for item in actions if item.get("platform_ai", {}).get("readiness") == "ai_ready"),
        "platform_ai_asset_gap_products": sum(1 for item in actions if item.get("platform_ai", {}).get("readiness") == "asset_gap"),
        "operator_test_ready_products": sum(1 for item in actions if (item.get("operator_gate") or {}).get("pass_to_test")),
        "market_proof_products": sum(1 for item in actions if (item.get("operator_gate") or {}).get("market_ready")),
        "asset_ready_products": sum(1 for item in actions if (item.get("operator_gate") or {}).get("assets_ready")),
        "unit_economics_ready_products": sum(1 for item in actions if (item.get("operator_gate") or {}).get("economics_ready")),
        "operator_gate_status_counts": _count_by(actions, lambda item: (item.get("operator_gate") or {}).get("status", "unknown")),
        "blocked_by_economics": sum(1 for item in actions if any("单品经济" in str(x) for x in ((item.get("operator_gate") or {}).get("blockers") or []))),
        "blocked_by_hard_risk": sum(1 for item in actions if any("硬风险" in str(x) for x in ((item.get("operator_gate") or {}).get("blockers") or []))),
        "pareto_lane_counts": _count_by(actions, lambda item: (item.get("pareto") or {}).get("lane", "unknown")),
        "copy_now_products": sum(1 for item in actions if (item.get("pareto") or {}).get("lane") == "copy_now"),
        "verify_first_products": sum(1 for item in actions if (item.get("pareto") or {}).get("lane") == "verify_first"),
        "teardown_first_products": sum(1 for item in actions if (item.get("pareto") or {}).get("lane") in {"verify_first", "teardown_first"}),
        "find_supplier_products": sum(1 for item in actions if (item.get("pareto") or {}).get("lane") == "find_supplier"),
        "skip_products": sum(1 for item in actions if (item.get("pareto") or {}).get("lane") == "skip"),
        "traffic_validated_products": sum(1 for item in actions if (item.get("pareto") or {}).get("traffic_validated")),
        "unverified_products": sum(1 for item in actions if not (item.get("pareto") or {}).get("traffic_validated")),
        "one_source_products": sum(1 for item in actions if int(safe_float((item.get("pareto") or {}).get("real_validation_count"), 0)) == 1),
        "supply_to_verify_products": sum(1 for item in actions if ((item.get("pareto") or {}).get("supply_chain") or {}).get("status") == "to_verify"),
        "launch_now_products": sum(1 for item in actions if (((item.get("pareto") or {}).get("launch_triage") or {}).get("level") == "launch_now")),
        "observe_products": sum(1 for item in actions if (((item.get("pareto") or {}).get("launch_triage") or {}).get("level") == "observe")),
        "avoid_products": sum(1 for item in actions if (((item.get("pareto") or {}).get("launch_triage") or {}).get("level") == "avoid")),
        "triage_counts": _count_by(actions, lambda item: (((item.get("pareto") or {}).get("launch_triage") or {}).get("level", "unknown"))),
    }
    generated_at = datetime.now().isoformat(timespec="seconds")
    output_dir = Path(config.output_root) / "profit_pipeline"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "profit_pipeline": str(output_dir / "profit_pipeline_latest.json"),
        "profit_pipeline_report": str(output_dir / "profit_pipeline_report.md"),
        "profit_test_orders": str(output_dir / "profit_test_orders.json"),
    }
    pipeline = {
        "ok": True,
        "version": PROFIT_PIPELINE_VERSION,
        "generated_at": generated_at,
        "source": source,
        "method_version": judgement.get("method_version", ""),
        "config": {
            "limit": limit,
            "trend_top_n": config.trend_top_n,
            "trend_geo": config.trend_geo,
            "fb_limit": config.fb_limit,
            "fb_exact_verify_limit": config.fb_exact_verify_limit,
        },
        "stages": [
            {"key": "monitor", "label": "监控原料", "status": "done", "count": stats["source_products"]},
            {"key": "truth", "label": "平台验真通过", "status": "done", "count": stats["traffic_validated_products"]},
            {"key": "launch", "label": "立即上架", "status": "done", "count": stats["launch_now_products"]},
            {"key": "observe", "label": "观察跟进", "status": "done", "count": stats["observe_products"]},
            {"key": "avoid", "label": "不碰", "status": "done", "count": stats["avoid_products"]},
            {"key": "action", "label": "今天一个动作", "status": "done", "count": stats["returned_actions"]},
        ],
        "stats": stats,
        "board": board,
        "workbench": build_profit_workbench(actions, stats),
        "operator_blueprint": build_operator_blueprint(),
        "actions": actions,
        "seven_day_summary": {
            "generated_at": judgement.get("generated_at", ""),
            "stats": judgement.get("stats", {}),
            "trend_summary": judgement.get("trend_summary", {}),
            "warnings": judgement.get("warnings", []),
        },
        "warnings": judgement.get("warnings", []),
        "artifacts": artifacts,
    }
    _write_json(Path(artifacts["profit_pipeline"]), pipeline)
    _write_json(Path(artifacts["profit_test_orders"]), {
        "ok": True,
        "generated_at": generated_at,
        "version": PROFIT_PIPELINE_VERSION,
        "orders": [item.get("launch_test_order", {}) for item in actions],
    })
    _write_text(Path(artifacts["profit_pipeline_report"]), build_profit_pipeline_report(pipeline))
    _write_json(Path(config.output_root) / "profit_pipeline_latest.json", pipeline)
    return pipeline


def get_latest_profit_pipeline(output_root: Path = DEFAULT_OUTPUT_ROOT) -> JsonDict:
    latest = Path(output_root) / "profit_pipeline_latest.json"
    if not latest.exists():
        return {"ok": False, "error": "No profit pipeline run found"}
    payload, error = _read_json_file(latest)
    if error:
        return {"ok": False, "error": error, "path": str(latest)}
    return payload


def read_latest_profit_artifact(name: str, output_root: Path = DEFAULT_OUTPUT_ROOT) -> JsonDict:
    latest = get_latest_profit_pipeline(output_root)
    if not latest.get("ok"):
        return latest
    artifacts = latest.get("artifacts", {})
    if name not in artifacts:
        return {"ok": False, "error": f"Unknown profit artifact: {name}", "available": sorted(artifacts)}
    path = Path(artifacts[name])
    if not path.exists():
        return {"ok": False, "error": f"Artifact missing: {path}"}
    if path.suffix == ".md":
        content, error = _read_text_file(path)
        if error:
            return {"ok": False, "error": error, "path": str(path)}
        return {"ok": True, "name": name, "path": str(path), "content": content}
    content, error = _read_json_file(path)
    if error:
        return {"ok": False, "error": error, "path": str(path)}
    return {"ok": True, "name": name, "path": str(path), "content": content}


def _money_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _count_by(items: list[JsonDict], key_fn: Callable[[JsonDict], Any]) -> JsonDict:
    counts: JsonDict = {}
    for item in items:
        key = str(key_fn(item) or "unknown")
        counts[key] = int(counts.get(key, 0)) + 1
    return counts


def build_operator_unit_economics(product: JsonDict, money: JsonDict | None = None) -> JsonDict:
    """Estimate the economics needed for a real paid-test gate.

    These are explicit assumptions until real COGS/shipping are connected.
    """

    money = money or {}
    price = safe_float(product.get("price"))
    if price <= 0:
        price = _money_number((money.get("evidence") or {}).get("price"), 0)
    cogs_ratio = 0.32 if price <= 80 else 0.38 if price <= 180 else 0.45
    cogs = round(price * cogs_ratio, 2)
    fulfillment_reserve = round(max(5.95, price * 0.10), 2) if price else 0
    payment_fee = round(price * 0.029 + 0.30, 2) if price else 0
    refund_reserve = round(price * 0.06, 2) if price else 0
    contribution = round(price - cogs - fulfillment_reserve - payment_fee - refund_reserve, 2)
    break_even_cpa = round(max(0, contribution), 2)
    target_cpa = round(break_even_cpa * 0.65, 2)
    first_budget = round(max(30, min(150, target_cpa * 3)), 2) if target_cpa else 0
    kill_no_atc = round(max(18, min(first_budget or 40, target_cpa * 0.9 if target_cpa else price * 0.75)), 2)
    kill_no_purchase = round(max(kill_no_atc, min(first_budget or 60, target_cpa * 2 if target_cpa else price * 1.5)), 2)
    margin_ratio = round((contribution / price) * 100, 1) if price else 0
    return {
        "price": round(price, 2),
        "assumption": "COGS/shipping/refund reserves are estimated until supplier cost is connected",
        "requires_real_cogs": True,
        "estimated_cogs": cogs,
        "estimated_cogs_ratio": round(cogs_ratio * 100, 1),
        "fulfillment_reserve": fulfillment_reserve,
        "payment_fee_reserve": payment_fee,
        "refund_reserve": refund_reserve,
        "contribution_before_ads": contribution,
        "contribution_margin_pct": margin_ratio,
        "break_even_cpa": break_even_cpa,
        "target_cpa": target_cpa,
        "first_test_budget": first_budget,
        "kill_spend_without_atc": kill_no_atc,
        "kill_spend_without_purchase": kill_no_purchase,
        "economics_ready": price >= 18 and contribution >= 12 and margin_ratio >= 25,
    }


def _next_gate_to_clear(status: str, blockers: list[str], missing: list[str]) -> str:
    if status == "operator_test_ready":
        return "人工确认真实COGS/库存/合规后，可以建测试单"
    if blockers:
        return str(blockers[0])
    if missing:
        return str(missing[0])
    return "继续补真实平台证据和原创素材"


def build_operator_gate(
    product: JsonDict,
    platform_ai: JsonDict,
    money: JsonDict,
    unit_economics: JsonDict,
    creative_pack: JsonDict,
) -> JsonDict:
    """Hard gate the product like an operator would before spending money."""

    signal = product.get("fb_signal") if isinstance(product.get("fb_signal"), dict) else {}
    trends = product.get("google_trends") if isinstance(product.get("google_trends"), dict) else (money.get("evidence", {}) or {}).get("google_trends", {})
    platform_validation = product.get("platform_validation") if isinstance(product.get("platform_validation"), dict) else {}
    risks = list(product.get("risk_flags") or [])
    price = safe_float(product.get("price"))
    fb_count = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    trend_verified = bool(trends.get("verified") or trends.get("trend_verified"))
    trend_status = str(trends.get("status") or trends.get("trend_status") or "unverified")
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    store_context = platform_validation.get("store_context") if isinstance(platform_validation.get("store_context"), dict) else {}
    is_micro_vertical = bool(store_context.get("is_micro_vertical"))
    image_count = int(safe_float(product.get("image_count"), 0))
    variant_count = int(safe_float(product.get("variant_count"), 0))
    creative_axes = list(platform_ai.get("creative_axes") or [])
    scripts = (creative_pack.get("scripts") or []) if isinstance(creative_pack, dict) else []

    proof_sources = []
    if fb_count >= 1:
        proof_sources.append("fb_ads")
    if trend_verified:
        proof_sources.append("google_trends")
    if platform_score >= 22:
        proof_sources.append("reddit_youtube_tiktok_amazon_cache")
    if is_micro_vertical:
        proof_sources.append("vertical_micro_site")

    blockers = []
    missing = []
    missing_evidence = []
    creative_gaps = []
    pdp_gaps = []
    economics_gaps = []
    if _has_hard_risk(risks):
        blockers.append("硬风险：IP/医疗/安全等风险未解除，不进投放")
    if not (18 <= price <= 250):
        blockers.append("价格不在冷启动可测区间 $18-$250")
    if not unit_economics.get("economics_ready"):
        gap = "单品经济不够清楚或预估贡献毛利不足，先接真实 COGS/运费"
        blockers.append(gap)
        economics_gaps.append(gap)
    if len(proof_sources) < 2:
        missing_evidence.append("至少需要2类真实验证：FB素材/Google Trends/Reddit-YouTube-TikTok-Amazon/小垂直站")
    if not trend_verified:
        missing_evidence.append("Google Trends 真实时间序列未验证")
    if fb_count < 1:
        missing_evidence.append("FB Ad Library 未命中竞品素材，不能证明付费流量已被市场验证")
    if platform_score < 22:
        missing_evidence.append("Reddit/YouTube/TikTok/Amazon 等平台证据不足")
    if image_count < 2:
        pdp_gaps.append("PDP素材不足：至少2张图或1张图+1个短视频/GIF")
    if len(creative_axes) < 2 or len(scripts) < 6:
        creative_gaps.append("素材包不足：至少2个素材角度和6条可投脚本")
    if variant_count > 12:
        pdp_gaps.append("变体过多，先收窄到主推SKU，降低建页和投放学习噪音")

    missing = _top_unique([*missing_evidence, *creative_gaps, *pdp_gaps, *economics_gaps], 10)

    assets_ready = image_count >= 2 and len(creative_axes) >= 2 and len(scripts) >= 6
    ai_ready_enough = str(platform_ai.get("readiness")) in {"ai_ready", "asset_gap", "research_first"} and int(safe_float(platform_ai.get("automation_score"), 0)) >= 50
    market_ready = len(proof_sources) >= 2

    if blockers:
        status = "blocked"
        pass_to_test = False
    elif market_ready and assets_ready and ai_ready_enough:
        status = "operator_test_ready"
        pass_to_test = True
    elif market_ready:
        status = "asset_pack_required"
        pass_to_test = False
    else:
        status = "validation_required"
        pass_to_test = False

    return {
        "status": status,
        "pass_to_test": pass_to_test,
        "market_ready": market_ready,
        "assets_ready": assets_ready,
        "ai_ready_enough": ai_ready_enough,
        "economics_ready": bool(unit_economics.get("economics_ready")),
        "proof_sources": proof_sources,
        "proof_source_count": len(proof_sources),
        "trend_status": trend_status,
        "fb_creative_count": fb_count,
        "platform_validation_score": platform_score,
        "image_count": image_count,
        "variant_count": variant_count,
        "blockers": _top_unique(blockers, 8),
        "missing_evidence": _top_unique(missing_evidence, 8),
        "creative_gaps": _top_unique(creative_gaps, 8),
        "pdp_gaps": _top_unique(pdp_gaps, 8),
        "economics_gaps": _top_unique(economics_gaps, 8),
        "missing": _top_unique(missing, 8),
        "next_gate_to_clear": _next_gate_to_clear(status, blockers, missing),
        "operator_rule": "只有无硬风险 + 价格/毛利可测 + 至少2类真实验证 + 6条原创素材脚本 + PDP素材齐，才进入小预算实测",
    }


def build_launch_triage(
    *,
    lane: str,
    gate: JsonDict,
    truth_check: JsonDict,
    supply_status: str,
    unit_economics: JsonDict,
    platform_ai: JsonDict,
    gaps: list[str],
) -> JsonDict:
    """Collapse noisy evidence into the three operator labels shown in V2."""

    strict_count = int(safe_float(truth_check.get("count"), 0))
    required_count = int(safe_float(truth_check.get("required_count"), 2))
    blockers = _top_unique(list(gate.get("blockers") or []), 8)
    economics_ready = bool(unit_economics.get("economics_ready"))
    supply_ready = supply_status == "marketplace_reference"
    assets_ready = bool(gate.get("assets_ready"))
    ai_score = int(safe_float(platform_ai.get("automation_score"), 0))
    ai_ready = str(platform_ai.get("readiness") or "") in {"ai_ready", "asset_gap", "research_first"} and ai_score >= 50
    passed_truth = strict_count >= required_count

    missing: list[str] = []
    if not passed_truth:
        missing.append(f"验真不足：严格证据 {strict_count}/{required_count}")
        missing.extend(list(truth_check.get("missing") or [])[:3])
    if not economics_ready:
        missing.append("经济性未过：售价、预估贡献毛利或真实COGS未确认")
    if not supply_ready:
        missing.append("供应链/市场参考未过：缺 Amazon/Google Shopping/严选缓存等同类价格证据")
    if not assets_ready:
        missing.append("素材/PDP未齐：至少2张图或1图+视频/GIF，并准备6条原创素材脚本")
    if not ai_ready:
        missing.append("平台AI可投性不足：需补素材角度、PDP结构或投放脚本")
    missing = _top_unique([*missing, *gaps], 8)

    if blockers or lane == "skip":
        label = "不碰"
        level = "avoid"
        color = "red"
        can_upload_draft = False
        can_publish = False
        one_line = (blockers or missing or ["硬风险、毛利或证据不成立"])[0]
    elif passed_truth and economics_ready and supply_ready:
        label = "立即上架"
        level = "launch_now"
        color = "green"
        can_upload_draft = True
        can_publish = False
        one_line = "已过2类验真 + 经济性 + 市场/供应链参考，可先上架DRAFT并人工复核"
    else:
        label = "观察跟进"
        level = "observe"
        color = "yellow"
        can_upload_draft = False
        can_publish = False
        one_line = (missing or ["继续观察广告持续性、趋势、讨论和供应链"])[0]

    return {
        "label": label,
        "level": level,
        "color": color,
        "can_upload_draft": can_upload_draft,
        "can_publish": can_publish,
        "one_line": one_line,
        "rules": [
            "立即上架：至少2类严格验真 + 无硬风险 + 经济性可测 + 同类市场/供应链参考成立；只允许先建DRAFT，发布前人工复核。",
            "观察跟进：有线索但缺平台验真、趋势、Reddit/X/YouTube讨论、广告持续性、PDP素材或供应链任一关键证据。",
            "不碰：硬风险、毛利不成立、证据无法闭环、价格不适合冷启动或平台信号明显不成立。",
        ],
        "validation": {
            "strict_count": strict_count,
            "required_count": required_count,
            "passed_truth": passed_truth,
            "economics_ready": economics_ready,
            "supply_ready": supply_ready,
            "assets_ready": assets_ready,
            "ai_ready": ai_ready,
            "proof_sources": list(gate.get("proof_sources") or []),
        },
        "missing": missing,
        "blockers": blockers,
    }


def build_operator_metric_rules(unit_economics: JsonDict) -> JsonDict:
    target_cpa = safe_float(unit_economics.get("target_cpa"), 0)
    break_even_cpa = safe_float(unit_economics.get("break_even_cpa"), 0)
    kill_no_atc = safe_float(unit_economics.get("kill_spend_without_atc"), 0)
    kill_no_purchase = safe_float(unit_economics.get("kill_spend_without_purchase"), 0)
    return {
        "review_window": "48-72h",
        "phase_0_creative_read": [
            "1,500 impressions 后 CTR < 0.7%：先停该素材，换首3秒钩子",
            "CTR >= 1.5% 但 ATC < 2%：素材可留，优先修PDP首屏/价格/Offer",
        ],
        "kill": [
            f"花费达到 ${kill_no_atc:.2f} 仍无 ATC：停该素材角度",
            f"花费达到 ${kill_no_purchase:.2f} 仍无 Purchase：停该广告组或该产品测试",
            f"有购买但 CPA > break-even ${break_even_cpa:.2f}：不放量，先降成本或提高AOV",
        ],
        "scale": [
            f"2单以上且 CPA <= target ${target_cpa:.2f}：预算每24小时增加20%-30%",
            "CTR >= 1.5%、ATC >= 4%、无购买：不加预算，先修 checkout/PDP 信任组件",
            "连续2天 CPA <= break-even 且 ROAS > 1.2：保留学习，补新素材而不是复制广告组",
        ],
        "guardrail": "不自动花费、不自动发布；只有指标达标才允许人工确认后放量",
    }


def build_operator_test_order(
    product: JsonDict,
    gate: JsonDict,
    unit_economics: JsonDict,
    creative_pack: JsonDict,
    platform_ai: JsonDict,
    money: JsonDict,
) -> JsonDict:
    title = clean_product_title(product.get("title", ""))
    test_id = f"{slugify(product.get('domain', 'store'))}-{slugify(title)[:42]}"
    scripts = creative_pack.get("scripts", []) if isinstance(creative_pack, dict) else []
    daily_budget = round(max(15, safe_float(unit_economics.get("first_test_budget"), 0) / 2), 2)
    return {
        "test_id": test_id,
        "status": "ready_for_human_launch" if gate.get("pass_to_test") else "not_launchable_until_gate_passes",
        "channel": (platform_ai.get("best_channels") or ["meta_advantage_plus"])[0],
        "campaign_shape": {
            "objective": "Purchase conversion",
            "budget_mode": "ABO for first signal; do not use scale budget until purchase CPA is known",
            "geo": "US",
            "audience": "Broad first, only constrain age/gender if product is legally or obviously audience-bound",
            "daily_budget": f"${daily_budget:.2f}",
            "total_48h_budget": f"${safe_float(unit_economics.get('first_test_budget'), 0):.2f}",
            "optimization_event": "Purchase; read CTR/ATC early but do not optimize for ATC unless purchase data is impossible",
        },
        "creative_matrix": [
            {
                "name": f"C{i + 1}-{slugify(script.get('angle', 'angle'))[:24]}",
                "angle": script.get("angle", ""),
                "asset_type": script.get("asset_type", "UGC short video"),
                "first_3s": script.get("hook_0_3s", ""),
                "proof": script.get("proof_3_12s", ""),
                "cta": script.get("cta_12_20s", ""),
            }
            for i, script in enumerate(scripts[:6])
            if isinstance(script, dict)
        ],
        "shopify_pdp_tasks": _top_unique([
            "首屏：产品实拍/演示 + 一句话结果 + 价格/Offer清楚",
            *((money.get("landing_page_must_haves") or [])[:6]),
            "FAQ：运输、退货、尺寸/兼容性、使用限制",
            "不要复制竞品品牌词、包装、Logo、达人素材",
        ], 10),
        "tracking_required": [
            "Meta Pixel + CAPI purchase/add_to_cart/view_content events checked",
            "UTM: product/test_id/creative_id written before launch",
            "Shopify order revenue and ad spend can be joined daily",
        ],
    }


def _pareto_supply_links(keyword: str) -> list[JsonDict]:
    query = _normalize_validation_keyword(keyword)
    encoded = urllib.parse.quote_plus(query)
    return [
        {"name": "Google Shopping", "url": f"https://www.google.com/search?tbm=shop&q={encoded}", "purpose": "看同款/近似款卖家、价格带和可替代供应"},
        {"name": "Amazon", "url": f"https://www.amazon.com/s?k={encoded}", "purpose": "看评论痛点、价格锚点、配件/Bundle"},
        {"name": "AliExpress", "url": f"https://www.aliexpress.com/wholesale?SearchText={encoded}", "purpose": "找可小单测试的同源货"},
        {"name": "Alibaba", "url": f"https://www.alibaba.com/trade/search?SearchText={encoded}", "purpose": "找工厂、MOQ、定制空间"},
        {"name": "1688", "url": f"https://s.1688.com/selloffer/offer_search.htm?keywords={encoded}", "purpose": "找源头价和可替代供应链"},
    ]


def _validation_link_subset(links: list[JsonDict], tokens: list[str], limit: int = 4) -> list[JsonDict]:
    out: list[JsonDict] = []
    token_text = [str(token or "").lower() for token in tokens if str(token or "").strip()]
    for link in links:
        if not isinstance(link, dict):
            continue
        text = " ".join(str(link.get(key, "")) for key in ("platform", "name", "purpose", "url")).lower()
        if token_text and not any(token in text for token in token_text):
            continue
        url = str(link.get("url") or "").strip()
        if not url:
            continue
        normalized = {
            "platform": link.get("platform") or link.get("name") or "Search",
            "url": url,
            "purpose": link.get("purpose") or "补验证据",
        }
        out.append(normalized)
        if len(out) >= limit:
            break
    if out:
        return out
    for link in links[:limit]:
        if isinstance(link, dict) and link.get("url"):
            out.append({
                "platform": link.get("platform") or link.get("name") or "Search",
                "url": str(link.get("url")),
                "purpose": link.get("purpose") or "补验证据",
            })
    return out


def _google_trends_links(keyword: str) -> list[JsonDict]:
    query = _normalize_validation_keyword(keyword)
    encoded = urllib.parse.quote_plus(query)
    return [
        {
            "platform": "Google Trends",
            "url": f"https://trends.google.com/trends/explore?geo=US&q={encoded}",
            "purpose": "确认真实时间序列、季节性和最近动量",
        }
    ]


def _validation_slot(
    *,
    key: str,
    label: str,
    status: str,
    evidence: str,
    next_action: str,
    search_query: str,
    search_links: list[JsonDict],
    why_not_verified: str = "",
    blocking: bool = False,
    blocks_publish: bool | None = None,
    attempted_sources: list[str] | None = None,
    expected_sources: list[str] | None = None,
    fallback_links: list[JsonDict] | None = None,
) -> JsonDict:
    allowed = {"verified", "pending", "failed", "lead_only"}
    normalized_status = status if status in allowed else "pending"
    verified = normalized_status == "verified"
    query = _normalize_validation_keyword(search_query) or _normalize_validation_keyword(label)
    links = _validation_link_subset(search_links or _platform_search_links(query), [], 4)
    fallback = _validation_link_subset(fallback_links or [], [], 4)
    if not links and not fallback:
        links = _validation_link_subset(_platform_search_links(query), [], 4)
    why = why_not_verified or ("" if verified else evidence) or "尚未拿到可验证证据"
    action = next_action or f"用「{query}」继续补 {label} 证据"
    return {
        "key": key,
        "label": label,
        "status": normalized_status,
        "verified": verified,
        "blocking": bool(blocking and not verified),
        "blocks_follow": bool(blocking and not verified),
        "blocks_publish": bool((blocking if blocks_publish is None else blocks_publish) and not verified),
        "evidence": evidence or "未找到可验证证据",
        "why_not_verified": "" if verified else why,
        "next_action": action,
        "search_query": query,
        "search_links": links,
        "fallback_links": fallback,
        "attempted_sources": _top_unique(attempted_sources or ["local cache", "8000 ads/trends/platform evidence"], 5),
        "expected_sources": _top_unique(expected_sources or [label], 5),
    }


def build_required_validation_matrix(
    product: JsonDict,
    money: JsonDict,
    platform_validation: JsonDict,
    unit_economics: JsonDict,
    creative_pack: JsonDict,
    operator_gate: JsonDict,
    truth_check: JsonDict,
    keyword: str,
    supply_chain: JsonDict | None = None,
) -> JsonDict:
    """Make every required validation explicit, including gaps and search paths."""

    title = clean_product_title(product.get("title", ""))
    domain = normalize_domain(product.get("domain", ""))
    keyword = (
        _normalize_validation_keyword(keyword)
        or _normalize_validation_keyword(platform_validation.get("primary_keyword"))
        or _normalize_validation_keyword((build_trend_keywords(product)[:1] or [title])[0])
        or domain
    )
    platform_links = _platform_search_links(keyword)
    supply_links = _pareto_supply_links(keyword)
    supply_chain = supply_chain if isinstance(supply_chain, dict) else {}
    signal = product.get("fb_signal") if isinstance(product.get("fb_signal"), dict) else {}
    trends = product.get("google_trends") if isinstance(product.get("google_trends"), dict) else (money.get("evidence", {}) or {}).get("google_trends", {})
    trends = trends if isinstance(trends, dict) else {}
    sources = platform_validation.get("sources") if isinstance(platform_validation.get("sources"), list) else []
    strict_sources = truth_check.get("strict_sources") if isinstance(truth_check.get("strict_sources"), list) else []
    strict_count = int(safe_float(truth_check.get("count"), 0))
    required_count = int(safe_float(truth_check.get("required_count"), 2)) or 2
    needs_more_strict = strict_count < required_count

    source_blob = " ".join(
        " ".join(str(source.get(key, "")) for key in ("platform", "source", "url", "signal", "title"))
        for source in sources + strict_sources
        if isinstance(source, dict)
    ).lower()
    fb_count = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    fb_status = str(signal.get("fb_verification_status") or "not_checked")
    trend_verified = bool(trends.get("verified") or trends.get("trend_verified"))
    trend_status = str(trends.get("status") or trends.get("trend_status") or "unverified")
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    image_count = int(safe_float(product.get("image_count"), operator_gate.get("image_count", 0)))
    variant_count = int(safe_float(product.get("variant_count"), operator_gate.get("variant_count", 0)))
    scripts = creative_pack.get("scripts") if isinstance(creative_pack.get("scripts"), list) else []
    angles = creative_pack.get("angles") if isinstance(creative_pack.get("angles"), list) else []
    risks = list(product.get("risk_flags") or [])
    hard_risk = _has_hard_risk(risks)
    price = safe_float(unit_economics.get("price"), safe_float(product.get("price"), 0))
    contribution = safe_float(unit_economics.get("contribution_before_ads"), 0)
    margin = safe_float(unit_economics.get("contribution_margin_pct"), 0)
    needs_real_cogs = bool(unit_economics.get("requires_real_cogs"))

    slots: list[JsonDict] = []

    meta_verified = fb_count > 0 and (fb_status == "matched" or bool(signal.get("fb_verified_by_8000")))
    meta_status = "verified" if meta_verified else "lead_only" if fb_count > 0 else "pending"
    meta_evidence = (
        f"8000 命中 {fb_count} 条广告素材/投放信号，状态 {fb_status}"
        if fb_count > 0
        else f"8000 状态 {fb_status}，当前未拿到广告素材计数"
    )
    slots.append(_validation_slot(
        key="meta_ads",
        label="Meta/Facebook Ads",
        status=meta_status,
        evidence=meta_evidence,
        why_not_verified="需要 Meta Ad Library 或 8000 品牌库命中真实广告素材" if not meta_verified else "",
        next_action=f"用品牌域名 {domain or keyword} 和关键词「{keyword}」复查 Meta Ad Library/8000，记录素材数、首3秒钩子和是否持续投放。",
        search_query=keyword,
        search_links=_validation_link_subset(platform_links, ["meta", "facebook", "ads transparency"], 4),
        blocking=needs_more_strict and not meta_verified,
        attempted_sources=["8000 brand lookup", "Meta Ad Library"],
        expected_sources=["active ads", "creative count", "landing page URL"],
    ))

    trend_strict = trend_verified and trend_status in {"hot", "watch", "emerging"}
    trend_status_out = "verified" if trend_strict else "lead_only" if trend_verified else "pending"
    slots.append(_validation_slot(
        key="google_trends",
        label="Google Trends",
        status=trend_status_out,
        evidence=_trend_reason_line(trends) or f"趋势状态 {trend_status}，query={trends.get('query') or trends.get('trend_query') or keyword}",
        why_not_verified="必须是真实时间序列；proxy/suggest/空缓存不能当需求验证" if not trend_strict else "",
        next_action=f"用「{keyword}」跑 Google Trends US 时间序列，确认当前值、12个月均值、动量和季节性。",
        search_query=keyword,
        search_links=[*_google_trends_links(keyword), *_validation_link_subset(platform_links, ["google ads"], 2)],
        blocking=needs_more_strict and not trend_strict,
        attempted_sources=["gtrends bulk cache", "money evidence google_trends"],
        expected_sources=["real time series", "US geo", "momentum"],
    ))

    social_tokens = ["reddit", "x/twitter", "youtube", "tiktok/instagram"]
    social_verified = any(
        token in str(source.get("source", "")).lower()
        for token in ["reddit", "x/twitter", "youtube", "tiktok", "instagram"]
        for source in strict_sources
        if isinstance(source, dict)
    )
    social_lead = any(token.split("/")[0] in source_blob for token in ["reddit", "youtube", "tiktok", "instagram", "x.com", "twitter"])
    slots.append(_validation_slot(
        key="social_ugc",
        label="Reddit/X/YouTube/TikTok UGC",
        status="verified" if social_verified else "lead_only" if social_lead or platform_score >= 22 else "pending",
        evidence=(
            "已命中真实讨论/评测/UGC 严格证据"
            if social_verified
            else f"平台分 {platform_score}，仍需逐条确认真实讨论、评测或演示"
        ),
        why_not_verified="缺少可追溯 Reddit/X/YouTube/TikTok/Instagram 讨论、评测或 UGC 链接" if not social_verified else "",
        next_action=f"用「{keyword}」查 Reddit/X/YouTube/TikTok，摘出购买理由、抱怨、替代品和可拍素材角度。",
        search_query=keyword,
        search_links=_validation_link_subset(platform_links, ["reddit", "youtube", "tiktok", "x"], 4),
        blocking=needs_more_strict and not social_verified,
        attempted_sources=["reddit validation cache", "platform validation cache", *social_tokens],
        expected_sources=["discussion URL", "review/demo URL", "comment pain point"],
    ))

    marketplace_verified = any(
        "amazon" in str(source.get("source", "")).lower()
        or "shopping" in str(source.get("source", "")).lower()
        or "marketplace" in str(source.get("source", "")).lower()
        for source in strict_sources
        if isinstance(source, dict)
    ) or any(token in source_blob for token in ["amazon", "google shopping", "rigorous validation"])
    slots.append(_validation_slot(
        key="marketplace",
        label="Amazon/Google Shopping 市场证据",
        status="verified" if marketplace_verified else "pending",
        evidence="已命中 Amazon/Google Shopping/严选缓存的同类价格、评论或卖家证据" if marketplace_verified else "当前没有同类价格带、评论痛点或卖家密度证据",
        why_not_verified="缺 Amazon/Google Shopping 评论、同款价格带或同类卖家验证" if not marketplace_verified else "",
        next_action=f"用「{keyword}」查 Amazon 和 Google Shopping，记录 3 个同类价格、评论痛点、评分和卖家密度。",
        search_query=keyword,
        search_links=_validation_link_subset(platform_links, ["amazon", "shopping"], 4),
        blocking=needs_more_strict and not marketplace_verified,
        attempted_sources=["rigorous validation cache", "Amazon", "Google Shopping"],
        expected_sources=["market price", "review pain points", "seller count"],
    ))

    supply_status = str(supply_chain.get("status") or "")
    has_supply_reference = supply_status == "marketplace_reference" or marketplace_verified
    supply_verified = has_supply_reference and not needs_real_cogs
    slots.append(_validation_slot(
        key="supply_chain",
        label="供应链/到手成本",
        status="verified" if supply_verified else "lead_only" if has_supply_reference else "pending",
        evidence=(
            "已有市场/供应链参考且真实 COGS 不再待核"
            if supply_verified
            else "有市场参考，但真实 COGS/MOQ/时效仍未验证" if has_supply_reference else "未拿到同源货、MOQ、时效和到手成本"
        ),
        why_not_verified="必须拿到至少 3 个供应商报价、MOQ、时效和可改包装空间" if not supply_verified else "",
        next_action=supply_chain.get("first_check") or f"用「{keyword}」找 3 个同源/近似供应商，记录到手成本、MOQ、时效、可定制空间。",
        search_query=keyword,
        search_links=supply_links,
        blocking=not has_supply_reference,
        blocks_publish=not supply_verified,
        attempted_sources=["Google Shopping", "Amazon", "AliExpress", "Alibaba", "1688"],
        expected_sources=["supplier quote", "MOQ", "shipping time", "landed cost"],
    ))

    economics_ready = bool(unit_economics.get("economics_ready"))
    economics_verified = economics_ready and not needs_real_cogs
    economics_status = "verified" if economics_verified else "lead_only" if economics_ready and price > 0 and contribution > 0 else "pending" if price > 0 else "failed"
    slots.append(_validation_slot(
        key="unit_economics",
        label="单品经济性",
        status=economics_status,
        evidence=f"售价 ${price:.2f}，预估贡献 ${contribution:.2f}，毛利 {margin:.1f}%，真实 COGS 待核={needs_real_cogs}",
        why_not_verified="纸面利润存在但真实 COGS/运费/退货率未接入" if economics_ready and needs_real_cogs else "售价或贡献毛利不足以证明可买量",
        next_action="接真实 COGS、头程/尾程、退货率和支付费，重算 break-even CPA 与目标 CPA。",
        search_query=keyword,
        search_links=supply_links[:4],
        blocking=not economics_ready,
        blocks_publish=not economics_verified,
        attempted_sources=["price snapshot", "estimated COGS model", "fulfillment reserve"],
        expected_sources=["real COGS", "shipping cost", "refund rate", "break-even CPA"],
    ))

    pdp_ready = image_count >= 2 and variant_count <= 12
    product_url = product.get("product_url") or _product_url(product)
    pdp_links = ([{"platform": "Competitor PDP", "url": str(product_url), "purpose": "核对图片、变体、FAQ、首屏证明"}] if product_url else []) + _validation_link_subset(platform_links, ["shopping", "amazon"], 2)
    slots.append(_validation_slot(
        key="pdp_assets",
        label="PDP素材/变体承接",
        status="verified" if pdp_ready else "pending",
        evidence=f"图片 {image_count} 张，变体 {variant_count} 个；要求至少2张图且变体不超过12个",
        why_not_verified="PDP 图片/演示素材不足或变体过多，会拖慢建页和投放学习" if not pdp_ready else "",
        next_action="补首屏实拍/演示图、场景图、FAQ 和主推 SKU；变体过多先收窄到最可能卖的 1-3 个。",
        search_query=keyword,
        search_links=pdp_links,
        blocking=not pdp_ready,
        attempted_sources=["product snapshot", "competitor PDP"],
        expected_sources=["hero image", "demo asset", "FAQ", "main SKU"],
    ))

    creative_ready = len(scripts) >= 6 and len(angles) >= 2
    slots.append(_validation_slot(
        key="creative_pack",
        label="原创素材包",
        status="verified" if creative_ready else "pending",
        evidence=f"素材角度 {len(angles)} 个，脚本 {len(scripts)} 条；要求至少2个角度和6条脚本",
        why_not_verified="没有足够原创素材脚本，不能把竞品素材当自己的投放素材" if not creative_ready else "",
        next_action="生成并人工复核 6 条原创脚本：痛点演示、差评反打、before/after、bundle/offer、FAQ objection、场景化UGC。",
        search_query=keyword,
        search_links=_validation_link_subset(platform_links, ["youtube", "tiktok", "meta"], 4),
        blocking=not creative_ready,
        attempted_sources=["platform AI creative axes", "money decision angles"],
        expected_sources=["6 scripts", "2+ angles", "original shooting plan"],
    ))

    risk_status = "failed" if hard_risk else "pending" if risks else "verified"
    slots.append(_validation_slot(
        key="risk_compliance",
        label="合规/IP/履约风险",
        status=risk_status,
        evidence="；".join(str(risk) for risk in risks[:3]) if risks else "未发现硬风险；仍需发布前人工复核宣称、素材版权和履约限制",
        why_not_verified="存在硬风险或待人工确认的合规/IP/履约风险" if risk_status != "verified" else "",
        next_action="人工检查品牌词、Logo、IP、医疗/安全宣称、材质/尺寸/配送/退货限制；硬风险未解除不建 DRAFT。",
        search_query=keyword,
        search_links=_validation_link_subset(platform_links, ["amazon", "reddit", "youtube"], 3),
        fallback_links=[
            {"platform": "Shopify AUP", "url": "https://www.shopify.com/legal/aup", "purpose": "核平台禁售/限制类目"},
            {"platform": "FTC Advertising", "url": "https://www.ftc.gov/business-guidance/advertising-marketing", "purpose": "核广告宣称边界"},
        ],
        blocking=risk_status != "verified",
        attempted_sources=["risk flags", "title/product type scan"],
        expected_sources=["claim boundaries", "IP clearance", "shipping/returns constraints"],
    ))

    verified_count = sum(1 for slot in slots if slot.get("status") == "verified")
    pending_count = sum(1 for slot in slots if slot.get("status") == "pending")
    failed_count = sum(1 for slot in slots if slot.get("status") == "failed")
    lead_count = sum(1 for slot in slots if slot.get("status") == "lead_only")
    blocking_slots = [slot for slot in slots if slot.get("blocking") and slot.get("status") != "verified"]
    pending_tasks = _top_unique([slot.get("next_action") for slot in slots if slot.get("status") != "verified"], 12)
    blocking_missing = _top_unique([
        f"{slot.get('label')}: {slot.get('why_not_verified') or slot.get('evidence')}"
        for slot in blocking_slots
    ], 10)
    if failed_count:
        summary_status = "failed"
    elif blocking_slots:
        summary_status = "blocked"
    elif pending_count or lead_count:
        summary_status = "pending"
    else:
        summary_status = "verified"
    next_action = (
        pending_tasks[0]
        if pending_tasks
        else f"已完成 {verified_count}/{len(slots)} 项验证；发布前做最后人工复核。"
    )
    return {
        "version": "required-validation-v1-no-empty",
        "keyword": keyword,
        "summary": {
            "status": summary_status,
            "verified_count": verified_count,
            "required_count": len(slots),
            "pending_count": pending_count,
            "lead_only_count": lead_count,
            "failed_count": failed_count,
            "blocking_count": len(blocking_slots),
            "strict_validation_count": strict_count,
            "strict_required_count": required_count,
            "next_action": next_action,
        },
        "slots": slots,
        "pending_tasks": pending_tasks,
        "blocking_missing": blocking_missing,
        "rule": "所有必验项必须显式返回 verified/pending/failed/lead_only；非 verified 必须带原因、搜索入口和下一步，不能空值或忽略。",
    }


def build_platform_truth_check(
    product: JsonDict,
    money: JsonDict,
    platform_validation: JsonDict,
) -> JsonDict:
    """Return strict cross-platform validation. New listing and micro-site alone do not count."""

    signal = product.get("fb_signal") if isinstance(product.get("fb_signal"), dict) else {}
    trends = product.get("google_trends") if isinstance(product.get("google_trends"), dict) else (money.get("evidence", {}) or {}).get("google_trends", {})
    trends = trends if isinstance(trends, dict) else {}
    sources = platform_validation.get("sources") if isinstance(platform_validation.get("sources"), list) else []
    fb_count = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    fb_status = str(signal.get("fb_verification_status") or "")
    trend_verified = bool(trends.get("verified") or trends.get("trend_verified"))
    trend_status = str(trends.get("status") or trends.get("trend_status") or "unverified")

    proof_map: dict[str, JsonDict] = {}
    soft_sources: list[JsonDict] = []

    if fb_count > 0 and (fb_status == "matched" or signal.get("fb_verified_by_8000")):
        proof_map["meta_ads"] = {
            "source": "Meta/Facebook Ads",
            "status": fb_status or "matched",
            "signal": f"8000品牌库命中，看到 {fb_count} 条广告素材/投放信号",
            "verification_level": "strict",
        }

    if trend_verified and trend_status in {"hot", "watch", "emerging"}:
        proof_map["google_trends"] = {
            "source": "Google Trends",
            "status": trend_status,
            "signal": _trend_reason_line(trends) or f"真实时间序列状态 {trend_status}",
            "verification_level": "strict",
        }

    for source in sources:
        if not isinstance(source, dict):
            continue
        platform = str(source.get("platform") or "").lower()
        signal_text = str(source.get("signal") or source.get("title") or "")[:120]
        url = str(source.get("url") or "")
        status = str(source.get("status") or "")
        if "vertical" in platform:
            soft_sources.append({
                "source": "小垂直站",
                "status": status or "source_lead",
                "signal": signal_text or "小站点只算来源线索，不算跨平台验真",
                "url": url,
                "verification_level": "lead_only",
            })
            continue
        if "reddit" in platform:
            proof_map.setdefault("reddit", {
                "source": "Reddit",
                "status": status,
                "signal": signal_text,
                "url": url,
                "verification_level": "strict",
            })
        elif platform in {"x", "twitter"} or "x.com" in url or "twitter.com" in url:
            proof_map.setdefault("x_social", {
                "source": "X/Twitter",
                "status": status,
                "signal": signal_text,
                "url": url,
                "verification_level": "strict",
            })
        elif "youtube" in platform:
            proof_map.setdefault("youtube", {
                "source": "YouTube",
                "status": status,
                "signal": signal_text,
                "url": url,
                "verification_level": "strict",
            })
        elif "tiktok" in platform or "instagram" in platform:
            proof_map.setdefault("short_video", {
                "source": "TikTok/Instagram",
                "status": status,
                "signal": signal_text,
                "url": url,
                "verification_level": "strict",
            })
        elif "amazon" in platform or "shopping" in platform or "rigorous" in platform:
            proof_map.setdefault("marketplace", {
                "source": "Amazon/Google Shopping",
                "status": status,
                "signal": signal_text or "同类价格/评论/市场验证缓存命中",
                "url": url,
                "verification_level": "strict",
            })
        elif "google trends" in platform and "google_trends" not in proof_map and status not in {"proxy", "suggest"}:
            proof_map["google_trends"] = {
                "source": "Google Trends cache",
                "status": status,
                "signal": signal_text,
                "url": url,
                "verification_level": "strict",
            }

    strict_sources = list(proof_map.values())
    missing = []
    if "meta_ads" not in proof_map:
        missing.append("缺 Meta/Facebook Ads 竞品广告验真")
    if "google_trends" not in proof_map:
        missing.append("缺 Google Trends 真实时间序列验真")
    if not any(key in proof_map for key in ("reddit", "x_social", "youtube", "short_video")):
        missing.append("缺 Reddit/X/YouTube/TikTok 真实讨论、评测或UGC验真")
    if "marketplace" not in proof_map:
        missing.append("缺 Amazon/Google Shopping 评论、价格带或同类卖家验真")

    return {
        "strict_sources": strict_sources,
        "soft_sources": soft_sources,
        "count": len(strict_sources),
        "passed": len(strict_sources) >= 2,
        "required_count": 2,
        "missing": missing,
        "rule": "只有至少2类严格跨平台证据，才允许进入跟品拆解；新上架、小垂直站、PDP图片只算线索，不算验真。",
    }


def build_competitor_monitoring_plan(
    product: JsonDict,
    truth_check: JsonDict,
    platform_validation: JsonDict,
    supply_chain: JsonDict,
) -> JsonDict:
    domain = normalize_domain(product.get("domain", ""))
    product_count = ((platform_validation.get("store_context") or {}) if isinstance(platform_validation.get("store_context"), dict) else {}).get("store_product_count", product.get("store_product_count", 0))
    strict_count = int(safe_float(truth_check.get("count"), 0))
    return {
        "principle": "监控竞品不是看它上了什么，而是看它是否持续投入流量、怎么承接转化、货从哪里来。",
        "competitor": domain,
        "product_count": product_count,
        "watch_items": [
            {"item": "新品节奏", "why": "看它是不是围绕同一人群/痛点连续扩SKU", "frequency": "daily"},
            {"item": "Meta/Facebook Ads", "why": "确认是否真的买量、素材是否持续迭代", "frequency": "daily"},
            {"item": "Google Ads Transparency / Shopping", "why": "判断是否有搜索/Shopping/PMax承接", "frequency": "2-3 days"},
            {"item": "TikTok/YouTube/Reddit", "why": "找UGC演示、真实评论和可拍内容角度", "frequency": "2-3 days"},
            {"item": "落地页首屏/Offer", "why": "看它用什么承诺、价格锚点和信任组件促单", "frequency": "daily"},
            {"item": "Bundle/加购/免邮线", "why": "判断AOV和利润空间，不只看单品售价", "frequency": "weekly"},
            {"item": "评论/FAQ/退货运输", "why": "找用户异议、履约风险和页面补强点", "frequency": "weekly"},
            {"item": "供应链同款/近似款", "why": "确认能不能跟、成本能不能跑广告", "frequency": "before_action"},
        ],
        "status": "teardown_allowed" if strict_count >= 2 else "verify_before_teardown",
        "next_monitor": (truth_check.get("missing") or [supply_chain.get("first_check", "先补平台验真")])[0],
    }


def build_traffic_source_strategy(truth_check: JsonDict, traffic_sources: list[JsonDict]) -> JsonDict:
    strict = [s for s in traffic_sources if isinstance(s, dict) and s.get("verification_level") == "strict"]
    source_names = " ".join(str(s.get("source", "")) for s in strict).lower()
    confirmed = [s.get("source", "") for s in strict if s.get("source")]
    inferred = []
    tactics = []

    if "meta" in source_names or "facebook" in source_names:
        tactics.append({
            "channel": "Meta/Facebook Ads",
            "confidence": "confirmed",
            "play": "拆竞品素材角度，不复制素材；重做3-6条原创UGC/演示短视频，先看CTR、ATC、CPA。",
        })
    else:
        inferred.append("Meta/Facebook Ads 未验真：先查 Ad Library/8000品牌库")

    if "google trends" in source_names or "amazon" in source_names or "shopping" in source_names:
        tactics.append({
            "channel": "Google Search/Shopping/PMax",
            "confidence": "confirmed" if "google trends" in source_names or "shopping" in source_names else "inferred",
            "play": "整理搜索意图词、干净标题/feed、对比图和FAQ；没有Shopping/Trends证据前不假装PMax可跑。",
        })
    else:
        inferred.append("Google流量未验真：补 Trends、Shopping、Ads Transparency")

    if "tiktok" in source_names or "youtube" in source_names or "reddit" in source_names:
        tactics.append({
            "channel": "TikTok/UGC/Content",
            "confidence": "confirmed",
            "play": "把真实讨论/评测转成首3秒演示脚本；优先拍问题-演示-结果，不照搬达人素材。",
        })
    else:
        inferred.append("内容流量未验真：补 TikTok/YouTube demo 和 Reddit评论")

    if not tactics:
        tactics.append({
            "channel": "Unknown",
            "confidence": "unknown",
            "play": "没有已确认流量来源，只能进入待验真，不能给跟品打法。",
        })

    return {
        "confidence": "confirmed" if len(strict) >= 2 else "needs_verification",
        "confirmed_sources": confirmed,
        "inferred_or_missing": inferred[:4],
        "tactics": tactics[:3],
        "rule": "confirmed 才能写打法；inferred 只能提示去验证，不能当已知流量来源。",
    }


def build_landing_page_clone_plan(product: JsonDict, money: JsonDict, verified_to_follow: bool) -> JsonDict:
    title = clean_product_title(product.get("title", ""))
    offer = money.get("offer_strategy", "") if isinstance(money, dict) else ""
    must_haves = money.get("landing_page_must_haves", []) if isinstance(money, dict) else []
    return {
        "mode": "structure_only",
        "status": "allowed_after_validation" if verified_to_follow else "blocked_until_validation",
        "principle": "照抄落地页只允许抄结构和销售逻辑，不允许复制图片、视频、文案、评价、品牌词、Logo或夸大声明。",
        "copy_structure": [
            {"section": "Hero", "task": f"用自己的素材重写 {title} 的一句话结果、主图/演示和CTA"},
            {"section": "Offer", "task": offer or "重做价格锚点、Bundle、免邮线和限时利益点"},
            {"section": "Proof", "task": "用自己的演示、测评、对比图或真实评论承接，不搬竞品评价"},
            {"section": "Objection", "task": "FAQ覆盖尺寸/兼容性/使用限制/运输/退货"},
            {"section": "Trust", "task": "退换、配送、支付安全、客服和合规词放在购买决策前"},
            {"section": "Upsell", "task": "同源配件/多件装/补充耗材用于提高AOV"},
        ],
        "competitor_sections_to_observe": _top_unique([
            "首屏卖点和视觉顺序",
            "价格锚点/Bundle/免邮线",
            "评价和信任组件位置",
            "FAQ处理的反对点",
            "运输、退货、保修承诺",
            *(must_haves[:4] if isinstance(must_haves, list) else []),
        ], 8),
        "never_copy": [
            "竞品图片/视频/GIF",
            "竞品文案原句、评价、品牌词、Logo",
            "达人素材、包装设计、授权IP",
            "未经证实的医疗/收益/安全承诺",
        ],
    }


def build_pareto_follow_plan(
    product: JsonDict,
    money: JsonDict,
    gate: JsonDict,
    unit_economics: JsonDict,
    platform_validation: JsonDict,
    platform_ai: JsonDict,
    creative_pack: JsonDict,
) -> JsonDict:
    """Compress the full evidence stack into an 80/20 follow-product decision."""

    title = clean_product_title(product.get("title", ""))
    domain = normalize_domain(product.get("domain", ""))
    keyword = (
        str(platform_validation.get("primary_keyword") or "").strip()
        or (build_trend_keywords(product)[:1] or [title])[0]
    )
    keyword = keyword or title or domain
    signal = product.get("fb_signal") if isinstance(product.get("fb_signal"), dict) else {}
    trends = product.get("google_trends") if isinstance(product.get("google_trends"), dict) else (money.get("evidence", {}) or {}).get("google_trends", {})
    trends = trends if isinstance(trends, dict) else {}
    store_context = platform_validation.get("store_context") if isinstance(platform_validation.get("store_context"), dict) else {}
    sources = platform_validation.get("sources") if isinstance(platform_validation.get("sources"), list) else []
    fb_count = int(safe_float(signal.get("ad_creative_count") or signal.get("appearance_count"), 0))
    trend_verified = bool(trends.get("verified") or trends.get("trend_verified"))
    trend_status = str(trends.get("status") or trends.get("trend_status") or "unverified")
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    is_micro_vertical = bool(store_context.get("is_micro_vertical"))
    image_count = int(safe_float(product.get("image_count"), 0))
    variant_count = int(safe_float(product.get("variant_count"), 0))
    price = safe_float(product.get("price"), 0)
    truth_check = build_platform_truth_check(product, money, platform_validation)
    strict_sources = truth_check.get("strict_sources", [])
    soft_sources = truth_check.get("soft_sources", [])
    real_validation_count = int(safe_float(truth_check.get("count"), 0))
    verified_to_follow = bool(truth_check.get("passed"))

    traffic_sources: list[JsonDict] = []
    for source in strict_sources:
        if isinstance(source, dict):
            traffic_sources.append(source)
    if fb_count and not any(s.get("source") == "Meta/Facebook Ads" for s in traffic_sources if isinstance(s, dict)):
        traffic_sources.append({
            "source": "Meta/Facebook Ads",
            "status": "lead_only",
            "signal": f"{fb_count} 条广告素材线索，但未达到严格验真规则",
            "verification_level": "lead_only",
        })
    if trend_verified and not any(str(s.get("source", "")).startswith("Google Trends") for s in traffic_sources if isinstance(s, dict)):
        traffic_sources.append({
            "source": "Google Trends",
            "status": trend_status,
            "signal": _trend_reason_line(trends) or f"趋势状态 {trend_status}",
            "verification_level": "strict" if trend_status in {"hot", "watch", "emerging"} else "lead_only",
        })
    for source in sources[:4]:
        if not isinstance(source, dict):
            continue
        platform_name = str(source.get("platform") or "External proof")
        if any(platform_name.lower() in str(existing.get("source", "")).lower() for existing in traffic_sources if isinstance(existing, dict)):
            continue
        traffic_sources.append({
            "source": platform_name,
            "status": str(source.get("status") or ""),
            "signal": str(source.get("signal") or source.get("title") or "")[:120],
            "url": str(source.get("url") or ""),
            "verification_level": "lead_only",
        })
    if is_micro_vertical:
        traffic_sources.append({
            "source": "小垂直站",
            "status": "focused_store",
            "signal": f"{store_context.get('store_product_count', 0)} 个产品，只算来源线索，不能替代跨平台验真",
            "verification_level": "lead_only",
        })
    for source in soft_sources:
        if isinstance(source, dict):
            traffic_sources.append(source)
    unique_traffic_sources: list[JsonDict] = []
    seen_traffic: set[str] = set()
    for source in traffic_sources:
        key = "|".join(str(source.get(part, "")) for part in ("source", "status", "signal", "url")).lower()
        if not key.strip("|") or key in seen_traffic:
            continue
        seen_traffic.add(key)
        unique_traffic_sources.append(source)
        if len(unique_traffic_sources) >= 6:
            break
    traffic_sources = unique_traffic_sources
    traffic_validated = verified_to_follow

    competitor_advantages = _top_unique([
        f"{domain} 已有严格广告验证，先拆首3秒钩子和CTA" if any(s.get("source") == "Meta/Facebook Ads" for s in strict_sources if isinstance(s, dict)) else "",
        "小垂直站产品少，可以作为来源拆解，但必须再去平台验真" if is_micro_vertical else "",
        "PDP素材够初拆，先学习首屏图、场景图和价格锚点" if image_count >= 2 else "",
        "变体数量克制，适合学习主推SKU选择" if 1 <= variant_count <= 8 else "",
        *((money.get("why_will_sell") or [])[:2]),
        *((product.get("conversion_angles") or [])[:2]),
    ], 6)
    if not competitor_advantages:
        competitor_advantages = ["先拆竞品首屏：它卖给谁、解决什么痛点、用什么证明让用户下单"]

    learn_points = _top_unique([
        "首屏一句话卖点",
        "价格锚点/Bundle",
        "素材首3秒演示",
        "FAQ/运输/退货信任",
        "评论里反复出现的痛点",
    ], 5)
    supply_links = _pareto_supply_links(keyword)
    marketplace_verified = any(
        str(source.get("platform", "")).lower() in {"amazon", "google shopping", "rigorous validation"}
        or "amazon" in str(source.get("url", "")).lower()
        for source in sources
        if isinstance(source, dict)
    )
    supply_status = "marketplace_reference" if marketplace_verified else "to_verify"
    supply_chain = {
        "status": supply_status,
        "keyword": keyword,
        "principle": "系统只给供应链线索，不假装已确认成本；必须人工比价、看MOQ、时效和可定制空间。",
        "routes": supply_links,
        "first_check": f"用「{keyword}」找 3 个同源/近似供应商，记录到手成本、发货时效、MOQ、是否可改包装。",
    }
    traffic_strategy = build_traffic_source_strategy(truth_check, traffic_sources)
    landing_page_clone = build_landing_page_clone_plan(product, money, verified_to_follow)
    monitor_plan = build_competitor_monitoring_plan(product, truth_check, platform_validation, supply_chain)
    required_validation = build_required_validation_matrix(
        product,
        money,
        platform_validation,
        unit_economics,
        creative_pack,
        gate,
        truth_check,
        keyword,
        supply_chain,
    )

    gaps = []
    blockers = list(gate.get("blockers") or [])
    if not verified_to_follow:
        gaps.append(f"未通过验真：严格证据 {real_validation_count}/2，新上架或小垂直站不能当可跟依据")
        gaps.extend(truth_check.get("missing", [])[:3])
    if supply_status == "to_verify":
        gaps.append("供应链未确认：只能给搜索路径，不能当已拿到成本")
    if not unit_economics.get("economics_ready"):
        gaps.append("价格/毛利可能不适合付费跟品，先确认真实COGS和履约")
    if not competitor_advantages:
        gaps.append("竞品优点不清楚，先拆页面和广告素材")
    gaps = _top_unique([*blockers, *gaps, *required_validation.get("blocking_missing", [])], 8)

    if blockers:
        lane = "skip"
        decision = "不跟"
    elif verified_to_follow and unit_economics.get("economics_ready") and competitor_advantages:
        lane = "copy_now"
        decision = "验真通过，才可跟品拆解"
    elif not verified_to_follow:
        lane = "verify_first"
        decision = "待验真，不可跟"
    elif supply_status == "to_verify":
        lane = "find_supplier"
        decision = "已验真，先核供应链"
    else:
        lane = "skip"
        decision = "少看"

    launch_triage = build_launch_triage(
        lane=lane,
        gate=gate,
        truth_check=truth_check,
        supply_status=supply_status,
        unit_economics=unit_economics,
        platform_ai=platform_ai,
        gaps=gaps,
    )

    if launch_triage.get("level") == "launch_now":
        one_action = f"立即上架只到 DRAFT：用「{keyword}」建草稿页，人工复核COGS/库存/合规后再决定是否发布。"
    elif launch_triage.get("level") == "avoid":
        one_action = (launch_triage.get("blockers") or launch_triage.get("missing") or gaps or ["不碰这个品，把时间留给证据更完整的品。"])[0]
    elif lane == "verify_first":
        missing_first = (truth_check.get("missing") or ["先做跨平台验真"])[0]
        one_action = f"观察跟进：{missing_first}。用「{keyword}」去 Reddit/X/YouTube/TikTok/Amazon/Google Trends 补到2类证据。"
    elif lane == "find_supplier":
        one_action = f"观察跟进：先用「{keyword}」去 Google Shopping/Amazon/AliExpress/Alibaba/1688 找同源货和价格带。"
    elif lane == "copy_now":
        one_action = f"观察跟进：先拆 {domain or '竞品站'} 的首屏/价格/素材角度，并补齐上架门槛缺口。"
    else:
        one_action = (launch_triage.get("missing") or gaps or ["继续观察广告持续性、趋势、讨论和供应链。"])[0]

    priority = 0
    priority += 45 if verified_to_follow else real_validation_count * 14
    priority += min(16, fb_count * 2) if fb_count and verified_to_follow else 0
    priority += 15 if is_micro_vertical else 0
    priority += 15 if unit_economics.get("economics_ready") else 0
    priority += 10 if supply_status == "marketplace_reference" else 4
    priority -= 35 if blockers else 0

    return {
        "principle": "28定律：不要看100个字段，只看20%赚钱信号：竞品在卖什么、流量从哪里来、供应链能不能跟、今天下一步做什么。",
        "lane": lane,
        "decision": decision,
        "priority": max(0, min(100, int(priority))),
        "copy_target": {
            "domain": domain,
            "product_url": product.get("product_url", ""),
            "product_count": store_context.get("store_product_count", product.get("store_product_count", 0)),
            "is_micro_vertical": is_micro_vertical,
        },
        "traffic_validated": traffic_validated,
        "truth_check": truth_check,
        "real_validation_count": real_validation_count,
        "required_validation_count": truth_check.get("required_count", 2),
        "traffic_sources": traffic_sources,
        "traffic_strategy": traffic_strategy,
        "launch_triage": launch_triage,
        "required_validation": required_validation,
        "competitor_advantages": competitor_advantages,
        "learn_points": learn_points,
        "monitor_plan": monitor_plan,
        "landing_page_clone": landing_page_clone,
        "supply_chain": supply_chain,
        "money_check": {
            "price": round(price, 2),
            "break_even_cpa": unit_economics.get("break_even_cpa", 0),
            "target_cpa": unit_economics.get("target_cpa", 0),
            "requires_real_cogs": unit_economics.get("requires_real_cogs", True),
        },
        "evidence_gaps": gaps,
        "do_not_copy": _top_unique([
            "不要复制竞品品牌词、Logo、包装和达人素材",
            "不要在供应链/成本未确认前开广告",
            *((money.get("what_not_to_copy") or [])[:2] if isinstance(money.get("what_not_to_copy"), list) else []),
        ], 5),
        "one_action": one_action,
    }


def evaluate_profit_test_metrics(metrics: JsonDict) -> JsonDict:
    """Evaluate imported ad metrics and return stop/scale/fix decisions."""

    price = _money_number(metrics.get("price") or metrics.get("aov") or metrics.get("average_order_value"), 0)
    unit = build_operator_unit_economics({"price": price}, {})
    spend = _money_number(metrics.get("spend"), 0)
    impressions = int(safe_float(metrics.get("impressions"), 0))
    clicks = int(safe_float(metrics.get("clicks") or metrics.get("link_clicks"), 0))
    atc = int(safe_float(metrics.get("add_to_cart") or metrics.get("atc"), 0))
    checkouts = int(safe_float(metrics.get("checkout") or metrics.get("initiate_checkout"), 0))
    purchases = int(safe_float(metrics.get("purchases") or metrics.get("orders"), 0))
    revenue = _money_number(metrics.get("revenue") or metrics.get("sales"), 0)
    ctr = round((clicks / impressions) * 100, 2) if impressions else 0
    atc_rate = round((atc / clicks) * 100, 2) if clicks else 0
    checkout_rate = round((checkouts / clicks) * 100, 2) if clicks else 0
    cpa = round(spend / purchases, 2) if purchases else None
    roas = round(revenue / spend, 2) if spend else 0
    target_cpa = safe_float(unit.get("target_cpa"), 0)
    break_even = safe_float(unit.get("break_even_cpa"), 0)

    actions: list[str] = []
    if purchases >= 2 and cpa is not None and cpa <= target_cpa:
        decision = "scale_20_30_percent"
        actions.append("2单以上且低于目标CPA，允许人工确认后每24小时加20%-30%预算")
    elif purchases >= 1 and cpa is not None and cpa <= break_even:
        decision = "hold_and_refresh_creatives"
        actions.append("有单且未亏损，不急放量；补2条新素材观察稳定性")
    elif spend >= safe_float(unit.get("kill_spend_without_purchase"), 0) and purchases == 0:
        decision = "kill_product_or_adset"
        actions.append("已到无购买停损线，停该广告组/该品测试")
    elif spend >= safe_float(unit.get("kill_spend_without_atc"), 0) and atc == 0:
        decision = "kill_creative_angle"
        actions.append("已到无ATC停损线，停该素材角度")
    elif impressions >= 1500 and ctr < 0.7:
        decision = "refresh_hook"
        actions.append("CTR低，优先换首3秒钩子和画面，不改预算")
    elif clicks >= 40 and atc_rate < 2:
        decision = "fix_pdp_offer"
        actions.append("点击后不加购，优先修PDP首屏、价格、Offer和信任组件")
    else:
        decision = "keep_collecting"
        actions.append("样本未够，继续收集到下一条停损或放量线")

    return {
        "ok": True,
        "decision": decision,
        "actions": actions,
        "metrics": {
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "ctr_pct": ctr,
            "add_to_cart": atc,
            "atc_rate_pct": atc_rate,
            "checkout": checkouts,
            "checkout_rate_pct": checkout_rate,
            "purchases": purchases,
            "revenue": revenue,
            "cpa": cpa,
            "roas": roas,
        },
        "unit_economics": unit,
        "rules": build_operator_metric_rules(unit),
    }


def build_profit_pipeline_item(product: JsonDict, rank: int) -> JsonDict:
    judgement = product.get("seven_day_judgement") if isinstance(product.get("seven_day_judgement"), dict) else {}
    money = product.get("money_decision") if isinstance(product.get("money_decision"), dict) else {}
    platform_ai = product.get("platform_ai") if isinstance(product.get("platform_ai"), dict) else judgement.get("platform_ai", {})
    if not isinstance(platform_ai, dict):
        platform_ai = {}
    fb_signal = product.get("fb_signal") if isinstance(product.get("fb_signal"), dict) else {}
    trends = product.get("google_trends") if isinstance(product.get("google_trends"), dict) else (money.get("evidence", {}) or {}).get("google_trends", {})
    platform_validation = product.get("platform_validation") if isinstance(product.get("platform_validation"), dict) else {}
    budget = platform_ai.get("auto_rules") or money.get("first_48h_test_plan", {})
    creative_pack = build_profit_creative_pack(product, platform_ai, money)
    unit_economics = build_operator_unit_economics(product, money)
    operator_gate = build_operator_gate(product, platform_ai, money, unit_economics, creative_pack)
    metric_rules = build_operator_metric_rules(unit_economics)
    test_order = build_operator_test_order(product, operator_gate, unit_economics, creative_pack, platform_ai, money)
    pareto = build_pareto_follow_plan(
        product,
        money,
        operator_gate,
        unit_economics,
        platform_validation,
        platform_ai,
        creative_pack,
    )
    required_validation = pareto.get("required_validation") if isinstance(pareto.get("required_validation"), dict) else {}
    validation = {
        "fb_ads": {
            "status": fb_signal.get("fb_verification_status", "not_checked"),
            "creative_count": fb_signal.get("ad_creative_count", 0),
            "grade": fb_signal.get("grade", ""),
            "ai_score": fb_signal.get("ai_score", 0),
            "orbit_score": fb_signal.get("orbit_score", 0),
        },
        "google_trends": {
            "status": trends.get("status") or trends.get("trend_status") or "unverified",
            "verified": bool(trends.get("verified") or trends.get("trend_verified")),
            "score": trends.get("score") or trends.get("trend_score") or 0,
            "query": trends.get("query") or trends.get("trend_query") or "",
            "data_quality": trends.get("data_quality") or trends.get("trend_data_quality") or "未验证",
        },
        "platform_proof": {
            "status": platform_validation.get("status", "needs_platform_proof"),
            "score": platform_validation.get("score", 0),
            "sources": platform_validation.get("sources", []),
        },
        "platform_ai": {
            "automation_score": platform_ai.get("automation_score", 0),
            "readiness": platform_ai.get("readiness", ""),
            "best_channels": platform_ai.get("best_channels", []),
            "asset_gaps": platform_ai.get("asset_gaps", []),
        },
        "required_matrix": required_validation,
        "pending_tasks": required_validation.get("pending_tasks", []),
        "blocking_missing": required_validation.get("blocking_missing", []),
    }
    stage = profit_pipeline_stage(judgement, platform_ai, product, operator_gate)
    return {
        "rank": rank,
        "stage": stage,
        "title": product.get("title", ""),
        "domain": product.get("domain", ""),
        "handle": product.get("handle", ""),
        "product_url": product.get("product_url", ""),
        "price": product.get("price", 0),
        "product_type": product.get("product_type", ""),
        "score": product.get("score", 0),
        "follow_priority_score": judgement.get("follow_priority_score", 0),
        "decision": judgement.get("decision") or product.get("decision", ""),
        "timing": judgement.get("timing") or money.get("timing", ""),
        "selection": {
            "why_follow": judgement.get("why_follow", []) or money.get("why_now", []),
            "watchouts": judgement.get("watchouts", []) or money.get("why_may_fail", []),
            "days_since_new": product.get("days_since_new"),
            "new_product_date": product.get("new_product_date", ""),
        },
        "validation": validation,
        "operator_gate": operator_gate,
        "proof_sources": operator_gate.get("proof_sources", []),
        "missing_evidence": operator_gate.get("missing_evidence", []),
        "creative_gaps": operator_gate.get("creative_gaps", []),
        "pdp_gaps": operator_gate.get("pdp_gaps", []),
        "economics_gaps": operator_gate.get("economics_gaps", []),
        "unit_economics": unit_economics,
        "platform_ai": platform_ai,
        "creative_pack": creative_pack,
        "launch_test_order": test_order,
        "required_validation": required_validation,
        "validation_tasks": required_validation.get("pending_tasks", []),
        "validation_blocking_missing": required_validation.get("blocking_missing", []),
        "budget_rules": {
            "first_budget": f"${safe_float(unit_economics.get('first_test_budget'), 0):.2f}",
            "target_cpa": f"${safe_float(unit_economics.get('target_cpa'), 0):.2f}",
            "break_even_cpa": f"${safe_float(unit_economics.get('break_even_cpa'), 0):.2f}",
            "creative_count": budget.get("creative_count", creative_pack.get("minimum_creatives", 6)),
            "campaign_mode": platform_ai.get("launch_mode", ""),
            "best_channels": platform_ai.get("best_channels", []),
            "real_cogs_required": unit_economics.get("requires_real_cogs", True),
            "daily_budget": (test_order.get("campaign_shape") or {}).get("daily_budget", ""),
        },
        "stop_scale_rules": {
            "kill": " / ".join(metric_rules.get("kill", [])[:2]),
            "scale": " / ".join(metric_rules.get("scale", [])[:2]),
            "kill_rules": metric_rules.get("kill", []),
            "scale_rules": metric_rules.get("scale", []),
            "review_window": metric_rules.get("review_window", "48-72h"),
            "metric_rules": metric_rules,
            "guardrail": metric_rules.get("guardrail", "不自动花费、不自动发布"),
        },
        "pareto": pareto,
        "next_action": pareto.get("one_action") or profit_next_action(stage, platform_ai, judgement, money, operator_gate),
        "artifact_ready": {
            "shopify_draft": stage in {"ready_to_test", "build_asset_pack"},
            "creative_brief": True,
            "ad_rules": bool(budget),
            "human_review_required": True,
        },
    }


def build_profit_creative_pack(product: JsonDict, platform_ai: JsonDict, money: JsonDict) -> JsonDict:
    title = clean_product_title(product.get("title", ""))
    input_pack = platform_ai.get("creative_input_pack") if isinstance(platform_ai.get("creative_input_pack"), dict) else {}
    angles = _top_unique([
        *(input_pack.get("angles") or []),
        *((money.get("first_48h_test_plan") or {}).get("angles") or []),
        *(product.get("conversion_angles") or []),
    ], 6)
    if not angles:
        angles = ["Problem-first demo", "Before-after contrast", "Offer stack"]
    scripts = []
    for angle in angles[:6]:
        scripts.append({
            "angle": angle,
            "asset_type": "UGC short video" if "UGC" in angle or "demo" in angle.lower() else "Short ad cut",
            "hook_0_3s": f"Show the clearest problem or result for {title} in the first 3 seconds.",
            "proof_3_12s": "Show product use, before/after state, social proof, or the included bundle.",
            "cta_12_20s": "Show offer, guarantee/returns reassurance, and one direct CTA.",
        })
    return {
        "minimum_creatives": 6,
        "minimum_assets": input_pack.get("minimum_assets", "6 creatives: 3 UGC demos + 2 problem/benefit cuts + 1 offer/bundle cut"),
        "angles": angles,
        "scripts": scripts,
        "negative_controls": input_pack.get("negative_controls", []),
        "landing_page_must_haves": (money.get("landing_page_must_haves") or [])[:8],
        "offer_strategy": money.get("offer_strategy", ""),
    }


def profit_pipeline_stage(
    judgement: JsonDict,
    platform_ai: JsonDict,
    product: JsonDict,
    operator_gate: JsonDict | None = None,
) -> str:
    if isinstance(operator_gate, dict):
        status = str(operator_gate.get("status") or "")
        if status == "operator_test_ready":
            return "ready_to_test"
        if status == "asset_pack_required":
            return "build_asset_pack"
        if status == "validation_required":
            return "validation_queue"
        if status == "blocked":
            return "hold"

    decision = str(judgement.get("decision") or product.get("decision") or "")
    readiness = str(platform_ai.get("readiness") or "")
    risk_text = " ".join(str(r) for r in product.get("risk_flags", []))
    if readiness == "blocked" or _has_hard_risk(product.get("risk_flags", [])) or "暂不" in decision:
        return "hold"
    if readiness == "ai_ready" and decision in {"立即跟进", "轻量跟进"}:
        return "ready_to_test"
    if decision in {"立即跟进", "轻量跟进"} or readiness == "asset_gap":
        return "build_asset_pack"
    if "观察" in decision or risk_text:
        return "validation_queue"
    return "validation_queue"


def profit_next_action(
    stage: str,
    platform_ai: JsonDict,
    judgement: JsonDict,
    money: JsonDict,
    operator_gate: JsonDict | None = None,
) -> str:
    gate_missing = []
    if isinstance(operator_gate, dict):
        gate_missing = list(operator_gate.get("blockers") or operator_gate.get("missing") or [])
    if stage == "ready_to_test":
        return "门禁已过：人工确认真实COGS/库存/合规后，按测试单建PDP、上6条原创素材、小预算跑48-72h。"
    if stage == "build_asset_pack":
        gaps = gate_missing or platform_ai.get("asset_gaps", [])
        return "先补齐实战门禁：" + "；".join(str(g) for g in gaps[:3])
    if stage == "validation_queue":
        tasks = gate_missing or judgement.get("watchouts", []) or money.get("why_may_fail", [])
        return "先补验证：" + "；".join(str(t) for t in tasks[:3])
    return "暂不进入投放自动化，只保留监控信号。"


def build_profit_pipeline_board(actions: list[JsonDict]) -> JsonDict:
    board = {
        "ready_to_test": [],
        "build_asset_pack": [],
        "validation_queue": [],
        "hold": [],
    }
    for action in actions:
        stage = action.get("stage", "validation_queue")
        board.setdefault(stage, []).append(action)
    return board


def build_profit_pipeline_report(pipeline: JsonDict) -> str:
    stats = pipeline.get("stats", {})
    lines = [
        "# 28 Follow Product Workbench",
        "",
        f"- Generated: {pipeline.get('generated_at', '')}",
        f"- Version: {pipeline.get('version', '')}",
        f"- Source products: {stats.get('source_products', 0)}",
        f"- Launch now: {stats.get('launch_now_products', 0)}",
        f"- Observe: {stats.get('observe_products', 0)}",
        f"- Avoid: {stats.get('avoid_products', 0)}",
        f"- Copy now: {stats.get('copy_now_products', 0)}",
        f"- Verify first: {stats.get('verify_first_products', 0)}",
        f"- Find supplier: {stats.get('find_supplier_products', 0)}",
        f"- Strict platform validated: {stats.get('traffic_validated_products', 0)}",
        f"- Skip: {stats.get('skip_products', 0)}",
        "",
        "## 28 Principle",
        "",
        "新品线索 -> 跨平台验真 -> 竞品在卖什么 -> 供应链从哪里找 -> 今天只做一个动作",
        "",
    ]
    for item in pipeline.get("actions", [])[:30]:
        pareto = item.get("pareto", {})
        triage = pareto.get("launch_triage", {}) if isinstance(pareto.get("launch_triage"), dict) else {}
        supply = pareto.get("supply_chain", {}) if isinstance(pareto.get("supply_chain"), dict) else {}
        unit = item.get("unit_economics", {})
        lines.extend([
            f"## {item.get('rank')}. {item.get('title', '')}",
            "",
            f"- Launch triage: {triage.get('label', '')} / level: {triage.get('level', '')} / reason: {triage.get('one_line', '')}",
            f"- 28 decision: {pareto.get('decision', '')} / lane: {pareto.get('lane', '')} / strict validation: {pareto.get('real_validation_count', 0)}/{pareto.get('required_validation_count', 2)} / priority: {pareto.get('priority', 0)}",
            f"- Product: {item.get('product_url', '')}",
            f"- Money check: price ${unit.get('price', 0)} / break-even CPA ${unit.get('break_even_cpa', 0)} / target CPA ${unit.get('target_cpa', 0)}",
            f"- Supply keyword: {supply.get('keyword', '')} / status: {supply.get('status', '')}",
            f"- Next: {item.get('next_action', '')}",
            "",
            "### Competitor Advantages",
            "",
        ])
        for advantage in pareto.get("competitor_advantages", [])[:5]:
            lines.append(f"- {advantage}")
        lines.extend([
            "",
            "### Traffic Sources",
            "",
        ])
        for source in pareto.get("traffic_sources", [])[:5]:
            if isinstance(source, dict):
                lines.append(f"- {source.get('source', '')}: {source.get('signal', source.get('status', ''))}")
        strategy = pareto.get("traffic_strategy", {}) if isinstance(pareto.get("traffic_strategy"), dict) else {}
        if strategy.get("tactics"):
            lines.extend(["", "### Traffic Play", ""])
            for tactic in strategy.get("tactics", [])[:3]:
                if isinstance(tactic, dict):
                    lines.append(f"- {tactic.get('channel', '')}: {tactic.get('play', '')}")
        clone = pareto.get("landing_page_clone", {}) if isinstance(pareto.get("landing_page_clone"), dict) else {}
        if clone.get("copy_structure"):
            lines.extend(["", "### Landing Page Structure To Replicate", ""])
            for section in clone.get("copy_structure", [])[:5]:
                if isinstance(section, dict):
                    lines.append(f"- {section.get('section', '')}: {section.get('task', '')}")
        if pareto.get("evidence_gaps"):
            lines.extend(["", "### Gaps", ""])
            for gap in pareto.get("evidence_gaps", [])[:5]:
                lines.append(f"- {gap}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_profit_workbench(actions: list[JsonDict], stats: JsonDict | None = None) -> JsonDict:
    stats = stats or {}
    lane_defs = [
        ("copy_now", "验真通过再跟", "至少2类真实平台证据后，才拆竞品页面、素材和供应链"),
        ("verify_first", "待验真", "只看到新品/小垂直站/单一线索时，先去各平台验真，不允许跟"),
        ("find_supplier", "已验真，核供应链", "已有平台证据后，再找同源/近似货、成本、MOQ、时效"),
        ("skip", "少看/不跟", "风险、毛利或证据不成立，把时间让给更像赚钱的20%"),
    ]
    lanes = []
    for key, label, operator_action in lane_defs:
        lane_items = [item for item in actions if (item.get("pareto") or {}).get("lane") == key]
        lanes.append({
            "key": key,
            "label": label,
            "count": len(lane_items),
            "operator_action": operator_action,
            "items": [operator_lane_item(item) for item in lane_items[:20]],
        })
    return {
        "summary": {
            "products": len(actions),
            "launch_now": stats.get("launch_now_products", 0),
            "observe": stats.get("observe_products", 0),
            "avoid": stats.get("avoid_products", 0),
            "copy_now": stats.get("copy_now_products", 0),
            "verify_first": stats.get("verify_first_products", 0),
            "find_supplier": stats.get("find_supplier_products", 0),
            "skip": stats.get("skip_products", 0),
            "traffic_validated": stats.get("traffic_validated_products", 0),
            "unverified": stats.get("unverified_products", 0),
            "one_source": stats.get("one_source_products", 0),
            "supply_to_verify": stats.get("supply_to_verify_products", 0),
        },
        "lanes": lanes,
        "single_screen_questions": [
            "哪些品可以立即上架DRAFT？",
            "哪些品只能观察跟进？",
            "哪些品不碰？",
            "哪些品已经2类平台验真？",
            "每个品缺哪类平台证据？",
            "供应链从哪里找？",
            "今天只做哪一个动作？",
        ],
        "principle": [
            "新上架不是证据，小垂直站也不是证据；它们只是线索。",
            "立即上架必须同时满足2类真实平台证据、经济性、市场/供应链参考和无硬风险。",
            "缺任何关键证据只能观察跟进，证据或风险不成立就不碰。",
            "80%的噪音都删掉，只保留验真、竞品、供应链、动作四件事。",
            "系统给线索，不编造销量、成本、利润；供应链和广告数据必须人工核实。",
        ],
    }


def operator_lane_item(item: JsonDict) -> JsonDict:
    gate = item.get("operator_gate") or {}
    unit = item.get("unit_economics") or {}
    budget = item.get("budget_rules") or {}
    pareto = item.get("pareto") or {}
    supply = pareto.get("supply_chain") if isinstance(pareto.get("supply_chain"), dict) else {}
    return {
        "rank": item.get("rank"),
        "title": item.get("title", ""),
        "domain": item.get("domain", ""),
        "product_url": item.get("product_url", ""),
        "price": item.get("price", 0),
        "decision": pareto.get("decision", ""),
        "launch_triage": pareto.get("launch_triage", {}),
        "priority": pareto.get("priority", 0),
        "real_validation_count": pareto.get("real_validation_count", 0),
        "required_validation_count": pareto.get("required_validation_count", 2),
        "truth_check": pareto.get("truth_check", {}),
        "traffic_sources": pareto.get("traffic_sources", []),
        "traffic_strategy": pareto.get("traffic_strategy", {}),
        "competitor_advantages": pareto.get("competitor_advantages", []),
        "monitor_plan": pareto.get("monitor_plan", {}),
        "landing_page_clone": pareto.get("landing_page_clone", {}),
        "supply_status": supply.get("status", ""),
        "supply_keyword": supply.get("keyword", ""),
        "gate_status": gate.get("status", ""),
        "proof_sources": gate.get("proof_sources", []),
        "proof_source_count": gate.get("proof_source_count", 0),
        "missing": (pareto.get("evidence_gaps") or gate.get("blockers") or gate.get("missing") or [])[:3],
        "break_even_cpa": unit.get("break_even_cpa", 0),
        "target_cpa": unit.get("target_cpa", 0),
        "first_budget": budget.get("first_budget", ""),
        "next_action": pareto.get("one_action") or item.get("next_action", ""),
    }


def build_operator_blueprint() -> JsonDict:
    return {
        "version": PROFIT_PIPELINE_VERSION,
        "positioning": "独立站 Shopify + Meta/Facebook Ads 28跟品赚钱工作台",
        "monitoring_principle": [
            "监控的原则不是收集更多新品，而是先验真，再跟品。",
            "新上架、小垂直站、PDP图片只算线索，不算真实验证。",
            "立即上架必须同时满足2类真实平台证据、无硬风险、经济性可测、市场/供应链参考成立；否则只能观察跟进或不碰。",
            "至少2类真实平台证据后，才看竞争对手页面/素材/Offer的优点。",
            "落地页只能复刻结构和销售逻辑，不能复制图片、视频、文案、评价、Logo、品牌词。",
            "供应链只给可追踪路径，不虚构成本；必须人工核实同源货、MOQ、时效和利润。",
            "每天每个品只给一个动作：拆竞品、找供应链、补流量证据或放弃。",
        ],
        "workflow": [
            "监控新品/小垂直站",
            "跨平台验真",
            "找可跟竞品",
            "判断流量来源和打法",
            "拆竞品优点",
            "复刻落地页结构",
            "找供应链路径",
            "只输出一个下一步动作",
        ],
        "monitoring_targets": [
            "竞品新品节奏和小垂直站SKU扩展",
            "Meta/Facebook Ads 是否持续投放和素材迭代",
            "Google Ads Transparency / Shopping / Trends 是否有搜索需求",
            "TikTok / YouTube / Reddit / X 是否有UGC、demo、评论痛点或实时讨论",
            "落地页首屏、Offer、Bundle、FAQ、信任组件、运输退货",
            "Amazon / Google Shopping / AliExpress / Alibaba / 1688 同源或近似供应链",
        ],
        "official_verification_sources": [
            {"platform": "Meta Ad Library", "url": "https://www.facebook.com/ads/library/", "use": "确认 Facebook/Instagram/Meta 产品上的活跃广告线索"},
            {"platform": "Google Ads Transparency Center", "url": "https://adstransparency.google.com/", "use": "查看 Google Search/Display/Gmail/YouTube 等广告透明度线索"},
            {"platform": "X Ads Transparency", "url": "https://business.x.com/en/help/ads-policies/product-policies/ads-transparency", "use": "按地区可见的 X 广告透明度线索"},
            {"platform": "Reddit Ads Inspiration", "url": "https://www.business.reddit.com/smb/get-ads-inspiration", "use": "看 Reddit 广告样式和讨论语境，不能单独当销量证明"},
        ],
        "api": {
            "run": "POST /api/v2/workbench/run",
            "latest": "GET /api/v2/workbench/latest",
            "blueprint": "GET /api/v2/workbench/blueprint",
            "test_orders": "GET /api/v2/workbench/test-orders",
            "evaluate": "POST /api/v2/workbench/evaluate",
        },
        "product_schema": operator_product_schema(),
        "gate_rules": {
            "launch_now": "立即上架：2类严格验真 + 无硬风险 + 经济性可测 + 市场/供应链参考成立；只生成 Shopify DRAFT，发布前人工复核",
            "observe": "观察跟进：已有线索但缺广告持续性、趋势、讨论、PDP素材、经济性或供应链任一关键证据",
            "avoid": "不碰：硬风险、毛利/价格不成立、证据无法闭环或平台信号明显不成立",
        },
        "stop_scale_rules": {
            "kill_creative_angle": "花费达到无ATC停损线且ATC=0",
            "kill_product_or_adset": "花费达到无购买停损线且purchase=0",
            "refresh_hook": "impressions>1500且CTR<0.7%",
            "fix_pdp_offer": "CTR好但ATC低，优先修PDP/价格/Offer",
            "scale_20_30_percent": "2单以上且CPA<=target CPA，每24小时加20%-30%",
            "keep_collecting": "样本不足，继续收集到停损或放量线",
        },
        "data_safety": [
            "不虚构销量、利润、广告花费、供应链成本",
            "未达到2类严格平台验真时，不输出可跟结论",
            "不允许1:1照抄落地页素材、文案、评价、品牌词、Logo",
            "COGS/履约/退款为显式估算，必须人工确认后才能投放",
            "不自动发布产品，不自动花广告费",
        ],
    }


def operator_product_schema() -> JsonDict:
    return {
        "pareto": {
            "lane": "copy_now | verify_first | find_supplier | skip",
            "launch_triage": "立即上架 | 观察跟进 | 不碰, with validation gates and upload permission",
            "decision": "验真通过，才可跟品拆解 | 待验真，不可跟 | 已验真，先核供应链 | 少看/不跟",
            "truth_check": "strict_sources, count, required_count=2, missing",
            "traffic_sources": "strict: Meta/Facebook Ads, Google Trends, Reddit/YouTube/TikTok/Amazon/Shopping; lead_only: 小垂直站/新品",
            "traffic_strategy": "confirmed/inferred/unknown traffic source and practical play",
            "competitor_advantages": "list[str]",
            "monitor_plan": "what competitor changes to monitor and why",
            "landing_page_clone": "structure-only replication plan, with never_copy boundaries",
            "supply_chain": {
                "status": "marketplace_reference | to_verify",
                "keyword": "str",
                "routes": "Google Shopping / Amazon / AliExpress / Alibaba / 1688 links",
            },
            "money_check": "price, BE CPA, target CPA, real COGS required",
            "one_action": "only one concrete next action",
        },
        "operator_gate": "kept as background evidence, not the primary UI",
        "next_action": "same as pareto.one_action",
    }


def build_seven_day_judgement(candidate: JsonDict) -> JsonDict:
    """A product-operator judgement tuned for seven-day new listings."""

    price = safe_float(candidate.get("price"))
    appearances = int(safe_float(candidate.get("fb_signal", {}).get("ad_creative_count"), 0))
    text = _text_blob(candidate)
    risks = list(candidate.get("risk_flags") or [])
    if _has_licensed_ip_risk(text):
        risks.append("授权/IP/艺人周边风险高，未拿授权前不可直接跟")
    trend = candidate.get("trend_signal") if isinstance(candidate.get("trend_signal"), dict) else empty_trend_signal("not_checked")
    trend_status = str(trend.get("trend_status") or "unverified")
    trend_verified = bool(trend.get("trend_verified"))
    days_since_new = candidate.get("days_since_new")
    base_score = int(safe_float(candidate.get("score"), 0))
    score = 18 + int(base_score * 0.16)
    reasons: list[str] = []
    watchouts: list[str] = []

    if isinstance(days_since_new, int):
        if days_since_new <= 1:
            score += 12
            reasons.append("1天内新上架，时间窗口新")
        elif days_since_new <= 3:
            score += 9
            reasons.append("3天内新上架，仍在可抢先观察期")
        elif days_since_new <= 7:
            score += 6
            reasons.append("7天内新品，可进入跟进池")

    if 29 <= price <= 89:
        score += 16
        reasons.append("价格在Meta冷启动甜蜜区间")
    elif 90 <= price <= 180:
        score += 12
        reasons.append("AOV足够支撑小预算广告测试")
    elif price < 18:
        score -= 8
        watchouts.append("客单价偏低，广告打平空间小")
    elif price > 250:
        score -= 10
        watchouts.append("高客单新品需要更强信任页和素材证明")

    creative_axes = []
    if _has_any(text, PAIN_WORDS):
        creative_axes.append("痛点解决")
        score += 10
    if _has_any(text, VISUAL_WORDS):
        creative_axes.append("视觉演示")
        score += 10
    if _has_any(text, GIFT_WORDS):
        creative_axes.append("礼品场景")
        score += 6
    if _has_any(text, BUNDLE_WORDS):
        creative_axes.append("组合Offer")
        score += 4
    if creative_axes:
        reasons.append("素材钩子清楚：" + " / ".join(creative_axes[:3]))
    else:
        watchouts.append("首3秒素材表达不够直接，需要先找演示角度")

    if 5 <= appearances <= 80:
        score += 16
        reasons.append(f"FB已有{appearances}条素材，需求被验证且未明显拥挤")
    elif 1 <= appearances < 5:
        score += 10
        reasons.append(f"FB已有{appearances}条早期素材，可当作轻量验证")
    elif appearances == 0:
        if creative_axes and 29 <= price <= 180:
            score += 5
            reasons.append("暂无FB素材但产品形态可拍，可能是早期机会")
        watchouts.append("缺少FB素材验证，需要先做竞品素材和小预算测试")
    elif 80 < appearances <= 300:
        score += 7
        reasons.append("FB已放量，证明需求存在")
        watchouts.append("广告侧可能变拥挤，必须做差异化素材")
    else:
        score -= 4
        reasons.append("需求很强但素材环境拥挤")
        watchouts.append("过热品不能照抄，需换角度/套装/落地页")

    if trend_verified and trend_status == "hot":
        score += 14
        reasons.append("Google Trends真实时间序列为hot")
    elif trend_verified and trend_status == "watch":
        score += 10
        reasons.append("Google Trends趋势可用")
    elif trend_verified and trend_status == "emerging":
        score += 6
        reasons.append("Google Trends有早期需求苗头")
    elif trend_status == "weak":
        watchouts.append("Google Trends偏弱，只能作为辅助验证")
    elif trend_status == "unverified":
        watchouts.append("Google Trends未验证，不把趋势当作跟进依据")

    platform_validation = candidate.get("platform_validation") if isinstance(candidate.get("platform_validation"), dict) else build_platform_validation_evidence(candidate)
    platform_score = int(safe_float(platform_validation.get("score"), 0))
    platform_status = str(platform_validation.get("status") or "")
    store_context = platform_validation.get("store_context") if isinstance(platform_validation.get("store_context"), dict) else {}
    if platform_score >= 48:
        score += 14
        reasons.append("Reddit/GT/小垂直站外部验证强，可提高跟进优先级")
    elif platform_score >= 22:
        score += 8
        reasons.append("已有部分平台验证，适合先轻量跟进")
    elif platform_status == "needs_platform_proof":
        watchouts.append("缺少Reddit/YouTube/TikTok等平台验证，先补证据再放量")
    if store_context.get("is_micro_vertical"):
        score += 6
        reasons.append("来源站点约10个产品，小而垂直，适合重点拆解")

    platform_ai = candidate.get("platform_ai") if isinstance(candidate.get("platform_ai"), dict) else build_platform_ai_profit_engine(candidate)
    ai_score = int(safe_float(platform_ai.get("automation_score"), 0))
    if platform_ai.get("readiness") == "ai_ready":
        score += 10
        reasons.append(f"平台AI可投性{ai_score}分，适合Meta/Google/TikTok自动化轻测")
    elif platform_ai.get("readiness") == "asset_gap":
        score += 4
        reasons.append(f"平台AI可投性{ai_score}分，但需先补素材包/PDP证据")
        watchouts.extend(platform_ai.get("asset_gaps", [])[:2])
    elif platform_ai.get("readiness") == "blocked":
        score -= 18
        watchouts.append("平台AI自动投放会放大合规/IP风险，先阻断")
    else:
        watchouts.extend(platform_ai.get("asset_gaps", [])[:2])

    image_count = int(safe_float(candidate.get("image_count"), 0))
    variant_count = int(safe_float(candidate.get("variant_count"), 0))
    if image_count >= 2:
        score += 3
        reasons.append("商品页素材数量足够初步判断卖点")
    if 1 <= variant_count <= 8:
        score += 2
        reasons.append("变体复杂度可控，适合快速建页")

    if _has_hard_risk(risks):
        score -= 24
        watchouts.append("存在硬风险，不能直接跟")
    elif risks:
        score -= min(10, len(risks) * 3)
        watchouts.extend(risks[:2])

    score = max(0, min(100, int(round(score))))
    if score >= 82 and not _has_hard_risk(risks):
        decision = "立即跟进"
        follow_level = "强跟"
        timing = "24-48小时内拆素材并建测试页"
    elif score >= 72 and not _has_hard_risk(risks):
        decision = "轻量跟进"
        follow_level = "轻跟"
        timing = "先做素材拆解与小预算验证"
    elif score >= 58:
        decision = "观察验证"
        follow_level = "观察"
        timing = "等FB素材/趋势/竞品页补证据"
    else:
        decision = "暂不跟进"
        follow_level = "不跟"
        timing = "证据不足或风险偏高"

    guardrails = _break_even_guardrails(price)
    return {
        "method_version": SEVEN_DAY_METHOD_VERSION,
        "follow_priority_score": score,
        "decision": decision,
        "follow_level": follow_level,
        "timing": timing,
        "why_follow": _top_unique(reasons, 5),
        "watchouts": _top_unique(watchouts, 5),
        "first_72h_plan": [
            "按平台AI素材包拆6条素材：3条UGC演示 + 2条痛点/结果 + 1条Offer堆叠",
            "建一个轻量PDP：首屏结果图、使用场景、FAQ、价格锚点",
            f"测试预算先控在${guardrails.get('first_test_budget', 50)}左右，按ATC/CPA快速停损",
        ],
        "platform_ai": platform_ai,
        "platform_method_evidence": {
            "shopify": "需求/价格/竞争/履约/页面证据",
            "meta": "Advantage+/自动化投放需要素材多样性、首3秒钩子、CAPI/Pixel干净",
            "google": "PMax/AI Max需要商品Feed、PDP语义、趋势/搜索意图和图片资产",
            "tiktok": "Smart+更吃原生UGC、演示视频、Spark/达人语气和自动创意安全边界",
            "reddit_youtube_tiktok": "真实讨论/评测/UGC是能不能快出单的重要补证",
            "vertical_micro_site": "5-15个产品的小垂直站单独看，优先找新奇特和轻SKU机会",
            "operator_20y": "新品窗口、毛利容错、素材表达、风险先排除",
        },
    }


def rank_seven_day_candidates(candidates: list[JsonDict]) -> list[JsonDict]:
    return sorted(
        candidates,
        key=lambda c: (
            int(safe_float((c.get("seven_day_judgement") or {}).get("follow_priority_score"), 0)),
            int(c.get("score", 0)),
            int(safe_float(c.get("fb_signal", {}).get("ad_creative_count"), 0)),
            -int(safe_float(c.get("days_since_new"), 99)),
        ),
        reverse=True,
    )


def public_seven_day_product(candidate: JsonDict) -> JsonDict:
    money = money_decision_for_candidate(candidate)
    trends = (money.get("evidence", {}) or {}).get("google_trends", {})
    judgement = candidate.get("seven_day_judgement") or build_seven_day_judgement(candidate)
    return {
        "title": clean_product_title(candidate.get("title", "")),
        "domain": candidate.get("domain", ""),
        "handle": candidate.get("handle", ""),
        "product_url": candidate.get("product_url", ""),
        "price": candidate.get("price", 0),
        "product_type": candidate.get("product_type", ""),
        "vendor": candidate.get("vendor", ""),
        "created_at": candidate.get("created_at", ""),
        "published_at": candidate.get("published_at", ""),
        "updated_at": candidate.get("updated_at", ""),
        "new_product_date": candidate.get("new_product_date", ""),
        "days_since_new": candidate.get("days_since_new"),
        "image_count": candidate.get("image_count", 0),
        "variant_count": candidate.get("variant_count", 0),
        "store_product_count": candidate.get("store_product_count", 0),
        "score": candidate.get("score", 0),
        "decision": candidate.get("decision", ""),
        "fb_signal": candidate.get("fb_signal", {}),
        "google_trends": trends,
        "platform_validation": candidate.get("platform_validation", {}),
        "risk_flags": candidate.get("risk_flags", []),
        "conversion_angles": candidate.get("conversion_angles", []),
        "platform_ai": candidate.get("platform_ai") or ((candidate.get("seven_day_judgement") or {}).get("platform_ai") if isinstance(candidate.get("seven_day_judgement"), dict) else {}),
        "money_decision": money,
        "seven_day_judgement": judgement,
    }


def build_seven_day_method_blueprint() -> JsonDict:
    return {
        "version": SEVEN_DAY_METHOD_VERSION,
        "role": "20年DTC选品负责人 + Meta Ads增长负责人 + Shopify转化负责人",
        "goal": "从过去7天新上产品中选出最值得48-72小时跟进、拆素材、建页和轻测的产品",
        "pillars": [
            "新品时间窗口：created_at/published_at越新，越适合抢先拆解",
            "需求证据：FB素材数量、8000评分、Google Trends真实时间序列、Reddit/YouTube/TikTok真实讨论和评测",
            "广告可表达：痛点、视觉演示、礼品场景、Offer堆叠",
            "商业容错：价格带、毛利空间、履约复杂度、页面可快速搭建；5-15个产品的小垂直站单独列出",
            "平台AI可投性：Meta Advantage+、Google PMax/AI Max、TikTok Smart+、Shopify Magic/Sidekick 所需素材与页面输入是否齐全",
            "风险过滤：授权/IP/艺人周边/医疗承诺/过热素材/低客单广告打平风险",
        ],
        "decision_scale": {
            "82+": "立即跟进",
            "72-81": "轻量跟进",
            "58-71": "观察验证",
            "<58": "暂不跟进",
        },
    }


def _is_newly_listed_within(product: JsonDict, as_of: datetime, days: int) -> bool:
    listed = _new_product_date(product)
    if not listed:
        return False
    valid_dates = {
        (as_of - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(max(1, days))
    }
    return listed in valid_dates


def _new_product_date(product: JsonDict) -> str:
    return max(
        _date_prefix(product.get("created_at", "")),
        _date_prefix(product.get("published_at", "")),
    )


def _days_since_new(product: JsonDict, as_of: datetime) -> int | None:
    listed = _new_product_date(product)
    if not listed:
        return None
    try:
        return max(0, (as_of.date() - datetime.strptime(listed, "%Y-%m-%d").date()).days)
    except ValueError:
        return None


def _read_rejected_products(summary: JsonDict) -> list[JsonDict]:
    path = (summary.get("artifacts", {}) or {}).get("rejected_products")
    if not path:
        return []
    payload, error = _read_json_file(Path(path))
    if error:
        return []
    products = payload.get("products", [])
    return products if isinstance(products, list) else []


PAIN_WORDS = [
    "pain", "relief", "repair", "remove", "cleaner", "anti", "wrinkle", "acne",
    "sleep", "posture", "support", "protect", "odor", "stain", "hair", "teeth",
    "pet", "baby", "organizer", "comfort", "walking", "shock", "absorbing",
]
VISUAL_WORDS = [
    "led", "light", "before", "after", "instant", "portable", "mini", "tool",
    "kit", "spray", "brush", "roller", "massager", "vacuum", "magnetic",
    "automatic", "kneeler", "seat", "boots", "shoes", "diffuser",
]
GIFT_WORDS = ["gift", "personalized", "custom", "holiday", "christmas", "mother", "father", "couple", "kids"]
BUNDLE_WORDS = ["bundle", "bogo", "buy 1 get 1", "2x", "3x", "kit", "set", "pack"]
GIFT_IDENTITY_WORDS = ["name", "necklace", "jewelry", "bracelet", "ring", "personalized", "engraved", "birthstone"]
LICENSED_IP_WORDS = [
    "official md", "official merch", "licensed merch", "fanclub", "fan club",
    "photocard", "photo card", "pob", "lucky draw", "weverse", "lightstick",
    "k-pop", "kpop", "idol", "fan meeting", "fanmeeting", "comeback",
    "mini album", "standard ver", "standard version",
]
KNOWN_ENTERTAINMENT_IP_WORDS = [
    "bts", "blackpink", "twice", "stray kids", "newjeans", "illit",
    "le sserafim", "aespa", "seventeen", "enhypen", "txt", "ateez",
]
CLAIM_SENSITIVE_WORDS = [
    "pregnancy", "safety", "compression", "ems", "bioelectric", "acupressure",
    "posture", "orthopedic", "pain-free", "medical", "therapy", "therapeutic",
    "painless", "kills 99.9", "99.9% germs", "germs", "bacteria", "antibacterial",
    "sterilizer", "disinfect", "capsule", "capsules", "supplement", "detox",
    "extract capsules", "odorless garlic", "fungus", "acne", "eczema",
]
DECOR_WORDS = ["canvas", "wall art", "wall decor", "print", "poster", "painting", "decor"]
COMFORT_WORDS = ["walking", "shoes", "comfort", "supportive", "wide toe", "loafer", "slip in"]
TOOL_WORDS = ["tool", "saw", "grafting", "repair kit", "cleaner", "vacuum", "brush", "spray", "garden"]
RESTRICTED_WORDS = [
    "cbd", "hemp", "nicotine", "vape", "fda", "medical", "diabetes",
    "weight loss", "ozempic", "disney", "pokemon", "nintendo", "apple",
    "tesla", "replica", "tooth repair", "teeth repair", "dental repair",
]
LOW_QUALITY_WORDS = ["charity donation", "app product", "duplicate", "do not delete", "not available product"]
FRAGILE_WORDS = ["glass", "ceramic", "furniture", "mirror", "large sofa", "table", "chair"]
FAMILY_STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "our", "pro", "max", "plus",
    "free", "bogo", "buy", "get", "new", "hot", "sale", "early", "access", "today",
    "extra", "off", "bundle", "pack", "set", "kit",
}
TAG_TRANSLATIONS = {
    "立即测款": "test-now",
    "素材拆解": "creative-research",
    "加入观察": "watch",
    "放弃": "reject",
    "未分类": "uncategorized",
}


def _product_from_snapshot(domain: str, key: str, raw: JsonDict) -> JsonDict | None:
    title = str(raw.get("title") or raw.get("product_title") or "").strip()
    handle = str(raw.get("handle") or key or slugify(title)).strip()
    if not title or not handle or not domain:
        return None
    images = raw.get("images", [])
    variants = raw.get("variants", [])
    return {
        "domain": normalize_domain(domain),
        "title": title,
        "handle": handle,
        "price": _extract_price(raw),
        "product_type": raw.get("product_type") or raw.get("productType") or "未分类",
        "vendor": raw.get("vendor") or "",
        "tags": raw.get("tags", []) if isinstance(raw.get("tags"), list) else [],
        "created_at": raw.get("created_at") or raw.get("createdAt") or "",
        "published_at": raw.get("published_at") or raw.get("publishedAt") or "",
        "updated_at": raw.get("updated_at") or raw.get("updatedAt") or "",
        "image_count": len(images) if isinstance(images, list) else int(bool(raw.get("image_url") or raw.get("image"))),
        "variant_count": len(variants) if isinstance(variants, list) else int(safe_float(raw.get("variant_count") or raw.get("variants_count"), 0)),
    }


def tag_value(text: str, fallback: str = "value") -> str:
    return TAG_TRANSLATIONS.get(str(text or "").strip(), slugify(str(text or ""), fallback=fallback))


def rank_candidates(candidates: list[JsonDict]) -> list[JsonDict]:
    return sorted(
        candidates,
        key=lambda c: (
            int(c.get("score", 0)),
            int(c.get("fb_signal", {}).get("ad_creative_count", 0)),
            1 if 29 <= safe_float(c.get("price")) <= 180 else 0,
            -len(c.get("risk_flags", [])),
        ),
        reverse=True,
    )


def apply_portfolio_limits(candidates: list[JsonDict], config: AutoIntelConfig) -> tuple[list[JsonDict], list[JsonDict]]:
    selected: list[JsonDict] = []
    suppressed: list[JsonDict] = []
    domain_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}

    for candidate in candidates:
        domain = normalize_domain(candidate.get("domain", ""))
        family = candidate.get("family_key") or product_family_key(candidate)
        if domain_counts.get(domain, 0) >= config.max_per_domain:
            suppressed.append({**candidate, "portfolio_suppression": "domain_limit"})
            continue
        if family_counts.get(family, 0) >= config.max_per_family:
            suppressed.append({**candidate, "portfolio_suppression": "duplicate_family"})
            continue
        selected.append(candidate)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        family_counts[family] = family_counts.get(family, 0) + 1

    return selected, suppressed


def _candidate_identity(candidate: JsonDict) -> tuple[str, str]:
    return (normalize_domain(candidate.get("domain", "")), str(candidate.get("handle", "")))


def _public_expert_assessment(expert: JsonDict) -> JsonDict:
    return {
        key: value
        for key, value in expert.items()
        if key not in {"score_adjustment", "score_reason", "risk_flags", "conversion_angles"}
    }


def _creative_plan(angle: str, opening: str, asset: str) -> JsonDict:
    return {"angle": angle, "opening": opening, "asset": asset}


def _break_even_guardrails(price: float) -> JsonDict:
    if price <= 0:
        price = 39.95
    return {
        "first_test_budget": round(min(max(price * 2, 50), 250), 2),
        "target_cpa": round(price * 0.35, 2),
        "warning_cpa": round(price * 0.5, 2),
        "kill_spend_without_atc": round(price * 2, 2),
        "scale_signal": "CTR > 1.5%, ATC > 4%, CPA <= 35% of selling price within 48h",
    }


def _timing_for_ads(appearances: int, meta_launch_tier: str = "") -> str:
    tier = str(meta_launch_tier or "")
    if tier == "compliance_review":
        return "合规审查"
    if appearances <= 0:
        return "早期机会"
    if appearances < 5:
        return "初测窗口"
    if appearances <= 80:
        return "正在验证"
    if appearances <= 300:
        return "正在放量"
    return "已偏拥挤"


def _follow_level(decision: str, score: int, appearances: int, hard_risk: bool) -> str:
    if hard_risk:
        return "不跟"
    if decision == "立即测款" and appearances <= 300:
        return "强跟"
    if score >= 85 and 5 <= appearances <= 80:
        return "强跟"
    if decision == "立即测款":
        return "轻跟"
    if decision == "素材拆解" or score >= 70:
        return "轻跟"
    if decision in {"加入观察", "观察池"} or score >= 45:
        return "观察"
    return "不跟"


def _top_unique(values: list[Any], limit: int = 4) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _default_failure_reasons(candidate: JsonDict, expert: JsonDict) -> list[str]:
    price = safe_float(candidate.get("price"))
    signal = candidate.get("fb_signal", {})
    appearances = int(safe_float(signal.get("ad_creative_count"), 0))
    reasons: list[str] = []
    if price and price < 29:
        reasons.append("客单价低，Meta 冷启动需要很低 CPA 才能打平")
    elif price > 180:
        reasons.append("客单价高，落地页信任和再营销链路不够会拖慢转化")
    elif price <= 0:
        reasons.append("缺少可靠价格，无法判断毛利和首轮预算")
    if appearances == 0:
        status = str(signal.get("fb_verification_status") or "not_checked")
        source = str(signal.get("fb_verification_source") or "")
        if status == "not_found":
            reasons.append("8000 已按域名精确验证未命中 FB 素材；可能是早期机会，也可能是需求尚未被广告证明")
        elif status == "lookup_failed":
            reasons.append("8000 域名精确验证失败；先重试 8000，或用品牌名/关键词扩大验证")
        elif status == "matched":
            reasons.append(f"8000 已命中品牌信号但素材数为 0；来源 {source or 'unknown'}，需复查广告窗口")
        else:
            reasons.append("8000 尚未完成域名级验证；先补跑 8000 精确验证再判断需求强弱")
    if expert.get("meta_launch_tier") in {"research_first", "creative_research"}:
        reasons.append("购买理由还不够尖锐，需要先证明人群、痛点和素材钩子")
    reasons.append("供应链成本、交付时效、退货率未确认前不能放量")
    return reasons


def _differentiation_angle(candidate: JsonDict, expert: JsonDict) -> str:
    archetype = str(expert.get("archetype", ""))
    timing = _timing_for_ads(
        int(safe_float(candidate.get("fb_signal", {}).get("ad_creative_count"), 0)),
        str(expert.get("meta_launch_tier", "")),
    )
    if archetype == "problem_solver_demo":
        base = "用不同人群/场景重拍 before-after，不抄竞品镜头；把痛点瞬间、解决动作和结果证据压进前 8 秒。"
    elif archetype == "gift_identity":
        base = "按收礼对象拆素材和 PDP，主打送礼理由、打开礼物反应和到货节点，而不是泛泛卖产品。"
    elif archetype == "comfort_mobility":
        base = "避开医疗承诺，差异化放在尺码安心、日常舒适场景、退换保障和材质证明。"
    elif archetype == "utility_tool":
        base = "用旧方法 vs 新方法的真实任务演示做差异化，证明省时省力和使用频率。"
    elif archetype == "taste_identity_decor":
        base = "先绑定具体审美人群和空间风格，用 collection/room context 做区隔，不做泛家居爆品。"
    else:
        base = "先找到一个明确人群、一个明确痛点和一个可拍证明点，再决定是否建草稿。"
    if timing in {"正在放量", "已偏拥挤"}:
        return base + " 该品类已出现拥挤信号，必须从原创素材、offer 结构和落地页证明点同时错位。"
    return base


def _what_not_to_copy(candidate: JsonDict, expert: JsonDict) -> list[str]:
    risks = " ".join(str(r) for r in candidate.get("risk_flags", []))
    blocked = [
        "竞品视频、图片、评论截图、logo、品牌词和页面排版",
        "医疗治疗、安全保证、永久效果、夸大 before/after 等不可验证承诺",
        "竞品原价锚点、限时折扣话术和 exact bundle 结构",
        "IP、名人、授权、平台背书等未获得许可的暗示",
    ]
    if "合规" in risks or expert.get("meta_launch_tier") == "compliance_review":
        blocked.append("任何会让 Meta 或 Shopify 判定为敏感健康/安全声明的词")
    return _top_unique(blocked, 5)


def _money_next_action(follow_level: str, timing: str, hard_risk: bool) -> str:
    if hard_risk or follow_level == "不跟":
        return "停止进入草稿；只保留为风险样本，先处理合规/IP/不可售问题。"
    if follow_level == "强跟":
        return "今天建 review-only Shopify 草稿，准备 3 条原创素材，按 48h go/kill 规则小预算测试。"
    if follow_level == "轻跟":
        return "先拆素材结构和落地页证明点，重拍 2-3 条差异化素材后再决定是否建草稿。"
    if timing == "早期机会":
        return "进入观察池，补 Meta/TikTok/Google Trends 和供应链验证，等出现需求证据再测。"
    return "进入观察池，保留信号并等待更好的素材、价格或时机窗口。"


def _queue_item(candidate: JsonDict, next_step: str) -> JsonDict:
    signal = candidate.get("fb_signal", {})
    expert = expert_for_candidate(candidate)
    money = money_decision_for_candidate(candidate)
    trends = (money.get("evidence", {}) or {}).get("google_trends", {})
    return {
        "title": clean_product_title(candidate.get("title", "")),
        "domain": candidate.get("domain", ""),
        "handle": candidate.get("handle", ""),
        "product_url": candidate.get("product_url", ""),
        "score": candidate.get("score", 0),
        "decision": candidate.get("decision", ""),
        "price": candidate.get("price", 0),
        "product_type": candidate.get("product_type", "未分类"),
        "ad_creative_count": signal.get("ad_creative_count", 0),
        "ad_heat": candidate.get("ad_heat", ""),
        "shopify_fit": candidate.get("shopify_fit", ""),
        "fb_ads_fit": candidate.get("fb_ads_fit", ""),
        "expert_archetype": expert.get("archetype", ""),
        "meta_launch_tier": expert.get("meta_launch_tier", ""),
        "money_decision": money,
        "google_trends": trends,
        "follow_level": money.get("follow_level", ""),
        "timing": money.get("timing", ""),
        "why_now": money.get("why_now", []),
        "why_will_sell": money.get("why_will_sell", []),
        "why_may_fail": money.get("why_may_fail", []),
        "first_48h_test_plan": money.get("first_48h_test_plan", {}),
        "next_step": next_step,
        "validation_tasks": _validation_tasks(candidate),
        "landing_page_must_haves": expert.get("landing_page_must_haves", []),
        "offer_strategy": expert.get("offer_strategy", ""),
        "operator_note": expert.get("operator_note", ""),
        "break_even_guardrails": expert.get("break_even_guardrails", {}),
        "creative_angles": candidate.get("conversion_angles", [])[:3],
        "creative_testing_plan": expert.get("creative_testing_plan", []),
        "why": " | ".join(str(r) for r in candidate.get("reasons", [])[:3]),
        "risk_flags": candidate.get("risk_flags", []),
    }


def _validation_tasks(candidate: JsonDict) -> list[str]:
    tasks = ["核供应链成本和交付周期", "确认素材必须原创拍摄/剪辑", "检查 Meta/Shopify 合规词"]
    expert = expert_for_candidate(candidate)
    trend = candidate.get("trend_signal") if isinstance(candidate.get("trend_signal"), dict) else None
    if safe_float(candidate.get("price")) >= 90:
        tasks.append("验证高客单信任组件：保修、退换、评价、分期")
    if int(safe_float(candidate.get("fb_signal", {}).get("ad_creative_count"), 0)) == 0:
        tasks.append("补查 Meta/TikTok 是否已有同类素材验证")
    if expert.get("meta_launch_tier") == "compliance_review":
        tasks.append("先写合规版卖点和禁用词清单")
    if expert.get("archetype") == "comfort_mobility":
        tasks.append("补尺码/退换/试穿风险说明")
    if candidate.get("risk_flags"):
        tasks.append("先处理风险项，否则不进入发布")
    if not trend or not trend.get("trend_verified"):
        tasks.append("补 Google Trends 时间序列验证，不能用模拟趋势分")
    platform_validation = candidate.get("platform_validation") if isinstance(candidate.get("platform_validation"), dict) else {}
    if int(safe_float(platform_validation.get("score"), 0)) < 22:
        tasks.append("补 Reddit / YouTube / TikTok 验证：真实讨论、评测、UGC 三类至少命中一类")
    if (platform_validation.get("store_context") or {}).get("is_micro_vertical"):
        tasks.append("单独拆这个小垂直站：产品数约10个，记录同类SKU、定价和上新节奏")
    return tasks


def _extract_price(raw: JsonDict) -> float:
    for key in ("price", "price_min", "priceMin"):
        price = safe_float(raw.get(key), -1)
        if price >= 0:
            return price
    variants = raw.get("variants")
    if isinstance(variants, list) and variants:
        return safe_float(variants[0].get("price"), 0)
    return 0.0


def _is_recent(entry: JsonDict, valid_dates: set[str]) -> bool:
    return any(_date_prefix(entry.get(k, "")) in valid_dates for k in ("updated_at", "created_at", "published_at"))


def _date_prefix(value: Any) -> str:
    text = str(value or "")
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _recent_sort_key(entry: JsonDict) -> str:
    return max(_date_prefix(entry.get(k, "")) for k in ("updated_at", "created_at", "published_at"))


def _text_blob(product: JsonDict) -> str:
    tags = product.get("tags", [])
    if isinstance(tags, list):
        tags = " ".join(str(t) for t in tags)
    return " ".join([
        str(product.get("title", "")),
        str(product.get("product_type", "")),
        str(product.get("vendor", "")),
        str(tags),
    ]).lower()


def _has_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def _has_licensed_ip_risk(text: str) -> bool:
    text = f" {text.lower()} "
    if _has_any(text, LICENSED_IP_WORDS + KNOWN_ENTERTAINMENT_IP_WORDS):
        return True
    if re.search(r"\b(album|photobook|photo book)\b", text) and _has_any(text, ["official", "pre-order", "pob", "lucky draw", "ver", "version"]):
        return True
    if re.search(r"\bver\s*(set|\.|$)", text) and _has_any(text, ["standard", "random", "photo", "paw", "official"]):
        return True
    return False


def _decision_for_score(score: int, risks: list[str]) -> str:
    if _has_hard_risk(risks):
        return "放弃"
    if score >= 82 and not _has_hard_risk(risks):
        return "立即测款"
    if score >= 70:
        return "素材拆解"
    if score >= 60:
        return "加入观察"
    return "放弃"


def _has_hard_risk(risks: list[str]) -> bool:
    joined = " ".join(risks)
    return "合规" in joined or "不可售" in joined or "授权" in joined or "IP" in joined


def _product_url(product: JsonDict) -> str:
    domain = normalize_domain(product.get("domain", ""))
    handle = product.get("handle", "")
    return f"https://{domain}/products/{handle}" if domain and handle else ""


def clean_product_title(title: str) -> str:
    title = re.sub(r"[™®]", "", title or "")
    title = re.sub(r"\s*\((buy\s*1\s*get\s*1\s*free|bogo)\)\s*", " ", title, flags=re.I)
    title = re.sub(r"\b(buy\s*1\s*get\s*1\s*free|bogo)\b", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title[:120] or "Smart Utility Product"


def suggested_price(source_price: float) -> float:
    if source_price <= 0:
        return 39.95
    if source_price < 25:
        return 29.95
    if source_price <= 90:
        return round(source_price + 10, 2)
    if source_price <= 180:
        return round(source_price * 1.12, 2)
    return round(source_price, 2)


def build_description_html(candidate: JsonDict) -> str:
    title = html.escape(clean_product_title(candidate.get("title", "")))
    angle_items = "".join(f"<li>{html.escape(str(a))}</li>" for a in candidate.get("conversion_angles", [])[:4])
    reason_items = "".join(f"<li>{html.escape(str(r))}</li>" for r in candidate.get("reasons", [])[:5])
    risk = candidate.get("risk_flags") or []
    risk_note = ""
    if risk:
        risk_note = "<p><strong>Internal review note:</strong> " + html.escape("; ".join(risk[:3])) + "</p>"
    return (
        f"<p><strong>{title}</strong> is a review-ready product concept generated from FB Ads and Shopify competitor signals.</p>"
        f"<p><strong>Primary angles:</strong></p><ul>{angle_items}</ul>"
        f"<p><strong>Why it is worth testing:</strong></p><ul>{reason_items}</ul>"
        "<p><em>Draft only. Replace competitor assets with original creative before publishing.</em></p>"
        f"{risk_note}"
    )


def build_landing_page_angle(candidate: JsonDict) -> str:
    title = clean_product_title(candidate.get("title", ""))
    price = safe_float(candidate.get("price"))
    if _has_any(_text_blob(candidate), GIFT_WORDS):
        return f"Giftable PDP for {title}: hero gift moment, personalization/value proof, bundle savings, reviews."
    if price >= 90:
        return f"High-AOV PDP for {title}: problem authority, product demo, guarantee, payment confidence, comparison table."
    return f"Impulse PDP for {title}: problem hero, fast demo GIF, benefit bullets, reviews, two-tier bundle offer."


def build_action_report(summary: JsonDict, top: list[JsonDict], briefs: list[JsonDict], selection_board: JsonDict | None = None) -> str:
    board_summary = (selection_board or {}).get("summary", {})
    lines = [
        "# Auto Intelligence Top Products",
        "",
        f"- Generated: {summary['generated_at']}",
        f"- Run date: {summary['run_date']}",
        f"- Qualified candidates: {summary['stats']['qualified_candidates']}",
        f"- Draft products: {summary['stats']['top_count']}",
        f"- Watchlist products: {summary['stats'].get('watchlist_count', 0)}",
        f"- Duplicate/domain suppressed: {summary['stats'].get('duplicate_suppressed_count', 0)} / {summary['stats'].get('domain_limited_count', 0)}",
        f"- Source products scanned: {summary['stats']['recent_products']}",
        f"- FB signal domains: {summary['stats']['fb_signal_domains']}",
        f"- Google Trends verified: {summary['stats'].get('trend_verified_products', 0)} / {summary['stats'].get('trend_checked_products', 0)}; proxy: {summary['stats'].get('trend_proxy_products', 0)}",
        "",
        "## Action Queues",
        "",
        f"- Test now: {board_summary.get('go_queue_count', 0)}",
        f"- Creative research: {board_summary.get('creative_queue_count', 0)}",
        f"- Review queue: {board_summary.get('review_queue_count', 0)}",
        f"- Watchlist: {board_summary.get('watchlist_count', 0)}",
        f"- Kill list: {board_summary.get('kill_list_count', 0)}",
        "",
        "## Top Action List",
        "",
    ]
    for idx, item in enumerate(top, 1):
        signal = item.get("fb_signal", {})
        expert = expert_for_candidate(item)
        money = money_decision_for_candidate(item)
        trends = (money.get("evidence", {}) or {}).get("google_trends", {})
        plan = money.get("first_48h_test_plan", {})
        lines.extend([
            f"### {idx}. {clean_product_title(item.get('title', ''))}",
            "",
            f"- Decision: {item.get('decision')} / Score {item.get('score')}",
            f"- Money decision: {money.get('follow_level', '')} / {money.get('timing', '')} / can_follow={money.get('can_follow', False)}",
            f"- Expert layer: {expert.get('archetype')} / {expert.get('meta_launch_tier')}",
            f"- Source: {item.get('domain')} / ${item.get('price')} / {item.get('product_url')}",
            f"- FB signal: {signal.get('ad_creative_count', 0)} ads, Orbit {signal.get('orbit_score', 0)}, AI {signal.get('ai_score', 0)}",
            f"- Google Trends: {trends.get('query', '') or 'not checked'} / status {trends.get('status', 'unverified')} / score {trends.get('score', 0)} / current {trends.get('current', 0)} / quality {trends.get('data_quality', '未验证')} / source {trends.get('source', '')}",
            "- Why now: " + " | ".join(str(r) for r in money.get("why_now", [])[:4]),
            "- Why it can sell: " + " | ".join(str(r) for r in money.get("why_will_sell", [])[:4]),
            "- Why it may fail: " + " | ".join(str(r) for r in money.get("why_may_fail", [])[:4]),
            f"- Draft angle: {build_landing_page_angle(item)}",
            f"- Operator note: {expert.get('operator_note', '')}",
            "- Landing must-haves: " + " | ".join(str(x) for x in expert.get("landing_page_must_haves", [])[:5]),
            f"- Offer strategy: {expert.get('offer_strategy', '')}",
            f"- Differentiation: {money.get('differentiation_angle', '')}",
            "- Do not copy: " + " | ".join(str(x) for x in money.get("what_not_to_copy", [])[:4]),
            f"- First 48h plan: budget {plan.get('budget', '')}; angles " + " | ".join(str(x) for x in plan.get("angles", [])[:3]),
            f"- Kill rule: {plan.get('kill_rule', '')}",
            f"- Scale rule: {plan.get('scale_rule', '')}",
            "- Risks: " + (" | ".join(str(r) for r in item.get("risk_flags", [])[:3]) if item.get("risk_flags") else "No hard risk flagged"),
            f"- Next step: {money.get('next_action', '')}",
            "",
        ])
        if idx <= len(briefs):
            first_hook = briefs[idx - 1].get("hooks", [{}])[0]
            lines.append(f"- First creative hook: {first_hook.get('opening', '')}")
            lines.append("")

    watch_items = (selection_board or {}).get("watchlist", [])[:10]
    if watch_items:
        lines.extend(["## Watchlist", ""])
        for item in watch_items:
            lines.append(f"- {item.get('title')} ({item.get('score')}) — {item.get('next_step')}")
        lines.append("")

    if summary.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {w}" for w in summary["warnings"])
        lines.append("")

    return "\n".join(lines)


@contextmanager
def _auto_intel_run_lock(config: AutoIntelConfig):
    lock_path = Path(config.output_root) / ".auto_intelligence.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        stat = lock_path.stat()
        if now - stat.st_mtime > config.lock_stale_seconds:
            lock_path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AutoIntelRunLocked(f"Auto intelligence run already in progress: {lock_path}") from exc

    try:
        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _unique_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _read_json_file(path: Path) -> tuple[JsonDict, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"File missing: {path}"
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON in {path.name}: line {exc.lineno} column {exc.colno}"
    except OSError as exc:
        return {}, f"Read failed for {path.name}: {exc}"
    if not isinstance(payload, dict):
        return {}, f"JSON artifact is not an object: {path.name}"
    return payload, ""


def _read_text_file(path: Path) -> tuple[str, str]:
    try:
        return path.read_text(encoding="utf-8"), ""
    except FileNotFoundError:
        return "", f"File missing: {path}"
    except OSError as exc:
        return "", f"Read failed for {path.name}: {exc}"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, payload: JsonDict) -> None:
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _write_text(path: Path, content: str) -> None:
    _write_atomic(path, content)
