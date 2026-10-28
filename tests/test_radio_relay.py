from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from musica_sync import radio_relay


class RadioRelayTests(unittest.TestCase):
    def test_only_station_scoped_signatures_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state/discovery/radios.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "stations": [
                            {
                                "id": "station",
                                "streamUrl": "https://ice2.somafm.com/groovesalad-256-mp3",
                            }
                        ]
                    }
                )
            )
            key = b"x" * 32
            with (
                patch.object(radio_relay, "ROOT", root),
                patch.object(radio_relay, "relay_key", return_value=key),
            ):
                token = radio_relay.signature("station", key)
                self.assertEqual(
                    radio_relay.resolve("/radio/station/" + token),
                    "https://ice2.somafm.com/groovesalad-256-mp3",
                )
                self.assertIsNone(radio_relay.resolve("/radio/other/" + token))
                self.assertIsNone(radio_relay.resolve("/radio/station/" + "0" * 32))
                self.assertIsNone(radio_relay.resolve("/radio/../../etc/passwd"))

    def test_arbitrary_targets_and_embedded_authentication_are_rejected(self):
        for value in [
            "https://127.0.0.1/secret",
            "https://example.com/radio",
            "https://ice2.somafm.com.evil.test/radio",
            "https://u:p@ice2.somafm.com/a",
            "https://ice2.somafm.com/a?redirect=http://127.0.0.1",
            "http://ice2.somafm.com/radio",
            "https://ice2.somafm.com:8000/radio",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                radio_relay.validate_upstream(value)
