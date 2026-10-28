from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from musica_sync import cli, navidrome, new_music
from scripts import setup


class PortableSetupTests(unittest.TestCase):
    def test_local_library_does_not_require_a_youtube_playlist(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(cli, "ENV_FILE", Path(folder) / ".env"):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(cli.read_settings().playlist_url, "")
                    with self.assertRaisesRegex(SystemExit, "YT_PLAYLIST_URL"):
                        cli.sync(argparse.Namespace(dry_run=True))

    def test_service_url_uses_config_and_rejects_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("NAVIDROME_URL=http://localhost:4534\n")
            with patch.object(navidrome, "ROOT", root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(navidrome.navidrome_url(), "http://localhost:4534")
                with patch.dict(os.environ, {"NAVIDROME_URL": "http://user:pass@localhost"}):
                    with self.assertRaises(ValueError):
                        navidrome.navidrome_url()

    def test_setup_verifies_credentials_and_saves_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(setup, "ROOT", root), patch.object(setup, "Subsonic") as client:
                setup.connect("demo-user", "test-password")
                client.return_value.call.assert_called_once_with("ping")
                path = root / "state/sonic/credentials.json"
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(json.loads(path.read_text())["navidrome_user"], "demo-user")
                with self.assertRaisesRegex(SystemExit, "already exist"):
                    setup.connect("other", "another-password")

    def test_scan_uses_server_api_and_waits_for_completion(self):
        with (
            patch.object(
                new_music,
                "credentials",
                return_value={
                    "navidrome_user": "demo-user",
                    "navidrome_password": "test-password",
                },
            ),
            patch.object(new_music, "Subsonic") as client,
            patch.object(new_music.time, "sleep"),
        ):
            client.return_value.call.side_effect = [
                {},
                {"scanStatus": {"scanning": True}},
                {"scanStatus": {"scanning": False}},
            ]
            new_music.scan_library()
            self.assertEqual(
                [call.args[0] for call in client.return_value.call.call_args_list],
                ["startScan", "getScanStatus", "getScanStatus"],
            )


if __name__ == "__main__":
    unittest.main()
