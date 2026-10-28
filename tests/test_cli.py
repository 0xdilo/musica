from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from musica_sync import cli
from musica_sync.playlists import Track


def make_settings(**overrides: object) -> cli.Settings:
    defaults: dict[str, object] = {
        "playlist_url": "https://example.com/playlist",
        "playlist_items": None,
        "cookies_from_browser": None,
        "cookies_file": None,
        "acoustid_api_key": None,
        "lastfm_api_key": None,
        "local_watch_dirs": (cli.DEFAULT_LOCAL_WATCH_DIR,),
        "local_ready_age_seconds": 10,
        "sync_interval_minutes": 15,
        "auto_playlist_top_genres": 20,
        "auto_playlist_min_tracks": 5,
        "yt_audio_format": "mp3",
        "yt_audio_quality": "0",
        "yt_break_on_existing": True,
        "yt_skip_video_ids": (),
    }
    defaults.update(overrides)
    return cli.Settings(**defaults)


class CliTests(unittest.TestCase):
    def test_parse_path_list_defaults_to_local_watch_dir(self) -> None:
        self.assertEqual(cli.parse_path_list(""), (cli.DEFAULT_LOCAL_WATCH_DIR,))

    def test_collect_local_candidates_skips_fresh_and_imported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            old_file = watch_dir / "old.mp3"
            old_file.write_bytes(b"old")
            fresh_file = watch_dir / "fresh.mp3"
            fresh_file.write_bytes(b"fresh")

            os.utime(old_file, (100, 100))
            os.utime(fresh_file, (195, 195))

            state_file = root / "imported-signatures.txt"
            state_file.write_text(f"{cli.build_local_signature(old_file)}\n", encoding="utf-8")

            settings = make_settings(local_watch_dirs=(watch_dir,))

            with (
                patch.object(cli, "LOCAL_STATE_FILE", state_file),
                patch.object(cli, "INBOX_DIR", root / "inbox"),
                patch.object(cli, "LIBRARY_DIR", root / "library"),
                patch.object(cli, "LOCAL_STAGE_DIR", root / "local-stage"),
                patch.object(cli.time, "time", return_value=200),
            ):
                candidates = cli.collect_local_candidates(settings)

            self.assertEqual(candidates, [])

    def test_stage_local_candidates_copies_cover_art(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch_dir = root / "watch"
            album_dir = watch_dir / "album"
            album_dir.mkdir(parents=True)

            track = album_dir / "song.mp3"
            track.write_bytes(b"track")
            cover = album_dir / "cover.jpg"
            cover.write_bytes(b"cover")

            candidate = cli.LocalCandidate(
                source_root=watch_dir,
                source_path=track,
                signature="sig",
            )

            with patch.object(cli, "LOCAL_STAGE_DIR", root / "local-stage"):
                batch_dir = cli.stage_local_candidates([candidate])

            staged_track = batch_dir / cli.local_root_label(watch_dir) / "album" / "song.mp3"
            staged_cover = batch_dir / cli.local_root_label(watch_dir) / "album" / "cover.jpg"
            self.assertTrue(staged_track.exists())
            self.assertTrue(staged_cover.exists())

    def test_mark_local_candidates_imported_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "imported-signatures.txt"

            candidate = cli.LocalCandidate(
                source_root=root / "watch",
                source_path=root / "watch" / "song.mp3",
                signature="abc123",
            )
            with patch.object(cli, "LOCAL_STATE_FILE", state_file):
                cli.mark_local_candidates_imported([candidate])

            self.assertEqual(state_file.read_text(encoding="utf-8"), "abc123\n")

    def test_build_yt_dlp_command_uses_fast_playlist_options(self) -> None:
        settings = make_settings(yt_skip_video_ids=("abc123def45",))

        command = cli.build_yt_dlp_command(settings, dry_run=False, probe_only=False)

        self.assertIn("--lazy-playlist", command)
        self.assertIn("--break-on-existing", command)
        self.assertIn("--audio-format", command)
        self.assertEqual(command[command.index("--audio-format") + 1], "mp3")
        self.assertIn("--audio-quality", command)
        self.assertEqual(command[command.index("--audio-quality") + 1], "0")
        self.assertIn("id != 'abc123def45'", command)

    def test_build_yt_dlp_command_skips_break_when_playlist_items_are_pinned(self) -> None:
        settings = make_settings(playlist_items="1,7")

        command = cli.build_yt_dlp_command(settings, dry_run=False, probe_only=False)

        self.assertNotIn("--break-on-existing", command)

    def test_quarantine_remaining_inbox_files_moves_audio_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            review = root / "review" / "inbox-leftovers"
            audio = inbox / "song [abc123def45].opus"
            info = inbox / "song [abc123def45].info.json"
            audio.write_bytes(b"audio")
            info.write_text("{}", encoding="utf-8")

            with patch.object(cli, "INBOX_LEFTOVERS_DIR", review):
                count, quarantine_dir = cli.quarantine_remaining_inbox_files(inbox)

            self.assertEqual(count, 1)
            self.assertIsNotNone(quarantine_dir)
            assert quarantine_dir is not None
            self.assertFalse(audio.exists())
            self.assertFalse(info.exists())
            self.assertTrue((quarantine_dir / audio.name).exists())
            self.assertTrue((quarantine_dir / info.name).exists())

    def test_find_wanted_matches_scores_artist_and_title(self) -> None:
        track = Track(
            path=Path("/library/song.mp3"),
            artist="The Cure",
            title="Jumping Someone Else's Train",
            album="Boys Don't Cry",
            year=1979,
            genres=("Post-Punk",),
        )

        matches = cli.find_wanted_matches("cure jumping train", [track])

        self.assertEqual(matches, [track])

    def test_append_wanted_entry_dedupes_active_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wanted_dir = Path(tmp)
            wanted_file = wanted_dir / "wanted-tracks.jsonl"
            searches_file = wanted_dir / "searches.txt"

            with (
                patch.object(cli, "WANTED_DIR", wanted_dir),
                patch.object(cli, "WANTED_FILE", wanted_file),
                patch.object(cli, "WANTED_SEARCHES_FILE", searches_file),
            ):
                self.assertTrue(cli.append_wanted_entry("Artist Title"))
                self.assertFalse(cli.append_wanted_entry("artist title"))

            self.assertEqual(len(wanted_file.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(searches_file.read_text(encoding="utf-8"), "Artist Title\n")

    def test_render_beets_config_enables_lastgenre_when_key_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "config.runtime.yaml"
            with patch.object(cli, "BEETS_RUNTIME_CONFIG", runtime):
                cli.render_beets_config(make_settings(lastfm_api_key="abc123"))
                rendered = yaml.safe_load(runtime.read_text(encoding="utf-8"))

        self.assertIn("lastgenre", rendered["plugins"])
        self.assertEqual(rendered["lastfm"], {"api_key": "abc123"})
        self.assertTrue(rendered["lastgenre"]["auto"])

    def test_render_beets_config_strips_lastgenre_when_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "config.runtime.yaml"
            with patch.object(cli, "BEETS_RUNTIME_CONFIG", runtime):
                cli.render_beets_config(make_settings(lastfm_api_key=None))
                rendered = yaml.safe_load(runtime.read_text(encoding="utf-8"))

        self.assertNotIn("lastgenre", rendered["plugins"])
        self.assertNotIn("lastgenre", rendered)
        self.assertNotIn("lastfm", rendered)


if __name__ == "__main__":
    unittest.main()
