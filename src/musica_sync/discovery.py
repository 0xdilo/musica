"""Authenticated discovery for Feishin; playback remains in the native queue."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from musica_sync.curation import (
    choose_preference_seed,
    load_library,
    played_age,
    select_tracks,
    similar_scores,
)
from musica_sync.navidrome import ROOT, Subsonic, audiomuse_url, navidrome_url, request_json
from musica_sync.preferences import read_preferences, update_preferences

TRANSLATIONS = {
    "senza voce": "instrumental",
    "senza voci": "instrumental",
    "pianoforte": "piano",
    "malinconico": "melancholic",
    "malinconica": "melancholic",
    "triste": "sad",
    "rilassante": "relaxing",
    "elettronica": "electronic",
    "elettronico": "electronic",
    "chitarra": "guitar",
    "acustica": "acoustic",
    "acustico": "acoustic",
    "lento": "slow",
    "lenta": "slow",
    "sognante": "dreamy",
    "energico": "energetic",
    "energica": "energetic",
    "batteria": "drums",
    "scuro": "dark",
    "cupo": "dark",
}


class DiscoveryError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def credentials() -> dict:
    return json.loads((ROOT / "state/sonic/credentials.json").read_text())


def authenticate(token: str) -> str:
    if not re.fullmatch(r"Bearer [A-Za-z0-9_.-]{30,4096}", token):
        raise DiscoveryError(401, "Accedi a Musica per usare Esplora.")
    username = credentials()["navidrome_user"]
    with sqlite3.connect(f"file:{ROOT}/data/navidrome/navidrome.db?mode=ro", uri=True) as db:
        user = db.execute("SELECT id FROM user WHERE user_name=?", (username,)).fetchone()
    if not user:
        raise DiscoveryError(503, "Il profilo musicale non è disponibile.")
    request = Request(
        navidrome_url() + "/api/user/" + user[0],
        headers={
            "X-ND-Authorization": token,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = json.load(response)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise DiscoveryError(401, "Sessione scaduta. Accedi di nuovo a Musica.") from None
        raise DiscoveryError(503, "Navidrome non è disponibile. Riprova tra poco.") from None
    except (URLError, TimeoutError):
        raise DiscoveryError(503, "Navidrome non è disponibile. Riprova tra poco.") from None
    if data.get("userName") != username:
        raise DiscoveryError(403, "Questo profilo non può usare Esplora.")
    return username


def sonic_query(query: str) -> str:
    for source, target in TRANSLATIONS.items():
        query = re.sub(r"\b" + re.escape(source) + r"\b", target, query, flags=re.I)
    return "A recording of " + query + " music."


def search_policy(data: dict) -> tuple[str, str, str, bool, str]:
    mode = data.get("mode", "text")
    query = data.get("query", "")
    variety = data.get("variety", "focused")
    avoid_recent = data.get("avoid_recent", True)
    variation = data.get("variation", "")
    if not isinstance(mode, str) or mode not in {"text", "personal", "forgotten", "genre", "track"}:
        raise DiscoveryError(400, "Scegli un tipo di radio valido.")
    if not isinstance(query, str) or len(query) > 240 or re.search(r"[\x00-\x1f]", query):
        raise DiscoveryError(400, "Descrivi la musica in massimo 240 caratteri.")
    query = query.strip()
    if mode in {"text", "genre"} and len(query) < 2:
        raise DiscoveryError(400, "Scrivi almeno due caratteri per cercare musica.")
    if mode == "track" and not re.fullmatch(r"[a-zA-Z0-9]{1,64}", query):
        raise DiscoveryError(400, "Brano di partenza non valido.")
    if (
        not isinstance(variety, str)
        or variety not in {"focused", "varied"}
        or not isinstance(avoid_recent, bool)
    ):
        raise DiscoveryError(400, "Opzioni della radio non valide.")
    if not isinstance(variation, str) or not re.fullmatch(r"[a-zA-Z0-9-]{0,40}", variation):
        raise DiscoveryError(400, "Richiesta della radio non valida.")
    return mode, query, variety, avoid_recent, variation


def overview(username: str) -> dict:
    library = load_library(username)
    counts = Counter(g for song in library.values() for g in song.genres)
    return {
        "songs": len(library),
        "favourites": sum(s.favourite for s in library.values()),
        "genres": [{"name": g, "count": n} for g, n in counts.most_common() if n >= 8][:40],
        "preferences": read_preferences(),
    }


def search(username: str, data: dict) -> dict:
    mode, query, variety, avoid_recent, variation = search_policy(data)
    library = load_library(username)
    config = credentials()
    client = Subsonic(navidrome_url(), username, config["navidrome_password"])
    candidates = list(library.values())
    description = ""
    seed_id = None
    if mode in {"personal", "track"}:
        seed = (
            library.get(query)
            if mode == "track"
            else choose_preference_seed(library, variation=variation)
        )
        if seed is None:
            return {"ids": [], "description": "Metti un cuore a un brano per iniziare.", "pool": 0}
        scores = similar_scores(client, library, seed)
        seed_id = seed.id
        description = f"Vicini a {seed.title} · {seed.artist}"
        candidates = [library[i] for i in scores]
        candidates = sorted(candidates, key=lambda s: -scores[s.id])[
            : 60 if variety == "focused" else 150
        ]
    elif mode == "forgotten":
        candidates = [s for s in candidates if played_age(s) >= 30]
        scores = {s.id: 1 / (1 + s.play_count) for s in candidates}
        description = "Mai ascoltati o assenti dai tuoi ascolti da almeno 30 giorni"
    else:
        if mode == "genre":
            candidates = [s for s in candidates if query in s.genres]
        response = request_json(
            audiomuse_url() + "/api/clap/search",
            {"query": sonic_query(query), "limit": 2000},
            config["audiomuse_token"],
        )
        results = response.get("results", [])
        if not results:
            raise DiscoveryError(503, "La ricerca musicale si sta preparando. Riprova tra poco.")
        allowed = {s.id for s in candidates}
        scores = {r["item_id"]: float(r["similarity"]) for r in results if r["item_id"] in allowed}
        ranked = sorted(scores, key=lambda i: -scores[i])
        if ranked:
            best = scores[ranked[0]]
            threshold = max(
                best - (0.18 if variety == "focused" else 0.24),
                best * (0.52 if variety == "focused" else 0.42),
                0.16,
            )
            ranked = [i for i in ranked if scores[i] >= threshold]
        candidates = [library[i] for i in ranked[: 80 if variety == "focused" else 200]]
        description = f"Una radio di {query}" if mode == "genre" else f"La tua ricerca: {query}"
    if avoid_recent:
        candidates = [s for s in candidates if played_age(s) >= 1]
    selected = select_tracks(candidates, scores, seed=f"{date.today()}:{mode}:{query}:{variation}")
    return {
        "ids": [s.id for s in selected],
        "description": description,
        "pool": len(candidates),
        "seed_id": seed_id,
    }


def feedback(username: str, data: dict) -> dict:
    kind = data.get("kind")
    remove = data.get("remove", False)
    if (
        not isinstance(kind, str)
        or kind not in {"tracks", "artists"}
        or not isinstance(remove, bool)
    ):
        raise DiscoveryError(400, "Preferenza non valida.")
    if remove:
        key = data.get("key", "")
        if not isinstance(key, str) or key not in read_preferences()[kind]:
            raise DiscoveryError(400, "Esclusione non trovata.")
        return update_preferences(kind, key, "", remove=True)
    item_id = data.get("id", "")
    if not isinstance(item_id, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,64}", item_id):
        raise DiscoveryError(400, "Brano non valido.")
    song = load_library(username, include_excluded=True).get(item_id)
    if song is None:
        raise DiscoveryError(404, "Il brano non è più in libreria.")
    key = song.id if kind == "tracks" else song.artist_key
    label = f"{song.title} · {song.artist}" if kind == "tracks" else song.artist
    return update_preferences(kind, key, label)
