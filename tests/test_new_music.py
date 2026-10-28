"""Check persistent saves, ownership, failure recovery and source selection."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from musica_sync import new_music
from musica_sync.discovery import DiscoveryError


class NewMusicTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = patch.object(new_music, "ROOT", Path(self.directory.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.owned = patch.object(new_music, "owned_tracks", return_value={})
        self.owned.start()
        self.addCleanup(self.owned.stop)
        self.key = self.add_track("Artist", "Song")

    def add_track(self, artist, title, owner="demo-user", reason="Seed"):
        key = new_music.key_for(artist, title)
        with new_music.database() as db:
            db.execute(
                "INSERT INTO recommendations(id,owner,artist,title,reason,score,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, owner, artist, title, reason, 1, time.time()),
            )
        return key

    def test_repeated_saves_and_double_clicks_produce_one_persistent_job(self):
        for _ in range(3):
            new_music.save("demo-user", {"id": self.key})
        result = new_music.jobs("demo-user")["tracks"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "queued")

    def test_other_users_cannot_save_or_inspect_someone_elses_recommendations(self):
        with self.assertRaises(DiscoveryError) as caught:
            new_music.save("other", {"id": self.key})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(new_music.jobs("other")["tracks"], [])

    def test_save_never_accepts_arbitrary_download_urls_or_untrusted_titles(self):
        for key in ["https://localhost/secret", "../file", [], None, "x" * 32]:
            with self.assertRaises(DiscoveryError):
                new_music.save("demo-user", {"id": key})

    def test_existing_recording_is_linked_without_downloading(self):
        with patch.object(new_music, "owned_tracks", return_value={"artist:song": "existing"}):
            new_music.save("demo-user", {"id": self.key})
        result = new_music.jobs("demo-user")["tracks"][0]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["song_id"], "existing")
        with patch.object(new_music, "acquire") as acquire:
            self.assertFalse(new_music.work_once())
            acquire.assert_not_called()

    def test_worker_failure_is_visible_and_retry_reuses_the_same_job(self):
        new_music.save("demo-user", {"id": self.key})
        with (
            patch.object(new_music, "acquire", side_effect=RuntimeError("Source unavailable")),
            self.assertLogs(new_music.LOGGER, level="ERROR"),
        ):
            self.assertTrue(new_music.work_once())
        failed = new_music.jobs("demo-user")["tracks"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "Source unavailable")
        new_music.save("demo-user", {"id": self.key})
        with patch.object(new_music, "acquire", return_value="imported"):
            self.assertTrue(new_music.work_once())
        result = new_music.jobs("demo-user")["tracks"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "ready")
        self.assertEqual(result[0]["song_id"], "imported")

    def test_dismissal_persists_and_owned_tracks_are_not_recommended(self):
        self.add_track("Other", "Other song")
        new_music.dismiss("demo-user", {"id": self.key})
        with (
            patch.object(new_music, "owned_tracks", return_value={"other:other song": "owned"}),
            patch.object(new_music, "read_preferences", return_value={"artists": {}}),
        ):
            self.assertEqual(new_music.recommendations("demo-user")["tracks"], [])

    def test_recommendations_mix_seeds_and_do_not_repeat_one_artist(self):
        for i in range(12):
            self.add_track("Repeated", f"Track {i}", reason="Seed A")
            self.add_track(f"Other {i}", "Song", reason="Seed B")
        with patch.object(new_music, "read_preferences", return_value={"artists": {}}):
            result = new_music.recommendations("demo-user")["tracks"]
        self.assertEqual(sum(t["artist"] == "Repeated" for t in result), 2)
        self.assertTrue(any(t["reason"] == "Seed B" for t in result[:3]))

    def test_source_matching_rejects_wrong_versions_covers_and_full_albums(self):
        track = {"artist": "Mazzy Star", "title": "Cry, Cry"}
        good = {"title": "Cry, Cry", "channel": "Mazzy Star - Topic", "duration": 240}
        self.assertGreater(new_music.candidate_score(track, good), 0)
        for title in ["Cry Cry live", "Cry Cry remix", "Cry Cry cover", "Full Album"]:
            self.assertLess(new_music.candidate_score(track, {**good, "title": title}), 0)
        self.assertLess(new_music.candidate_score(track, {**good, "channel": "Other"}), 0)
        self.assertLess(new_music.candidate_score(track, {**good, "duration": 8000}), 0)

    def test_identity_collapses_cosmetic_video_labels_but_keeps_remixes(self):
        self.assertEqual(new_music.key_for("Artist", "Song (Official Video)"), self.key)
        self.assertNotEqual(new_music.key_for("Artist", "Song (Remix)"), self.key)

    def test_interrupted_import_is_indexed_without_downloading_another_copy(self):
        track = {"id": self.key, "owner": "demo-user", "artist": "Artist", "title": "Song"}
        with (
            patch.object(new_music, "owned_tracks", side_effect=[{}, {"artist:song": "indexed"}]),
            patch.object(new_music, "already_imported", return_value=True),
            patch.object(new_music, "scan_library") as scan,
            patch.object(new_music, "resolve_video") as resolve,
        ):
            self.assertEqual(new_music.acquire(track), "indexed")
            scan.assert_called_once()
            resolve.assert_not_called()

    def test_deleted_saved_track_can_be_acquired_again(self):
        new_music.save("demo-user", {"id": self.key})
        with patch.object(new_music, "acquire", return_value="old-id"):
            new_music.work_once()
        new_music.save("demo-user", {"id": self.key})
        self.assertEqual(new_music.jobs("demo-user")["tracks"][0]["status"], "queued")

    def test_preview_links_are_signed_expire_and_cannot_read_arbitrary_files(self):
        path = Path(self.directory.name) / "state/new-music/downloads" / self.key / "track.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"audio")
        with patch.object(new_music, "download_audio", return_value=path):
            url = new_music.preview("demo-user", {"id": self.key})["audio_url"]
        self.assertEqual(new_music.preview_path(url), path)
        self.assertIsNone(new_music.preview_path(url.replace(self.key, "b" * 32)))
        self.assertIsNone(new_music.preview_path("/preview/../../etc/passwd"))
        with patch.object(new_music.time, "time", return_value=time.time() + 7200):
            self.assertIsNone(new_music.preview_path(url))

    def test_cached_preview_is_reused_without_a_second_download(self):
        path = Path(self.directory.name) / "state/new-music/downloads" / self.key
        path.mkdir(parents=True)
        (path / "track.mp3").write_bytes(b"verified audio")
        (path / "verified").touch()
        with patch.object(new_music, "resolve_video") as resolve:
            self.assertEqual(new_music.download_audio({"id": self.key}), path / "track.mp3")
            resolve.assert_not_called()

    def test_preview_cleanup_removes_old_cache_but_preserves_active_saves_and_other_files(self):
        new_music.save("demo-user", {"id": self.key})
        root = Path(self.directory.name) / "state/new-music/downloads"
        paths = [root / self.key, root / ("b" * 32), root / "unrelated"]
        for path in paths:
            path.mkdir(parents=True)
            stamp = path / "verified"
            stamp.touch()
            os.utime(stamp, (time.time() - 90000, time.time() - 90000))
        new_music.cleanup_previews()
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(paths[2].exists())

    def test_radio_prepares_unowned_audio_without_creating_a_save_job(self):
        track = {"id": self.key, "artist": "Artist", "title": "Song"}
        with (
            patch.object(new_music, "recommendations", return_value={"tracks": [track]}),
            patch.object(new_music, "preview", return_value={"audio_url": "signed"}),
            patch.object(new_music, "preview_path", return_value=Path("/audio.mp3")),
            patch.object(new_music, "MediaFile", return_value=SimpleNamespace(length=180)),
        ):
            result = new_music.radio_next("demo-user", {})
        self.assertEqual(result["track"]["id"], self.key)
        self.assertEqual(result["duration"], 180)
        self.assertEqual(new_music.jobs("demo-user")["tracks"], [])

    def test_radio_skips_unavailable_sources_and_avoids_the_previous_artist(self):
        tracks = [
            {"id": self.key, "artist": "Previous", "title": "Song"},
            {"id": "b" * 32, "artist": "Other", "title": "Unavailable"},
            {"id": "c" * 32, "artist": "Third", "title": "Available"},
        ]
        with (
            patch.object(new_music, "recommendations", return_value={"tracks": tracks}),
            patch.object(
                new_music,
                "preview",
                side_effect=[RuntimeError("Unavailable"), {"audio_url": "signed"}],
            ),
            patch.object(new_music, "preview_path", return_value=Path("/audio.mp3")),
            patch.object(new_music, "MediaFile", return_value=SimpleNamespace(length=180)),
            self.assertLogs(new_music.LOGGER, level="WARNING"),
        ):
            result = new_music.radio_next("demo-user", {"last_artist": "Previous"})
        self.assertEqual(result["track"]["artist"], "Third")

    def test_radio_can_continue_past_the_first_page_of_suggestions(self):
        for i in range(50):
            self.add_track(f"Artist {i}", f"Song {i}")
        with patch.object(new_music, "read_preferences", return_value={"artists": {}}):
            first = new_music.recommendations("demo-user")["tracks"]
            second = new_music.recommendations("demo-user", exclude_ids={t["id"] for t in first})[
                "tracks"
            ]
        self.assertEqual(len(first), 24)
        self.assertEqual(len(second), 24)
        self.assertFalse({t["id"] for t in first} & {t["id"] for t in second})

    def test_radio_rejects_unbounded_and_untrusted_exclusion_lists(self):
        for body in [
            {"exclude": ["x"]},
            {"exclude": self.key},
            {"exclude": [self.key] * 81},
            {"last_artist": []},
        ]:
            with self.assertRaises(DiscoveryError):
                new_music.radio_next("demo-user", body)

    def test_radio_skips_audio_already_owned_under_different_metadata(self):
        tracks = [
            {"id": self.key, "artist": "Artist", "title": "Duplicate"},
            {"id": "b" * 32, "artist": "Other", "title": "New song"},
        ]
        with (
            patch.object(new_music, "recommendations", return_value={"tracks": tracks}),
            patch.object(new_music, "preview", return_value={"audio_url": "signed"}),
            patch.object(new_music, "preview_path", return_value=Path("/audio.mp3")),
            patch.object(new_music, "fingerprint_duplicate", side_effect=["owned-id", None]),
            patch.object(new_music, "MediaFile", return_value=SimpleNamespace(length=180)),
        ):
            result = new_music.radio_next("demo-user", {})
        self.assertEqual(result["track"]["title"], "New song")


if __name__ == "__main__":
    unittest.main()
