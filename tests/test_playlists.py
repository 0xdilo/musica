from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from musica_sync.playlists import (
    Track,
    count_genres,
    filter_tracks,
    load_tracks,
    parse_query,
    rebuild_auto_playlists,
    render_m3u,
    safe_filename,
    split_genres,
    top_genres,
    track_matches,
    write_playlist,
)


def make_track(
    *,
    artist: str = "Artist",
    title: str = "Title",
    album: str = "Album",
    year: int | None = 2000,
    genres: tuple[str, ...] = (),
    path: str = "/library/Albums/Artist/Album/01 - Title.opus",
) -> Track:
    return Track(
        path=Path(path),
        artist=artist,
        title=title,
        album=album,
        year=year,
        genres=genres,
    )


class SplitGenresTests(unittest.TestCase):
    def test_drops_youtube_category_junk(self) -> None:
        self.assertEqual(split_genres("Music; Entertainment; Hip-Hop"), ("Hip-Hop",))

    def test_dedupes_case_and_separator_variants(self) -> None:
        self.assertEqual(split_genres("Hip-Hop, hip-hop / Rap | rap"), ("Hip-Hop", "Rap"))

    def test_handles_beets_null_byte_separators(self) -> None:
        self.assertEqual(split_genres("Hip Hop\\␀Rap"), ("Hip Hop", "Rap"))
        self.assertEqual(split_genres("Ambient␀Breakcore"), ("Ambient", "Breakcore"))
        self.assertEqual(split_genres("Hip Hop\x00Rap"), ("Hip Hop", "Rap"))

    def test_returns_empty_for_blank(self) -> None:
        self.assertEqual(split_genres(""), ())
        self.assertEqual(split_genres(None), ())


class ParseQueryTests(unittest.TestCase):
    def test_decade_token_normalises_to_century(self) -> None:
        query = parse_query("90s hip-hop")
        self.assertEqual(query.decades, (1990,))
        self.assertEqual(query.free_terms, ("hip-hop",))

    def test_full_decade_and_explicit_year(self) -> None:
        query = parse_query("2010s 1999 country")
        self.assertEqual(query.decades, (2010,))
        self.assertEqual(query.years, (1999,))
        self.assertEqual(query.free_terms, ("country",))

    def test_genre_prefix_routes_into_genre_terms(self) -> None:
        query = parse_query("genre:synthwave dark")
        self.assertEqual(query.genre_terms, ("synthwave",))
        self.assertEqual(query.free_terms, ("dark",))

    def test_blank_query_is_inert(self) -> None:
        query = parse_query("   ")
        self.assertEqual(query.free_terms, ())
        self.assertEqual(query.decades, ())


class TrackMatchesTests(unittest.TestCase):
    def test_matches_decade_and_genre_substring(self) -> None:
        track = make_track(year=1995, genres=("East Coast Hip-Hop",))
        query = parse_query("90s hip-hop")
        self.assertTrue(track_matches(track, query))

    def test_rejects_when_decade_mismatches(self) -> None:
        track = make_track(year=2005, genres=("Hip-Hop",))
        query = parse_query("90s hip-hop")
        self.assertFalse(track_matches(track, query))

    def test_free_term_matches_artist_or_album(self) -> None:
        track = make_track(artist="Daddy Yankee", album="Barrio Fino", genres=("Reggaeton",))
        self.assertTrue(track_matches(track, parse_query("daddy")))
        self.assertTrue(track_matches(track, parse_query("barrio")))
        self.assertFalse(track_matches(track, parse_query("country")))

    def test_year_range_query(self) -> None:
        track = make_track(year=1999, genres=("Pop",))
        self.assertTrue(track_matches(track, parse_query("1999")))
        self.assertFalse(track_matches(track, parse_query("2000")))


class FilterTracksTests(unittest.TestCase):
    def test_filters_and_sorts_alphabetically(self) -> None:
        tracks = [
            make_track(artist="Zeta", title="b", genres=("Country",), year=1995),
            make_track(artist="Alpha", title="a", genres=("Country",), year=1992),
            make_track(artist="Beta", title="c", genres=("Pop",), year=1995),
        ]
        result = filter_tracks(tracks, parse_query("90s country"))
        self.assertEqual([t.artist for t in result], ["Alpha", "Zeta"])

    def test_blank_query_returns_nothing(self) -> None:
        self.assertEqual(filter_tracks([make_track()], parse_query("")), [])


class TopGenresTests(unittest.TestCase):
    def test_counts_unique_genres_per_track_and_orders_by_frequency(self) -> None:
        tracks = [
            make_track(genres=("Hip-Hop", "Rap")),
            make_track(genres=("Hip-Hop",)),
            make_track(genres=("Country",)),
            make_track(genres=("Hip-Hop", "hip-hop")),
        ]
        counts = count_genres(tracks)
        self.assertEqual(counts["Hip-Hop"], 3)
        self.assertEqual(counts["Country"], 1)

        ranked = top_genres(tracks, limit=10, min_tracks=2)
        self.assertEqual([genre for genre, _ in ranked], ["Hip-Hop"])

    def test_zero_limit_or_min_disables(self) -> None:
        tracks = [make_track(genres=("Hip-Hop",))]
        self.assertEqual(top_genres(tracks, limit=0, min_tracks=1), [])
        self.assertEqual(top_genres(tracks, limit=5, min_tracks=0), [])


