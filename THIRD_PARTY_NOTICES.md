# Third-party notices

Musica is a small backend and a set of modifications to existing music software. It does not claim authorship of those projects.

- **Feishin**, Jeff Vli and contributors: GPL-3.0. The source revision is pinned in `config/feishin-source.json`; `scripts/feishin-restore-source.py` obtains that exact revision and applies the bundled patch. Feishin's original LICENSE and notices remain in the restored source.
- **node-mpv**, Jan Holub and contributors: MIT. Restored from its pinned upstream revision, with its LICENSE preserved.
- **Butterchurn presets**, Jordan Berg: MIT. The frontend patch includes the original preset license and notices.
- **TagLib tag-property data**: its upstream notice and LICENSE are retained in the restored frontend.
- **Navidrome**, **beets**, **yt-dlp**, **Last.fm**, **SomaFM**, and optional **AudioMuse-AI** are independent projects/services. Their code, binaries, music catalogues and models are not bundled in this repository; their own licenses and service terms apply.

The frontend's dependency licenses are available in its package metadata and restored source. No recordings, standalone album artwork files, browser cookies, API keys or service binaries are part of this release. Screenshots show real tracks and cover artwork from the maintainer’s installation with permission. The visible album artwork belongs to its respective rights holders and is shown to illustrate the interface.
