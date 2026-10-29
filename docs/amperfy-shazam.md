# Amperfy with Shazam saves

Use Amperfy as the phone player and Navidrome as the library. Musica Capture replaces the web player with a small private setup/status page and keeps the existing SomaFM relay running.

## Start the companion

Keep Navidrome and the automatic acquisition worker running. Configure the companion:

```sh
cp config/capture.env.example config/capture.env
```

Set `MUSICA_CAPTURE_PUBLIC_URL` to the companion's private HTTPS address and `MUSICA_LIBRARY_PUBLIC_URL` to the Navidrome address reachable from the phone. `NAVIDROME_URL` is the backend-facing Navidrome address. Then run:

```sh
uv run python scripts/run-capture.py
```

The service binds to loopback port 9181 by default. Put it behind your authenticated private network/HTTPS proxy. It is designed for the existing single Navidrome account in `state/sonic/credentials.json`.

Open the private page on the phone, log in with that account, and follow its setup steps. No Shazam account password, Apple Music subscription or Spotify integration is required. The phone must reach the server to send a recognized track; there is no offline retry queue on the phone.

## What stays running

- Navidrome: library, playlists and Internet Radio stations for Amperfy.
- The existing `musica_sync.new_music` worker: matching, download, tagging, duplicate checking and import.
- Musica Capture: authenticated ingestion, import status, playlist publication and radio relay.
- Existing local imports, curation and analysis, if you use them independently of the web player.

The old Feishin web service can be stopped after updating the proxy to the companion. Keep the relay at its existing public base URL so previously configured radio stations continue working. The web player's source and assets can remain on disk for rollback. Enable `MUSICA_NATIVE_RADIOS=true` in `config/capture.env` for native discovery streams. Create a station from the private page using a genre, favourites, personal recommendations or a playlist. Each station is published to Navidrome only after audio has been prepared, then appears in Amperfy’s Radio section. Refresh the radio list in the app after creating one.

## Credentials and recovery

Capture generates a random token in `state/capture/token` with mode `0600`. The setup page returns it only after verifying a Navidrome login. It grants the ability to submit saves and read their status for this installation; the shortcut does not need your Navidrome password. The page keeps the token in browser session storage. “Scollega pagina” clears that browser session and does not revoke an installed shortcut.

To revoke installed shortcuts, stop Capture, remove the token file, then restart Capture. Log in again and reconfigure your own shortcut with the new configuration. Never publish the connection configuration or include it in screenshots.

The acquisition queue and save history persist in `state/new-music/queue.sqlite`. Repeated recognitions reuse the same job. Failed imports stay visible and can be retried from the page. Ready songs are added to **Da Shazam**, typically within 15 seconds after the library import. Refresh playlists in Amperfy if its local cache has not picked up the change.

Recognition depends on Shazam finding the recording. A recognized recording may still be unavailable from the configured acquisition source; that failure remains visible rather than being reported as imported.

## Discovery stations and curated live stations

Native discovery stations run in the companion process and expose a continuous 160 kbps stereo MP3 stream with ICY track metadata. They reuse the existing recommendation and temporary-preview pipeline. Two tracks are buffered ahead, remembered across restarts, and recently offered tracks are excluded from subsequent choices. Cached recommendations that have since been imported are skipped. Acquisition into the permanent library still requires an explicit Shazam save.

There is one preparation worker and up to four simultaneous listeners. A newly created station needs time to prepare before appearing in Navidrome. A ready station starts from its buffered tracks; errors remain visible on the companion page and preparation retries after a minute. If a provider stays unavailable longer than the buffer lasts, playback can stop and needs restarting. External service availability and recommendation quality are not guaranteed.

Buffers and history live in `state/native-radios/stations.json`; normalized audio lives under `state/native-radios/audio`. Unreferenced audio is pruned after a day or when the cache exceeds 512 MiB. Queued tracks and files used within the past hour are retained, so the limit is soft. The existing preview worker also cleans up its own temporary downloads. Station URLs carry a scoped signature and should remain inside the private network.

The private page creates stations without changing the iPhone app. A playlist-based discovery station plays new recommendations; play the original playlist directly in Amperfy when you want its existing tracks. Native streams have radio semantics and do not expose per-song seek or queue editing in Amperfy. Playback and lock-screen controls must still be checked on the actual phone.

For additional curated live stations, the maintained source list is `config/radio-stations.json`. Its streams come from the official Post-Rock Nation, DKFM and Sanctuary listening pages. Verify the streams first:

```sh
uv run python scripts/install-radio-stations.py
```

Then publish them, preserving existing station entries:

```sh
uv run python scripts/install-radio-stations.py --install
```

The installer verifies audio before publishing and saves the previous station list locally. Re-running it updates entries with the same homepage instead of creating duplicates. These stations are independent broadcasts, separate from the personalised discovery streams.
