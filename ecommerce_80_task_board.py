#!/usr/bin/env python3
"""Build a tracked task board for 80+ enrichment packs.

The board records what is verified locally and what remains blocked before any
product can re-enter the 80+ test gate. It only reads local reports and local
8000 evidence; it never creates Shopify drafts or starts ads.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
ENRICHMENT_LATEST = BASE_DIR / "output" / "ecommerce_80_enrichment" / "latest.json"
OUTPUT_DIR = BASE_DIR / "output" / "ecommerce_80_task_board"
BRAND_URL = "http://127.0.0.1:8000/brands/{domain}"


@dataclass
class TaskStatus:
    key: str
    label: str
    status: str
    evidence: str = ""
    next_action: str = ""


@dataclass
class BoardItem:
    rank: int
    family_key: str
    title: str
    score: float
    lane: str
    local_ad_status: str
    allowed_action: str
    retest_condition: str
    tasks: list[TaskStatus] = field(default_factory=list)
    source_products: list[dict[str, Any]] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _curl_json(url: str, timeout: int = 8) -> tuple[dict[str, Any] | None, str]:
    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", str(timeout), url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except Exception as exc:
        return None, f"error:{exc}"
    if result.returncode != 0:
        return None, "not_found"
    try:
        return json.loads(result.stdout), "ok"
    except json.JSONDecodeError:
        return None, "invalid_json"


def local_brand_evidence(domains: list[str]) -> dict[str, Any]:
    results = []
    total_ads = 0
    max_orbit = 0
    for domain in domains:
        data, status = _curl_json(BRAND_URL.format(domain=domain))
        if data:
            ads = int(data.get("total_ads_across_scans") or 0)
            total_ads += ads
            max_orbit = max(max_orbit, int(data.get("orbit_score") or 0))
            results.append({"domain": domain, "status": status, "ads": ads, "orbit_score": data.get("orbit_score"), "keywords": data.get("keywords") or []})
        else:
            results.append({"domain": domain, "status": status, "ads": 0, "orbit_score": 0, "keywords": []})
    return {
        "status": "verified" if total_ads > 0 else "missing",
        "total_ads": total_ads,
        "max_orbit_score": max_orbit,
        "domains": results,
    }


def _lane_for_pack(pack: dict[str, Any], ad_evidence: dict[str, Any]) -> str:
    key = pack.get("family_key")
    if key == "demand-hair-removal-local-service":
        return "productization_research"
    if key == "petsnowy-consumables-needs-classification":
        return "identity_hold"
    if ad_evidence.get("status") == "verified":
        return "asset_pack_priority"
    return "asset_pack_waiting_for_ad_proof"


def _priority(lane: str, score: float, ad_evidence: dict[str, Any]) -> tuple[int, float]:
    lane_order = {
        "asset_pack_priority": 0,
        "productization_research": 1,
        "asset_pack_waiting_for_ad_proof": 2,
        "identity_hold": 3,
    }
    return (lane_order.get(lane, 9), -score - float(ad_evidence.get("total_ads") or 0) / 100)


def _tasks_for_pack(pack: dict[str, Any], ad_evidence: dict[str, Any]) -> list[TaskStatus]:
    key = pack.get("family_key")
    tasks = [
        TaskStatus(
            key="local_ad_evidence",
            label="本地8000广告证据",
            status=ad_evidence.get("status", "missing"),
            evidence=f"ads={ad_evidence.get('total_ads', 0)}, orbit={ad_evidence.get('max_orbit_score', 0)}",
            next_action="若 missing，继续用 8000/Meta Ad Library 查品牌与关键词；若 verified，进入素材/PDP补齐。",
        ),
        TaskStatus(
            key="sourcing_cost",
            label="供应链/到手成本",
            status="pending",
            evidence="未接入真实 unit cost / freight / MOQ / lead time",
            next_action="至少记录 3 个供应商：unit cost、MOQ、交期、是否可改包装、兼容风险。",
        ),
        TaskStatus(
            key="pdp_assets",
            label="PDP素材",
            status="pending",
            evidence="仍缺原创首屏图/演示图/GIF/FAQ/主推SKU",
            next_action="补 2+ 原创图片或 1图+1短视频/GIF，并收窄到 1-3 个 SKU。",
        ),
        TaskStatus(
            key="creative_pack",
            label="原创素材包",
            status="pending",
            evidence="需要 6 条原创脚本和 2+ 素材角度，不复用竞品素材",
            next_action="按补证包输出的 C1-C6 脚本拍摄或生成素材 brief。",
        ),
        TaskStatus(
            key="unit_economics",
            label="贡献毛利复核",
            status="pending",
            evidence=str(pack.get("economics_check") or {}),
            next_action="接真实 COGS、运费、退货率和目标 CPA，确认贡献毛利为正。",
        ),
    ]
    if key == "demand-hair-removal-local-service":
        tasks.insert(0, TaskStatus(
            key="productization",
            label="产品化判断",
            status="required",
            evidence="当前是本地服务页需求，不是可直接测的独立站产品",
            next_action="先选择 at-home IPL device、aftercare kit 或 prep kit；未产品化不复测 Gate。",
        ))
    if key == "petsnowy-consumables-needs-classification":
        tasks.insert(0, TaskStatus(
            key="identity",
            label="产品身份确认",
            status="required",
            evidence="标题、URL、品类存在错位，且可能是专有兼容耗材",
            next_action="确认 material、pack count、compatibility、是否专有 SKU；不清楚则保持 hold。",
        ))
    return tasks


def _allowed_action(lane: str) -> str:
    return {
        "asset_pack_priority": "优先补 PDP + 原创素材 + 供应链成本；补齐后重跑 80+ Gate",
        "productization_research": "只做产品化研究和合规边界，不建页不测品",
        "asset_pack_waiting_for_ad_proof": "先补广告/UGC证据，再做素材包",
        "identity_hold": "先确认产品身份和供应链可替代性，未确认前不继续",
    }.get(lane, "补证")


def build_board() -> dict[str, Any]:
    enrichment = _read_json(ENRICHMENT_LATEST)
    items: list[tuple[tuple[int, float], BoardItem]] = []
    for pack in enrichment.get("packs", []):
        domains = pack.get("domains") or []
        ad_evidence = local_brand_evidence(domains)
        score = float(pack.get("score") or 0)
        lane = _lane_for_pack(pack, ad_evidence)
        item = BoardItem(
            rank=0,
            family_key=pack.get("family_key", ""),
            title=pack.get("family_title", ""),
            score=score,
            lane=lane,
            local_ad_status=f"{ad_evidence.get('status')} · ads={ad_evidence.get('total_ads', 0)} · orbit={ad_evidence.get('max_orbit_score', 0)}",
            allowed_action=_allowed_action(lane),
            retest_condition="All required/pending tasks must become verified; then rerun ecommerce_80_gate_report.py and require 测品候选 > 0.",
            tasks=_tasks_for_pack(pack, ad_evidence),
            source_products=pack.get("source_products") or [],
        )
        items.append((_priority(lane, score, ad_evidence), item))
    items.sort(key=lambda pair: pair[0])
    board_items = []
    for index, (_, item) in enumerate(items, 1):
        item.rank = index
        board_items.append(asdict(item))
    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": "ecommerce-80-task-board-v1",
        "mode": "REPORT_ONLY_TASK_BOARD",
        "summary": {
            "families": len(board_items),
            "ready_to_test": 0,
            "priority_lane": board_items[0]["lane"] if board_items else "",
            "no_shopify_no_ads": True,
        },
        "items": board_items,
    }


def write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# 80+ Task Board",
        "",
        f"- 生成时间: {payload.get('generated_at')}",
        f"- 产品族: {payload.get('summary', {}).get('families')}",
        f"- 当前可测品: {payload.get('summary', {}).get('ready_to_test')}",
        "- 安全规则: 不建 Shopify，不投广告；只补证、素材、PDP、供应链成本。",
        "",
    ]
    for item in payload.get("items", []):
        lines.extend([
            f"## {item.get('rank')}. {item.get('title')}",
            f"- lane: {item.get('lane')} · score: {item.get('score')} · 本地广告: {item.get('local_ad_status')}",
            f"- 允许动作: {item.get('allowed_action')}",
            f"- 复测条件: {item.get('retest_condition')}",
            "",
            "### 任务状态",
        ])
        for task in item.get("tasks", []):
            lines.append(f"- {task.get('label')}: {task.get('status')} · {task.get('evidence')} · 下一步: {task.get('next_action')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_artifacts(payload: dict[str, Any]) -> dict[str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"ecommerce_80_task_board_{ts}.json"
    md_path = OUTPUT_DIR / f"ecommerce_80_task_board_{ts}.md"
    latest_json = OUTPUT_DIR / "latest.json"
    latest_md = OUTPUT_DIR / "latest.md"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    write_markdown(payload, md_path)
    write_markdown(payload, latest_md)
    return {"json": str(json_path), "markdown": str(md_path), "latest_json": str(latest_json), "latest_markdown": str(latest_md)}


def main() -> None:
    payload = build_board()
    artifacts = write_artifacts(payload)
    print(json.dumps({"ok": True, "summary": payload["summary"], "artifacts": artifacts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
