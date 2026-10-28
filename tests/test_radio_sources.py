"""Ensure a radio keeps its requested genre or collection across tracks and caches."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from musica_sync import new_music, radio_sources
from musica_sync.discovery import DiscoveryError


class RadioSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = patch.object(new_music, "ROOT", Path(self.directory.name))
        root.start()
        self.addCleanup(root.stop)
        owned = patch.object(radio_sources, "owned_tracks", return_value={})
        owned.start()
        self.addCleanup(owned.stop)
        preferences = patch.object(radio_sources, "read_preferences", return_value={"artists": {}})
        preferences.start()
        self.addCleanup(preferences.stop)

    @staticmethod
    def response(artist, title):
        return {"tracks": {"track": [{"artist": {"name": artist}, "name": title}]}}

    def test_context_validation_rejects_unknown_modes_and_playlist_urls(self):
        self.assertEqual(
            radio_sources.parse_context({"mode": "genre", "value": " Punk "}), ("genre", "punk")
        )
        for value in [
            [],
            None,
            {"mode": "random"},
            {"mode": "genre", "value": ""},
            {"mode": "playlist", "value": "http://localhost/private"},
            {"mode": "favourites", "value": "ignored"},
        ]:
            with self.assertRaises(DiscoveryError):
                radio_sources.parse_context(value)

    def test_genre_caches_remain_separate_and_skip_already_owned_recordings(self):
        with patch.object(
            radio_sources,
            "lastfm",
            side_effect=[
                self.response("Punk Band", "Punk Track"),
                self.response("Jazz Band", "Jazz Track"),
            ],
        ) as fetch:
            punk, _ = radio_sources.scoped_candidates("demo-user", "genre", "punk", set())
            jazz, _ = radio_sources.scoped_candidates("demo-user", "genre", "jazz", set())
            repeated, _ = radio_sources.scoped_candidates("demo-user", "genre", "punk", set())
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(punk[0]["artist"], "Punk Band")
        self.assertEqual(jazz[0]["artist"], "Jazz Band")
        self.assertEqual(repeated, punk)
        with patch.object(
            radio_sources, "owned_tracks", return_value={"punk band:punk track": "owned"}
        ):
            with self.assertRaises(DiscoveryError):
                radio_sources.scoped_candidates("demo-user", "genre", "punk", set())

    def test_scoped_results_do_not_leak_into_general_personal_recommendations(self):
        with patch.object(
            radio_sources, "lastfm", return_value=self.response("Punk Band", "Punk Track")
        ):
            radio_sources.scoped_candidates("demo-user", "genre", "punk", set())
        with new_music.database() as db:
            db.execute(
                "INSERT INTO recommendations(id,owner,artist,title,reason,score,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "a" * 32,
                    "demo-user",
                    "Personal Artist",
                    "Personal Song",
                    "Personal",
                    1,
                    time.time(),
                ),
            )
        with (
            patch.object(new_music, "owned_tracks", return_value={}),
            patch.object(new_music, "read_preferences", return_value={"artists": {}}),
        ):
            result = new_music.recommendations("demo-user")["tracks"]
        self.assertEqual([t["artist"] for t in result], ["Personal Artist"])

    def test_playlist_membership_changes_refresh_only_that_playlist(self):
        first = {"name": "My playlist", "entry": [{"id": "a", "artist": "First", "title": "Seed"}]}
        changed = {
            "name": "My playlist",
            "entry": [{"id": "b", "artist": "Second", "title": "Seed"}],
        }

        def similar(method, **values):
            return {
                "similartracks": {
                    "track": [{"artist": {"name": values["artist"] + " related"}, "name": "New"}]
                }
            }

        with (
            patch.object(radio_sources, "playlist_info", side_effect=[first, first, changed]),
            patch.object(radio_sources, "lastfm", side_effect=similar) as fetch,
        ):
            initial, _ = radio_sources.scoped_candidates(
                "demo-user", "playlist", "myplaylist", set()
            )
            cached, _ = radio_sources.scoped_candidates(
                "demo-user", "playlist", "myplaylist", set()
            )
            edited, _ = radio_sources.scoped_candidates(
                "demo-user", "playlist", "myplaylist", set()
            )
        self.assertEqual(initial, cached)
        self.assertEqual(initial[0]["artist"], "First related")
        self.assertEqual(edited[0]["artist"], "Second related")
        self.assertEqual(fetch.call_count, 2)

    def test_favourites_use_only_hearted_seeds_and_empty_collections_fail_clearly(self):
        songs = {
            "a": SimpleNamespace(id="a", favourite=True, artist="Hearted", title="Hearted seed"),
            "b": SimpleNamespace(id="b", favourite=False, artist="Other", title="Other seed"),
        }
        with patch.object(radio_sources, "load_library", return_value=songs):
            seeds, _, _ = radio_sources.seed_tracks("demo-user", "favourites", "")
        self.assertEqual([t["artist"] for t in seeds], ["Hearted"])
        with patch.object(radio_sources, "load_library", return_value={}):
            with self.assertRaises(DiscoveryError) as error:
                radio_sources.seed_tracks("demo-user", "favourites", "")
            self.assertEqual(error.exception.status, 422)

    def test_scoped_radio_track_can_be_saved_without_leaking_into_the_personal_pool(self):
        with patch.object(
            radio_sources, "lastfm", return_value=self.response("Punk Band", "Punk Track")
        ):
            tracks, _ = radio_sources.scoped_candidates("demo-user", "genre", "punk", set())
        with patch.object(new_music, "owned_tracks", return_value={}):
            new_music.save("demo-user", {"id": tracks[0]["id"]})
        with patch.object(new_music, "acquire", return_value="imported"):
            new_music.work_once()
        saved = new_music.jobs("demo-user")["tracks"][0]
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["song_id"], "imported")
        with new_music.database() as db:
            self.assertEqual(
                db.execute(
                    "SELECT personal FROM recommendations WHERE id=?", (tracks[0]["id"],)
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
