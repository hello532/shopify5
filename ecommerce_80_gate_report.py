#!/usr/bin/env python3
"""Build a conservative 80+ ecommerce intelligence gate report.

The report merges the local 8011 competitor/profit pipeline output, the 8000 FB
orbit signal when available, and the existing social/Amazon auto-launch report.
It is report-only: it never creates Shopify drafts, publishes products, or starts
ads.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output" / "ecommerce_80_gate"
PROFIT_PIPELINE_LATEST = BASE_DIR / "output" / "auto_intelligence" / "profit_pipeline_latest.json"
AUTO_LAUNCH_LATEST = BASE_DIR / "output" / "auto_launch" / "latest.json"

PROFIT_COMMAND_URL = "http://127.0.0.1:8011/api/dashboard/profit-command?limit=50"
FB_ORBITS_URL = "http://127.0.0.1:8000/orbits?limit=120"

DEFAULT_MIN_TEST_SCORE = 80.0


@dataclass
class SourceStatus:
    name: str
    status: str
    count: int = 0
    path_or_url: str = ""
    note: str = ""


@dataclass
class GateCandidate:
    source: str
    lane: str
    title: str
    score: float
    domain: str = ""
    product_url: str = ""
    price: float | None = None
    gate_status: str = ""
    pass_to_test: bool = False
    decision: str = ""
    next_action: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)


def load_json_file(path: Path) -> tuple[Any | None, SourceStatus]:
    if not path.exists():
        return None, SourceStatus(path.name, "missing", path_or_url=str(path), note="file not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive path
        return None, SourceStatus(path.name, "error", path_or_url=str(path), note=str(exc))
    count = len(data) if isinstance(data, list) else len(data.get("actions") or data.get("opportunities") or [])
    return data, SourceStatus(path.name, "ok", count=count, path_or_url=str(path))


def fetch_json_with_curl(url: str, timeout: int = 8) -> tuple[Any | None, SourceStatus]:
    """Fetch local dashboards through curl.

    In this environment Python socket access can be sandbox-blocked while curl is
    allowed, so the runtime report uses curl first and falls back to local files.
    """

    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", str(timeout), url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        return None, SourceStatus(url, "error", path_or_url=url, note=str(exc))
    if result.returncode != 0:
        return None, SourceStatus(url, "unavailable", path_or_url=url, note=result.stderr.strip())
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, SourceStatus(url, "error", path_or_url=url, note=f"invalid json: {exc}")
    count = len(data) if isinstance(data, list) else len(data.get("actions") or data.get("lanes") or [])
    return data, SourceStatus(url, "ok", count=count, path_or_url=url)


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def compact_list(values: list[Any], limit: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value).split())
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def classify_candidate(candidate: GateCandidate, min_score: float = DEFAULT_MIN_TEST_SCORE) -> str:
    if candidate.score >= min_score and candidate.pass_to_test and not candidate.blocking:
        return "测品候选"
    if candidate.score >= min_score:
        return "80+补证"
    if candidate.score >= 65:
        return "观察/补证"
    return "低优先/淘汰"


def _candidate_from_profit_item(item: dict[str, Any], lane: str) -> GateCandidate:
    operator_gate = item.get("operator_gate") or {}
    validation = item.get("required_validation") or item.get("validation") or {}
    validation_summary = validation.get("summary") if isinstance(validation, dict) else {}
    score = as_float(item.get("operator_score"), as_float(item.get("score")))
    blocking = compact_list(
        as_list(operator_gate.get("blockers"))
        + as_list(item.get("validation_blocking_missing"))
        + as_list(validation.get("blocking_missing") if isinstance(validation, dict) else [])
    )
    missing = compact_list(
        as_list(operator_gate.get("missing"))
        + as_list(operator_gate.get("missing_evidence"))
        + as_list(item.get("missing_evidence"))
        + as_list(item.get("evidence_gaps"))
    )
    if not blocking and lane in {"can_prepare", "cannot_follow"}:
        blocking = compact_list(as_list(item.get("evidence_gaps")) + as_list(item.get("blocking")))
    pass_to_test = bool(operator_gate.get("pass_to_test")) or lane in {"ready_to_test", "can_follow_now"}
    if validation_summary and as_float(validation_summary.get("blocking_count")) > 0:
        pass_to_test = False
    candidate = GateCandidate(
        source="8011 profit pipeline",
        lane=lane,
        title=str(item.get("title") or item.get("product_title") or "Untitled"),
        score=score,
        domain=str(item.get("domain") or ""),
        product_url=str(item.get("product_url") or item.get("url") or ""),
        price=item.get("price") if isinstance(item.get("price"), (int, float)) else None,
        gate_status=str(operator_gate.get("status") or item.get("stage") or lane),
        pass_to_test=pass_to_test,
        next_action=str(item.get("next_action") or operator_gate.get("next_gate_to_clear") or ""),
        missing_evidence=missing,
        blocking=blocking,
    )
    candidate.decision = classify_candidate(candidate)
    return candidate


def extract_profit_candidates(data: dict[str, Any] | None) -> list[GateCandidate]:
    if not isinstance(data, dict):
        return []
    candidates: list[GateCandidate] = []
    board = data.get("board") if isinstance(data.get("board"), dict) else {}
    if board:
        for lane, items in board.items():
            if isinstance(items, list):
                candidates.extend(_candidate_from_profit_item(item, str(lane)) for item in items if isinstance(item, dict))
    elif isinstance(data.get("lanes"), dict):
        for lane, items in data["lanes"].items():
            if isinstance(items, list):
                candidates.extend(_candidate_from_profit_item(item, str(lane)) for item in items if isinstance(item, dict))
    elif isinstance(data.get("actions"), list):
        candidates.extend(_candidate_from_profit_item(item, "actions") for item in data["actions"] if isinstance(item, dict))
    return candidates


def extract_auto_launch_candidates(data: dict[str, Any] | None) -> list[GateCandidate]:
    if not isinstance(data, dict):
        return []
    candidates: list[GateCandidate] = []
    for item in data.get("opportunities") or []:
        if not isinstance(item, dict):
            continue
        candidate = GateCandidate(
            source="social/Amazon auto_launch",
            lane="auto_launch",
            title=str(item.get("concept_title") or item.get("title") or "Untitled"),
            score=as_float(item.get("score")),
            domain=str((item.get("amazon") or {}).get("domain") or item.get("domain") or ""),
            product_url=str((item.get("amazon") or {}).get("url") or item.get("product_url") or ""),
            gate_status=str(item.get("decision") or ""),
            pass_to_test=bool(item.get("can_create_draft")),
            next_action=" / ".join(item.get("next_actions") or []),
            missing_evidence=compact_list(item.get("why") or []),
            blocking=[] if item.get("can_create_draft") else ["未达到 80 分测品门槛或缺少关键证据"],
        )
        candidate.decision = classify_candidate(candidate)
        candidates.append(candidate)
    return candidates


def dedupe_candidates(candidates: list[GateCandidate]) -> list[GateCandidate]:
    best: dict[str, GateCandidate] = {}
    for candidate in candidates:
        key = f"{candidate.domain}|{candidate.title}".lower()
        current = best.get(key)
        if current is None or candidate.score > current.score:
            best[key] = candidate
    return sorted(best.values(), key=lambda item: item.score, reverse=True)


def summarize(candidates: list[GateCandidate]) -> dict[str, Any]:
    counts = {"测品候选": 0, "80+补证": 0, "观察/补证": 0, "低优先/淘汰": 0}
    for candidate in candidates:
        counts[candidate.decision] = counts.get(candidate.decision, 0) + 1
    return {
        "total_candidates": len(candidates),
        "decision_counts": counts,
        "score_80_plus": sum(1 for candidate in candidates if candidate.score >= DEFAULT_MIN_TEST_SCORE),
        "ready_to_test": counts.get("测品候选", 0),
        "top_score": max((candidate.score for candidate in candidates), default=0),
    }


def build_payload(min_score: float = DEFAULT_MIN_TEST_SCORE) -> dict[str, Any]:
    profit_data, profit_http_status = fetch_json_with_curl(PROFIT_COMMAND_URL, timeout=10)
    source_statuses = [profit_http_status]
    if not isinstance(profit_data, dict):
        profit_data, profit_file_status = load_json_file(PROFIT_PIPELINE_LATEST)
        profit_file_status.note = "fallback used because 8011 HTTP was unavailable"
        source_statuses.append(profit_file_status)

    auto_launch_data, auto_status = load_json_file(AUTO_LAUNCH_LATEST)
    source_statuses.append(auto_status)

    fb_orbits, fb_status = fetch_json_with_curl(FB_ORBITS_URL, timeout=6)
    if isinstance(fb_orbits, list):
        fb_status.count = len(fb_orbits)
    source_statuses.append(fb_status)

    candidates = dedupe_candidates(
        extract_profit_candidates(profit_data if isinstance(profit_data, dict) else None)
        + extract_auto_launch_candidates(auto_launch_data if isinstance(auto_launch_data, dict) else None)
    )
    # Re-classify with caller-provided threshold.
    for candidate in candidates:
        candidate.decision = classify_candidate(candidate, min_score=min_score)

    summary = summarize(candidates)
    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": "ecommerce-80-intelligence-gate-v1",
        "mode": "REPORT_ONLY",
        "rules": {
            "min_test_score": min_score,
            "test_gate": "score >= 80 AND ready_to_test/pass_to_test AND no blocking evidence",
            "safety": "No Shopify draft creation, no publishing, no ad spend.",
        },
        "summary": summary,
        "sources": [asdict(status) for status in source_statuses],
        "fb_orbits_top": fb_orbits[:20] if isinstance(fb_orbits, list) else [],
        "candidates": [asdict(candidate) for candidate in candidates],
    }


def write_markdown(payload: dict[str, Any], path: Path) -> None:
    summary = payload.get("summary", {})
    lines = [
        "# 80+ Intelligence Gate Report",
        "",
        f"- 生成时间: {payload.get('generated_at')}",
        f"- 模式: {payload.get('mode')}",
        f"- 测品门槛: score >= {payload.get('rules', {}).get('min_test_score')}，且无 blocking，且 ready_to_test/pass_to_test",
        f"- 当前可测品: {summary.get('ready_to_test', 0)}",
        f"- 80+ 候选: {summary.get('score_80_plus', 0)}",
        f"- 总候选: {summary.get('total_candidates', 0)}",
        "",
        "## 数据源状态",
    ]
    for source in payload.get("sources", []):
        lines.append(f"- {source.get('name')}: {source.get('status')} · {source.get('count')} · {source.get('path_or_url')} · {source.get('note', '')}")

    lines.extend(["", "## 分流统计"])
    for decision, count in (summary.get("decision_counts") or {}).items():
        lines.append(f"- {decision}: {count}")

    if summary.get("ready_to_test", 0) == 0:
        lines.extend([
            "",
            "## 当前结论",
            "当前没有满足 80+ 且无 blocking 的测品。系统应继续补证和素材拆解，不应创建 Shopify 草稿、发布商品或启动广告。",
        ])

    lines.extend(["", "## Top 候选"])
    for idx, item in enumerate(payload.get("candidates", [])[:30], 1):
        url = item.get("product_url") or ""
        title = item.get("title") or "Untitled"
        title_text = f"[{title}]({url})" if url else title
        missing = "; ".join((item.get("blocking") or item.get("missing_evidence") or [])[:3])
        lines.extend(
            [
                "",
                f"### {idx}. {title_text}",
                f"- 分流: {item.get('decision')} · 分数: {item.get('score')} · 来源: {item.get('source')} · lane: {item.get('lane')}",
                f"- 域名: {item.get('domain')} · gate: {item.get('gate_status')} · pass_to_test: {item.get('pass_to_test')}",
                f"- 阻塞/缺证: {missing or '无'}",
                f"- 下一步: {item.get('next_action') or '继续补 Meta/趋势/UGC/供应链/素材证据'}",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_artifacts(payload: dict[str, Any]) -> dict[str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"ecommerce_80_gate_{ts}.json"
    md_path = OUTPUT_DIR / f"ecommerce_80_gate_{ts}.md"
    latest_json = OUTPUT_DIR / "latest.json"
    latest_md = OUTPUT_DIR / "latest.md"
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")
    write_markdown(payload, md_path)
    write_markdown(payload, latest_md)
    return {"json": str(json_path), "markdown": str(md_path), "latest_json": str(latest_json), "latest_markdown": str(latest_md)}


def main() -> None:
    payload = build_payload()
    artifacts = write_artifacts(payload)
    payload["artifacts"] = artifacts
    print(json.dumps({"ok": True, "summary": payload["summary"], "artifacts": artifacts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
