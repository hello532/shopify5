#!/usr/bin/env python3
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_intelligence_loop import (
    AutoIntelConfig,
    AutoIntelRunLocked,
    build_selection_prompt_blueprint,
    build_selection_board,
    build_shopify_draft,
    enrich_top_products_with_gtrends_bulk,
    get_latest_run_summary,
    load_recent_products,
    normalize_domain,
    normalize_trend_record,
    product_family_key,
    read_latest_artifact,
    run_auto_intelligence,
    run_latest_auto_intel_trends,
    run_seven_day_new_product_judgement,
    safe_float,
    score_candidate,
    slugify,
    verify_missing_fb_signals,
)
from shopify_api_server import _build_v2_closed_loop, _v2_load_snapshot_products, _v2_vertical_sites_payload


def _verified_required_validation():
    links = [{"platform": "Proof", "url": "https://example.com/proof", "purpose": "test proof"}]
    slots = [
        {
            "key": key,
            "label": label,
            "status": "verified",
            "verified": True,
            "blocking": False,
            "evidence": f"{label} verified",
            "next_action": f"Keep monitoring {label}",
            "search_query": "portable relief tool",
            "search_links": links,
        }
        for key, label in [
            ("meta_ads", "Meta/Facebook Ads"),
            ("google_trends", "Google Trends"),
            ("social_ugc", "Reddit/X/YouTube/TikTok UGC"),
            ("marketplace", "Amazon/Google Shopping 市场证据"),
            ("supply_chain", "供应链/到手成本"),
            ("unit_economics", "单品经济性"),
            ("pdp_assets", "PDP素材/变体承接"),
            ("creative_pack", "原创素材包"),
            ("risk_compliance", "合规/IP/履约风险"),
        ]
    ]
    return {
        "version": "required-validation-v1-test",
        "keyword": "portable relief tool",
        "summary": {
            "status": "verified",
            "verified_count": len(slots),
            "required_count": len(slots),
            "pending_count": 0,
            "lead_only_count": 0,
            "failed_count": 0,
            "blocking_count": 0,
            "next_action": "All validation complete; keep monitoring.",
        },
        "slots": slots,
        "pending_tasks": [],
        "blocking_missing": [],
    }


