from __future__ import annotations

import unittest

from musica_sync.profiles import merged_annotation


class ProfileMergeTests(unittest.TestCase):
    def test_merge_keeps_original_favourite_date_and_adds_independent_plays(self):
        old = {
            "play_count": 9,
            "play_date": "2026-10-20",
            "starred": True,
            "starred_at": "2026-01-01",
            "rating": 5,
            "rated_at": "2026-10-20",
        }
        new = {
            "play_count": 2,
            "play_date": "2026-10-27",
            "starred": True,
            "starred_at": "2026-10-27",
            "rating": 0,
            "rated_at": None,
        }
        result = merged_annotation(old, new)
        self.assertEqual(result["play_count"], 11)
        self.assertEqual(result["play_date"], "2026-10-27")
        self.assertEqual(result["starred_at"], "2026-01-01")
        self.assertEqual(result["rating"], 5)

    def test_newer_explicit_rating_clear_wins(self):
        old = {"rating": 5, "rated_at": "2026-10-20"}
        new = {"rating": 0, "rated_at": "2026-10-27"}
        self.assertEqual(merged_annotation(old, new)["rating"], 0)

    def test_favourites_are_preserved_from_either_account(self):
        self.assertTrue(merged_annotation({"starred": True}, {"starred": False})["starred"])
        self.assertTrue(merged_annotation({"starred": False}, {"starred": True})["starred"])


if __name__ == "__main__":
    unittest.main()
