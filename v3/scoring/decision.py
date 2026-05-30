"""Decision = hard rules → GO_TEST | WATCH | KILL with enumerated reasons."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .. import config
from ..signals import SignalResult


def compute(pid: int, signals: dict[str, SignalResult], score_pack: dict[str, Any]) -> dict[str, Any]:
    fb = signals.get("fb_ads")
    pr = signals.get("profit")
    tr = signals.get("trends")
    lp = signals.get("lp")

    # ---- Hard one-vote-kills (highest priority first) ----
    if pr is None or pr.status == "failed":
        return _watch("awaiting_profit_signal")
    if pr.score == 0.0:
        # Marcus rule or other profit issue
        return _kill("profit_too_thin")

    if fb is not None and fb.status == "ok" and fb.score == 0.0:
        d = fb.data or {}
        if (d.get("days_active") or 0) < config.get("fb_ads.min_days_active", 14):
            return _kill("ad_not_persistent")
        if (d.get("impressions_total") or 0) < config.get("fb_ads.min_impressions", 100000):
            return _kill("low_traction")
        if (d.get("distinct_entity_ids") or 0) < 3:
            return _kill("no_creative_diversity")
        return _kill("ad_not_persistent")
    elif fb is None or fb.status == "failed":
        return _watch("awaiting_creative_proof")

    if tr is not None and tr.status == "ok" and tr.score == 0.0:
        return _kill("declining_trend")

    if lp is not None and lp.status == "ok" and lp.score == 0.0:
        return _kill("lp_unprofessional")
    if lp is None or lp.status == "failed":
        return _watch("awaiting_creative_proof")

    composite = score_pack["composite"]
    fb_score = score_pack["fb_score"]
    profit_score = score_pack["profit_score"]
    trend_score = score_pack["trend_score"]

    # ---- GO_TEST ----
    go_min = config.get("scoring.go_test.composite_min", 75)
    fb_min = config.get("scoring.go_test.fb_min", 60)
    profit_min = config.get("scoring.go_test.profit_min", 60)

    if composite >= go_min and fb_score >= fb_min and profit_score >= profit_min:
        return _go_test(pr)

    # ---- WATCH ----
    watch_min = config.get("scoring.watch.composite_min", 55)
    if composite >= watch_min:
        if (fb.data or {}).get("days_active", 0) < config.get("fb_ads.min_days_active", 14):
            reason = "awaiting_persistence"
        elif trend_score < 50:
            reason = "awaiting_trend"
        else:
            reason = "awaiting_creative_proof"
        return _watch(reason)

    return _kill("low_overall_score")


def _go_test(profit_signal: SignalResult) -> dict[str, Any]:
    tp_cfg = config.get("test_plan", {})
    target_roas = (profit_signal.data or {}).get("target_roas")
    plan = {
        "test_budget_usd": tp_cfg.get("default_budget_usd", 50),
        "test_creatives_required": tp_cfg.get("default_creatives", 3),
        "target_roas": target_roas,
        "kill_rule_day3": tp_cfg.get("kill_day3_roas", 1.5),
        "kill_rule_day7": tp_cfg.get("kill_day7_roas", 2.0),
        "scale_rule": tp_cfg.get("scale_day7_roas", 3.0),
        "scale_step_pct": tp_cfg.get("scale_step_pct", 0.20),
    }
    return {"decision": "GO_TEST", "test_plan": plan}


def _watch(reason: str) -> dict[str, Any]:
    days = config.get("decision.watch_recheck_days", 7)
    return {
        "decision": "WATCH",
        "watch_reason": reason,
        "watch_recheck_at": (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds") + "Z",
    }


def _kill(reason: str) -> dict[str, Any]:
    return {"decision": "KILL", "kill_reason": reason}