class AutoIntelligenceLoopTests(unittest.TestCase):
    def test_normalize_domain_slugify_and_safe_float(self):
        self.assertEqual(normalize_domain("https://www.Demo-Store.com/products/a"), "demo-store.com")
        self.assertEqual(slugify("  Recovery Boots 2.0!!! "), "recovery-boots-2-0")
        self.assertEqual(safe_float("39.95"), 39.95)
        self.assertEqual(safe_float("bad", default=7), 7)

    def test_load_recent_products_reads_snapshot_and_filters_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor_dir = Path(tmp)
            self._write_snapshot(
                monitor_dir / "demo.com_snapshot.json",
                {
                    "domain": "https://www.demo.com",
                    "products": {
                        "recent-kit": {
                            "title": "Portable LED Cleaner Kit",
                            "handle": "recent-kit",
                            "price": 49,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "created_at": "2026-05-18T10:00:00Z",
                            "product_type": "Cleaner",
                            "vendor": "Demo",
                        },
                        "old-kit": {
                            "title": "Old Kit",
                            "handle": "old-kit",
                            "price": 49,
                            "updated_at": "2026-05-10T10:00:00Z",
                        },
                    },
                },
            )

            products, stats, warnings = load_recent_products(
                AutoIntelConfig(monitor_dir=monitor_dir, as_of_date="2026-05-19", lookback_days=2)
            )

        self.assertEqual(warnings, [])
        self.assertEqual(stats["snapshot_files"], 1)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["domain"], "demo.com")
        self.assertEqual(products[0]["handle"], "recent-kit")

    def test_score_candidate_rewards_fb_ads_and_shopify_fit(self):
        product = {
            "domain": "demo.com",
            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
            "handle": "pet-hair-vacuum",
            "price": 59,
            "product_type": "Cleaning Tool",
            "vendor": "Demo",
            "updated_at": "2026-05-19T10:00:00Z",
        }
        signal = {
            "ad_creative_count": 18,
            "orbit_score": 86,
            "ai_score": 82,
            "grade": "HOT",
            "brand": "Demo",
        }

        scored = score_candidate(product, signal)

        self.assertGreaterEqual(scored["score"], 82)
        self.assertEqual(scored["decision"], "立即测款")
        self.assertEqual(scored["shopify_fit"], "高")
        self.assertIn("短视频演示：变化/效果/使用场景", scored["conversion_angles"])
        self.assertEqual(scored["expert_assessment"]["archetype"], "problem_solver_demo")
        self.assertEqual(scored["expert_assessment"]["meta_launch_tier"], "test_now")
        self.assertIn("Before/after demo", scored["expert_assessment"]["landing_page_must_haves"])
        money = scored["money_decision"]
        self.assertEqual(money["follow_level"], "强跟")
        self.assertEqual(money["timing"], "正在验证")
        self.assertTrue(money["can_follow"])
        self.assertTrue(money["why_now"])
        self.assertTrue(money["why_will_sell"])
        self.assertIn("kill_rule", money["first_48h_test_plan"])
        self.assertIn("scale_rule", money["first_48h_test_plan"])

    def test_score_candidate_keeps_strong_shopify_only_products_observable(self):
        scored = score_candidate(
            {
                "domain": "comfort.example",
                "title": "Ultra Comfy Supportive Walking Shoes BOGO",
                "handle": "supportive-walking-shoes",
                "price": 79.95,
                "product_type": "Walking Shoes",
                "vendor": "Comfort",
                "updated_at": "2026-05-19T10:00:00Z",
            },
            {},
        )

        self.assertGreaterEqual(scored["score"], 60)
        self.assertEqual(scored["decision"], "加入观察")
        self.assertEqual(scored["fb_ads_fit"], "高")

    def test_score_candidate_uses_8000_exact_not_found_language(self):
        scored = score_candidate(
            {
                "domain": "comfort.example",
                "title": "Ultra Comfy Supportive Walking Shoes BOGO",
                "handle": "supportive-walking-shoes",
                "price": 79.95,
                "product_type": "Walking Shoes",
                "vendor": "Comfort",
                "updated_at": "2026-05-19T10:00:00Z",
            },
            {
                "ad_creative_count": 0,
                "fb_verified_by_8000": True,
                "fb_verification_status": "not_found",
                "fb_verification_source": "exact_brand_lookup",
                "fb_verification_endpoint": "/brands/{domain}",
            },
        )

        self.assertTrue(scored["fb_signal"]["fb_verified_by_8000"])
        self.assertEqual(scored["money_decision"]["evidence"]["fb_verification_status"], "not_found")
        self.assertTrue(any("8000 已按域名精确验证未命中" in item for item in scored["money_decision"]["why_may_fail"]))

    def test_score_candidate_flags_health_safety_claims_for_compliance_review(self):
        scored = score_candidate(
            {
                "domain": "mimibelt.com",
                "title": "Pregnancy Safety Belt (2 Pack)",
                "handle": "pregnancy-safety-belt-2pack",
                "price": 64.98,
                "product_type": "Pregnancy Safety",
                "vendor": "MimiBelt",
                "updated_at": "2026-05-19T10:00:00Z",
            },
            {},
        )

        self.assertEqual(scored["expert_assessment"]["archetype"], "claim_sensitive_health_safety")
        self.assertEqual(scored["expert_assessment"]["meta_launch_tier"], "compliance_review")
        self.assertTrue(any("健康/安全声明" in risk for risk in scored["risk_flags"]))

    def test_score_candidate_rejects_unlicensed_entertainment_merch(self):
        scored = score_candidate(
            {
                "domain": "album.example",
                "title": "ILLIT - MAMIHLAPINATAPAI PAW PAW VER SET",
                "handle": "illit-paw-paw-ver-set",
                "price": 42,
                "product_type": "K-Pop Official MD",
                "vendor": "AlbumShop",
                "updated_at": "2026-05-19T10:00:00Z",
            },
            {"ad_creative_count": 24, "orbit_score": 88, "ai_score": 86},
        )

        self.assertEqual(scored["decision"], "放弃")
        self.assertEqual(scored["expert_assessment"]["archetype"], "licensed_ip_merch_risk")
        self.assertTrue(any("授权/IP" in risk for risk in scored["risk_flags"]))
        self.assertFalse(scored["money_decision"]["can_follow"])

    def test_score_candidate_identifies_gift_identity_offer(self):
        scored = score_candidate(
            {
                "domain": "jewelry.example",
                "title": "Personalized Name Necklace Gift Set",
                "handle": "personalized-name-necklace",
                "price": 49,
                "product_type": "Necklace",
                "vendor": "Jewelry",
                "updated_at": "2026-05-19T10:00:00Z",
            },
            {"ad_creative_count": 6},
        )

        self.assertEqual(scored["expert_assessment"]["archetype"], "gift_identity")
        self.assertIn("Gift-recipient angle", scored["expert_assessment"]["creative_testing_plan"][0]["angle"])
        self.assertIn("Bundle or gift-box offer", scored["expert_assessment"]["offer_strategy"])

    def test_product_family_key_groups_offer_variants(self):
        first = product_family_key({"domain": "demo.com", "title": "2x Portable LED Cleaner Kit (BOGO)"})
        second = product_family_key({"domain": "demo.com", "title": "3x Portable LED Cleaner Kit Buy 1 Get 1 Free"})
        other_domain = product_family_key({"domain": "other.com", "title": "Portable LED Cleaner Kit"})

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_domain)

    def test_score_candidate_rejects_low_quality_placeholders(self):
        scored = score_candidate(
            {
                "domain": "demo.com",
                "title": "Charity Donation App Product",
                "handle": "donation",
                "price": 5,
                "product_type": "Donation",
                "vendor": "Demo",
            },
            {},
        )

        self.assertLess(scored["score"], 60)
        self.assertEqual(scored["decision"], "放弃")
        self.assertTrue(any("不可售" in risk for risk in scored["risk_flags"]))

    def test_build_shopify_draft_is_review_only(self):
        candidate = score_candidate(
            {
                "domain": "demo.com",
                "title": "Portable LED Cleaner Kit",
                "handle": "cleaner-kit",
                "price": 49,
                "product_type": "Cleaner",
                "vendor": "Demo",
            },
            {"ad_creative_count": 12, "orbit_score": 75, "grade": "SOLID"},
        )

        draft = build_shopify_draft(candidate, own_vendor="MY-STORE")

        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["vendor"], "MY-STORE")
        self.assertEqual(draft["source"]["domain"], "demo.com")
        self.assertIn("decision-test-now", draft["tags"])
        self.assertNotIn("decision-product", draft["tags"])
        self.assertTrue(any(field["key"] == "score" for field in draft["metafields"]))
        self.assertTrue(any(field["key"] == "expert_archetype" for field in draft["metafields"]))
        self.assertTrue(any(field["key"] == "meta_launch_tier" for field in draft["metafields"]))
        self.assertTrue(any(field["key"] == "follow_level" for field in draft["metafields"]))
        self.assertTrue(any(field["key"] == "timing" for field in draft["metafields"]))
        self.assertIn("money_decision", draft["source"])

    def test_build_selection_prompt_blueprint_documents_money_decision_schema(self):
        prompt = build_selection_prompt_blueprint()

        self.assertEqual(prompt["version"], "money-decision-v2-trends")
        self.assertIn("Meta/Facebook Ads", prompt["role"])
        self.assertIn("money_decision", prompt["output_schema"])
        self.assertIn("first_48h_test_plan", prompt["output_schema"]["money_decision"])
        self.assertIn("evidence.google_trends", prompt["output_schema"]["money_decision"])

    def test_normalize_trend_record_marks_real_trends_hot_and_proxy_separate(self):
        hot = normalize_trend_record("pet hair vacuum", {
            "keyword": "pet hair vacuum",
            "avg_score": 52,
            "last_30d": 65,
            "trend_dir": "up",
            "confidence": "high",
            "data_points": 52,
            "source": "direct",
        })
        proxy = normalize_trend_record("pet hair vacuum", {
            "keyword": "pet hair vacuum",
            "score": 74,
            "confidence": "suggest",
            "source": "google-suggest",
        })

        self.assertEqual(hot["trend_status"], "hot")
        self.assertTrue(hot["trend_verified"])
        self.assertEqual(hot["trend_data_quality"], "高")
        self.assertEqual(proxy["trend_status"], "proxy")
        self.assertFalse(proxy["trend_verified"])
        self.assertEqual(proxy["trend_data_quality"], "代理信号")

    def test_run_auto_intelligence_writes_artifacts_with_fake_fb_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "demo.com_snapshot.json",
                {
                    "domain": "demo.com",
                    "products": {
                        "cleaner-kit": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "cleaner-kit",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Demo",
                            "tags": ["pet", "cleaner", "kit"],
                        }
                    },
                },
            )

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=3,
                    min_score=60,
                ),
                fb_signals={
                    "demo.com": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                        "brand": "Demo",
                    }
                },
            )

            drafts = json.loads(Path(summary["artifacts"]["shopify_draft_products"]).read_text(encoding="utf-8"))
            briefs = json.loads(Path(summary["artifacts"]["creative_briefs"]).read_text(encoding="utf-8"))
            prompt = json.loads(Path(summary["artifacts"]["selection_prompt_blueprint"]).read_text(encoding="utf-8"))
            report = Path(summary["artifacts"]["top20_action_report"]).read_text(encoding="utf-8")
            run_summary_exists = Path(summary["artifacts"]["run_summary"]).exists()
            latest_run_exists = (Path(summary["output_dir"]).parent / "latest_run.json").exists()

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["stats"]["top_count"], 1)
        self.assertTrue(run_summary_exists)
        self.assertTrue(latest_run_exists)
        self.assertEqual(drafts["products"][0]["status"], "DRAFT")
        self.assertEqual(briefs["briefs"][0]["decision"], "立即测款")
        self.assertIn("money_decision", briefs["briefs"][0])
        self.assertEqual(prompt["version"], "money-decision-v2-trends")
        self.assertIn("selection_prompt_blueprint", summary)
        self.assertIn("Auto Intelligence Top Products", report)
        self.assertIn("Money decision", report)
        self.assertIn("Google Trends", report)

    def test_run_auto_intelligence_enriches_top_products_with_google_trends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "trend.example_snapshot.json",
                {
                    "domain": "trend.example",
                    "products": {
                        "pet-vacuum": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "pet-vacuum",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Trend",
                            "tags": ["pet", "cleaner", "kit"],
                        }
                    },
                },
            )

            def fake_trends(products, geo, max_keywords):
                return ([
                    normalize_trend_record("pet hair vacuum", {
                        "keyword": "pet hair vacuum",
                        "avg_score": 52,
                        "last_30d": 66,
                        "trend_dir": "up",
                        "confidence": "high",
                        "data_points": 52,
                        "source": "direct",
                    })
                ], {"requested_products": len(products), "matched_products": 1, "verified_products": 1, "proxy_products": 0, "hot_products": 1}, [])

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=3,
                    min_score=60,
                    enable_trends=True,
                    trend_top_n=7,
                ),
                fb_signals={
                    "trend.example": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                        "brand": "Trend",
                    }
                },
                trend_provider=fake_trends,
            )

        first = summary["top_products"][0]
        trends = first["money_decision"]["evidence"]["google_trends"]
        self.assertEqual(summary["stats"]["trend_checked_products"], 1)
        self.assertEqual(summary["stats"]["trend_verified_products"], 1)
        self.assertEqual(summary["stats"]["trend_hot_products"], 1)
        self.assertEqual(first["trend_signal"]["trend_status"], "hot")
        self.assertTrue(trends["verified"])
        self.assertEqual(trends["query"], "pet hair vacuum")

    def test_enrich_top_products_with_gtrends_bulk_reads_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            gtrends_dir = Path(tmp)
            conn = sqlite3.connect(gtrends_dir / "cache.db")
            conn.execute("""
                CREATE TABLE trends_v3 (
                    keyword TEXT NOT NULL,
                    geo TEXT NOT NULL DEFAULT '',
                    timeframe TEXT NOT NULL DEFAULT 'today 12-m',
                    avg_score REAL,
                    median REAL,
                    last_30d REAL,
                    peak INTEGER,
                    peak_date TEXT,
                    trend_dir TEXT,
                    confidence TEXT,
                    volatility REAL,
                    seasonal_peak_month INTEGER,
                    yoy_change REAL,
                    data_points INTEGER,
                    source TEXT,
                    ts REAL,
                    PRIMARY KEY (keyword, geo, timeframe)
                )
            """)
            conn.execute(
                """
                INSERT INTO trends_v3 (
                    keyword, geo, timeframe, avg_score, median, last_30d,
                    peak, peak_date, trend_dir, confidence, volatility,
                    seasonal_peak_month, yoy_change, data_points, source, ts
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "pet hair vacuum",
                    "US",
                    "today 12-m",
                    52,
                    49,
                    66,
                    100,
                    "2026-05-01",
                    "up",
                    "high",
                    18.5,
                    None,
                    22.0,
                    52,
                    "pytrends",
                    time.time(),
                ),
            )
            conn.commit()
            conn.close()

            with patch.dict(os.environ, {
                "AUTO_INTEL_GTRENDS_BULK_DIR": str(gtrends_dir),
                "AUTO_INTEL_GTRENDS_DIR": str(gtrends_dir),
            }):
                records, stats, warnings = enrich_top_products_with_gtrends_bulk(
                    [{"title": "Pet Hair Vacuum Cleaner Kit", "product_type": "Cleaning Tool"}],
                    geo="US",
                    max_keywords=1,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(stats["provider"], "gtrends_bulk")
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["verified_products"], 1)
        self.assertEqual(records[0]["trend_query"], "pet hair vacuum")
        self.assertEqual(records[0]["trend_status"], "hot")

    def test_run_latest_auto_intel_trends_updates_latest_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "trend.example_snapshot.json",
                {
                    "domain": "trend.example",
                    "products": {
                        "pet-vacuum": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "pet-vacuum",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Trend",
                        }
                    },
                },
            )
            run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=3,
                    min_score=60,
                    enable_trends=False,
                ),
                fb_signals={
                    "trend.example": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                        "brand": "Trend",
                    }
                },
            )

            def fake_trends(products, geo, max_keywords):
                return ([
                    normalize_trend_record("pet hair vacuum", {
                        "keyword": "pet hair vacuum",
                        "avg_score": 52,
                        "last_30d": 66,
                        "trend_dir": "up",
                        "confidence": "high",
                        "data_points": 52,
                        "source": "pytrends",
                    })
                ], {"requested_products": 1, "matched_products": 1, "verified_products": 1, "proxy_products": 0, "hot_products": 1, "provider": "gtrends_bulk"}, [])

            result = run_latest_auto_intel_trends(output_root=output_root, trend_provider=fake_trends)
            latest = get_latest_run_summary(output_root)
            report = Path(result["artifacts"]["top20_action_report"]).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["trend_checked_products"], 1)
        self.assertEqual(result["stats"]["trend_verified_products"], 1)
        self.assertEqual(latest["top_products"][0]["trend_signal"]["trend_status"], "hot")
        self.assertIn("pet hair vacuum", report)

    def test_run_seven_day_new_product_judgement_filters_and_ranks_new_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "seven.example_snapshot.json",
                {
                    "domain": "seven.example",
                    "products": {
                        "new-vacuum": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "new-vacuum",
                            "price": 59,
                            "created_at": "2026-05-18T10:00:00Z",
                            "published_at": "2026-05-18T10:00:00Z",
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Seven",
                            "images": ["a", "b"],
                            "variants": [{"id": 1}],
                        },
                        "old-vacuum": {
                            "title": "Old Pet Hair Vacuum Cleaner Kit",
                            "handle": "old-vacuum",
                            "price": 59,
                            "created_at": "2026-05-01T10:00:00Z",
                            "published_at": "2026-05-01T10:00:00Z",
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Seven",
                        },
                    },
                },
            )

            def fake_trends(products, geo, max_keywords):
                return ([
                    normalize_trend_record("pet hair vacuum", {
                        "keyword": "pet hair vacuum",
                        "avg_score": 52,
                        "last_30d": 66,
                        "trend_dir": "up",
                        "confidence": "high",
                        "data_points": 52,
                        "source": "pytrends",
                    })
                ], {"requested_products": 1, "matched_products": 1, "verified_products": 1, "proxy_products": 0, "hot_products": 1, "provider": "gtrends_bulk"}, [])

            result = run_seven_day_new_product_judgement(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    fb_exact_verify_limit=0,
                ),
                limit=10,
                trend_top_n=1,
                fb_signals={
                    "seven.example": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                        "brand": "Seven",
                    }
                },
                trend_provider=fake_trends,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method_version"], "seven-day-new-products-v2")
        self.assertEqual(result["stats"]["new_products"], 1)
        self.assertEqual(result["products"][0]["handle"], "new-vacuum")
        self.assertEqual(result["products"][0]["seven_day_judgement"]["decision"], "立即跟进")
        self.assertGreaterEqual(result["products"][0]["seven_day_judgement"]["follow_priority_score"], 82)

    def test_run_auto_intelligence_exact_verifies_domains_missing_from_bulk_8000(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "exact.example_snapshot.json",
                {
                    "domain": "exact.example",
                    "products": {
                        "pet-cleaner": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "pet-cleaner",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Exact",
                            "tags": ["pet", "cleaner", "kit"],
                        }
                    },
                },
            )

            def empty_bulk(_config):
                return {}, []

            exact_signal = {
                "domain": "exact.example",
                "brand": "Exact",
                "ad_creative_count": 9,
                "appearance_count": 9,
                "orbit_score": 77,
                "ai_score": 0,
                "grade": "",
                "fb_verified_by_8000": True,
                "fb_verification_status": "matched",
                "fb_verification_source": "exact_brand_lookup",
                "fb_verification_endpoint": "/brands/{domain}",
            }
            with patch("auto_intelligence_loop.fetch_exact_fb_signal", return_value=(exact_signal, "")) as fetch_one:
                summary = run_auto_intelligence(
                    AutoIntelConfig(
                        monitor_dir=monitor_dir,
                        output_root=output_root,
                        as_of_date="2026-05-19",
                        limit=3,
                        min_score=60,
                    ),
                    signal_provider=empty_bulk,
                )

        fetch_one.assert_called_once()
        self.assertEqual(summary["stats"]["fb_exact_verify_checked"], 1)
        self.assertEqual(summary["stats"]["fb_exact_verify_matched"], 1)
        first = summary["top_products"][0]
        self.assertEqual(first["fb_signal"]["fb_verification_source"], "exact_brand_lookup")
        self.assertEqual(first["money_decision"]["evidence"]["fb_verification_status"], "matched")

    def test_verify_missing_fb_signals_skips_domains_already_in_bulk(self):
        signals = {
            "demo.com": {
                "domain": "demo.com",
                "ad_creative_count": 12,
                "fb_verification_status": "matched",
                "fb_verification_source": "bulk_brands",
            }
        }
        with patch("auto_intelligence_loop.fetch_exact_fb_signal") as fetch_one:
            stats, warnings = verify_missing_fb_signals(
                AutoIntelConfig(fb_exact_verify_limit=10),
                [{"domain": "demo.com", "title": "Demo"}],
                signals,
            )

        fetch_one.assert_not_called()
        self.assertEqual(warnings, [])
        self.assertEqual(stats["checked"], 0)

    def test_latest_summary_handles_corrupted_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            (output_root / "latest_run.json").write_text("{bad json", encoding="utf-8")

            summary = get_latest_run_summary(output_root)

        self.assertFalse(summary["ok"])
        self.assertIn("Invalid JSON", summary["error"])

    def test_read_latest_artifact_handles_corrupted_json_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            artifact = output_root / "board.json"
            artifact.write_text("{bad json", encoding="utf-8")
            (output_root / "latest_run.json").write_text(
                json.dumps({"ok": True, "artifacts": {"selection_board": str(artifact)}}),
                encoding="utf-8",
            )

            result = read_latest_artifact("selection_board", output_root)

        self.assertFalse(result["ok"])
        self.assertIn("Invalid JSON", result["error"])

    def test_run_auto_intelligence_rejects_active_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            output_root.mkdir()
            (output_root / ".auto_intelligence.lock").write_text("{}", encoding="utf-8")

            with self.assertRaises(AutoIntelRunLocked):
                run_auto_intelligence(
                    AutoIntelConfig(
                        monitor_dir=monitor_dir,
                        output_root=output_root,
                        as_of_date="2026-05-19",
                    ),
                    fb_signals={},
                )

    def test_run_auto_intelligence_removes_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            output_root.mkdir()
            lock = output_root / ".auto_intelligence.lock"
            lock.write_text("{}", encoding="utf-8")
            old_ts = 1
            os.utime(lock, (old_ts, old_ts))
            self._write_snapshot(
                monitor_dir / "demo.com_snapshot.json",
                {
                    "domain": "demo.com",
                    "products": {
                        "cleaner-kit": {
                            "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "cleaner-kit",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Demo",
                        }
                    },
                },
            )

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=3,
                    min_score=60,
                ),
                fb_signals={
                    "demo.com": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                    }
                },
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["stats"]["top_count"], 1)

    def test_run_auto_intelligence_deduplicates_family_and_writes_selection_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "demo.com_snapshot.json",
                {
                    "domain": "demo.com",
                    "products": {
                        "cleaner-kit-2x": {
                            "title": "2x Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "cleaner-kit-2x",
                            "price": 59,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Demo",
                            "tags": ["pet", "cleaner", "kit"],
                        },
                        "cleaner-kit-3x": {
                            "title": "3x Portable LED Pet Hair Vacuum Cleaner Kit",
                            "handle": "cleaner-kit-3x",
                            "price": 79,
                            "updated_at": "2026-05-19T11:00:00Z",
                            "product_type": "Cleaning Tool",
                            "vendor": "Demo",
                            "tags": ["pet", "cleaner", "kit"],
                        },
                    },
                },
            )

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=10,
                    min_score=60,
                    max_per_family=1,
                ),
                fb_signals={
                    "demo.com": {
                        "ad_creative_count": 18,
                        "orbit_score": 86,
                        "ai_score": 82,
                        "grade": "HOT",
                        "brand": "Demo",
                    }
                },
            )
            board = json.loads(Path(summary["artifacts"]["selection_board"]).read_text(encoding="utf-8"))

        self.assertEqual(summary["stats"]["qualified_candidates"], 2)
        self.assertEqual(summary["stats"]["top_count"], 1)
        self.assertEqual(summary["stats"]["duplicate_suppressed_count"], 1)
        self.assertEqual(board["summary"]["go_queue_count"], 1)
        self.assertEqual(board["summary"]["duplicate_suppressed_count"], 1)

    def test_run_auto_intelligence_puts_soft_near_misses_into_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "comfort.example_snapshot.json",
                {
                    "domain": "comfort.example",
                    "products": {
                        "walking-shoes": {
                            "title": "Supportive Walking Shoes",
                            "handle": "walking-shoes",
                            "price": 79,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Shoes",
                            "vendor": "Comfort",
                            "tags": ["comfort", "walking", "shoes"],
                        }
                    },
                },
            )

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=10,
                    min_score=70,
                    research_min_score=45,
                ),
                fb_signals={},
            )

        self.assertEqual(summary["stats"]["top_count"], 0)
        self.assertEqual(summary["stats"]["watchlist_count"], 1)
        self.assertEqual(summary["watchlist"][0]["decision"], "观察池")

    def test_run_auto_intelligence_applies_domain_limit_to_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor_dir = root / "monitor"
            output_root = root / "auto_intel"
            monitor_dir.mkdir()
            self._write_snapshot(
                monitor_dir / "comfort.example_snapshot.json",
                {
                    "domain": "comfort.example",
                    "products": {
                        f"walking-shoes-{idx}": {
                            "title": f"Supportive Walking Shoes Style {idx}",
                            "handle": f"walking-shoes-{idx}",
                            "price": 79 + idx,
                            "updated_at": "2026-05-19T10:00:00Z",
                            "product_type": "Shoes",
                            "vendor": "Comfort",
                            "tags": ["comfort", "walking", "shoes"],
                        }
                        for idx in range(4)
                    },
                },
            )

            summary = run_auto_intelligence(
                AutoIntelConfig(
                    monitor_dir=monitor_dir,
                    output_root=output_root,
                    as_of_date="2026-05-19",
                    limit=10,
                    min_score=70,
                    research_min_score=45,
                    max_per_domain=2,
                    max_per_family=5,
                ),
                fb_signals={},
            )

        self.assertEqual(summary["stats"]["watchlist_count"], 2)
        self.assertLessEqual(
            sum(1 for item in summary["watchlist"] if item["domain"] == "comfort.example"),
            2,
        )

    def test_build_selection_board_keeps_watchlist_and_kill_list_separate(self):
        top = [
            {
                "title": "Portable LED Pet Hair Vacuum Cleaner Kit",
                "domain": "demo.com",
                "handle": "cleaner-kit",
                "product_url": "https://demo.com/products/cleaner-kit",
                "score": 88,
                "decision": "立即测款",
                "price": 59,
                "product_type": "Cleaning Tool",
                "fb_signal": {"ad_creative_count": 18},
                "reasons": ["FB 素材数 18，验证充分且未过饱和 (+22)"],
                "risk_flags": [],
                "conversion_angles": ["短视频演示：变化/效果/使用场景"],
            }
        ]
        watchlist = [
            {
                "title": "Supportive Walking Shoes",
                "domain": "comfort.example",
                "handle": "walking-shoes",
                "product_url": "https://comfort.example/products/walking-shoes",
                "score": 56,
                "decision": "观察池",
                "price": 79,
                "product_type": "Shoes",
                "fb_signal": {"ad_creative_count": 0},
                "reasons": ["价格带适合冷启动"],
                "risk_flags": [],
                "conversion_angles": ["痛点前置：问题 -> 演示 -> 结果"],
            }
        ]
        rejected = [
            {
                "title": "Charity Donation App Product",
                "domain": "demo.com",
                "score": 12,
                "decision": "放弃",
                "risk_flags": ["疑似捐赠、App 占位或不可售商品"],
                "reasons": [],
            }
        ]

        board = build_selection_board(top, watchlist, rejected, duplicate_suppressed=2)

        self.assertEqual(board["summary"]["go_queue_count"], 1)
        self.assertEqual(board["summary"]["watchlist_count"], 1)
        self.assertEqual(board["summary"]["kill_list_count"], 1)
        self.assertEqual(board["go_queue"][0]["next_step"], "建 1 个 Shopify 草稿 + 3 条原创素材脚本，先小预算测 48h")
        self.assertEqual(board["watchlist"][0]["next_step"], "先补 FB/TikTok/Google Trends 验证，不进草稿")
        self.assertIn("operator_note", board["go_queue"][0])
        self.assertIn("landing_page_must_haves", board["go_queue"][0])
        self.assertIn("money_decision", board["go_queue"][0])
        self.assertEqual(board["go_queue"][0]["follow_level"], "强跟")
        self.assertIn("first_48h_test_plan", board["go_queue"][0])
        self.assertIn("money_decision", board["kill_list"][0])

    def test_vertical_site_aggregation_loads_snapshot_sites_without_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor_dir = Path(tmp)
            old_monitor_dir = monitor_dir
            self._write_snapshot(
                monitor_dir / "micro.example_snapshot.json",
                {
                    "domain": "micro.example",
                    "product_count": 6,
                    "products": {
                        f"micro-{idx}": {
                            "title": f"Micro Product {idx}",
                            "handle": f"micro-{idx}",
                            "price": 20 + idx,
                            "product_type": "Niche",
                            "vendor": "MicroCo",
                        }
                        for idx in range(6)
                    },
                },
            )
            self._write_snapshot(
                monitor_dir / "big.example_snapshot.json",
                {
                    "domain": "big.example",
                    "product_count": 24,
                    "products": {
                        f"big-{idx}": {
                            "title": f"Big Product {idx}",
                            "handle": f"big-{idx}",
                            "price": 100 + idx,
                            "product_type": "Mass",
                            "vendor": "BigCo",
                        }
                        for idx in range(24)
                    },
                },
            )

            import shopify_api_server as server

            prev_monitor_dir = server.MONITOR_DIR
            prev_cache = server._v2_vertical_sites_cache
            prev_sig = server._v2_vertical_sites_cache_signature
            try:
                server.MONITOR_DIR = monitor_dir
                server._v2_vertical_sites_cache = None
                server._v2_vertical_sites_cache_signature = ""

                payload = _v2_vertical_sites_payload()
            finally:
                server.MONITOR_DIR = prev_monitor_dir
                server._v2_vertical_sites_cache = prev_cache
                server._v2_vertical_sites_cache_signature = prev_sig

        sites = payload["vertical_micro_sites"]
        domains = [site["domain"] for site in sites]
        self.assertIn("micro.example", domains)
        self.assertNotIn("big.example", domains)
        self.assertEqual(payload["vertical_micro_sites_total"], len(sites))
        self.assertEqual(len(sites), 1)

    def test_vertical_site_aggregation_keeps_all_matching_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor_dir = Path(tmp)
            for idx in range(31):
                self._write_snapshot(
                    monitor_dir / f"site{idx}.example_snapshot.json",
                    {
                        "domain": f"site{idx}.example",
                        "product_count": 5,
                        "products": {
                            f"p{idx}-{j}": {
                                "title": f"Site {idx} Product {j}",
                                "handle": f"p{idx}-{j}",
                                "price": 10 + j,
                                "product_type": "Niche",
                                "vendor": f"V{idx}",
                            }
                            for j in range(5)
                        },
                    },
                )

            import shopify_api_server as server

            prev_monitor_dir = server.MONITOR_DIR
            prev_cache = server._v2_vertical_sites_cache
            prev_sig = server._v2_vertical_sites_cache_signature
            try:
                server.MONITOR_DIR = monitor_dir
                server._v2_vertical_sites_cache = None
                server._v2_vertical_sites_cache_signature = ""

                payload = _v2_vertical_sites_payload()
            finally:
                server.MONITOR_DIR = prev_monitor_dir
                server._v2_vertical_sites_cache = prev_cache
                server._v2_vertical_sites_cache_signature = prev_sig

        self.assertEqual(payload["vertical_micro_sites_total"], 31)
        self.assertEqual(len(payload["vertical_micro_sites"]), 31)
        self.assertEqual({site["domain"] for site in payload["vertical_micro_sites"]}, {f"site{idx}.example" for idx in range(31)})

    def test_closed_loop_marks_important_ads_verified_item_as_follow_now(self):
        latest = {
            "ok": True,
            "generated_at": "2026-05-23T12:00:00",
            "actions": [
                {
                    "title": "Portable Relief Tool",
                    "domain": "demo.example",
                    "product_url": "https://demo.example/products/tool",
                    "price": 59,
                    "score": 84,
                    "follow_priority_score": 88,
                    "validation": {
                        "fb_ads": {
                            "status": "matched",
                            "creative_count": 12,
                            "orbit_score": 82,
                            "ai_score": 70,
                        },
                        "google_trends": {"status": "hot", "score": 75},
                    },
                    "operator_gate": {"economics_ready": True, "blockers": []},
                    "unit_economics": {"economics_ready": True, "break_even_cpa": 25, "target_cpa": 18},
                    "required_validation": _verified_required_validation(),
                    "pareto": {
                        "lane": "copy_now",
                        "decision": "验真通过，才可跟品拆解",
                        "priority": 92,
                        "real_validation_count": 2,
                        "required_validation_count": 2,
                        "one_action": "建草稿页",
                        "launch_triage": {
                            "level": "launch_now",
                            "label": "立即上架",
                            "can_upload_draft": True,
                        },
                    },
                },
                {
                    "title": "Unverified Product",
                    "domain": "watch.example",
                    "price": 49,
                    "score": 62,
                    "follow_priority_score": 64,
                    "validation": {"fb_ads": {"creative_count": 0}},
                    "operator_gate": {"economics_ready": True, "blockers": []},
                    "unit_economics": {"economics_ready": True},
                    "pareto": {
                        "lane": "verify_first",
                        "priority": 61,
                        "real_validation_count": 0,
                        "required_validation_count": 2,
                    },
                },
            ],
        }

        payload = _build_v2_closed_loop(latest, cycles=3, limit=50)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["important_20"], 1)
        self.assertEqual(payload["summary"]["important_20_with_ads"], 1)
        self.assertEqual(payload["summary"]["follow_now"], 1)
        self.assertEqual(payload["rounds"][0]["kept"], 1)
        self.assertEqual(payload["rounds"][1]["kept"], 1)
        self.assertEqual(payload["rounds"][2]["kept"], 1)
        self.assertEqual(payload["items"][0]["follow_status"], "马上跟")
        self.assertTrue(payload["items"][0]["draft_ready"])

    def test_closed_loop_requires_draft_ready_before_follow_now(self):
        latest = {
            "ok": True,
            "generated_at": "2026-05-23T12:00:00",
            "actions": [
                {
                    "title": "Ad Proven But Missing Supply",
                    "domain": "gap.example",
                    "product_url": "https://gap.example/products/tool",
                    "price": 59,
                    "score": 84,
                    "follow_priority_score": 88,
                    "validation": {
                        "fb_ads": {
                            "status": "matched",
                            "creative_count": 12,
                            "orbit_score": 82,
                            "ai_score": 70,
                        },
                    },
                    "operator_gate": {"economics_ready": True, "blockers": []},
                    "unit_economics": {"economics_ready": True, "break_even_cpa": 25, "target_cpa": 18},
                    "pareto": {
                        "lane": "copy_now",
                        "priority": 92,
                        "real_validation_count": 2,
                        "required_validation_count": 2,
                        "launch_triage": {
                            "level": "observe",
                            "label": "观察跟进",
                            "can_upload_draft": False,
                            "one_line": "供应链/市场参考未过",
                        },
                    },
                },
            ],
        }

        payload = _build_v2_closed_loop(latest, cycles=3, limit=50)

        self.assertEqual(payload["summary"]["follow_now"], 0)
        self.assertEqual(payload["summary"]["teardown_first"], 1)
        self.assertEqual(payload["rounds"][2]["kept"], 0)
        self.assertEqual(payload["items"][0]["follow_status"], "先拆素材")
        self.assertFalse(payload["items"][0]["draft_ready"])

    def test_closed_loop_validation_matrix_never_leaves_required_slots_empty(self):
        latest = {
            "ok": True,
            "generated_at": "2026-05-23T12:00:00",
            "actions": [
                {
                    "title": "Unverified Relief Tool",
                    "domain": "needs-proof.example",
                    "product_url": "https://needs-proof.example/products/tool",
                    "price": 59,
                    "score": 84,
                    "follow_priority_score": 88,
                    "validation": {
                        "fb_ads": {"status": "not_found", "creative_count": 0},
                        "google_trends": {"status": "unverified", "score": 0},
                    },
                    "operator_gate": {
                        "economics_ready": True,
                        "assets_ready": False,
                        "image_count": 0,
                        "variant_count": 0,
                        "blockers": [],
                    },
                    "unit_economics": {
                        "economics_ready": True,
                        "requires_real_cogs": True,
                        "break_even_cpa": 25,
                        "target_cpa": 18,
                    },
                    "pareto": {
                        "lane": "verify_first",
                        "priority": 92,
                        "real_validation_count": 0,
                        "required_validation_count": 2,
                        "launch_triage": {
                            "level": "observe",
                            "label": "观察跟进",
                            "can_upload_draft": False,
                        },
                    },
                },
            ],
        }

        payload = _build_v2_closed_loop(latest, cycles=3, limit=50)
        item = payload["items"][0]
        matrix = item["required_validation"]

        self.assertEqual(item["follow_status"], "补证观察")
        self.assertEqual(matrix["summary"]["required_count"], 9)
        self.assertGreater(matrix["summary"]["blocking_count"], 0)
        self.assertTrue(item["validation_tasks"])
        for slot in matrix["slots"]:
            self.assertIn(slot["status"], {"verified", "pending", "failed", "lead_only"})
            self.assertTrue(slot["key"])
            self.assertTrue(slot["label"])
            self.assertTrue(slot["evidence"])
            if slot["status"] != "verified":
                self.assertTrue(slot["why_not_verified"])
                self.assertTrue(slot["next_action"])
                self.assertTrue(slot["search_links"] or slot["fallback_links"])

    def _write_snapshot(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
