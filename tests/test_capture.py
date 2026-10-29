"""Verify authenticated Shazam ingestion, durable retries and playlist publication."""

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from musica_sync import capture, capture_server, new_music
from musica_sync.discovery import DiscoveryError


class CaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for module in [capture, new_music]:
            root_patch = patch.object(module, "ROOT", self.root)
            root_patch.start()
            self.addCleanup(root_patch.stop)
        for module in [capture, capture_server]:
            auth_patch = patch.object(
                module, "credentials", return_value={"navidrome_user": "listener"}
            )
            auth_patch.start()
            self.addCleanup(auth_patch.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        owned = patch.object(new_music, "owned_tracks", return_value={})
        self.owned = owned.start()
        self.addCleanup(owned.stop)
        with new_music.database():
            pass

    def test_device_token_is_private_stable_and_required(self):
        token = capture.device_token()
        self.assertEqual(len(token), 43)
        self.assertEqual(token, capture.device_token())
        self.assertEqual(capture.token_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(capture.authenticate("Bearer " + token), "listener")
        for invalid in ["", "Bearer wrong", "Bearer " + token + "x", "Bearer ü"]:
            with self.assertRaises(DiscoveryError) as caught:
                capture.authenticate(invalid)
            self.assertEqual(caught.exception.status, 401)
        capture.token_path().chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "0600"):
            capture.device_token()

    def test_concurrent_shazams_create_one_job_and_one_save(self):
        payload = {"artist": "Artist", "title": "Song"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: capture.submit("listener", payload), range(8)))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(len(capture.saved_tracks("listener")), 1)
        self.assertEqual(len(new_music.jobs("listener")["tracks"]), 1)
        self.assertEqual(capture.saved_tracks("other"), [])

    def test_invalid_recognition_never_creates_a_job(self):
        for payload in [
            {},
            {"artist": [], "title": "Song"},
            {"artist": "A", "title": " "},
            {"artist": "A", "title": "https://localhost/private"},
            {"artist": "A", "title": "bad\nname"},
            {"artist": "A", "title": "x" * 241},
        ]:
            with self.assertRaises(DiscoveryError):
                capture.submit("listener", payload)
        self.assertEqual(new_music.jobs("listener")["tracks"], [])

    def test_owned_song_is_ready_without_download_and_recommendation_is_preserved(self):
        key = new_music.key_for("Artist", "Song")
        with new_music.database() as db:
            db.execute(
                "INSERT INTO recommendations(id,owner,artist,title,reason,score,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, "listener", "Artist", "Song", "Seed", 1, 0),
            )
        self.owned.return_value = {"artist:song": "owned"}
        result = capture.submit("listener", {"artist": "Artist", "title": "Song"})
        self.assertEqual(result["track"]["status"], "ready")
        self.assertEqual(capture.saved_tracks("listener")[0]["song_id"], "owned")
        self.assertFalse(new_music.work_once())
        with new_music.database() as db:
            reason = db.execute("SELECT reason FROM recommendations WHERE id=?", (key,)).fetchone()[
                0
            ]
        self.assertEqual(reason, "Seed")

    def test_failed_import_is_visible_and_retry_completes_the_same_job(self):
        payload = {"artist": "Artist", "title": "Song"}
        capture.submit("listener", payload)
        with patch.object(new_music, "acquire", side_effect=RuntimeError("Source unavailable")):
            with self.assertLogs(new_music.LOGGER, "ERROR"):
                self.assertTrue(new_music.work_once())
        self.assertEqual(capture.saved_tracks("listener")[0]["error"], "Source unavailable")
        capture.submit("listener", payload)
        with patch.object(new_music, "acquire", return_value="imported"):
            self.assertTrue(new_music.work_once())
        self.assertEqual(capture.saved_tracks("listener")[0]["status"], "ready")
        self.assertEqual(len(capture.saved_tracks("listener")), 1)

    def test_playlist_adds_missing_tracks_without_replacing_manual_entries(self):
        client = Mock()
        client.call.side_effect = [
            {"playlists": {"playlist": [{"id": "p", "name": "Da Shazam", "owner": "listener"}]}},
            {"playlist": {"entry": [{"id": "a"}, {"id": "manual"}]}},
            {},
        ]
        capture.sync_playlist("listener", ["a", "b"], client)
        client.call.assert_called_with(
            "updatePlaylist", [("playlistId", "p"), ("songIdToAdd", "b")]
        )
        client.reset_mock()
        client.call.side_effect = [{"playlists": {}}, {}]
        capture.sync_playlist("listener", ["a"], client)
        client.call.assert_called_with("createPlaylist", [("name", "Da Shazam"), ("songId", "a")])

    def test_pair_requires_valid_navidrome_login(self):
        with patch.object(capture, "urlopen") as upstream:
            with self.assertRaises(DiscoveryError):
                capture.pair({"username": "other", "password": "secret"})
            upstream.assert_not_called()
            upstream.side_effect = HTTPError("local", 401, "Denied", {}, None)
            with self.assertRaises(DiscoveryError) as caught:
                capture.pair({"username": "listener", "password": "wrong"})
            self.assertEqual(caught.exception.status, 401)

    def test_http_rejects_unauthorized_cross_origin_and_malformed_requests(self):
        capture_server.ATTEMPTS.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), capture_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def send(path, data=None, **headers):
            body = json.dumps(data).encode() if data is not None else None
            request = Request(
                base + path, data=body, headers={"Content-Type": "application/json", **headers}
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)

        self.assertEqual(send("/api/capture", {"artist": "A", "title": "B"})[0], 401)
        self.assertEqual(send("/api/capture/jobs")[0], 401)
        self.assertEqual(send("/api/radios")[0], 401)
        auth = {"Authorization": "Bearer " + capture.device_token()}
        self.assertEqual(send("/api/capture", [], **auth)[0], 400)
        self.assertEqual(send("/api/capture", {}, Origin="https://evil.example", **auth)[0], 403)
        status, result = send("/api/capture", {"artist": "A", "title": "B"}, **auth)
        self.assertEqual(status, 202)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(send("/api/capture/jobs", **auth)[1]["tracks"]), 1)
