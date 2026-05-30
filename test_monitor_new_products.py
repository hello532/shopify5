#!/usr/bin/env python3
import unittest

from monitor_new_products import (
    build_daily_checklist,
    build_task_queues,
    build_trend_queries,
    build_opportunity_board,
    classify_trend,
    enrich_product_actionability,
    detect_catalog_changes,
    extract_image_urls,
    normalize_query,
    parse_sitemap_locs,
    trend_summary,
)


class MonitorNewProductsTests(unittest.TestCase):
    def test_normalize_query_strips_noise(self):
        self.assertEqual(
            normalize_query("  QuietPro-Recovery_Boots!!!  "),
            "QuietPro Recovery Boots",
        )

    def test_build_trend_queries_prefers_clean_product_phrases(self):
        product = {
            "title": "QuietPro Recovery Boots",
            "vendor": "QuietPro",
            "product_type": "Recovery Boots",
            "handle": "quietpro-recovery-boots",
        }
        queries = build_trend_queries(product)
        self.assertIn("QuietPro Recovery Boots", queries)
        self.assertIn("Recovery Boots", queries)

    def test_classify_trend_marks_hot_and_unverified(self):
        self.assertEqual(classify_trend(52, 61, 14, 4), ("hot", True))
        self.assertEqual(classify_trend(0, 0, 0, 0), ("unverified", False))

    def test_trend_summary_counts_verified_products(self):
        products = [
            {"title": "A", "trend_status": "hot", "trend_verified": True, "trend_interest_avg": 60, "trend_momentum_pct": 12},
            {"title": "B", "trend_status": "watch", "trend_verified": True, "trend_interest_avg": 35, "trend_momentum_pct": 3},
            {"title": "C", "trend_status": "weak", "trend_verified": False, "trend_interest_avg": 8, "trend_momentum_pct": -2},
        ]
        summary = trend_summary(products)
        self.assertEqual(summary["verified_count"], 2)
        self.assertEqual(summary["status_breakdown"]["hot"], 1)
        self.assertEqual(summary["top_trending"][0]["title"], "A")

    def test_parse_sitemap_locs_reads_loc_and_lastmod(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://demo.com/products/recovery-boots</loc>
            <lastmod>2026-05-15</lastmod>
          </url>
        </urlset>"""
        parsed = parse_sitemap_locs(xml)
        self.assertEqual(parsed[0]["loc"], "https://demo.com/products/recovery-boots")
        self.assertEqual(parsed[0]["lastmod"], "2026-05-15")

    def test_extract_image_urls_supports_dict_and_string(self):
        images = [{"src": "a.jpg"}, "b.jpg", {"url": "c.jpg"}]
        self.assertEqual(extract_image_urls(images), ["a.jpg", "b.jpg", "c.jpg"])

    def test_detect_catalog_changes_finds_price_and_copy_events(self):
        old_products = {
            "1": {
                "domain": "demo.com",
                "product_id": "1",
                "title": "Recovery Boots",
                "handle": "recovery-boots",
                "price_min": 199.0,
                "price_max": 199.0,
                "compare_at_max": 0.0,
                "image_url": "old.jpg",
                "body_hash": "aaa",
                "variants_count": 2,
                "status": "published",
                "available": True,
                "url": "https://demo.com/products/recovery-boots",
            }
        }
        current_products = {
            "1": {
                "domain": "demo.com",
                "product_id": "1",
                "title": "Recovery Boots 2.0",
                "handle": "recovery-boots",
                "price_min": 179.0,
                "price_max": 199.0,
                "compare_at_max": 249.0,
                "image_url": "new.jpg",
                "body_hash": "bbb",
                "variants_count": 3,
                "status": "published",
                "available": False,
                "url": "https://demo.com/products/recovery-boots",
            }
        }
        changes = detect_catalog_changes(old_products, current_products)
        self.assertEqual(changes["counts"]["price"], 1)
        self.assertEqual(changes["counts"]["image"], 1)
        self.assertEqual(changes["counts"]["copy"], 1)
        self.assertEqual(changes["counts"]["variant"], 1)
        self.assertEqual(changes["counts"]["availability"], 1)

    def test_enrich_product_actionability_scores_promising_product(self):
        product = {
            "domain": "demo.com",
            "title": "Recovery Boots",
            "product_type": "Recovery Boots",
            "vendor": "Demo",
            "price_min": 89.0,
            "price_max": 89.0,
            "compare_at_max": 129.0,
            "variants_count": 4,
            "images_count": 5,
            "available": True,
            "source": "sitemap",
            "trend_status": "watch",
            "trend_verified": True,
            "trend_interest_avg": 42.0,
            "trend_momentum_pct": 11.0,
        }
        enriched = enrich_product_actionability(product)
        self.assertGreaterEqual(enriched["action_score"], 60)
        self.assertIn(enriched["action_bucket"], {"scale_now", "test_now"})
        self.assertTrue(enriched["meta_brand_url"].startswith("https://www.facebook.com/ads/library/"))

    def test_build_opportunity_board_sorts_by_action_score(self):
        products = [
            {"title": "A", "action_score": 55, "trend_verified": True, "trend_interest_avg": 25},
            {"title": "B", "action_score": 82, "trend_verified": True, "trend_interest_avg": 40},
        ]
        changes = [{"domain": "x.com", "counts": {"price": 3, "copy": 2, "new": 1}}]
        board = build_opportunity_board(products, changes, limit=10)
        self.assertEqual(board["top_products"][0]["title"], "B")
        self.assertEqual(board["hot_domains"][0]["domain"], "x.com")

    def test_build_task_queues_routes_high_priority_product(self):
        product = {
            "domain": "demo.com",
            "product_id": "1",
            "title": "Recovery Boots",
            "handle": "recovery-boots",
            "action_score": 84,
            "action_bucket": "scale_now",
            "action_label": "立即抄作业",
            "action_reasons": ["趋势热度强", "价格带适合冷启动"],
            "primary_channel": "Meta UGC/Testimonial + Google Search",
            "offer_strategy": "做 hero bundle + 单品对照，首页突出节省金额",
            "trend_status": "hot",
            "trend_verified": True,
            "meta_brand_url": "https://www.facebook.com/ads/library/?q=demo",
            "meta_domain_url": "https://www.facebook.com/ads/library/?q=demo.com",
            "google_ads_transparency_url": "https://adstransparency.google.com/?region=US&domain=demo.com",
            "tiktok_creative_center_url": "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en",
            "tiktok_query": "recovery boots",
        }
        queues = build_task_queues([product], [], limit_per_queue=10)
        self.assertEqual(len(queues["ad_research_queue"]), 1)
        self.assertEqual(len(queues["creative_queue"]), 1)
        self.assertEqual(queues["ad_research_queue"][0]["priority"], "P1")
        self.assertIn("先看 Meta/TikTok 广告素材", queues["ad_research_queue"][0]["next_step"])

    def test_build_daily_checklist_collects_top_tasks(self):
        product = {
            "title": "Recovery Boots",
            "domain": "demo.com",
            "action_label": "立即抄作业",
            "action_score": 84,
        }
        board = {"top_products": [product], "hot_domains": [{"domain": "demo.com", "competitive_heat_score": 70, "counts": {"price": 2}}]}
        queues = {
            "ad_research_queue": [{"title": "Recovery Boots", "priority": "P1", "next_step": "查广告", "why_now": "趋势热度强", "timebox_min": 20, "expected_impact": "high"}],
            "research_queue": [],
            "offer_test_queue": [],
            "creative_queue": [],
            "landing_page_queue": [],
            "watch_queue": [],
        }
        checklist = build_daily_checklist(board, queues)
        self.assertEqual(checklist["today_focus"][0], "Recovery Boots")
        self.assertEqual(checklist["morning_tasks"][0]["title"], "Recovery Boots")


if __name__ == "__main__":
    unittest.main()
