"""End-to-end product launch orchestrator — minimize human steps.

When user clicks "🚀 上架到我的店":
  1. Generate 3 Andromeda-distinct creative variants
  2. Build full landing_kit (brief + lp_blocks + Klaviyo flows + ads_paste.json)
  3. Push product to Shopify as DRAFT (or ACTIVE if requested)
  4. Return a single summary with all URLs + creative bodies

Also: human-readable verdict generation (`why_card`) that converts numbers into
plain-language reasoning the user can act on without interpreting fb=72/trend=65.
"""
from __future__ import annotations

import json
from typing import Any

from . import config, db


# ───────────────────────── Human-readable reasoning ─────────────────────────

def why_card(product_id: int) -> dict[str, Any]:
    """Return plain-language reasoning for a single product's decision.

    No LLM required — uses the same signals the decision engine already wrote.
    Output is a structured object the UI renders as bullet points + verdict.
    """
    with db.conn() as c:
        prow = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not prow:
            return {"ok": False, "error": "product not found"}
        p = dict(prow)
        d = c.execute(
            "SELECT * FROM decisions WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        s = c.execute(
            "SELECT * FROM scores WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        ad = c.execute(
            "SELECT * FROM ad_signals WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        pr = c.execute(
            "SELECT * FROM profit_signals WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        tr = c.execute(
            "SELECT * FROM trend_signals WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        lp = c.execute(
            "SELECT * FROM lp_signals WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    d = dict(d) if d else {}
    s = dict(s) if s else {}
    ad = dict(ad) if ad else {}
    pr = dict(pr) if pr else {}
    tr = dict(tr) if tr else {}
    lp = dict(lp) if lp else {}

    pros: list[dict[str, Any]] = []
    cons: list[dict[str, Any]] = []

    # 1) Profit pro/con
    margin = pr.get("gross_margin_pct")
    beroas = pr.get("beroas")
    if margin is not None:
        if margin >= 60:
            pros.append({"icon": "💰", "title": f"毛利 {margin:.0f}% — 每单赚 ${(p.get('price_usd') or 0) * margin / 100:.0f}",
                         "why": f"BEROAS {beroas:.2f} 起算就赚,留足广告试错空间"})
        elif margin >= 40:
            pros.append({"icon": "💰", "title": f"毛利 {margin:.0f}% — 健康水位",
                         "why": f"BEROAS {beroas:.2f},广告打到 {(beroas or 2) * 1.2:.1f}x 就开始赚钱"})
        elif margin >= 15:
            cons.append({"icon": "⚠️", "title": f"毛利仅 {margin:.0f}% — 紧",
                         "why": f"BEROAS {beroas:.2f},广告要打到 {(beroas or 3) * 1.2:.1f}x 才不亏,Meta Andromeda CPM 涨 20% 容易踩坑"})
        else:
            cons.append({"icon": "🚫", "title": f"毛利仅 {margin:.0f}% — Marcus 规则一票否决",
                         "why": "毛利 <15% 的产品基本无法跑 paid ads"})

    # 2) Ad persistence pro/con
    days = ad.get("days_active")
    distinct = ad.get("distinct_entity_ids")
    impressions = ad.get("impressions_total")
    if days is not None and days >= 14:
        pros.append({"icon": "🔥", "title": f"广告已跑 {days} 天 — 别人验证过在赚",
                     "why": "Meta 学习期过了还在花钱 = 大概率赚钱,这是行业最硬的赚钱信号"})
    elif days is not None and days < 14:
        cons.append({"icon": "⏳", "title": f"广告仅 {days} 天 — 还没跨学习期",
                     "why": f"Meta 学习期 ~14 天,{days} 天还没验证别人是否真在赚"})
    if distinct is not None and distinct >= 5:
        pros.append({"icon": "🎨", "title": f"{distinct} 个 distinct 创意 — 在认真扩量",
                     "why": "Andromeda 视觉去重后还有 5+ 个独立创意 = 广告主在加单/扩量"})
    elif distinct is not None and distinct < 3:
        cons.append({"icon": "🎭", "title": f"仅 {distinct} 个 distinct 创意 — Andromeda 陷阱",
                     "why": "Meta 会把视觉相似的创意合并成 1 个 Entity ID,等于只投了 1 条"})
    if impressions and impressions >= 100000:
        pros.append({"icon": "📊", "title": f"已 {impressions/1000:.0f}K 展示 — 跨过测款窗口",
                     "why": "10万+ 展示证明广告主跨过了测款门槛进入扩量"})

    # 3) Trend pro/con
    s7 = tr.get("score_7d")
    s90 = tr.get("score_90d")
    yoy = tr.get("yoy_growth")
    phase = tr.get("seasonality_phase")
    if s7 and s90 and s7 > s90 * 1.2:
        pros.append({"icon": "📈", "title": f"Google Trends 上升中 — 7d {s7:.0f} vs 90d {s90:.0f}",
                     "why": "近期搜索量超过过去 3 月平均的 20%,需求在涨"})
    elif yoy and yoy > 50:
        pros.append({"icon": "🚀", "title": f"YoY +{yoy:.0f}% — 同比大涨",
                     "why": "去年同期比对显示这是真增长品类不是季节性峰"})
    elif s7 and s90 and s7 < s90 * 0.7:
        cons.append({"icon": "📉", "title": f"Trends 下行 — 7d {s7:.0f} < 90d {s90:.0f}",
                     "why": "需求在快速衰减,现在入场可能赶到尾巴"})

    # 4) Landing page pro/con
    has_klaviyo = lp.get("has_klaviyo")
    has_reviews = lp.get("has_reviews_app")
    has_pixel = lp.get("has_pixel")
    has_shopify = lp.get("has_shopify")
    awareness = lp.get("awareness_match_layer")
    pms = lp.get("payment_methods_count") or 0
    full_signals = sum([bool(has_klaviyo), bool(has_reviews), bool(has_pixel), bool(has_shopify)])
    if full_signals >= 3:
        items = []
        if has_shopify: items.append("Shopify")
        if has_klaviyo: items.append("Klaviyo")
        if has_reviews: items.append("Reviews")
        if has_pixel: items.append("Pixel")
        pros.append({"icon": "🏪", "title": f"落地页配齐 {'/'.join(items)} — 专业卖家",
                     "why": "竞品是品牌级运营,不是地推站,模板和打法可借鉴"})
    if awareness in ("problem_aware", "solution_aware"):
        pros.append({"icon": "🎯", "title": f"Hook 对准 {awareness} 层",
                     "why": "Mark 七层 Awareness:hook 触发的是用户真实痛点,转化路径短"})
    if pms >= 3:
        pros.append({"icon": "💳", "title": f"{pms} 种支付方式 — 摩擦小",
                     "why": "Shop Pay/Apple Pay/PayPal 全配,checkout 流失率低"})

    # 5) Generate suggested next step
    test_plan = {}
    if d.get("test_plan_json"):
        try:
            test_plan = json.loads(d["test_plan_json"])
        except Exception:
            pass

    verdict_text = _verdict_sentence(d.get("decision"), p, margin, days, distinct, s7, s90)

    return {
        "ok": True,
        "verdict": d.get("decision"),
        "verdict_text": verdict_text,
        "composite": s.get("composite_score"),
        "pros": pros,
        "cons": cons,
        "test_plan": test_plan,
        "title": p.get("title"),
        "price": p.get("price_usd"),
        "shop": p.get("shop_domain"),
    }


def _verdict_sentence(decision: str | None, product: dict, margin: float | None,
                      days: int | None, distinct: int | None,
                      s7: float | None, s90: float | None) -> str:
    """One-sentence human verdict — like a friend telling you 'do it' or 'skip it'."""
    if decision == "GO_TEST":
        bits = []
        if days and days >= 14: bits.append(f"广告 {days} 天验证在赚")
        if margin and margin >= 40: bits.append(f"毛利 {margin:.0f}% 健康")
        if s7 and s90 and s7 > s90 * 1.1: bits.append("Trends 上升")
        if not bits: bits.append("综合分够高")
        return f"✅ 现在测 — {' · '.join(bits)},直接花 $50 测 3 天看 ROAS。"
    if decision == "WATCH":
        if days is not None and days < 14:
            return f"⏸ 再等等 — 别人广告才跑 {days} 天,过 14 天再看是否真在赚。"
        if margin and margin < 25:
            return f"⏸ 再等等 — 毛利 {margin:.0f}% 偏紧,等 1688 找到更便宜的源头再做。"
        return "⏸ 再等等 — 一两个关键信号还没到位,7 天后系统会重测。"
    if decision == "KILL":
        if margin and margin < 15:
            return f"❌ 不碰 — 毛利仅 {margin:.0f}%,paid ads 根本养不起。"
        return "❌ 不碰 — 关键信号触发了一票否决,把精力放到机会更大的品上。"
    return "状态未知"


# ───────────────────────── End-to-end launch ─────────────────────────

def launch(product_id: int, dry_run: bool = False, status: str = "DRAFT") -> dict[str, Any]:
    """Complete launch pipeline. Returns a single object with everything user needs.

    Steps:
      1. Generate creative variants (Andromeda-distinct)
      2. Build full landing kit
      3. Clone to Shopify
      4. Bundle URLs + creative bodies into a single response
    """
    summary: dict[str, Any] = {
        "steps": {},
        "ok": False,
    }

    # Step 1: creative factory
    try:
        from .creative import factory
        # Only generate if we don't already have 3 distinct variants for this product
        with db.conn() as c:
            cnt = c.execute(
                "SELECT COUNT(DISTINCT entity_signature) FROM creative_variants WHERE product_id = ?",
                (product_id,),
            ).fetchone()[0]
        if cnt < 3:
            variants = factory.factory_for_product(product_id, count=3)
            summary["steps"]["creative"] = {"ok": True, "generated": len(variants),
                                            "andromeda_safe": factory.diversity_audit(product_id).get("is_andromeda_safe")}
        else:
            summary["steps"]["creative"] = {"ok": True, "reused": cnt, "andromeda_safe": cnt >= 3}
    except Exception as e:
        summary["steps"]["creative"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Step 2: landing kit
    try:
        from .outputs import landing_kit
        with db.conn() as c:
            row = c.execute(
                """SELECT p.*, d.test_plan_json
                   FROM products p LEFT JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
                   WHERE p.id = ?""",
                (product_id,),
            ).fetchone()
        if row:
            kit_path = landing_kit.generate_for_product(dict(row))
            summary["steps"]["landing_kit"] = {"ok": True, "path": str(kit_path),
                                                "files": [
                                                    "brief.md",
                                                    "creatives/hook_problem.md",
                                                    "creatives/hook_solution.md",
                                                    "creatives/hook_product.md",
                                                    "lp_blocks/hero.md",
                                                    "klaviyo/abandoned_cart.json",
                                                    "klaviyo/post_purchase.json",
                                                    "klaviyo/welcome_series.json",
                                                    "ads_manager_paste.json",
                                                    "attribution_survey.json",
                                                ]}
    except Exception as e:
        summary["steps"]["landing_kit"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Step 3: clone to Shopify
    try:
        from .shopify import auto_create
        clone_res = auto_create.clone(product_id, dry_run=dry_run, status=status)
        summary["steps"]["shopify"] = clone_res
        if clone_res.get("ok"):
            cl = clone_res.get("clone") or {}
            summary["shopify_admin_url"] = cl.get("shopify_admin_url")
            summary["shopify_storefront_url"] = cl.get("shopify_storefront_url")
    except RuntimeError as e:
        # Token missing — graceful
        summary["steps"]["shopify"] = {"ok": False, "error": str(e),
                                        "hint": "Set SHOPIFY_SHOP + SHOPIFY_ADMIN_ACCESS_TOKEN env"}
    except Exception as e:
        summary["steps"]["shopify"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Step 4: pull creative bodies for user to copy
    with db.conn() as c:
        cvs = c.execute(
            "SELECT variant_type, hook, body, cta FROM creative_variants WHERE product_id = ? ORDER BY id DESC LIMIT 3",
            (product_id,),
        ).fetchall()
    summary["creatives"] = [dict(r) for r in cvs]

    # Step 5: ads_manager_paste.json content (for direct copy)
    try:
        from pathlib import Path
        base = config.resolve_path("paths.landing_kits")
        ap = base / f"product_{product_id}" / "ads_manager_paste.json"
        if ap.exists():
            summary["ads_manager_paste"] = json.loads(ap.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Overall ok if shopify step succeeded
    summary["ok"] = summary["steps"].get("shopify", {}).get("ok", False)
    return summary
