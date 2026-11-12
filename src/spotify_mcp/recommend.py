"""Local, deterministic playlist planning. Never sends listening data to an LLM."""
import argparse
import asyncio
import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from . import server


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="My personal mix", min_length=1, max_length=100)
    size: int = Field(default=30, ge=1, le=100)
    time_range: str = "medium_term"
    recommendations: list[str] = Field(default_factory=list, max_length=50)
    favorite_artists: list[str] = Field(default_factory=list, max_length=50)
    excluded_artists: list[str] = Field(default_factory=list, max_length=50)
    exclude_explicit: bool = False
    max_per_artist: int = Field(default=3, ge=1, le=100)


async def plan(prefs: Preferences) -> dict:
    if prefs.time_range not in ("short_term", "medium_term", "long_term"):
        raise ValueError("time_range must be short_term, medium_term or long_term")
    # Validate every user ID before making any network request.
    recommended = [server._track_uri(t) for t in prefs.recommendations]
    candidates = {}
    warnings = []

    def add(track, score, reason):
        if not track or track.get("is_local") or track.get("is_playable") is False:
            return
        if not track.get("id") or track.get("type", "track") != "track":
            return
        names = [a.get("name", "") for a in track.get("artists", [])]
        if set(n.casefold() for n in names) & set(n.casefold() for n in prefs.excluded_artists):
            return
        if prefs.exclude_explicit and track.get("explicit", True):
            return
        uri = server._track_uri(track["id"])
        if uri not in candidates:
            candidates[uri] = {"uri": uri, "name": track.get("name", ""), "artists": names,
                               "score": 0, "reasons": []}
        candidate = candidates[uri]
        candidate["score"] += score
        if reason not in candidate["reasons"]:
            candidate["reasons"].append(reason)

    # Missing scopes or restricted endpoints are visible, never disguised as empty history.
    for path, params, label in [
        ("me/top/tracks", {"limit": 50, "time_range": prefs.time_range}, "top tracks"),
        ("me/player/recently-played", {"limit": 50}, "recent listening"),
    ]:
        try:
            data = await server._get(path, **params)
        except server.httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403:
                raise
            warnings.append(f"{label} unavailable (403): check scopes and app access")
            continue
        for index, item in enumerate((data or {}).get("items", [])):
            track = item.get("track") if label == "recent listening" else item
            add(track, 2 if label == "recent listening" else (50-index)/10, label)
    for uri in dict.fromkeys(recommended):
        add(await server._get("tracks/" + uri.split(":")[-1]), 100, "your recommendation")
    favorite = {a.casefold() for a in prefs.favorite_artists}
    for candidate in candidates.values():
        if favorite & {a.casefold() for a in candidate["artists"]}:
            candidate["score"] += 10
            candidate["reasons"].append("favorite artist")
    counts = Counter()
    selected = []
    for candidate in sorted(candidates.values(), key=lambda c: (-c["score"], c["uri"])):
        artists = {a.casefold() for a in candidate["artists"]}
        if any(counts[a] >= prefs.max_per_artist for a in artists):
            continue
        selected.append(candidate)
        counts.update(artists)
        if len(selected) == prefs.size:
            break
    if len(selected) < prefs.size:
        warnings.append(f"Only {len(selected)} tracks match the available history and filters.")
    return {"name": prefs.name, "public": False, "tracks": selected, "warnings": warnings}


async def create(mix: dict) -> dict:
    """Create once; never retry writes automatically after ambiguous network errors."""
    uris = [server._track_uri(t["uri"]) for t in mix["tracks"]]
    if not 1 <= len(uris) <= 100:
        raise ValueError("A playlist must contain 1–100 tracks")
    playlist = await server._post("me/playlists", {"name": mix["name"], "public": False,
        "description": "A personal mix selected locally from listening history and recommendations."})
    playlist_id = playlist["id"]
    if not server.re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id):
        raise RuntimeError("Spotify returned an invalid playlist ID")
    url = "https://open.spotify.com/playlist/" + playlist_id
    try:
        await server._post(f"playlists/{playlist_id}/items", {"uris": uris})
    except Exception as exc:
        raise RuntimeError(f"Playlist created at {url}, but adding tracks failed or is uncertain. "
                           "Inspect it before retrying; rerunning create makes another playlist.") from exc
    return {"url": url, "count": len(uris), "public": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preferences", required=True, help="Local preferences JSON file")
    parser.add_argument("--create", action="store_true", help="Preview, then ask before creating a private playlist")
    args = parser.parse_args()

    async def run():
        try:
            with open(args.preferences) as f:
                prefs = Preferences.model_validate(json.load(f))
            mix = await plan(prefs)
            print(json.dumps(mix, indent=2, ensure_ascii=False))
            if args.create and mix["tracks"]:
                if input("Create this private playlist? Type yes: ").strip().lower() == "yes":
                    print(json.dumps(await create(mix), indent=2))
        finally:
            if server._http is not None:
                await server._http.aclose()
                server._http = None
    try:
        asyncio.run(run())
    except (ValueError, OSError, RuntimeError, server.httpx.HTTPError) as exc:
        parser.exit(1, f"{exc if isinstance(exc, RuntimeError) else server._handle_error(exc)}\n")
