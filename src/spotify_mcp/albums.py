"""Deterministic album resolution and durable playlist execution."""

import hashlib
import re
import time
import unicodedata
import uuid
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import client
from .storage import Store


class WorkflowError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def spotify_id(value: str, kind="album") -> str:
    if value.startswith("https://"):
        parsed = urlparse(value)
        if (
            parsed.netloc != "open.spotify.com"
            or parsed.fragment
            or not parsed.path.startswith(f"/{kind}/")
        ):
            raise ValueError("Invalid Spotify link")
        value = parsed.path.removeprefix(f"/{kind}/")
    value = value.removeprefix(f"spotify:{kind}:")
    if not re.fullmatch(r"[A-Za-z0-9]{22}", value):
        raise ValueError("Invalid Spotify ID")
    return value


def normalized(value):
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(
        "".join(
            c if c.isalnum() else " " for c in value if not unicodedata.combining(c)
        ).split()
    )


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artist: str = Field(min_length=1, max_length=200)
    album: str = Field(min_length=1, max_length=200)
    year: int | None = Field(default=None, ge=1900, le=2100)
    edition: str | None = Field(default=None, max_length=100)
    album_id: str | None = None

    @field_validator("artist", "album")
    @classmethod
    def not_blank(cls, value):
        if not normalized(value):
            raise ValueError("A name is required")
        return value.strip()

    @field_validator("album_id")
    @classmethod
    def valid_id(cls, value):
        return spotify_id(value) if value is not None else None


class PlaylistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    selections: list[Selection] = Field(min_length=1, max_length=20)
    exclude_explicit: bool = False

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("A name is required")
        return value.strip()


