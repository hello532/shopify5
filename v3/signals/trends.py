"""Google Trends signal — real pytrends, no mock.

If pytrends is unavailable or rate-limited, returns SignalResult.failed
(never returns simulated numbers).
"""
from __future__ import annotations

from typing import Any

from .. import config
from .._base_local_helpers import explain_writer
from ._base import SignalResult


def collect(p: dict[str, Any], raw: dict[str, Any]) -> SignalResult:
    pid = p["id"]
    keyword = _derive_keyword(p, raw)
    if not keyword:
        return SignalResult(signal="trends", status="failed", error="cannot derive keyword for trends")

    try:
        from pytrends.request import TrendReq
    except ImportError:
        # Don't fake. Just report failed with clear status.
        return SignalResult(
            signal="trends",
            status="failed",
            error="pytrends not installed",
        )

    try:
        pyt = TrendReq(hl=config.get("trends.hl", "en-US"), tz=0, timeout=(5, 25), retries=1, backoff_factor=0.3)
        # Build separate payloads per timeframe to extract scores
        scores = {}
        related: list[str] = []
        for tf_label, tf_value in [
            ("7d", "now 7-d"),
            ("30d", "today 1-m"),
            ("90d", "today 3-m"),
            ("12m", "today 12-m"),
        ]:
            pyt.build_payload([keyword], timeframe=tf_value, geo=config.get("trends.geo", ""))
            df = pyt.interest_over_time()
            if df is None or df.empty or keyword not in df.columns:
                scores[tf_label] = None
                continue
            s = df[keyword].dropna()
            scores[tf_label] = float(s.mean()) if len(s) else None
            if tf_label == "90d" and len(s) >= 5:
                slope = _linear_slope(list(range(len(s))), list(s))
                scores["slope_90d"] = slope
            if tf_label == "12m" and len(s) >= 24:
                recent = float(s.iloc[-4:].mean())
                yoy = float(s.iloc[:4].mean())
                if yoy > 0:
                    scores["yoy_growth"] = (recent - yoy) / yoy * 100.0
        try:
            rq = pyt.related_queries()
            top = (rq.get(keyword) or {}).get("top")
            if top is not None and len(top):
                related = top["query"].head(5).tolist()
        except Exception:
            related = []
    except Exception as e:
        return SignalResult(signal="trends", status="failed", error=f"{type(e).__name__}: {e}")

    s7 = scores.get("7d")
    s30 = scores.get("30d")
    s90 = scores.get("90d")
    yoy = scores.get("yoy_growth")
    slope = scores.get("slope_90d")

    # Killer rule: declining trend
    decline_ratio = config.get("trends.decline_kill_ratio_7d_vs_90d", 0.7)
    if (slope is not None and slope < config.get("trends.decline_kill_slope", 0.0)) and (
        s7 is not None and s90 is not None and s7 < s90 * decline_ratio
    ):
        explain_writer.queue(pid, "trends", "declining_trend",
                             f"slope>=0 OR 7d>={decline_ratio:.0%}*90d",
                             f"slope={slope:.3f} 7d/90d={s7/(s90 or 1):.2f}", False,
                             "declining_trend kill triggered")
        return SignalResult(
            signal="trends",
            status="ok",
            score=0.0,
            data={
                "keyword": keyword, "score_7d": s7, "score_30d": s30, "score_90d": s90,
                "yoy_growth": yoy, "slope_90d": slope,
                "seasonality_phase": "declining",
                "related_queries": related,
            },
        )

    explain_writer.queue(pid, "trends", "declining_trend",
                         f"slope>=0 OR 7d>={decline_ratio:.0%}*90d",
                         f"slope={slope} 7d={s7} 90d={s90}", True, None)

    baseline = min(50.0, s90 or 0.0)
    rising = 0.0
    if s7 is not None and s90 is not None and s90 > 0:
        rising = max(0.0, (s7 - s90) / s90 * 100.0)
    score = min(100.0, baseline + min(50.0, rising))

    # Phase heuristic
    if s7 is not None and s90 is not None:
        if s7 > s90 * 1.2:
            phase = "rising"
        elif s7 < s90 * 0.85:
            phase = "declining"
        elif s7 > 70:
            phase = "peak"
        else:
            phase = "stable"
    else:
        phase = "unknown"

    return SignalResult(
        signal="trends",
        status="ok",
        score=round(score, 2),
        data={
            "keyword": keyword,
            "score_7d": s7,
            "score_30d": s30,
            "score_90d": s90,
            "yoy_growth": yoy,
            "slope_90d": slope,
            "seasonality_phase": phase,
            "related_queries": related,
        },
    )


def _derive_keyword(p: dict[str, Any], raw: dict[str, Any]) -> str | None:
    if raw.get("source_keyword"):
        return str(raw["source_keyword"])
    title = p.get("title") or raw.get("title")
    if not title:
        return None
    # Take first 3-4 words as trend keyword
    words = [w for w in str(title).split() if w.isalpha()]
    return " ".join(words[:3]) if words else None


def _linear_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0
