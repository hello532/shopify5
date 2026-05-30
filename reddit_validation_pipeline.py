#!/usr/bin/env python3
"""
Reddit validation pipeline for product research.

Primary path:
1) Search Reddit discussions via DuckDuckGo HTML through Jina reader.
2) Validate/fetch post pages through live Redlib instances, rotating instances.
3) Output evidence JSON + concise Markdown report.

No API keys required. Designed for environments where reddit.com blocks unauthenticated JSON/HTML.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 HermesRedditValidator/1.0"
INSTANCE_INDEX = "https://raw.githubusercontent.com/redlib-org/redlib-instances/main/instances.md"
DEFAULT_FALLBACK_INSTANCES = [
    "https://redlib.perennialte.ch",
    "https://red.artemislena.eu",
    "https://redlib.privadency.com",
    "https://redlib.ducks.party",
]


def get(url: str, timeout: int = 30) -> requests.Response:
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)


def load_redlib_instances(max_instances: int = 12) -> list[str]:
    try:
        md = get(INSTANCE_INDEX, 20).text
        instances = re.findall(r"\|(https://[^|]+)\|", md)
        # Prefer non-CF lines in the public list; then append known fallback.
        merged = []
        for x in instances + DEFAULT_FALLBACK_INSTANCES:
            x = x.rstrip("/")
            if x not in merged:
                merged.append(x)
        return merged[:max_instances]
    except Exception:
        return DEFAULT_FALLBACK_INSTANCES


def search_ddg_reddit(keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    queries = [f'site:reddit.com/r "{keyword}" reddit', f"site:reddit.com/r {keyword} reddit"]
    hits: list[dict[str, Any]] = []
    for q in queries:
        url = "https://r.jina.ai/http://duckduckgo.com/html/?q=" + urllib.parse.quote(q)
        try:
            text = get(url, 45).text
        except Exception:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if not (line.startswith("## [") and "reddit.com" in line.lower()):
                continue
            title = re.sub(r"^## \[|\]\(.*$", "", line)
            m = re.search(r"uddg=([^&\)]+)", line)
            post_url = urllib.parse.unquote(m.group(1)) if m else ""
            if not post_url or any(h.get("url") == post_url for h in hits):
                continue
            sm = re.search(r"reddit\.com/r/([^/]+)(?:/comments/([^/]+)/([^/?#]+))?", post_url)
            snippet = ""
            for j in range(idx + 1, min(idx + 8, len(lines))):
                if lines[j].startswith("[") and not lines[j].startswith("[!"):
                    s = re.sub(r"\]\(.*$", "", re.sub(r"^\[", "", lines[j])).strip()
                    if s and "reddit.com/r/" not in s:
                        snippet = s
                        break
            hits.append(
                {
                    "title": html.unescape(title),
                    "url": post_url,
                    "subreddit": sm.group(1) if sm else "",
                    "post_id": sm.group(2) if sm and sm.group(2) else "",
                    "slug": sm.group(3) if sm and sm.group(3) else "",
                    "snippet": html.unescape(snippet),
                }
            )
            if len(hits) >= limit:
                return hits
        time.sleep(0.4)
    return hits[:limit]


def fetch_redlib(hit: dict[str, Any], instances: list[str]) -> dict[str, Any]:
    path = urllib.parse.urlparse(hit["url"]).path
    for base in instances:
        redlib_url = base.rstrip("/") + path
        try:
            r = get(redlib_url, 18)
            soup = BeautifulSoup(r.text, "html.parser")
            clean = soup.get_text("\n", strip=True)
            low = clean.lower()
            blocked = any(x in low[:1200] for x in ["blocked", "cloudflare", "enable javascript", "rate limited"])
            ok = r.status_code == 200 and len(clean) > 500 and not blocked
            if ok:
                keyword_anchor = (hit.get("keyword") or "").split()[0].lower()
                pos = low.find(keyword_anchor) if keyword_anchor else -1
                excerpt = clean[max(0, pos - 180) : pos + 520] if pos >= 0 else clean[:700]
                cm = re.search(r"(\d+)\s+comments?", clean, re.I)
                return {
                    **hit,
                    "fetched": True,
                    "redlib_url": redlib_url,
                    "status": r.status_code,
                    "text_len": len(clean),
                    "comments_found": int(cm.group(1)) if cm else None,
                    "excerpt": excerpt[:900],
                }
        except Exception as e:
            last_error = repr(e)[:180]
            continue
    return {**hit, "fetched": False, "error": locals().get("last_error", "all instances failed")}


def validate_keywords(keywords: list[str], outdir: Path, limit: int = 8) -> tuple[Path, Path, dict[str, list[dict[str, Any]]]]:
    outdir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    instances = load_redlib_instances()
    results: dict[str, list[dict[str, Any]]] = {}
    for kw in keywords:
        hits = search_ddg_reddit(kw, limit=limit)
        for h in hits:
            h["keyword"] = kw
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            validated = list(ex.map(lambda h: fetch_redlib(h, instances), hits))
        results[kw] = validated
    json_path = outdir / f"reddit_validated_{ts}.json"
    md_path = outdir / f"reddit_validated_{ts}.md"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Reddit Validation Report — {ts}", "", f"Redlib instances tried: {', '.join(instances[:6])}", ""]
    for kw, arr in results.items():
        ok = [x for x in arr if x.get("fetched")]
        lines += [f"## {kw}", f"Validated: {len(ok)}/{len(arr)}", ""]
        for x in ok[:5]:
            excerpt = re.sub(r"\s+", " ", x.get("excerpt") or x.get("snippet") or "")[:350]
            lines += [f"- r/{x.get('subreddit','')} — {x.get('title','')}", f"  - Source: {x.get('url')}", f"  - Mirror: {x.get('redlib_url')}", f"  - Evidence: {excerpt}"]
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path, results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("keywords", nargs="+", help="Product/topic keywords to validate on Reddit")
    ap.add_argument("--outdir", default="/Users/doi/Desktop/amazon/output/ecommerce_rank_monitor")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    json_path, md_path, results = validate_keywords(args.keywords, Path(args.outdir), args.limit)
    print(f"JSON: {json_path}")
    print(f"MD: {md_path}")
    for kw, arr in results.items():
        print(f"{kw}: {sum(1 for x in arr if x.get('fetched'))}/{len(arr)} validated")


if __name__ == "__main__":
    main()
