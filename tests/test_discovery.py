from __future__ import annotations

import unittest
from unittest.mock import Mock

from musica_sync.genres import music_genres
from musica_sync.navidrome import Subsonic, validate_local_url
from musica_sync.radios import music_channels, publish, stream_urls


class DiscoveryTests(unittest.TestCase):
    def test_radio_playlist_requires_direct_https_audio(self):
        playlist = "[playlist]\nFile1=http://legacy.example/audio\nFile2=https://radio.example/a\n"
        self.assertEqual(stream_urls(playlist), ["https://radio.example/a"])
        with self.assertRaisesRegex(ValueError, "no HTTPS"):
            stream_urls("[playlist]\nFile1=http://legacy.example/audio\n")

    def test_radio_playlist_rejects_embedded_credentials(self):
        with self.assertRaisesRegex(ValueError, "credentials"):
            stream_urls("[playlist]\nFile1=https://user:secret@radio.example/a\n")

    def test_catalogue_excludes_spoken_and_seasonal_channels(self):
        channels = [
            {"id": "jazz", "genre": "jazz"},
            {"id": "talk", "genre": "spoken"},
            {"id": "xmas", "genre": "holiday|rock"},
            {"id": "defcon", "genre": "electronic|specials"},
        ]
        self.assertEqual(
            [c["id"] for c in music_channels({"channels": channels})], ["jazz", "defcon"]
        )

    def test_import_updates_matching_station_and_preserves_user_stations(self):
        client = Mock(spec=Subsonic)
        client.call.return_value = {
            "internetRadioStations": {
                "internetRadioStation": [
                    {"id": "mine", "name": "User Radio", "homePageUrl": "https://example.com"},
                    {"id": "soma", "homePageUrl": "https://somafm.com/jazz/"},
                ]
            }
        }
        publish(
            client,
            [
                {
                    "name": "Jazz",
                    "genre": "jazz",
                    "streamUrl": "https://radio.example/a",
                    "homepageUrl": "https://somafm.com/jazz/",
                }
            ],
        )
        self.assertEqual(client.call.call_count, 2)
        method, values = client.call.call_args.args
        self.assertEqual(method, "updateInternetRadioStation")
        self.assertIn(("id", "soma"), values)

    def test_video_categories_are_removed_without_damaging_real_genres(self):
        self.assertEqual(
            music_genres(
                ["Music", "People & Blogs", "Beautiful Music", "R&B/Soul", "Chamber Music"]
            ),
            ["Beautiful Music", "R&B/Soul", "Chamber Music"],
        )

    def test_server_urls_cannot_embed_secrets_or_queries(self):
        for url in ["file:///etc/passwd", "https://u:secret@host", "https://host/?token=secret"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_local_url(url)

    def test_subsonic_authentication_stays_out_of_request_url(self):
        client = Subsonic("http://127.0.0.1:4533", "test", "secret")
        request = client.request("getGenres")
        self.assertNotIn("secret", request.full_url)
        self.assertNotIn("secret", request.data.decode())
        self.assertEqual(request.get_method(), "POST")


if __name__ == "__main__":
    unittest.main()