class RenderM3UTests(unittest.TestCase):
    def test_uses_relative_paths_and_extinf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            track_path = root / "Albums" / "Artist" / "song.opus"
            track_path.parent.mkdir(parents=True)
            track_path.write_bytes(b"x")
            track = make_track(path=str(track_path))

            playlist_path = root / "Playlists" / "mix.m3u"
            playlist_path.parent.mkdir()
            content = render_m3u([track], playlist_path)

        self.assertIn("#EXTM3U", content)
        self.assertIn("#EXTINF:-1,Artist - Title", content)
        self.assertIn("../Albums/Artist/song.opus", content)


class SafeFilenameTests(unittest.TestCase):
    def test_strips_unsafe_characters(self) -> None:
        self.assertEqual(safe_filename("90s / hip-hop?"), "90s hip-hop")

    def test_falls_back_to_default_when_empty(self) -> None:
        self.assertEqual(safe_filename("///"), "playlist")


class RebuildAutoPlaylistsTests(unittest.TestCase):
    def test_daily_shuffle_preserves_membership_and_is_stable_between_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            tracks = [
                make_track(
                    artist=f"Artist {i}",
                    title=f"Track {i}",
                    path=f"/library/{i}.mp3",
                    genres=("Rock",),
                )
                for i in range(12)
            ]

            def build(day: date) -> list[str]:
                report = rebuild_auto_playlists(
                    tracks,
                    playlists_dir=destination,
                    limit=5,
                    min_tracks=5,
                    day=day,
                )
                return [
                    line
                    for line in report.written[0].path.read_text().splitlines()
                    if line and not line.startswith("#")
                ]

            first = build(date(2026, 10, 27))
            same_day = build(date(2026, 10, 27))
            tomorrow = build(date(2026, 10, 28))
            self.assertEqual(first, same_day)
            self.assertNotEqual(first, tomorrow)
            self.assertEqual(set(first), set(tomorrow))
            self.assertEqual(len(first), len(tracks))

    def test_writes_per_genre_files_and_removes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            playlists_dir = Path(tmp) / "Playlists"
            playlists_dir.mkdir()
            stale = playlists_dir / "Auto - Old.m3u"
            stale.write_text("stale", encoding="utf-8")

            tracks = [
                make_track(artist="A", genres=("Hip-Hop",)),
                make_track(artist="B", genres=("Hip-Hop",)),
                make_track(artist="C", genres=("Country",)),
                make_track(artist="D", genres=("Country",)),
            ]
            report = rebuild_auto_playlists(
                tracks,
                playlists_dir=playlists_dir,
                limit=5,
                min_tracks=2,
                prefix="Auto - ",
            )

            written_names = sorted(result.name for result in report.written)
            self.assertEqual(written_names, ["Country", "Hip-Hop"])
            self.assertFalse(stale.exists())
            self.assertIn(stale, report.removed)
            self.assertTrue((playlists_dir / "Auto - Hip-Hop.m3u").exists())

    def test_skips_when_min_tracks_unmet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            playlists_dir = Path(tmp) / "Playlists"
            playlists_dir.mkdir()
            tracks = [make_track(genres=("Country",))]
            report = rebuild_auto_playlists(
                tracks,
                playlists_dir=playlists_dir,
                limit=5,
                min_tracks=3,
                prefix="Auto - ",
            )
            self.assertEqual(report.written, [])


class LoadTracksTests(unittest.TestCase):
    def test_reads_path_blob_and_decodes_genres(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "library.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE items (
                        path BLOB,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        year INTEGER,
                        genres TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        b"/lib/song.opus",
                        "Title",
                        "Artist",
                        "Album",
                        1999,
                        "Music; Hip-Hop; Rap",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            tracks = load_tracks(db_path)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].path, Path("/lib/song.opus"))
        self.assertEqual(tracks[0].genres, ("Hip-Hop", "Rap"))
        self.assertEqual(tracks[0].year, 1999)


class WritePlaylistTests(unittest.TestCase):
    def test_creates_directory_and_file_with_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            track_path = root / "Albums" / "song.opus"
            track_path.parent.mkdir(parents=True)
            track_path.write_bytes(b"x")
            track = make_track(path=str(track_path))

            playlists_dir = root / "Playlists"
            result = write_playlist(
                "country mix",
                [track],
                playlists_dir=playlists_dir,
                prefix="Mix - ",
            )

            self.assertEqual(result.path.name, "Mix - country mix.m3u")
            self.assertTrue(result.path.exists())
            self.assertEqual(result.track_count, 1)
            content = result.path.read_text(encoding="utf-8")
            self.assertIn("../Albums/song.opus", content)


if __name__ == "__main__":
    unittest.main()
