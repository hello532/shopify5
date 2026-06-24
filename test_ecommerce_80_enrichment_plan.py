#!/usr/bin/env python3
import unittest

from ecommerce_80_enrichment_plan import family_key, _creative_scripts, _pdp_requirements, _report_only_tasks


class Ecommerce80EnrichmentPlanTests(unittest.TestCase):
    def test_family_key_groups_hair_removal_pages(self):
        self.assertEqual(
            family_key({"domain": "lasercenteroforlando.com", "title": "Laser Hair Removal Orlando FL"}),
            "demand-hair-removal-local-service",
        )
        self.assertEqual(
            family_key({"domain": "lasercenteroforlando.com", "title": "Electrolysis Hair Removal Orlando FL"}),
            "demand-hair-removal-local-service",
        )

    def test_family_key_groups_pet_filter(self):
        self.assertEqual(
            family_key({"domain": "petsnowy.com", "title": "Air Purifier Filter"}),
            "pet-air-purifier-filter",
        )

    def test_enrichment_assets_are_report_only_and_originality_oriented(self):
        pdp = _pdp_requirements("demand-hair-removal-local-service")
        scripts = _creative_scripts("pet-air-purifier-filter", "Pet Air Purifier Replacement Filter")

        self.assertTrue(any("Do not use Orlando" in item for item in pdp))
        self.assertEqual(len(scripts), 6)
        self.assertTrue(all("never reuse competitor" in script["proof_3_12s"] for script in scripts))

    def test_report_only_tasks_filter_draft_and_ad_actions(self):
        tasks = _report_only_tasks([
            "立即上架只到 DRAFT：建草稿页",
            "补首屏实拍/演示图、场景图、FAQ",
            "启动广告投放",
        ])

        self.assertEqual(tasks, ["补首屏实拍/演示图、场景图、FAQ"])


if __name__ == "__main__":
    unittest.main()
