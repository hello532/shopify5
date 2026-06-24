#!/usr/bin/env python3
"""Create enrichment execution packs for 80+ candidates that are not test-ready.

This is deliberately report-only. It turns high-scoring blocked candidates into
concrete evidence, PDP, creative, and sourcing tasks without creating Shopify
drafts or launching ads.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


BASE_DIR = Path(__file__).resolve().parent
GATE_LATEST = BASE_DIR / "output" / "ecommerce_80_gate" / "latest.json"
OUTPUT_DIR = BASE_DIR / "output" / "ecommerce_80_enrichment"
PROFIT_COMMAND_URL = "http://127.0.0.1:8011/api/dashboard/profit-command?limit=50"


@dataclass
class EnrichmentPack:
    family_key: str
    family_title: str
    score: float
    decision: str
    domains: list[str]
    source_products: list[dict[str, Any]]
    blocking: list[str]
    automated_judgement: str
    allowed_next_step: str
    validation_tasks: list[str]
    pdp_requirements: list[str]
    creative_scripts: list[dict[str, str]]
    sourcing_checks: list[dict[str, str]]
    economics_check: dict[str, Any]
    safety_rules: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _curl_json(url: str, timeout: int = 10) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", str(timeout), url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out[:80] or "product"


def _score(item: dict[str, Any]) -> float:
    for key in ("operator_score", "score"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def family_key(item: dict[str, Any]) -> str:
    title = _norm(item.get("title")).lower()
    domain = _norm(item.get("domain")).lower()
    if "lasercenteroforlando.com" in domain and any(token in title for token in ("hair removal", "electrolysis", "laser")):
        return "demand-hair-removal-local-service"
    if "air purifier" in title or "purifier filter" in title:
        return "pet-air-purifier-filter"
    if "petsnowy" in domain:
        return "petsnowy-consumables-needs-classification"
    words = [w for w in re.findall(r"[a-z0-9]+", title) if len(w) > 2]
    return f"{domain}-{ '-'.join(words[:5]) }"


def family_title(key: str, items: list[dict[str, Any]]) -> str:
    if key == "demand-hair-removal-local-service":
        return "Hair Removal Demand Signal - productize before ecommerce test"
    if key == "pet-air-purifier-filter":
        return "Pet Air Purifier Replacement Filter"
    if key == "petsnowy-consumables-needs-classification":
        return "PetSnowy Consumables - classification and sourcing check"
    return _norm(items[0].get("title"))


def _unique(values: list[Any], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _norm(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _report_only_tasks(values: list[Any], limit: int = 12) -> list[str]:
    blocked_terms = ("draft", "上架", "草稿页", "建草稿", "发布", "投放")
    safe: list[Any] = []
    for value in values:
        text = _norm(value)
        if not text:
            continue
        if any(term in text.lower() for term in blocked_terms):
            continue
        safe.append(text)
    return _unique(safe, limit)


def _search_links(keyword: str) -> list[dict[str, str]]:
    q = quote_plus(keyword)
    return [
        {"platform": "Meta Ad Library", "url": f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&media_type=all&q={q}&search_type=keyword_unordered", "purpose": "确认是否有持续投放和可学习的素材母题"},
        {"platform": "Google Ads Transparency", "url": f"https://adstransparency.google.com/?region=US&query={q}", "purpose": "确认搜索/视频/Shopping 广告线索"},
        {"platform": "Amazon", "url": f"https://www.amazon.com/s?k={q}", "purpose": "确认同类价格带、评论痛点、评分和卖家密度"},
        {"platform": "Google Shopping", "url": f"https://www.google.com/search?tbm=shop&q={q}", "purpose": "确认同款/近似款价格带和可替代供应"},
        {"platform": "Alibaba", "url": f"https://www.alibaba.com/trade/search?SearchText={q}", "purpose": "查 MOQ、定制空间、样品和报价"},
        {"platform": "AliExpress", "url": f"https://www.aliexpress.com/wholesale?SearchText={q}", "purpose": "查小单测试货源、发货时效和评价"},
        {"platform": "1688", "url": f"https://s.1688.com/selloffer/offer_search.htm?keywords={q}", "purpose": "查源头价和可替代供应链"},
    ]


def _keyword_for_family(key: str, items: list[dict[str, Any]]) -> str:
    if key == "demand-hair-removal-local-service":
        return "at home IPL hair removal device"
    if key == "pet-air-purifier-filter":
        return "pet air purifier replacement filter"
    if key == "petsnowy-consumables-needs-classification":
        return "pet dryer absorbent towels disposable"
    return _norm(items[0].get("title"))


def _judgement(key: str) -> tuple[str, str]:
    if key == "demand-hair-removal-local-service":
        return (
            "local service pages are demand evidence, not a Shopify-testable product; productize into an at-home device or aftercare kit before any draft",
            "research_only_productization",
        )
    if key == "petsnowy-consumables-needs-classification":
        return (
            "title/category/URL mismatch and supply/compliance gaps are unresolved; hold until product identity and landed cost are proven",
            "classification_and_supplier_check_only",
        )
    return (
        "demand is strong enough for teardown, but PDP assets and original creative are missing; build proof pack before testing",
        "build_asset_pack_only",
    )


def _pdp_requirements(key: str) -> list[str]:
    base = [
        "Hero: one clear result promise, original image/video, visible offer, no competitor branding",
        "Proof: original demo photos or GIF, objection-handling FAQ, delivery/returns promise",
        "SKU: limit first test to 1-3 variants; no broad variant grid before signal",
        "Tracking checklist only after gate pass: Pixel/CAPI, content_id, event_id, Purchase event",
    ]
    if key == "demand-hair-removal-local-service":
        return [
            "Do not use Orlando/local clinic pages as product PDPs",
            "Choose ecommerce product direction first: at-home IPL device, aftercare gel, or hair-removal prep kit",
            "Avoid permanent-removal, medical, guaranteed-result, or before/after claims unless substantiated",
            *base,
        ]
    if key == "pet-air-purifier-filter":
        return [
            "Clarify compatibility: exact purifier model, dimensions, filter type, replacement interval",
            "Show original filter comparison, installation demo, airflow/odor use case without unverified claims",
            *base,
        ]
    return [
        "Fix product identity: title, URL, category, use case, material, dimensions, pack count",
        "Do not proceed if this is a proprietary consumable or has unclear compatibility",
        *base,
    ]


def _creative_scripts(key: str, title: str) -> list[dict[str, str]]:
    if key == "demand-hair-removal-local-service":
        angles = [
            ("Problem routine", "Show shaving/waxing repetition and time cost; do not mention permanent results."),
            ("At-home convenience", "Show a generic at-home routine concept after product direction is sourced."),
            ("Cost comparison", "Compare repeat appointments vs. at-home care only with verified numbers."),
            ("Sensitive-skin objection", "Frame around irritation concerns and patch-test guidance."),
            ("FAQ objection", "Answer safety, skin tone, usage cadence, warranty, and returns."),
            ("Offer stack", "Bundle device/aftercare/storage only after COGS is verified."),
        ]
    elif key == "pet-air-purifier-filter":
        angles = [
            ("Replacement reminder", "Show pet odor/dust context and filter replacement moment."),
            ("Install demo", "Show 10-second swap, exact model fit, and pack count."),
            ("Before-after filter", "Show used vs. new filter visually, no unsupported health claims."),
            ("Subscription/bundle", "Show 2-pack/4-pack replacement cadence after margin check."),
            ("FAQ objection", "Address compatibility, size, delivery, returns, and replacement interval."),
            ("Trust proof", "Show original packaging, fit test, and customer-style usage scene."),
        ]
    else:
        angles = [
            ("Identity proof", "Show what the product actually is, pack count, material, and use case."),
            ("Use demo", "Show one real pet-care use case from start to finish."),
            ("Problem/solution", "Show the mess/inconvenience and the cleanup result."),
            ("Compatibility", "Show where it works and where it does not work."),
            ("FAQ objection", "Answer size, safety, disposal, returns, and delivery time."),
            ("Offer stack", "Test single pack vs. multi-pack only after landed cost is verified."),
        ]
    return [
        {
            "name": f"C{i}-{_slug(angle)[:28]}",
            "angle": angle,
            "hook_0_3s": note,
            "proof_3_12s": f"Use original footage for {title}; never reuse competitor video, logos, reviews, or page copy.",
            "cta_12_20s": "Show offer and return/shipping reassurance; keep claims within verified proof.",
        }
        for i, (angle, note) in enumerate(angles, 1)
    ]


def _economics(items: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [item.get("price") for item in items if isinstance(item.get("price"), (int, float))]
    price = round(sum(prices) / len(prices), 2) if prices else None
    return {
        "observed_price": price,
        "required_inputs": ["unit cost", "inbound freight", "payment fee", "pick/pack", "refund reserve", "expected CPA"],
        "pass_rule": "Do not test unless landed cost and shipping leave positive contribution margin after target CPA.",
    }


def load_online_80_candidates() -> list[dict[str, Any]]:
    data = _curl_json(PROFIT_COMMAND_URL)
    if not data:
        return []
    out: list[dict[str, Any]] = []
    for lane, items in (data.get("lanes") or {}).items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if _score(item) >= 80:
                item = dict(item)
                item["lane"] = lane
                out.append(item)
    return out


def load_gate_80_candidates() -> list[dict[str, Any]]:
    data = _read_json(GATE_LATEST)
    return [item for item in data.get("candidates", []) if item.get("decision") == "80+补证"]


def build_packs() -> dict[str, Any]:
    candidates = load_online_80_candidates() or load_gate_80_candidates()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault(family_key(item), []).append(item)

    packs: list[EnrichmentPack] = []
    for key, items in sorted(grouped.items(), key=lambda pair: max(_score(x) for x in pair[1]), reverse=True):
        title = family_title(key, items)
        keyword = _keyword_for_family(key, items)
        judgement, next_step = _judgement(key)
        validation_tasks = _report_only_tasks(
            [task for item in items for task in item.get("validation_tasks", [])]
            + [item.get("next_action") for item in items]
            + [f"Use {keyword} to verify Meta/UGC/Amazon/Shopping/supplier evidence before retesting the gate."],
            10,
        )
        blocking = _unique(
            [block for item in items for block in item.get("blocking", [])]
            + [gap for item in items for gap in item.get("evidence_gaps", [])]
            + [gap for item in items for gap in item.get("missing_evidence", [])],
            10,
        )
        packs.append(
            EnrichmentPack(
                family_key=key,
                family_title=title,
                score=max(_score(item) for item in items),
                decision="补证执行包",
                domains=sorted({_norm(item.get("domain")) for item in items if _norm(item.get("domain"))}),
                source_products=[
                    {
                        "title": _norm(item.get("title")),
                        "url": _norm(item.get("product_url") or item.get("url")),
                        "lane": item.get("lane", ""),
                        "score": _score(item),
                    }
                    for item in items
                ],
                blocking=blocking,
                automated_judgement=judgement,
                allowed_next_step=next_step,
                validation_tasks=validation_tasks,
                pdp_requirements=_pdp_requirements(key),
                creative_scripts=_creative_scripts(key, title),
                sourcing_checks=_search_links(keyword),
                economics_check=_economics(items),
                safety_rules=[
                    "No Shopify draft until the 80+ gate rerun returns 测品候选.",
                    "No ads or budget spend from this pack.",
                    "No competitor media, reviews, logos, page copy, or brand terms may be reused.",
                    "Supplier cost, compatibility, and compliance must be verified with real evidence, not inferred.",
                ],
            )
        )

    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": "ecommerce-80-enrichment-plan-v1",
        "mode": "REPORT_ONLY_ENRICHMENT",
        "summary": {
            "raw_80_plus_candidates": len(candidates),
            "product_families": len(packs),
            "ready_to_test": 0,
            "allowed_action": "补证/素材/PDP/供应链任务，不上架不投放",
        },
        "packs": [asdict(pack) for pack in packs],
    }


def write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# 80+ Enrichment Execution Plan",
        "",
        f"- 生成时间: {payload.get('generated_at')}",
        f"- 模式: {payload.get('mode')}",
        f"- 80+ 原始候选: {payload.get('summary', {}).get('raw_80_plus_candidates')}",
        f"- 合并产品族: {payload.get('summary', {}).get('product_families')}",
        "- 当前允许动作: 补证、素材包、PDP任务、供应链核价；不建 Shopify，不投广告。",
        "",
    ]
    for idx, pack in enumerate(payload.get("packs", []), 1):
        lines.extend([
            f"## {idx}. {pack.get('family_title')}",
            f"- 分数: {pack.get('score')} · 下一步: {pack.get('allowed_next_step')}",
            f"- 判断: {pack.get('automated_judgement')}",
            f"- 域名: {', '.join(pack.get('domains') or [])}",
            f"- 阻塞: {'; '.join((pack.get('blocking') or [])[:4])}",
            "",
            "### 验证任务",
        ])
        lines.extend(f"- {task}" for task in pack.get("validation_tasks", [])[:8])
        lines.extend(["", "### PDP 补齐", *[f"- {task}" for task in pack.get("pdp_requirements", [])]])
        lines.extend(["", "### 原创素材脚本"])
        for script in pack.get("creative_scripts", []):
            lines.append(f"- {script.get('name')}: {script.get('hook_0_3s')} / {script.get('proof_3_12s')} / {script.get('cta_12_20s')}")
        lines.extend(["", "### 供应链/市场核查入口"])
        for link in pack.get("sourcing_checks", [])[:7]:
            lines.append(f"- [{link.get('platform')}]({link.get('url')}): {link.get('purpose')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_artifacts(payload: dict[str, Any]) -> dict[str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"ecommerce_80_enrichment_{ts}.json"
    md_path = OUTPUT_DIR / f"ecommerce_80_enrichment_{ts}.md"
    latest_json = OUTPUT_DIR / "latest.json"
    latest_md = OUTPUT_DIR / "latest.md"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    write_markdown(payload, md_path)
    write_markdown(payload, latest_md)
    return {"json": str(json_path), "markdown": str(md_path), "latest_json": str(latest_json), "latest_markdown": str(latest_md)}


def main() -> None:
    payload = build_packs()
    artifacts = write_artifacts(payload)
    print(json.dumps({"ok": True, "summary": payload["summary"], "artifacts": artifacts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
