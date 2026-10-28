from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from musica_sync import cli, curation
from musica_sync.curation import Song, canonical_genre, eligible, genre_pools, select_tracks
from musica_sync.navidrome import Subsonic


def song(index: int, *, artist: str | None = None, **values: object) -> Song:
    defaults = {
        "id": str(index),
        "path": Path("/song.mp3"),
        "artist": artist or f"Artist {index % 12}",
        "title": f"Song {index}",
        "album": f"Album {index % 3}",
        "genres": ("Rock",),
        "duration": 180.0,
    }
    defaults.update(values)
    return Song(**defaults)


class CurationTests(unittest.TestCase):
    def test_genre_aliases_do_not_make_duplicate_catalogue_entries(self):
        self.assertEqual(canonical_genre("Rap"), canonical_genre("Hip-Hop"))
        self.assertEqual(canonical_genre("Electronica"), canonical_genre("Electronic"))
        self.assertEqual(canonical_genre("IDM"), "IDM")

    def test_missing_long_and_whole_album_files_are_excluded_but_not_remixes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.mp3"
            path.touch()
            valid = song(1, path=path)
            self.assertTrue(eligible(valid))
            self.assertTrue(eligible(replace(valid, title="Track (Club Mix)")))
            self.assertFalse(eligible(replace(valid, path=path.with_name("missing.mp3"))))
            self.assertFalse(eligible(replace(valid, duration=3600)))
            self.assertFalse(eligible(replace(valid, title="Full Album")))
            self.assertFalse(eligible(replace(valid, title="Outro la fuga", duration=70)))

    def test_recording_deduplication_works_when_only_one_copy_has_a_musicbrainz_id(self):
        original = song(1, title="Track", recording_id="recording", bit_rate=320)
        video = replace(
            original, id="2", title="Track (Official Video)", recording_id="", bit_rate=128
        )
        remix = replace(original, id="3", title="Track (Remix)", recording_id="other")
        result = curation.unique_recordings([original, video, remix])
        self.assertEqual({s.id for s in result}, {"1", "3"})

    def test_artist_album_and_consecutive_repeat_limits_hold(self):
        songs = [song(i, album=f"Album {i // 12 % 3}") for i in range(120)]
        scores = {s.id: 1 / (i + 1) for i, s in enumerate(songs)}
        result = select_tracks(songs, scores, seed="today")
        self.assertEqual(len(result), 30)
        self.assertLessEqual(max(Counter(s.artist_key for s in result).values()), 3)
        self.assertLessEqual(max(Counter((s.artist_key, s.album) for s in result).values()), 2)
        self.assertTrue(all(a.artist_key != b.artist_key for a, b in zip(result, result[1:])))

    def test_daily_rotation_is_reproducible(self):
        songs = [song(i) for i in range(120)]
        first = [s.id for s in select_tracks(songs, {}, seed="today")]
        self.assertEqual(first, [s.id for s in select_tracks(songs, {}, seed="today")])
        self.assertNotEqual(first, [s.id for s in select_tracks(songs, {}, seed="tomorrow")])

    def test_audio_relevance_selects_strong_matches(self):
        songs = [song(i, artist=f"Artist {i}") for i in range(100)]
        scores = {s.id: 1 if i < 20 else 0 for i, s in enumerate(songs)}
        result = select_tracks(songs, scores, seed="today", count=10)
        self.assertTrue(all(int(s.id) < 30 for s in result))

    def test_near_identical_genres_are_merged_but_single_artist_genres_are_complete(self):
        songs = [song(i, genres=("Rock", "Almost Rock")) for i in range(12)]
        songs += [song(i + 20, artist="Solo", genres=("Solo Genre",)) for i in range(20)]
        groups = genre_pools({s.id: s for s in songs}, 8)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups["Solo Genre"]), 20)

    def test_genre_keeps_every_recording_without_artist_or_album_caps(self):
        songs = [
            song(i, artist=f"Artist {i % 3}", album="Same album", bit_rate=320) for i in range(150)
        ]
        duplicate = replace(songs[0], id="duplicate", bit_rate=1)
        result = curation.order_genre_tracks([*songs, duplicate], {}, seed="today")
        self.assertEqual({s.id for s in result}, {s.id for s in songs})
        self.assertEqual(len(result), 150)
        self.assertTrue(all(a.artist_key != b.artist_key for a, b in zip(result, result[1:])))
        self.assertEqual(result, curation.order_genre_tracks([*songs, duplicate], {}, seed="today"))

    def test_genre_retains_dominant_artist_when_repeats_are_unavoidable(self):
        songs = [song(i, artist="Solo" if i < 50 else "Other") for i in range(55)]
        result = curation.order_genre_tracks(songs, {}, seed="today")
        self.assertEqual(len(result), 55)
        repeats = sum(a.artist_key == b.artist_key for a, b in zip(result, result[1:]))
        self.assertEqual(repeats, 44)

    def test_analysis_failure_preserves_existing_published_playlists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "state/sonic/credentials.json"
            credentials.parent.mkdir(parents=True)
            credentials.write_text('{"navidrome_user":"user","navidrome_password":"test"}')
            with (
                patch.object(curation, "ROOT", root),
                patch.object(curation, "build_plan", side_effect=RuntimeError("Index unavailable")),
                patch.object(curation, "refresh_quality"),
                patch.object(curation, "publish") as publish,
            ):
                with self.assertRaisesRegex(RuntimeError, "Index unavailable"):
                    curation.refresh()
                publish.assert_not_called()

    def test_failed_library_scan_prevents_curation_on_stale_metadata(self):
        settings = Mock(spec=cli.Settings, curated_playlists=True)
        with (
            patch.object(cli, "trigger_navidrome_scan", return_value=False),
            patch.object(cli, "refresh_auto_playlists") as refresh,
        ):
            with self.assertRaisesRegex(RuntimeError, "scan failed"):
                cli.refresh_library(settings)
            refresh.assert_not_called()

    def test_failed_artwork_upload_can_resume_without_duplicates_or_deleting_manual_playlists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "cover.png"
            image.write_bytes(b"artwork")
            client = Subsonic("http://127.0.0.1:4533", "user", "test")
            store = {"manual": {"id": "manual", "name": "My playlist", "owner": "user"}}
            created = []

            def api(method: str, values=None):
                values = values or []
                params = dict(values)
                if method == "getPlaylists":
                    return {"playlists": {"playlist": list(store.values())}}
                if method == "createPlaylist":
                    item_id = params.get("playlistId") or f"p{len(created)}"
                    if item_id not in store:
                        created.append(item_id)
                    store[item_id] = {
                        "id": item_id,
                        "name": params["name"],
                        "owner": "user",
                        "entry": [{"id": v} for k, v in values if k == "songId"],
                    }
                    return {"playlist": store[item_id]}
                if method == "getPlaylist":
                    return {"playlist": store[params["id"]]}
                if method == "updatePlaylist":
                    return {}
                if method == "deletePlaylist":
                    del store[params["id"]]
                    return {}
                raise AssertionError(method)

            plan = [
                {
                    "name": f"Generi / Genre {i}",
                    "genre": f"Genre {i}",
                    "song_ids": [str(i)],
                    "comment": "Generated",
                }
                for i in range(2)
            ]
            with (
                patch.object(curation, "ROOT", root),
                patch.object(client, "call", side_effect=api),
                patch.object(curation, "request_json", return_value={"token": "test"}),
                patch.object(curation, "artwork", return_value=(image, "hash")),
                patch.object(
                    curation, "upload_image", side_effect=[RuntimeError("Offline"), None, None]
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "Offline"):
                    curation.publish(client, plan)
                self.assertTrue((root / "state/curation/pending.json").exists())
                curation.publish(client, plan)
                self.assertEqual(len(created), 2)
                self.assertEqual(len(store), 3)
                self.assertIn("manual", store)
                self.assertFalse((root / "state/curation/pending.json").exists())


if __name__ == "__main__":
    unittest.main()