class AlbumService:
    def __init__(self, store: Store, api=client):
        self.store = store
        self.api = api

    async def account(self):
        # Store only a binding fingerprint, not a reusable credential.
        return hashlib.sha256(
            (await self.api.account())["account_id"].encode()
        ).hexdigest()

    async def pages(self, path, *, maximum=1000):
        items = []
        offset = 0
        expected = None
        while True:
            data = await self.api._get(path, limit=50, offset=offset)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise WorkflowError("incomplete_pagination")
            total = data.get("total")
            if not isinstance(total, int) or total < 0 or total > maximum:
                raise WorkflowError("track_limit_or_invalid_total")
            if expected is not None and expected != total:
                raise WorkflowError("content_changed")
            expected = total
            batch = data["items"]
            if data.get("offset", offset) != offset or len(batch) > 50:
                raise WorkflowError("incomplete_pagination")
            items.extend(batch)
            if len(items) > total:
                raise WorkflowError("incomplete_pagination")
            if len(items) == total:
                if data.get("next"):
                    raise WorkflowError("incomplete_pagination")
                return items
            if not batch or not data.get("next"):
                raise WorkflowError("incomplete_pagination")
            offset += len(batch)

    def matches(self, album, selection):
        if normalized(selection.artist) not in {
            normalized(a.get("name", "")) for a in album.get("artists", [])
        }:
            return False
        title = normalized(album.get("name", ""))
        requested = normalized(selection.album)
        if selection.edition:
            edition = normalized(selection.edition)
            if (
                title not in {requested, requested + " " + edition}
                or edition not in title
            ):
                return False
        elif title != requested:
            return False
        if album.get("album_type") != "album":
            return False
        if selection.year and not album.get("release_date", "").startswith(
            str(selection.year)
        ):
            return False
        return True

    async def resolve(self, selection):
        if selection.album_id:
            album = await self.api._get("albums/" + selection.album_id)
            if (
                not isinstance(album, dict)
                or spotify_id(album.get("id", "")) != selection.album_id
            ):
                raise WorkflowError("invalid_album_response")
            # An exact ID is an explicit edition choice; still guard against a wrong artist/title.
            artists = {normalized(a.get("name", "")) for a in album.get("artists", [])}
            title = normalized(album.get("name", ""))
            requested = normalized(selection.album)
            if normalized(selection.artist) not in artists or not (
                title == requested or title.startswith(requested + " ")
            ):
                return None, [album], "selection_mismatch"
            if selection.year and not album.get("release_date", "").startswith(
                str(selection.year)
            ):
                return None, [album], "selection_mismatch"
            if selection.edition and normalized(selection.edition) not in title:
                return None, [album], "selection_mismatch"
            return album, [album], "resolved"
        query = f'artist:"{selection.artist.replace(chr(34), " ")}" album:"{selection.album.replace(chr(34), " ")}"'
        candidates = {}
        exhausted = False
        for offset in range(0, 100, 10):
            data = await self.api._get(
                "search", q=query, type="album", limit=10, offset=offset
            )
            page = (data or {}).get("albums", {})
            if not isinstance(page, dict) or "items" not in page:
                raise WorkflowError("incomplete_search")
            batch = page["items"]
            if not isinstance(batch, list) or (not batch and page.get("next")):
                raise WorkflowError("incomplete_search")
            for album in batch:
                if album:
                    candidates[spotify_id(album["id"])] = album
            if not page.get("next"):
                exhausted = True
                break
        matches = [a for a in candidates.values() if self.matches(a, selection)]
        if not exhausted:
            return None, list(candidates.values()), "search_limit"
        if len(matches) != 1:
            return (
                None,
                list(candidates.values()),
                "ambiguous" if matches else "not_found_or_edition",
            )
        album = await self.api._get("albums/" + matches[0]["id"])
        if (
            not isinstance(album, dict)
            or album.get("id") != matches[0]["id"]
            or not self.matches(album, selection)
        ):
            raise WorkflowError("catalog_changed")
        return album, list(candidates.values()), "resolved"

    async def plan(self, request: PlaylistRequest):
        binding = await self.account()
        self.store.cleanup()
        record = {
            "id": uuid.uuid4().hex,
            "account": binding,
            "status": "ready",
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
            "request": request.model_dump(),
            "results": [],
            "albums": [],
            "tracks": [],
            "warnings": [],
        }
        seen = set()
        for index, selection in enumerate(request.selections):
            album, candidates, status = await self.resolve(selection)
            result = {"input_index": index, "status": status, "candidates": candidates}
            record["results"].append(result)
            if album is None:
                record["status"] = "needs_resolution"
                continue
            aid = spotify_id(album["id"])
            if aid in seen:
                result["status"] = "duplicate_album"
                continue
            seen.add(aid)
            tracks = await self.pages(f"albums/{aid}/tracks")
            if not tracks or album.get("total_tracks") != len(tracks):
                raise WorkflowError("incomplete_album")
            if any(
                not isinstance(t, dict)
                or not isinstance(t.get("disc_number"), int)
                or not isinstance(t.get("track_number"), int)
                for t in tracks
            ):
                raise WorkflowError("invalid_track_order")
            tracks.sort(key=lambda t: (t["disc_number"], t["track_number"]))
            positions = [(t["disc_number"], t["track_number"]) for t in tracks]
            if len(set(positions)) != len(positions):
                raise WorkflowError("invalid_track_order")
            discs = sorted({disc for disc, _ in positions})
            if discs != list(range(1, len(discs) + 1)):
                raise WorkflowError("invalid_track_order")
            for disc in discs:
                numbers = [number for d, number in positions if d == disc]
                if numbers != list(range(1, len(numbers) + 1)):
                    raise WorkflowError("invalid_track_order")
            for track in tracks:
                if (
                    track.get("is_playable") is False
                    or track.get("is_local")
                    or track.get("restrictions")
                    or not track.get("id")
                ):
                    result["status"] = "unavailable_tracks"
                    record["status"] = "needs_resolution"
                if request.exclude_explicit and track.get("explicit", True):
                    result["status"] = "clean_version_required"
                    record["status"] = "needs_resolution"
                if "is_playable" not in track:
                    if "playability_unknown" not in record["warnings"]:
                        record["warnings"].append("playability_unknown")
                if track.get("id"):
                    uri = "spotify:track:" + spotify_id(track["id"], "track")
                    aliases = [uri]
                    original = track.get("linked_from", {}).get("id")
                    if original:
                        aliases.append("spotify:track:" + spotify_id(original, "track"))
                    record["tracks"].append({"uri": uri, "aliases": aliases})
            record["albums"].append({"album": album, "tracks": tracks})
        if len(record["tracks"]) > 1000:
            raise WorkflowError("track_limit")
        self.store.write(record)
        return self.summary(record)

    def summary(self, record):
        # Never return Spotify catalog strings or track metadata to an agent.
        result = {
            "operation_id": record["id"],
            "plan_id": record["id"],
            "status": record["status"],
        }
        if "results" in record:
            result["selections"] = [
                {"input_index": r["input_index"], "status": r["status"]}
                for r in record["results"]
            ]
        for key in ("error_code", "retry_at", "warnings"):
            if key in record:
                result[key] = record[key]
        if record.get("playlist_id"):
            result["playlist_url"] = (
                "https://open.spotify.com/playlist/" + record["playlist_id"]
            )
        return result

    async def load(self, key):
        record = self.store.read(key)
        if record["account"] != await self.account():
            raise WorkflowError("account_mismatch")
        if (
            record["status"] in ("ready", "needs_resolution", "creation_rejected")
            and record["expires_at"] < time.time()
        ):
            self.store.path(key).unlink()
            raise WorkflowError("plan_expired")
        if (
            record["status"] == "complete"
            and record["receipt_expires_at"] < time.time()
        ):
            self.store.path(key).unlink()
            raise WorkflowError("receipt_expired")
        return record

    def failure(self, record, exc, status):
        record["status"] = status
        if isinstance(exc, client.RateLimited):
            record["error_code"] = "rate_limited"
            record["retry_at"] = time.time() + exc.seconds
        elif isinstance(exc, httpx.HTTPStatusError):
            record["error_code"] = f"spotify_{exc.response.status_code}"
        elif isinstance(exc, WorkflowError):
            record["error_code"] = exc.code
        else:
            record["error_code"] = "request_failed"
        self.store.write(record)
        return self.summary(record)

    async def create(self, key):
        with self.store.lock(key):
            record = await self.load(key)
            if record["status"] not in ("ready", "creation_rejected"):
                return self.summary(record)
            if record.get("retry_at", 0) > time.time():
                return self.summary(record)
            record.pop("retry_at", None)
            record.pop("error_code", None)
            if not record["tracks"]:
                raise WorkflowError("empty_plan")
            record["status"] = "creation_uncertain"
            self.store.write(record)
            try:
                data = await self.api._post(
                    "me/playlists",
                    {
                        "name": record["request"]["name"],
                        "public": False,
                        "description": "Albums selected from independent music recommendations.",
                    },
                )
                record["playlist_id"] = spotify_id(data["id"], "playlist")
            except Exception as exc:
                rejected = isinstance(exc, client.RateLimited) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and 400 <= exc.response.status_code < 500
                )
                return self.failure(
                    record,
                    exc,
                    "creation_rejected" if rejected else "creation_uncertain",
                )
            record["status"] = "writing"
            record["acknowledged"] = 0
            record["in_flight"] = None
            self.store.write(record)
            return await self.write_remaining(record)

    async def write_remaining(self, record):
        while record["acknowledged"] < len(record["tracks"]):
            start = record["acknowledged"]
            stop = min(start + 100, len(record["tracks"]))
            record["in_flight"] = [start, stop]
            record["status"] = "write_uncertain"
            self.store.write(record)
            try:
                response = await self.api._post(
                    f"playlists/{record['playlist_id']}/items",
                    {"uris": [t["uri"] for t in record["tracks"][start:stop]]},
                )
                if not isinstance(response, dict) or not response.get("snapshot_id"):
                    raise WorkflowError("invalid_write_response")
            except Exception as exc:
                return self.failure(record, exc, "write_uncertain")
            record["acknowledged"] = stop
            record["in_flight"] = None
            record["status"] = "writing"
            self.store.write(record)
        return await self.verify(record, resume=False)

    async def verify(self, record, resume):
        try:
            playlist = await self.api._get(f"playlists/{record['playlist_id']}")
            if not isinstance(playlist, dict) or playlist.get("public") is not False:
                raise WorkflowError("visibility_changed_or_unknown")
            current = await self.api.account()
            if (
                hashlib.sha256(current["account_id"].encode()).hexdigest()
                != record["account"]
            ):
                raise WorkflowError("account_mismatch")
            if playlist.get("owner", {}).get("id") != current["id"]:
                raise WorkflowError("playlist_owner_changed")
            actual = await self.pages(f"playlists/{record['playlist_id']}/items")
            snapshot = playlist.get("snapshot_id")
            after = await self.api._get(f"playlists/{record['playlist_id']}")
            if (
                not snapshot
                or after.get("snapshot_id") != snapshot
                or after.get("public") is not False
            ):
                raise WorkflowError("content_changed")
            expected = record["tracks"]
            if len(actual) > len(expected):
                raise WorkflowError("playlist_edited")
            for item, wanted in zip(actual, expected):
                track = (item or {}).get("item") or (item or {}).get("track")
                if not isinstance(track, dict) or not track.get("id"):
                    raise WorkflowError("playlist_edited")
                ids = {"spotify:track:" + spotify_id(track["id"], "track")}
                original = track.get("linked_from", {}).get("id")
                if original:
                    ids.add("spotify:track:" + spotify_id(original, "track"))
                if not ids.intersection(wanted["aliases"]):
                    raise WorkflowError("playlist_edited")
            allowed_lengths = {record["acknowledged"]}
            if record.get("in_flight"):
                allowed_lengths.add(record["in_flight"][1])
            if len(actual) not in allowed_lengths:
                raise WorkflowError("playlist_edited")
        except Exception as exc:
            return self.failure(record, exc, "unverified")
        if len(actual) == len(expected):
            receipt = {
                "id": record["id"],
                "account": record["account"],
                "status": "complete",
                "playlist_id": record["playlist_id"],
                "receipt_expires_at": time.time() + 86400,
                "warnings": record.get("warnings", []),
            }
            self.store.write(receipt)
            return self.summary(receipt)
        if not resume:
            record["status"] = "partial"
            self.store.write(record)
            return self.summary(record)
        record["acknowledged"] = len(actual)
        record["in_flight"] = None
        record.pop("error_code", None)
        record.pop("retry_at", None)
        return await self.write_remaining(record)

    async def status(self, key, resume=False):
        retry_creation = False
        with self.store.lock(key):
            record = await self.load(key)
            if resume and record["status"] == "creation_rejected":
                retry_creation = True
            elif (
                resume and record.get("playlist_id") and record["status"] != "complete"
            ):
                if record.get("retry_at", 0) > time.time():
                    return self.summary(record)
                return await self.verify(record, resume=True)
            else:
                return self.summary(record)
        if retry_creation:
            # Reacquire and re-read atomically; another process may have proceeded.
            return await self.create(key)
