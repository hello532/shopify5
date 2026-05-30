#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from shopify_precision_suite import (
    audit_scrape_precision,
    product_quality,
    read_cached_scrape_precision,
)


class ShopifyPrecisionSuiteTests(unittest.TestCase):
    def test_product_quality_penalizes_missing_commercial_fields(self):
        quality = product_quality({
            "title": "Kit",
            "price": "",
            "images": [],
            "body_html": "",
        })

        self.assertLess(quality["score"], 50)
        self.assertIn("title_missing_or_too_short", quality["errors"])
        self.assertIn("price_missing", quality["errors"])
        self.assertIn("image_missing", quality["errors"])

    def test_product_quality_uses_extracted_count_fields(self):
        quality = product_quality({
            "title": "Premium Desk Posture Support Cushion",
            "price": "49.95",
            "image_count": 4,
            "variant_count": 2,
            "summary": "A detailed product summary with enough commercial detail for a Shopify product page and buying decision.",
            "handle": "premium-desk-posture-support-cushion",
            "tags": ["office", "ergonomic"],
        })

        self.assertEqual(quality["image_count"], 4)
        self.assertEqual(quality["variant_count"], 2)
        self.assertNotIn("image_missing", quality["errors"])
        self.assertNotIn("variant_data_missing", quality["warnings"])
        self.assertGreaterEqual(quality["score"], 90)

    def test_audit_scrape_precision_ranks_low_quality_snapshot_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_snapshot(root / "good.example_snapshot.json", {
                "domain": "good.example",
                "last_check": "2999-01-01T00:00:00",
                "products": {
                    "hero": {
                        "title": "Premium Pet Hair Vacuum Cleaner Kit",
                        "handle": "hero",
                        "price": "59.95",
                        "images": [{"src": "a.jpg"}, {"src": "b.jpg"}, {"src": "c.jpg"}],
                        "body_html": "A detailed product description with enough commercial detail for a Shopify product page.",
                        "variants": [{"price": "59.95"}],
                        "tags": ["pet", "cleaning"],
                    }
                },
            })
            self._write_snapshot(root / "bad.example_snapshot.json", {
                "domain": "bad.example",
                "last_check": "2020-01-01T00:00:00",
                "product_count": 4,
                "products": {
                    "thin": {"title": "Thin", "handle": "thin", "price": "", "images": []},
                },
            })

            audit = audit_scrape_precision(root, limit=2)

        self.assertTrue(audit["ok"])
        self.assertEqual(audit["summary"]["snapshot_files"], 2)
        self.assertEqual(audit["focus_domains"][0]["domain"], "bad.example")
        self.assertIn("image_coverage_low", audit["focus_domains"][0]["refresh_reasons"])
        self.assertIn("partial_snapshot", audit["focus_domains"][0]["refresh_reasons"])

    def test_audit_scrape_precision_writes_and_reads_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_snapshot(root / "first.example_snapshot.json", {
                "domain": "first.example",
                "last_check": "2026-01-01T00:00:00",
                "products": {
                    "a": {
                        "title": "Premium Travel Organizer Kit",
                        "handle": "a",
                        "price": "39.95",
                        "image_count": 3,
                        "summary": "A detailed product summary with enough commercial detail for a Shopify product page.",
                        "variant_count": 1,
                    }
                },
            })
            self._write_snapshot(root / "second.example_snapshot.json", {
                "domain": "second.example",
                "last_check": "2020-01-01T00:00:00",
                "products": {
                    "thin": {"title": "Thin", "price": "", "images": []},
                },
            })

            audit = audit_scrape_precision(root, limit=1)
            cached = read_cached_scrape_precision(root, limit=1, max_age_seconds=900)

        self.assertTrue(audit["ok"])
        self.assertFalse(audit["cached"])
        self.assertTrue(cached["ok"])
        self.assertTrue(cached["cached"])
        self.assertEqual(len(cached["focus_domains"]), 1)
        self.assertEqual(cached["summary"]["snapshot_files"], 2)

    @staticmethod
    def _write_snapshot(path: Path, payload: dict):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
