from __future__ import annotations

import unittest

from musica_sync.metadata import derive_tags


class MetadataTests(unittest.TestCase):
    def test_auto_generated_track_keeps_real_album_metadata(self) -> None:
        tags = derive_tags(
            path_stem="Anesthesia [CcPT_vs1qc8]",
            current={
                "title": "Anesthesia",
                "artist": "typeonegative",
                "album": "script",
                "albumartist": None,
                "year": None,
            },
            info={
                "title": "Anesthesia",
                "track": "Anesthesia",
                "artist": "Type O Negative",
                "album": "Life Is Killing Me",
                "creator": "Type O Negative",
                "release_year": 2003,
            },
            repair_legacy_playlist_album="script",
        )

        self.assertEqual(tags["title"], "Anesthesia")
        self.assertEqual(tags["artist"], "Type O Negative")
        self.assertEqual(tags["album"], "Life Is Killing Me")
        self.assertEqual(tags["albumartist"], "Type O Negative")
        self.assertEqual(tags["year"], 2003)

    def test_official_video_title_is_parsed_into_track_and_album(self) -> None:
        raw_title = (
            "Daddy Yankee | 08. Lo que Pasó Pasó - "
            "Barrio Fino (Bonus Track Version) (Audio Oficial)"
        )
        tags = derive_tags(
            path_stem="Daddy Yankee [8MpVPbN3_eE]",
            current={
                "title": raw_title,
                "artist": None,
                "album": "script",
                "albumartist": None,
                "year": None,
            },
            info={
                "title": raw_title,
                "uploader": "Daddy Yankee",
                "channel": "Daddy Yankee",
                "upload_date": "20170724",
            },
            repair_legacy_playlist_album="script",
        )

        self.assertEqual(tags["artist"], "Daddy Yankee")
        self.assertEqual(tags["title"], "Lo que Pasó Pasó")
        self.assertEqual(tags["album"], "Barrio Fino (Bonus Track Version)")
        self.assertEqual(tags["albumartist"], "Daddy Yankee")
        self.assertEqual(tags["year"], 2017)

    def test_non_ascii_album_metadata_is_preserved(self) -> None:
        tags = derive_tags(
            path_stem="悲しみがとまらない [H9R1Hl6sJeE]",
            current={
                "title": "悲しみがとまらない　I CAN'T STOP THE LONELINESS",
                "artist": "ANRI 杏里 Official",
                "album": "script",
                "albumartist": None,
                "year": None,
            },
            info={
                "track": "悲しみがとまらない　I CAN'T STOP THE LONELINESS",
                "artist": "Anri",
                "album": "Timely!!",
                "creator": "Anri",
                "release_year": 2011,
            },
            repair_legacy_playlist_album="script",
        )

        self.assertEqual(tags["artist"], "Anri")
        self.assertEqual(tags["album"], "Timely!!")
        self.assertEqual(tags["albumartist"], "Anri")
        self.assertEqual(tags["year"], 2011)

    def test_last_resort_fallback_uses_title_as_album(self) -> None:
        tags = derive_tags(
            path_stem="mix-file",
            current={
                "title": "the only dnb/jungle mix you need for work",
                "artist": "Toky",
                "album": None,
                "albumartist": None,
                "year": None,
            },
            info=None,
        )

        self.assertEqual(tags["title"], "the only dnb/jungle mix you need for work")
        self.assertEqual(tags["album"], "the only dnb/jungle mix you need for work")
        self.assertEqual(tags["albumartist"], "Toky")


if __name__ == "__main__":
    unittest.main()
