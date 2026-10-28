from __future__ import annotations

import random
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from musica_sync import curation, discovery
from musica_sync.curation import Song, select_tracks, unique_recordings
from musica_sync.enrichment import AMBIGUOUS_ARTIST, propose, verified_track
from musica_sync.fingerprints import compatible_versions, match_score
from musica_sync.metadata import clean_title, derive_title_and_album
from musica_sync.preferences import read_preferences, update_preferences


def song(index: int, **kwargs) -> Song:
    data = dict(
        id=str(index),
        path=Path("/song"),
        artist=f"Artist {index}",
        title=f"Track {index}",
        album="Album",
        genres=("Rock",),
        duration=180,
    )
    return Song(**(data | kwargs))


class RecommendationTests(unittest.TestCase):
    def test_fingerprints_tolerate_encoder_padding_but_reject_different_recordings(self):
        rng = random.Random(40)
        words = [rng.getrandbits(32) for _ in range(1000)]
        self.assertGreaterEqual(match_score(words, [0] * 4 + words), 0.99)
        different = [rng.getrandbits(32) for _ in range(1000)]
        self.assertLess(match_score(words, different), 0.99)
        self.assertFalse(compatible_versions("Song (Live)", "Song"))
        self.assertFalse(compatible_versions("Song (strumentale)", "Song"))
        self.assertTrue(compatible_versions("Song (Official)", "Song"))

    def test_recently_heard_songs_lose_priority_even_with_high_sonic_scores(self):
        now = datetime(2026, 10, 27, tzinfo=UTC)
        songs = [
            song(i, last_played=(now - timedelta(hours=2)).isoformat() if i < 10 else None)
            for i in range(60)
        ]
        selected = select_tracks(
            songs, {str(i): 1 for i in range(10)}, seed="cooldown", count=10, now=now
        )
        self.assertTrue(all(int(s.id) >= 10 for s in selected))

    def test_large_pools_replace_at_least_35_percent_of_previous_membership(self):
        songs = [song(i) for i in range(100)]
        yesterday = select_tracks(songs, {}, seed="yesterday")
        old = {s.id for s in yesterday}
        today = select_tracks(songs, {}, seed="today", previous_ids=old)
        self.assertEqual(len(today), 30)
        self.assertGreaterEqual(len({s.id for s in today} - old), 11)

    def test_small_pools_remain_playable_without_fake_newness(self):
        songs = [song(i) for i in range(8)]
        selected = select_tracks(songs, {}, seed="small", previous_ids={s.id for s in songs})
        self.assertEqual(len(selected), 8)

    def test_cross_codec_bitrate_does_not_override_the_users_chosen_version(self):
        mp3 = song(1, codec="mp3", bit_rate=320)
        opus = replace(mp3, id="2", codec="opus", bit_rate=128, favourite=True)
        self.assertEqual(unique_recordings([mp3, opus]), [opus])
        same_codec = replace(mp3, id="3", bit_rate=128)
        self.assertEqual(unique_recordings([same_codec, mp3]), [mp3])

    def test_single_favourite_anchor_does_not_merge_unrelated_tastes(self):
        library = {str(i): song(i, favourite=i < 3, play_count=i) for i in range(10)}
        client = Mock()
        client.call.return_value = {"similarSongs2": {"song": [{"id": str(i)} for i in range(10)]}}
        scores = curation.preference_scores(client, library)
        client.call.assert_called_once()
        self.assertEqual(client.call.call_args.args[0], "getSimilarSongs2")
        self.assertTrue(scores)
        self.assertTrue(all(not library[i].favourite for i in scores))

    def test_explicit_exclusions_persist_and_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            update_preferences("artists", "artist", "Artist", path=path)
            update_preferences("tracks", "42", "Track", path=path)
            self.assertEqual(read_preferences(path)["artists"], {"artist": "Artist"})
            update_preferences("artists", "artist", "", remove=True, path=path)
            self.assertEqual(read_preferences(path), {"artists": {}, "tracks": {"42": "Track"}})

    def test_unverified_uploader_title_is_not_split_into_false_artist_album_fields(self):
        title, album = derive_title_and_album("Eminem - Lose Yourself (Official Video)", "Uploader")
        self.assertEqual(title, "Eminem - Lose Yourself")
        self.assertIsNone(album)
        self.assertEqual(clean_title("Song (Official Video) (Live)"), "Song (Live)")

    def test_public_scrobble_echo_without_evidence_is_not_a_metadata_match(self):
        track = {"artist": {"name": "Uploader"}, "name": "Wrong", "listeners": "2"}
        self.assertFalse(verified_track(track, "Uploader", "Wrong"))
        track["listeners"] = "300"
        self.assertTrue(verified_track(track, "Uploader", "Wrong"))
        self.assertFalse(verified_track(track, "Uploader", "Wrong", correcting_identity=True))

    def test_swapped_title_repair_does_not_query_artist_name_as_a_song(self):
        api = Mock()
        api.call.return_value = {}
        item = dict(
            id=1,
            path="/song",
            artist="EminemMusic",
            albumartist="EminemMusic",
            title="Eminem",
            album="Lose Yourself",
            year=0,
            genres=[],
            length=180,
        )
        propose(item, api, set(), {})
        queries = [(c.kwargs.get("artist"), c.kwargs.get("track")) for c in api.call.call_args_list]
        self.assertNotIn(("Eminem", "Eminem"), queries)
        self.assertIn(("Eminem", "Lose Yourself"), queries)
        self.assertTrue(AMBIGUOUS_ARTIST.search("There are multiple artists called Nada."))

    def test_request_validation_rejects_bad_inputs_without_starting_audio_search(self):
        for data in [
            {"query": ""},
            {"query": "x" * 241},
            {"mode": "invalid"},
            {"query": "rock", "avoid_recent": "yes"},
            {"mode": "track", "query": "../secret"},
            {"query": "rock", "variation": "/etc/passwd"},
        ]:
            with self.subTest(data=data), self.assertRaises(discovery.DiscoveryError):
                discovery.search_policy(data)
        self.assertIn("melancholic piano", discovery.sonic_query("malinconico pianoforte"))

    def test_text_search_never_leaks_unrelated_unanalysed_or_recent_tracks_as_fallback(self):
        library = {
            "1": song(1),
            "2": song(2, last_played=datetime.now(UTC).isoformat()),
            "3": song(3),
        }
        with (
            patch.object(discovery, "load_library", return_value=library),
            patch.object(
                discovery,
                "credentials",
                return_value={"navidrome_password": "test", "audiomuse_token": "test"},
            ),
            patch.object(
                discovery,
                "request_json",
                return_value={
                    "results": [
                        {"item_id": "2", "similarity": 0.8},
                        {"item_id": "missing", "similarity": 0.9},
                    ]
                },
            ),
        ):
            result = discovery.search("test", {"mode": "text", "query": "sad piano"})
        self.assertEqual(result["ids"], [])


if __name__ == "__main__":
    unittest.main()
