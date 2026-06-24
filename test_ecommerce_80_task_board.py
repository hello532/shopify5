#!/usr/bin/env python3
import unittest

from ecommerce_80_task_board import _lane_for_pack, _tasks_for_pack


class Ecommerce80TaskBoardTests(unittest.TestCase):
    def test_petsnowy_verified_ads_prioritize_asset_pack(self):
        lane = _lane_for_pack(
            {"family_key": "pet-air-purifier-filter"},
            {"status": "verified", "total_ads": 5, "max_orbit_score": 70},
        )

        self.assertEqual(lane, "asset_pack_priority")

    def test_hair_removal_stays_productization_research(self):
        pack = {"family_key": "demand-hair-removal-local-service"}
        lane = _lane_for_pack(pack, {"status": "missing", "total_ads": 0})
        tasks = _tasks_for_pack(pack, {"status": "missing", "total_ads": 0})

        self.assertEqual(lane, "productization_research")
        self.assertEqual(tasks[0].key, "productization")
        self.assertEqual(tasks[0].status, "required")


if __name__ == "__main__":
    unittest.main()
