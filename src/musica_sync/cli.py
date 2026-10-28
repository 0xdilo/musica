from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import yaml
from mediafile import MediaFile

from musica_sync.genres import clean_library_genres
from musica_sync.metadata import derive_tags, load_info_json
from musica_sync.playlists import (
    filter_tracks,
    fold,
    load_tracks,
    parse_query,
    rebuild_auto_playlists,
    write_playlist,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"
BEETS_BASE_CONFIG = ROOT_DIR / "config" / "beets" / "config.yaml"
BEETS_DATA_DIR = ROOT_DIR / "data" / "beets"
BEETS_LIBRARY_DB = BEETS_DATA_DIR / "library.db"
BEETS_RUNTIME_CONFIG = BEETS_DATA_DIR / "config.runtime.yaml"
NAVIDROME_CONFIG = ROOT_DIR / "data" / "navidrome" / "navidrome.toml"
LOCAL_NAVIDROME_BIN = ROOT_DIR / "bin" / "navidrome"
INBOX_DIR = ROOT_DIR / "music" / "inbox"
LIBRARY_DIR = ROOT_DIR / "music" / "library"
PLAYLISTS_DIR = LIBRARY_DIR / "Playlists"
AUTO_PLAYLIST_PREFIX = "Auto - "
QUERY_PLAYLIST_PREFIX = "Mix - "
DEFAULT_LOCAL_WATCH_DIR = ROOT_DIR / "music" / "nicotine-inbox"
LOCAL_STAGE_DIR = ROOT_DIR / "music" / "local-stage"
LOGS_DIR = ROOT_DIR / "logs"
REVIEW_DIR = ROOT_DIR / "music" / "review"
INBOX_LEFTOVERS_DIR = REVIEW_DIR / "inbox-leftovers"
STATE_DIR = ROOT_DIR / "state" / "yt-dlp"
WANTED_DIR = ROOT_DIR / "state" / "wanted"
WANTED_FILE = WANTED_DIR / "wanted-tracks.jsonl"
WANTED_SEARCHES_FILE = WANTED_DIR / "searches.txt"
LOCAL_STATE_DIR = ROOT_DIR / "state" / "local-import"
LOCAL_STATE_FILE = LOCAL_STATE_DIR / "imported-signatures.txt"
AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mka",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
INBOX_SIDECAR_SUFFIXES = {".info.json", ".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}
LOCAL_ART_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
YOUTUBE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")
WANTED_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Settings:
    playlist_url: str
    playlist_items: str | None
    cookies_from_browser: str | None
    cookies_file: Path | None
    acoustid_api_key: str | None
    lastfm_api_key: str | None
    local_watch_dirs: tuple[Path, ...]
    local_ready_age_seconds: int
    sync_interval_minutes: int
    auto_playlist_top_genres: int
    auto_playlist_min_tracks: int
    yt_audio_format: str
    yt_audio_quality: str | None
    yt_break_on_existing: bool
    yt_skip_video_ids: tuple[str, ...]
    curated_playlists: bool = False


@dataclass(frozen=True)
class LocalCandidate:
    source_root: Path
    source_path: Path
    signature: str


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        cleaned = value.strip().strip("'").strip('"')
        values[key.strip()] = cleaned

    return values


def parse_path_list(raw: str) -> tuple[Path, ...]:
    if not raw.strip():
        return (DEFAULT_LOCAL_WATCH_DIR,)

    paths: list[Path] = []
    seen: set[str] = set()

    for chunk in raw.splitlines():
        for item in chunk.split(os.pathsep):
            cleaned = item.strip()
            if not cleaned:
                continue

            path = Path(cleaned).expanduser()
            key = str(path.resolve(strict=False))
            if key in seen:
                continue

            seen.add(key)
            paths.append(path)

    return tuple(paths) or (DEFAULT_LOCAL_WATCH_DIR,)


def parse_bool(raw: str, *, default: bool) -> bool:
    cleaned = raw.strip().casefold()
    if not cleaned:
        return default

    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True

    if cleaned in {"0", "false", "no", "n", "off"}:
        return False

    raise SystemExit(f"Invalid boolean value: {raw!r}")


def parse_csv(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in raw.replace("\n", ",").split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue

        seen.add(cleaned)
        values.append(cleaned)

    return tuple(values)


def read_settings() -> Settings:
    file_values = load_env_file(ENV_FILE)

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    playlist_url = get("YT_PLAYLIST_URL")

    local_ready_age_seconds = int(get("LOCAL_READY_AGE_SECONDS", "120"))
    if local_ready_age_seconds < 0:
        raise SystemExit("LOCAL_READY_AGE_SECONDS must be zero or greater.")

    cookies_file = get("YT_COOKIES_FILE")

    auto_playlist_top_genres = int(get("AUTO_PLAYLIST_TOP_GENRES", "20"))
    auto_playlist_min_tracks = int(get("AUTO_PLAYLIST_MIN_TRACKS", "5"))
    if auto_playlist_top_genres < 0 or auto_playlist_min_tracks < 0:
        raise SystemExit("AUTO_PLAYLIST_* values must be zero or greater.")

    return Settings(
        playlist_url=playlist_url,
        playlist_items=get("YT_PLAYLIST_ITEMS") or None,
        cookies_from_browser=get("YT_COOKIES_FROM_BROWSER") or None,
        cookies_file=Path(cookies_file).expanduser() if cookies_file else None,
        acoustid_api_key=get("ACOUSTID_API_KEY") or None,
        lastfm_api_key=get("LASTFM_API_KEY") or None,
        local_watch_dirs=parse_path_list(get("LOCAL_WATCH_DIRS")),
        local_ready_age_seconds=local_ready_age_seconds,
        sync_interval_minutes=int(get("SYNC_INTERVAL_MINUTES", "15")),
        auto_playlist_top_genres=auto_playlist_top_genres,
        auto_playlist_min_tracks=auto_playlist_min_tracks,
        yt_audio_format=get("YT_AUDIO_FORMAT", "mp3") or "mp3",
        yt_audio_quality=get("YT_AUDIO_QUALITY", "0") or None,
        yt_break_on_existing=parse_bool(get("YT_BREAK_ON_EXISTING", "true"), default=True),
        yt_skip_video_ids=parse_csv(get("YT_SKIP_VIDEO_IDS")),
        curated_playlists=parse_bool(get("CURATED_PLAYLISTS", "false"), default=False),
    )


def ensure_directories() -> None:
    for path in (
        BEETS_DATA_DIR,
        INBOX_DIR,
        LIBRARY_DIR,
        PLAYLISTS_DIR,
        DEFAULT_LOCAL_WATCH_DIR,
        LOCAL_STAGE_DIR,
        LOGS_DIR,
        REVIEW_DIR,
        INBOX_LEFTOVERS_DIR,
        STATE_DIR,
        WANTED_DIR,
        LOCAL_STATE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def navidrome_command() -> list[str] | None:
    if LOCAL_NAVIDROME_BIN.exists():
        return [str(LOCAL_NAVIDROME_BIN)]

    binary = shutil.which("navidrome")
    if binary:
        return [binary]

    return None


def run_command(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}")
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )


def build_yt_dlp_command(settings: Settings, *, dry_run: bool, probe_only: bool) -> list[str]:
    command = [
        "yt-dlp",
        "--ignore-errors",
        "--continue",
        "--no-overwrites",
        "--yes-playlist",
        "--lazy-playlist",
    ]

    if settings.cookies_file:
        command.extend(["--cookies", str(settings.cookies_file)])
    elif settings.cookies_from_browser:
        command.extend(["--cookies-from-browser", settings.cookies_from_browser])

    if settings.playlist_items:
        command.extend(["--playlist-items", settings.playlist_items])

    if settings.yt_skip_video_ids:
        filters = " & ".join(f"id != '{video_id}'" for video_id in settings.yt_skip_video_ids)
        command.extend(["--match-filters", filters])

    if probe_only:
        command.extend(
            [
                "--flat-playlist",
                "--playlist-end",
                "1",
                "--print",
                "%(id)s | %(title)s",
            ]
        )
    else:
        command.extend(
            [
                "--extract-audio",
                "--audio-format",
                settings.yt_audio_format,
                "--embed-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--embed-metadata",
                "--write-info-json",
                "--download-archive",
                str(STATE_DIR / "download-archive.txt"),
                "--paths",
                f"home:{INBOX_DIR}",
                "-o",
                "%(playlist_title|YouTube Inbox)s/%(title).200B [%(id)s].%(ext)s",
            ]
        )

        if settings.yt_audio_quality and settings.yt_audio_format != "best":
            command.extend(["--audio-quality", settings.yt_audio_quality])

        if settings.yt_break_on_existing and not settings.playlist_items:
            command.append("--break-on-existing")

        if dry_run:
            command.extend(["--flat-playlist", "--playlist-end", "3", "--simulate"])

    command.append(settings.playlist_url)
    return command


def render_beets_config(settings: Settings) -> Path:
    base = yaml.safe_load(BEETS_BASE_CONFIG.read_text(encoding="utf-8"))
    base["directory"] = str(LIBRARY_DIR)
    base["library"] = str(ROOT_DIR / "data/beets/library.db")
    base.setdefault("import", {})["log"] = str(LOGS_DIR / "beets-import.log")
    plugins = list(base.get("plugins", []))

    if settings.acoustid_api_key:
        if "chroma" not in plugins:
            plugins.insert(0, "chroma")
        base["chroma"] = {"auto": True}
        base["acoustid"] = {"apikey": settings.acoustid_api_key}
    else:
        plugins = [plugin for plugin in plugins if plugin != "chroma"]
        base.pop("chroma", None)
        base.pop("acoustid", None)

    if settings.lastfm_api_key:
        if "lastgenre" not in plugins:
            plugins.append("lastgenre")
        base["lastgenre"] = {
            "auto": True,
            "source": "track",
            "count": 4,
            "fallback": "",
            "force": False,
            "separator": "; ",
            "title_case": True,
        }
        base["lastfm"] = {"api_key": settings.lastfm_api_key}
    else:
        plugins = [plugin for plugin in plugins if plugin != "lastgenre"]
        base.pop("lastgenre", None)
        base.pop("lastfm", None)

    base["plugins"] = plugins
    BEETS_RUNTIME_CONFIG.write_text(
        yaml.safe_dump(base, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return BEETS_RUNTIME_CONFIG


def build_beets_command(import_path: Path) -> list[str]:
    return [
        "beet",
        "-c",
        str(BEETS_RUNTIME_CONFIG),
        "import",
        "-q",
        "-g",
        str(import_path),
    ]


def has_audio(root: Path) -> bool:
    return any(path.suffix.lower() in AUDIO_EXTENSIONS for path in root.rglob("*"))


def inbox_has_audio() -> bool:
    return has_audio(INBOX_DIR)


def iter_audio_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            yield path


def iter_inbox_sidecars(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        if any(path.name.endswith(suffix) for suffix in INBOX_SIDECAR_SUFFIXES):
            yield path


def extract_video_id(path: Path) -> str | None:
    match = YOUTUBE_ID_RE.search(path.stem)
    return match.group(1) if match else None


def build_metadata_probe_command(settings: Settings, video_id: str) -> list[str]:
    command = [
        "yt-dlp",
        "--dump-single-json",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    if settings.cookies_file:
        command[1:1] = ["--cookies", str(settings.cookies_file)]
    elif settings.cookies_from_browser:
        command[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
    return command


def backfill_missing_info_json(root: Path, settings: Settings) -> tuple[int, int]:
    scanned = 0
    created = 0

    for path in iter_audio_files(root):
        scanned += 1
        info_path = path.with_suffix(".info.json")
        if info_path.exists():
            continue

        video_id = extract_video_id(path)
        if not video_id:
            continue

        result = subprocess.run(
            build_metadata_probe_command(settings, video_id),
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue

        data = json.loads(result.stdout)
        info_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        created += 1

    return scanned, created


def normalize_tags(
    root: Path,
    *,
    repair_legacy_playlist_album: str | None = None,
) -> tuple[int, int]:
    scanned = 0
    updated = 0

    for path in iter_audio_files(root):
        scanned += 1
        media = MediaFile(path)
        desired = derive_tags(
            path_stem=path.stem,
            current={
                "title": media.title,
                "artist": media.artist,
                "album": media.album,
                "albumartist": media.albumartist,
                "year": media.year,
            },
            info=load_info_json(path),
            repair_legacy_playlist_album=repair_legacy_playlist_album,
        )
        changed = False

        for field in ("title", "artist", "album", "albumartist", "year"):
            value = desired.get(field)
            if value is None:
                continue

            if getattr(media, field) != value:
                setattr(media, field, value)
                changed = True

        if changed:
            media.save()
            updated += 1

    return scanned, updated


def cleanup_inbox_sidecars(root: Path) -> None:
    for path in iter_inbox_sidecars(root):
        path.unlink(missing_ok=True)


def quarantine_remaining_inbox_files(root: Path) -> tuple[int, Path | None]:
    leftovers = list(iter_audio_files(root))
    if not leftovers:
        return 0, None

    quarantine_dir = INBOX_LEFTOVERS_DIR / time.strftime("batch-%Y%m%d-%H%M%S")
    moved: set[Path] = set()

    for audio_path in leftovers:
        related_paths = [audio_path]
        for sibling in sorted(audio_path.parent.iterdir()):
            if sibling == audio_path or not sibling.is_file():
                continue

            if sibling.name.startswith(audio_path.stem) and any(
                sibling.name.endswith(suffix) for suffix in INBOX_SIDECAR_SUFFIXES
            ):
                related_paths.append(sibling)

        for path in related_paths:
            if path in moved or not path.exists():
                continue

            destination = quarantine_dir / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), destination)
            moved.add(path)

    return len(leftovers), quarantine_dir


def clean_wanted_query(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def wanted_terms(raw: str) -> tuple[str, ...]:
    folded = parse_query(raw).raw
    tokens = WANTED_TOKEN_RE.findall(fold(folded))
    return tuple(token for token in tokens if token)


def score_wanted_match(track: object, terms: tuple[str, ...]) -> float:
    if not terms:
        return 0.0

    title = fold(getattr(track, "title", ""))
    artist = fold(getattr(track, "artist", ""))
    album = fold(getattr(track, "album", ""))
    primary = f"{artist} {title}".strip()
    blob = f"{primary} {album} {getattr(track, 'search_blob', '')}".strip()

    score = 0.0
    for term in terms:
        if term in primary:
            score += 2.0
        elif term in blob:
            score += 1.0

    return score / (len(terms) * 2.0)


def find_wanted_matches(query: str, tracks: list[object], *, limit: int = 5) -> list[object]:
    terms = wanted_terms(query)
    if not terms:
        return []

    scored = []
    for track in tracks:
        score = score_wanted_match(track, terms)
        if score >= 0.75:
            scored.append((score, track))
    scored.sort(
        key=lambda item: (
            -item[0],
            fold(getattr(item[1], "artist", "")),
            fold(getattr(item[1], "title", "")),
        )
    )
    return [track for _, track in scored[:limit]]


def load_wanted_entries() -> list[dict[str, str]]:
    if not WANTED_FILE.exists():
        return []

    entries: list[dict[str, str]] = []
    for line in WANTED_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue

        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(raw, dict) and isinstance(raw.get("query"), str):
            entries.append({str(key): str(value) for key, value in raw.items()})

    return entries


def append_wanted_entry(query: str) -> bool:
    cleaned = clean_wanted_query(query)
    if not cleaned:
        raise SystemExit("Provide a song query, e.g. `musica-sync want artist title`.")

    key = fold(cleaned)
    for entry in load_wanted_entries():
        if entry.get("status", "wanted") == "wanted" and fold(entry["query"]) == key:
            return False

    WANTED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "query": cleaned,
        "status": "wanted",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    with WANTED_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    existing_searches = set()
    if WANTED_SEARCHES_FILE.exists():
        existing_searches = {
            line.strip()
            for line in WANTED_SEARCHES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    if cleaned not in existing_searches:
        with WANTED_SEARCHES_FILE.open("a", encoding="utf-8") as handle:
            handle.write(cleaned + "\n")

    return True


def remove_empty_directories(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def managed_roots() -> tuple[Path, ...]:
    return tuple(path.resolve() for path in (INBOX_DIR, LIBRARY_DIR, LOCAL_STAGE_DIR))


def is_under_managed_root(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in managed_roots())


def load_local_state() -> set[str]:
    if not LOCAL_STATE_FILE.exists():
        return set()

    return {
        line.strip()
        for line in LOCAL_STATE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_local_state(signatures: set[str]) -> None:
    LOCAL_STATE_FILE.write_text(
        "\n".join(sorted(signatures)) + ("\n" if signatures else ""),
        encoding="utf-8",
    )


def build_local_signature(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def is_local_file_ready(path: Path, minimum_age_seconds: int, *, now: float | None = None) -> bool:
    current_time = time.time() if now is None else now
    return path.stat().st_mtime <= current_time - minimum_age_seconds


def collect_local_candidates(settings: Settings) -> list[LocalCandidate]:
    imported = load_local_state()
    candidates: list[LocalCandidate] = []
    seen: set[str] = set()

    for watch_dir in settings.local_watch_dirs:
        if not watch_dir.exists():
            print(f"Skipping missing local watch dir: {watch_dir}")
            continue

        for path in iter_audio_files(watch_dir):
            if is_under_managed_root(path):
                continue

            if not is_local_file_ready(path, settings.local_ready_age_seconds):
                continue

            signature = build_local_signature(path)
            if signature in imported or signature in seen:
                continue

            seen.add(signature)
            candidates.append(
                LocalCandidate(
                    source_root=watch_dir,
                    source_path=path,
                    signature=signature,
                )
            )

    return candidates


def local_root_label(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:8]
    return f"{path.name or 'root'}-{digest}"


def copy_local_cover_art(candidates: list[LocalCandidate], batch_dir: Path) -> None:
    copied: set[Path] = set()

    for candidate in candidates:
        source_dir = candidate.source_path.parent
        if source_dir in copied:
            continue

        copied.add(source_dir)
        relative_dir = source_dir.relative_to(candidate.source_root)
        destination_dir = batch_dir / local_root_label(candidate.source_root) / relative_dir

        for art_path in sorted(source_dir.iterdir()):
            if not art_path.is_file() or art_path.suffix.lower() not in LOCAL_ART_EXTENSIONS:
                continue

            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(art_path, destination_dir / art_path.name)


def stage_local_candidates(candidates: list[LocalCandidate]) -> Path:
    batch_dir = LOCAL_STAGE_DIR / time.strftime("batch-%Y%m%d-%H%M%S")
    suffix = 1
    while batch_dir.exists():
        batch_dir = LOCAL_STAGE_DIR / f"{batch_dir.name}-{suffix}"
        suffix += 1

    for candidate in candidates:
        relative_path = candidate.source_path.relative_to(candidate.source_root)
        destination = batch_dir / local_root_label(candidate.source_root) / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate.source_path, destination)

    copy_local_cover_art(candidates, batch_dir)
    return batch_dir


def mark_local_candidates_imported(candidates: list[LocalCandidate]) -> None:
    signatures = load_local_state()
    signatures.update(candidate.signature for candidate in candidates)
    save_local_state(signatures)


def trigger_navidrome_scan() -> bool:
    command = navidrome_command()
    if command is None:
        from musica_sync.new_music import scan_library

        try:
            scan_library()
            return True
        except (RuntimeError, OSError) as exc:
            print(f"Navidrome scan failed: {exc}")
            return False

    scan = subprocess.run(
        [*command, "--configfile", str(NAVIDROME_CONFIG), "scan", "-f"],
        check=False,
        capture_output=True,
        text=True,
    )
    if scan.returncode == 0:
        print("Triggered Navidrome rescan.")
        return True

    detail = scan.stderr.strip() or scan.stdout.strip() or "unknown navidrome scan failure"
    print(f"Navidrome scan failed: {detail}")
    return False


def check_status(name: str, ok: bool, detail: str) -> bool:
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {name}: {detail}")
    return ok


def doctor(_: argparse.Namespace) -> int:
    settings = read_settings()
    ensure_directories()
    beets_config = render_beets_config(settings)

    checks: list[bool] = []
    checks.append(
        check_status("ffmpeg", command_exists("ffmpeg"), "required for yt-dlp post-processing")
    )
    checks.append(
        check_status(
            "fpcalc",
            command_exists("fpcalc"),
            (
                "required for AcoustID/chroma matching"
                if settings.acoustid_api_key
                else "optional; only needed if you later enable AcoustID/chroma"
            ),
        )
    )
    checks.append(
        check_status(
            "acoustid",
            bool(settings.acoustid_api_key),
            "configured in .env" if settings.acoustid_api_key else "not configured",
        )
    )
    checks.append(
        check_status(
            "lastfm",
            bool(settings.lastfm_api_key),
            (
                "configured; lastgenre plugin enabled"
                if settings.lastfm_api_key
                else "not configured; auto playlists will use existing tags only"
            ),
        )
    )
    checks.append(
        check_status(
            "navidrome",
            navidrome_command() is not None,
            "local binary or system package is required for the web server",
        )
    )
    checks.append(
        check_status(
            "local watch dirs",
            all(path.exists() for path in settings.local_watch_dirs),
            ", ".join(str(path) for path in settings.local_watch_dirs),
        )
    )

    try:
        yt_dlp_version = run_command(["yt-dlp", "--version"], capture=True).stdout.strip()
        checks.append(check_status("yt-dlp", True, yt_dlp_version))
    except subprocess.CalledProcessError as exc:
        checks.append(check_status("yt-dlp", False, exc.stderr.strip() or "yt-dlp failed"))

    try:
        beet_version = (
            run_command(
                ["beet", "-c", str(beets_config), "version"],
                capture=True,
            )
            .stdout.splitlines()[0]
            .strip()
        )
        checks.append(check_status("beets", True, beet_version))
    except subprocess.CalledProcessError as exc:
        checks.append(check_status("beets", False, exc.stderr.strip() or "beets failed"))

    try:
        probe = run_command(
            build_yt_dlp_command(settings, dry_run=False, probe_only=True),
            capture=True,
        )
        first_item = (
            probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else "playlist reachable"
        )
        checks.append(check_status("playlist", True, first_item))
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or "yt-dlp could not read the playlist"
        checks.append(check_status("playlist", False, detail))

    return 0 if all(checks) else 1


def sync(args: argparse.Namespace) -> int:
    settings = read_settings()
    if not settings.playlist_url:
        raise SystemExit("Set YT_PLAYLIST_URL in .env before running playlist sync.")
    ensure_directories()
    render_beets_config(settings)

    yt_result = run_command(
        build_yt_dlp_command(settings, dry_run=args.dry_run, probe_only=False),
        check=False,
    )

    if yt_result.returncode not in (0, 1, 101):
        raise SystemExit(f"yt-dlp failed with exit code {yt_result.returncode}")

    if yt_result.returncode == 1:
        print(
            "yt-dlp reported skipped or unavailable playlist entries. "
            "Continuing with imported files."
        )
    elif yt_result.returncode == 101:
        print("yt-dlp reached an archived playlist entry and stopped early.")

    if args.dry_run:
        print("Dry run complete. No files were imported.")
        return 0

    if not inbox_has_audio():
        cleanup_inbox_sidecars(INBOX_DIR)
        remove_empty_directories(INBOX_DIR)
        print("No new audio files were downloaded. Nothing to import.")
        return 0

    probed, created = backfill_missing_info_json(INBOX_DIR, settings)
    if created:
        print(f"Backfilled {created} info.json files for {probed} downloaded tracks.")

    scanned, updated = normalize_tags(INBOX_DIR)
    if scanned:
        print(f"Normalized tags for {updated} of {scanned} downloaded files before import.")

    run_command(build_beets_command(INBOX_DIR))
    leftovers, quarantine_dir = quarantine_remaining_inbox_files(INBOX_DIR)
    if leftovers:
        print(f"Moved {leftovers} duplicate/skipped inbox files to {quarantine_dir}.")
    cleanup_inbox_sidecars(INBOX_DIR)
    remove_empty_directories(INBOX_DIR)
    refresh_library(settings)
    print(
        "Sync complete. "
        f"Next scheduled run should happen every {settings.sync_interval_minutes} minutes."
    )
    return 0


def sync_local(_: argparse.Namespace) -> int:
    settings = read_settings()
    ensure_directories()
    render_beets_config(settings)

    candidates = collect_local_candidates(settings)
    if not candidates:
        print("No ready local audio files found in the watched directories.")
        return 0

    batch_dir = stage_local_candidates(candidates)
    watched_dirs = len({item.source_root for item in candidates})
    print(
        "Prepared local import batch with "
        f"{len(candidates)} files from {watched_dirs} watched dirs."
    )

    try:
        run_command(build_beets_command(batch_dir))
    except subprocess.CalledProcessError:
        print(f"Local import failed. Batch kept at {batch_dir}.")
        raise

    mark_local_candidates_imported(candidates)
    shutil.rmtree(batch_dir, ignore_errors=True)
    refresh_library(settings)
    print("Local import complete.")
    return 0


def refresh_auto_playlists(settings: Settings) -> None:
    clean_library_genres(BEETS_LIBRARY_DB, ROOT_DIR / "backups/genre-cleanup")
    if settings.auto_playlist_top_genres <= 0 or settings.auto_playlist_min_tracks <= 0:
        return

    if settings.curated_playlists:
        from musica_sync.curation import refresh

        refresh(
            limit=settings.auto_playlist_top_genres,
            minimum=max(8, settings.auto_playlist_min_tracks),
        )
        return

    tracks = load_tracks(BEETS_LIBRARY_DB)
    if not tracks:
        return

    PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
    report = rebuild_auto_playlists(
        tracks,
        playlists_dir=PLAYLISTS_DIR,
        limit=settings.auto_playlist_top_genres,
        min_tracks=settings.auto_playlist_min_tracks,
        prefix=AUTO_PLAYLIST_PREFIX,
    )
    print(f"Refreshed {len(report.written)} auto playlists (removed {len(report.removed)} stale).")


def refresh_library(settings: Settings) -> None:
    if settings.curated_playlists and not trigger_navidrome_scan():
        raise RuntimeError("Navidrome scan failed; curated playlists were not changed.")
    refresh_auto_playlists(settings)
    if not settings.curated_playlists:
        trigger_navidrome_scan()


def genres(_: argparse.Namespace) -> int:
    settings = read_settings()
    ensure_directories()
    render_beets_config(settings)

    if not settings.lastfm_api_key:
        raise SystemExit("LASTFM_API_KEY is not set. Add it to .env to enable lastgenre tagging.")

    run_command(
        [
            "beet",
            "-c",
            str(BEETS_RUNTIME_CONFIG),
            "lastgenre",
            "-A",
            "-f",
            "-s",
            "track",
        ]
    )
    run_command(
        [
            "beet",
            "-c",
            str(BEETS_RUNTIME_CONFIG),
            "lastgenre",
            "-A",
            "-s",
            "artist",
        ]
    )
    refresh_library(settings)
    return 0


def playlists(_: argparse.Namespace) -> int:
    settings = read_settings()
    ensure_directories()
    refresh_library(settings)
    return 0


def playlist(args: argparse.Namespace) -> int:
    read_settings()
    ensure_directories()

    raw_query = " ".join(args.query).strip()
    if not raw_query:
        raise SystemExit("Provide a query, e.g. `musica-sync playlist 90s hip-hop`.")

    parsed = parse_query(raw_query)
    tracks = load_tracks(BEETS_LIBRARY_DB)
    if not tracks:
        print("Beets library is empty. Import some music first.")
        return 0

    matched = filter_tracks(tracks, parsed)
    if not matched:
        print(f"No tracks matched query: {raw_query!r}")
        return 1

    name = args.name.strip() if args.name else raw_query
    result = write_playlist(
        name,
        matched,
        playlists_dir=PLAYLISTS_DIR,
        prefix=QUERY_PLAYLIST_PREFIX,
    )
    print(f"Wrote {result.track_count} tracks to {result.path}")
    trigger_navidrome_scan()
    return 0


def want(args: argparse.Namespace) -> int:
    read_settings()
    ensure_directories()

    raw_query = clean_wanted_query(" ".join(args.query))
    tracks = load_tracks(BEETS_LIBRARY_DB)
    matches = find_wanted_matches(raw_query, tracks)

    if matches and not args.force:
        print("Already in library:")
        for track in matches:
            print(f"- {track.artist} - {track.title} ({track.album})")
        return 0

    created = append_wanted_entry(raw_query)
    if created:
        print(f"Added wanted track: {raw_query}")
        print(f"Search list: {WANTED_SEARCHES_FILE}")
    else:
        print(f"Already wanted: {raw_query}")

    return 0


def wanted(_: argparse.Namespace) -> int:
    ensure_directories()
    entries = [
        entry for entry in load_wanted_entries() if entry.get("status", "wanted") == "wanted"
    ]
    if not entries:
        print("No wanted tracks queued.")
        return 0

    for index, entry in enumerate(entries, start=1):
        print(f"{index}. {entry['query']}")

    print(f"Search list: {WANTED_SEARCHES_FILE}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync a YouTube playlist into a Navidrome library."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate local dependencies and playlist access.",
    )
    doctor_parser.set_defaults(func=doctor)

    sync_parser = subparsers.add_parser("sync", help="Download and import new tracks.")
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe the playlist and planned downloads without importing anything.",
    )
    sync_parser.set_defaults(func=sync)

    sync_local_parser = subparsers.add_parser(
        "sync-local",
        help="Import local audio files from watched directories.",
    )
    sync_local_parser.set_defaults(func=sync_local)

    genres_parser = subparsers.add_parser(
        "genres",
        help="Backfill Last.fm genres across the library and refresh auto playlists.",
    )
    genres_parser.set_defaults(func=genres)

    playlists_parser = subparsers.add_parser(
        "playlists",
        help="Rebuild auto per-genre playlists from the current library.",
    )
    playlists_parser.set_defaults(func=playlists)

    playlist_parser = subparsers.add_parser(
        "playlist",
        help='Build a single playlist from a free-form query, e.g. "90s hip-hop".',
    )
    playlist_parser.add_argument(
        "--name",
        default="",
        help="Optional playlist name. Defaults to the query string.",
    )
    playlist_parser.add_argument(
        "query",
        nargs="+",
        help="Query terms (genre / decade / year / free text).",
    )
    playlist_parser.set_defaults(func=playlist)

    want_parser = subparsers.add_parser(
        "want",
        help="Queue a missing track for manual/legal acquisition.",
    )
    want_parser.add_argument(
        "--force",
        action="store_true",
        help="Queue the request even when a close library match exists.",
    )
    want_parser.add_argument(
        "query",
        nargs="+",
        help='Track query, e.g. "artist title".',
    )
    want_parser.set_defaults(func=want)

    wanted_parser = subparsers.add_parser(
        "wanted",
        help="List queued missing tracks and the external search text file.",
    )
    wanted_parser.set_defaults(func=wanted)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
