# Contributing

Use uv for Python and Bun for the frontend. Keep changes focused and run the checks from the README.

The frontend is restored from pinned upstream revisions, then modified by `config/feishin-musica.patch`. Edit `vendor/feishin` after restoring it. Regenerate the patch with `git diff HEAD --binary` plus diffs for any new source files; include only source, assets required by that source, and the lockfile. Verify that it applies to a fresh checkout of the pinned commit before submitting it.

Do not commit runtime configuration, credentials, cookies, logs, audio, databases, generated bundles or your personal library. Use fictional fixtures for tests and screenshots. Report security issues privately using GitHub's private vulnerability reporting when available; never post a token or password in an issue.

This project changes Feishin. Preserve its copyright and license notices, and describe upstream changes clearly.
