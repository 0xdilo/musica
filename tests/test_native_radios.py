"""Validate durable buffers, station isolation, failure recovery and ICY framing."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from musica_sync import native_radios
from musica_sync.discovery import DiscoveryError


class NativeRadioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        owned = patch.object(native_radios.new_music, "owned_tracks", return_value={})
        self.owned = owned.start()
        self.addCleanup(owned.stop)
        self.engine = native_radios.RadioEngine("listener", "https://music.example", self.directory)
        self.context = {"mode": "genre", "value": "post-rock"}
        self.station = self.engine.add({"context": self.context})
        self.identifier = self.station["id"]

    def test_station_identity_is_stable_and_requests_are_validated(self):
        same = self.engine.add({"context": {"mode": "genre", "value": "POST-ROCK"}})
        self.assertEqual(same["id"], self.identifier)
        self.assertEqual(len(self.engine.overview()), 1)
        for context in [
            {"mode": "url", "value": "https://localhost"},
            {"mode": "playlist", "value": "../bad"},
        ]:
            with self.assertRaises(DiscoveryError):
                self.engine.add({"context": context})
        self.assertEqual(len(self.engine.overview()), 1)

    def test_signed_station_urls_cannot_be_reused_or_forged(self):
        with patch.object(native_radios.radio_relay, "relay_key", return_value=b"k" * 32):
            path = native_radios.stream_path(self.identifier)
            self.assertEqual(native_radios.parse_path(path), self.identifier)
            self.assertIsNone(
                native_radios.parse_path(path[:-1] + ("0" if path[-1] != "0" else "1"))
            )
            self.assertIsNone(native_radios.parse_path(path.replace(self.identifier, "f" * 16)))
            self.assertIsNone(native_radios.parse_path("/discovery-radio/../../private"))

    def prepared(self, key="a" * 32):
        self.engine.cache.mkdir(parents=True, exist_ok=True)
        (self.engine.cache / (key + ".mp3")).write_bytes(b"audio")
        return {"id": key, "artist": "Artist", "title": "Song"}

    def test_ready_buffer_survives_restart_and_claims_are_not_repeated(self):
        with patch.object(native_radios, "prepare_track", return_value=self.prepared()) as prepare:
            with patch.object(self.engine, "publish"):
                self.assertTrue(self.engine.fill_one())
            self.assertEqual(prepare.call_args.args[2], [])
        restored = native_radios.RadioEngine("listener", "https://music.example", self.directory)
        self.assertEqual(restored.overview()[0]["buffered"], 1)
        track = restored.take(self.identifier)
        self.assertEqual(track["id"], "a" * 32)
        restarted = native_radios.RadioEngine("listener", "https://music.example", self.directory)
        self.assertEqual(list(restarted.stations[self.identifier].history), ["a" * 32])
        with self.assertRaises(DiscoveryError):
            restarted.take(self.identifier, timeout=0)

    def test_publication_failure_retries_without_discarding_or_overfilling_buffer(self):
        with patch.object(native_radios, "prepare_track", return_value=self.prepared()) as prepare:
            with patch.object(self.engine, "publish", side_effect=RuntimeError("Offline")):
                with self.assertLogs(native_radios.LOGGER, "ERROR"):
                    self.engine.fill_one()
            state = self.engine.stations[self.identifier]
            self.assertEqual(len(state.queue), 1)
            self.assertTrue(state.error)
            state.retry_at = 0
            with patch.object(self.engine, "publish") as publish:
                self.engine.fill_one()
                publish.assert_called_once()
            prepare.assert_called_once()
            self.assertEqual(len(state.queue), 1)

    def test_track_saved_after_preparation_is_skipped_when_radio_claims_it(self):
        first = self.prepared("a" * 32)
        second = self.prepared("b" * 32)
        second["title"] = "New song"
        self.engine.stations[self.identifier].queue.extend([first, second])
        self.owned.return_value = {"artist:song": "already-saved"}
        self.assertEqual(self.engine.take(self.identifier)["id"], second["id"])

    def test_small_candidate_pool_recycles_history_without_repeating_the_previous_track(self):
        track = self.prepared()
        history = ["b" * 32, "c" * 32]
        with patch.object(
            native_radios.new_music,
            "radio_next",
            side_effect=[
                DiscoveryError(422, "Pool exhausted"),
                {"track": track, "audio_url": "/preview"},
            ],
        ) as next_track:
            with patch.object(native_radios.new_music, "preview_path", return_value=self.directory):
                result = native_radios.prepare_track(
                    "listener", self.context, history, "Previous artist", self.engine.cache
                )
        self.assertEqual(result["id"], track["id"])
        self.assertEqual(next_track.call_args.args[1]["exclude"], [history[-1]])

    def test_cache_cleanup_preserves_buffered_audio(self):
        import os
        import time

        queued = self.prepared("a" * 32)
        orphan = self.prepared("b" * 32)
        self.engine.stations[self.identifier].queue.append(queued)
        for track in [queued, orphan]:
            path = self.engine.cache / (track["id"] + ".mp3")
            os.utime(path, (time.time() - 172800, time.time() - 172800))
        self.engine.cleanup_cache()
        self.assertTrue((self.engine.cache / (queued["id"] + ".mp3")).exists())
        self.assertFalse((self.engine.cache / (orphan["id"] + ".mp3")).exists())

    def test_removing_a_station_deletes_only_its_owned_server_entry(self):
        client = Mock()
        client.call.side_effect = [
            {
                "internetRadioStations": {
                    "internetRadioStation": [
                        {
                            "id": "native",
                            "homePageUrl": "https://music.example/#radio=" + self.identifier,
                        },
                        {"id": "other", "homePageUrl": "https://other.example/"},
                    ]
                }
            },
            {},
        ]
        with patch.object(
            native_radios, "credentials", return_value={"navidrome_password": "test"}
        ):
            with patch.object(native_radios, "Subsonic", return_value=client):
                self.engine.remove(self.identifier)
        client.call.assert_called_with("deleteInternetRadioStation", [("id", "native")])
        restored = native_radios.RadioEngine("listener", "https://music.example", self.directory)
        self.assertEqual(restored.overview(), [])

    def test_failed_server_deletion_preserves_local_station_for_retry(self):
        client = Mock()
        client.call.side_effect = RuntimeError("Server offline")
        with patch.object(
            native_radios, "credentials", return_value={"navidrome_password": "test"}
        ):
            with patch.object(native_radios, "Subsonic", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "Server offline"):
                    self.engine.remove(self.identifier)
        self.assertEqual(len(self.engine.overview()), 1)

    def test_icy_metadata_handles_unicode_quotes_and_sixteen_byte_padding(self):
        data = native_radios.metadata_block({"artist": "Björk", "title": "It's; music"})
        self.assertEqual(len(data), 1 + data[0] * 16)
        self.assertIn("Björk - It’s, music".encode(), data)
        self.assertTrue(data[1:].rstrip(b"\0").endswith(b"';"))

    def test_metadata_cadence_continues_across_track_boundaries(self):
        first = self.prepared("a" * 32)
        second = self.prepared("b" * 32)
        second["title"] = "Second"
        (self.engine.cache / (first["id"] + ".mp3")).write_bytes(b"a" * 10)
        (self.engine.cache / (second["id"] + ".mp3")).write_bytes(b"b" * 30)
        state = self.engine.stations[self.identifier]
        state.queue.extend([first, second])
        with patch.object(native_radios, "META_INTERVAL", 16):
            stream = self.engine.chunks(self.identifier, self.engine.take(self.identifier))
            audio, metadata = b"", []
            while len(metadata) < 2:
                chunk, is_metadata = next(stream)
                if is_metadata:
                    metadata.append(chunk)
                    self.assertEqual(len(audio), len(metadata) * 16)
                else:
                    audio += chunk
            stream.close()
        self.assertEqual(audio, b"a" * 10 + b"b" * 22)
        self.assertTrue(all(b"Second" in m for m in metadata))
