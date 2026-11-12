import asyncio
import copy
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from spotify_mcp import client
from spotify_mcp.album_server import build_server, error_result
from spotify_mcp.albums import (
    AlbumService,
    PlaylistRequest,
    Selection,
    WorkflowError,
    spotify_id,
)
from spotify_mcp.storage import Busy, Store


def sid(number):
    return str(number).zfill(22)


def album(number=1, name="Album", artist="Band", count=12):
    return {
        "id": sid(number),
        "name": name,
        "album_type": "album",
        "release_date": "2001-01-01",
        "artists": [{"name": artist, "id": sid(1000 + number)}],
        "total_tracks": count,
    }


def tracks(number=1, count=12):
    return [
        {
            "id": sid(number * 10000 + i),
            "name": f"Private title {i}",
            "disc_number": i // 50 + 1,
            "track_number": i % 50 + 1,
            "is_playable": True,
            "explicit": False,
        }
        for i in range(count)
    ]


class FakeSpotify:
    def __init__(self, count=12):
        self.albums = {sid(1): album(count=count)}
        self.tracks = {sid(1): tracks(count=count)}
        self.playlist = []
        self.writes = []
        self.reads = []
        self.user = "account-a"
        self.snapshot = 0
        self.public = False
        self.fail_add = None
        self.fail_create = None
        self.fail_read = False
        self.search_override = None
        self.pause_create = None

    async def account(self):
        return {"account_id": "stable-" + self.user, "id": self.user}

    async def _get(self, path, **params):
        self.reads.append((path, params))
        if path == "search":
            if self.search_override:
                return self.search_override(params)
            candidates = list(self.albums.values())
            offset = params["offset"]
            batch = candidates[offset : offset + 10]
            return {
                "albums": {
                    "items": copy.deepcopy(batch),
                    "next": "next" if offset + 10 < len(candidates) else None,
                }
            }
        if path.startswith("albums/"):
            aid = path.split("/")[1]
            if path.endswith("/tracks"):
                values = self.tracks[aid]
            else:
                return copy.deepcopy(self.albums[aid])
        elif path.endswith("/items"):
            if self.fail_read:
                raise http_error(403)
            values = [{"item": {"id": uri.split(":")[-1]}} for uri in self.playlist]
        else:
            return {
                "id": sid(900),
                "public": self.public,
                "owner": {"id": self.user},
                "snapshot_id": str(self.snapshot),
            }
        offset = params["offset"]
        batch = copy.deepcopy(values[offset : offset + params["limit"]])
        return {
            "items": batch,
            "total": len(values),
            "offset": offset,
            "next": "https://evil.invalid/must-not-follow"
            if offset + len(batch) < len(values)
            else None,
        }

    async def _post(self, path, body):
        self.writes.append((path, copy.deepcopy(body)))
        if path == "me/playlists":
            if self.pause_create:
                await self.pause_create.wait()
            if self.fail_create:
                raise self.fail_create
            return {"id": sid(900)}
        error = self.fail_add
        self.fail_add = None
        if error == "before":
            raise httpx.ReadTimeout("secret")
        if isinstance(error, Exception):
            raise error
        self.playlist.extend(body["uris"])
        self.snapshot += 1
        if error == "after":
            raise httpx.ReadTimeout("secret")
        return {"snapshot_id": str(self.snapshot)}


def http_error(status):
    request = httpx.Request("GET", "https://api.spotify.com/v1/test")
    return httpx.HTTPStatusError(
        "secret", request=request, response=httpx.Response(status, request=request)
    )


class AlbumsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state")
        self.api = FakeSpotify()
        self.service = AlbumService(self.store, self.api)

    async def plan(self, **kwargs):
        return await self.service.plan(
            PlaylistRequest(
                name="Mix",
                selections=[Selection(artist="Band", album="Album")],
                **kwargs,
            )
        )

    async def test_preview_resolves_complete_album_without_writing_or_leaking(self):
        result = await self.plan()
        self.assertEqual(result["status"], "ready")
        self.assertFalse(self.api.writes)
        self.assertNotIn("Private title", json.dumps(result))
        record = self.store.read(result["plan_id"])
        self.assertEqual(len(record["tracks"]), 12)
        self.assertEqual(
            stat.S_IMODE(self.store.path(result["plan_id"]).stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)

    async def test_large_multidisc_playlist_and_receipt(self):
        self.api = FakeSpotify(count=123)
        self.service = AlbumService(self.store, self.api)
        result = await self.plan()
        key = result["plan_id"]
        result = await self.service.create(key)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            [len(b["uris"]) for p, b in self.api.writes if p.endswith("/items")],
            [100, 23],
        )
        self.assertEqual(
            self.api.playlist,
            ["spotify:track:" + t["id"] for t in self.api.tracks[sid(1)]],
        )
        self.assertFalse(self.api.writes[0][1]["public"])
        self.assertNotIn("tracks", self.store.read(key))
        count = len(self.api.writes)
        self.assertEqual(await self.service.create(key), result)
        self.assertEqual(len(self.api.writes), count)
        self.assertTrue(
            all(not path.startswith("http") for path, params in self.api.reads)
        )

    async def test_ambiguous_missing_and_deluxe(self):
        self.api.albums[sid(2)] = album(2)
        result = await self.plan()
        self.assertEqual(result["status"], "needs_resolution")
        self.assertEqual(result["selections"][0]["status"], "ambiguous")
        await self.service.create(result["plan_id"])
        self.assertFalse(self.api.writes)
        self.api.albums = {sid(2): album(2, name="Album (Deluxe)")}
        result = await self.plan()
        self.assertEqual(result["selections"][0]["status"], "not_found_or_edition")
        self.api.albums = {}
        self.assertEqual((await self.plan())["status"], "needs_resolution")

    async def test_normalized_names_and_explicit_edition(self):
        self.api.albums[sid(1)] = album(name="Álbum", artist="Bánd")
        self.assertEqual((await self.plan())["status"], "ready")
        self.api.albums[sid(1)] = album(name="Album (Deluxe)")
        request = PlaylistRequest(
            name="Mix",
            selections=[Selection(artist="Band", album="Album", edition="Deluxe")],
        )
        self.assertEqual((await self.service.plan(request))["status"], "ready")

    async def test_literal_album_title_is_not_mistaken_for_edition_label(self):
        self.api.albums[sid(1)] = album(name="Live Through This")
        request = PlaylistRequest(
            name="Mix", selections=[Selection(artist="Band", album="Live Through This")]
        )
        self.assertEqual((await self.service.plan(request))["status"], "ready")

    async def test_exact_id_still_checks_artist(self):
        self.api.albums[sid(1)] = album(name="Album Deluxe", artist="Other")
        request = PlaylistRequest(
            name="Mix",
            selections=[Selection(artist="Band", album="Album", album_id=sid(1))],
        )
        self.assertEqual(
            (await self.service.plan(request))["selections"][0]["status"],
            "selection_mismatch",
        )

    async def test_bounded_search_does_not_choose_from_truncated_results(self):
        self.api.search_override = lambda params: {
            "albums": {"items": [album(params["offset"] + 1)], "next": "next"}
        }
        result = await self.plan()
        self.assertEqual(result["selections"][0]["status"], "search_limit")
        self.assertEqual(len([p for p, _ in self.api.reads if p == "search"]), 10)

    async def test_duplicate_albums_removed_shared_tracks_preserved(self):
        self.api.albums[sid(2)] = album(2, "Second")
        self.api.tracks[sid(2)] = tracks()  # same tracks on a different album: preserve
        selections = [
            Selection(artist="Band", album="Album", album_id=sid(1)),
            Selection(artist="Band", album="Album", album_id=sid(1)),
            Selection(artist="Band", album="Second", album_id=sid(2)),
        ]
        result = await self.service.plan(
            PlaylistRequest(name="Mix", selections=selections)
        )
        self.assertEqual(result["selections"][1]["status"], "duplicate_album")
        self.assertEqual(len(self.store.read(result["plan_id"])["tracks"]), 24)

    async def test_unavailable_and_clean_filter_require_resolution(self):
        self.api.tracks[sid(1)][0]["is_playable"] = False
        self.assertEqual(
            (await self.plan())["selections"][0]["status"], "unavailable_tracks"
        )
        self.api.tracks[sid(1)][0]["is_playable"] = True
        self.api.tracks[sid(1)][0]["explicit"] = True
        self.assertEqual(
            (await self.plan(exclude_explicit=True))["selections"][0]["status"],
            "clean_version_required",
        )
        self.assertFalse(self.api.writes)

    async def test_unknown_playability_reported(self):
        self.api.tracks[sid(1)][0].pop("is_playable")
        self.assertIn("playability_unknown", (await self.plan())["warnings"])

    async def test_incomplete_pages_and_invalid_order(self):
        original = self.api._get

        async def broken(path, **params):
            data = await original(path, **params)
            if path.endswith("/tracks"):
                data["total"] = 13
            return data

        with patch.object(self.api, "_get", side_effect=broken):
            with self.assertRaisesRegex(WorkflowError, "incomplete_pagination"):
                await self.plan()
        self.api.tracks[sid(1)][1]["track_number"] = 1
        with self.assertRaisesRegex(WorkflowError, "invalid_track_order"):
            await self.plan()
        self.assertFalse(self.api.writes)

    async def test_expired_and_changed_account(self):
        key = (await self.plan())["plan_id"]
        self.api.user = "other-account"
        with self.assertRaisesRegex(WorkflowError, "account_mismatch"):
            await self.service.create(key)
        self.api.user = "account-a"
        record = self.store.read(key)
        record["expires_at"] = 0
        self.store.write(record)
        with self.assertRaisesRegex(WorkflowError, "plan_expired"):
            await self.service.create(key)
        self.assertFalse(self.api.writes)

    async def test_concurrent_creation_is_locked(self):
        key = (await self.plan())["plan_id"]
        event = asyncio.Event()
        self.api.pause_create = event
        task = asyncio.create_task(self.service.create(key))
        await asyncio.sleep(0)
        try:
            with self.assertRaises(Busy):
                await self.service.create(key)
        finally:
            event.set()
        self.assertEqual((await task)["status"], "complete")
        self.assertEqual(sum(p == "me/playlists" for p, _ in self.api.writes), 1)

    async def test_timeout_before_and_after_write_resumes_without_duplicates(self):
        for failure in ("before", "after"):
            self.api.playlist = []
            self.api.fail_add = failure
            key = (await self.plan())["plan_id"]
            self.assertEqual(
                (await self.service.create(key))["status"], "write_uncertain"
            )
            # Simulate restart: only persisted operation state survives.
            restarted = AlbumService(Store(self.store.root), self.api)
            self.assertEqual(
                (await restarted.status(key, resume=True))["status"], "complete"
            )
            self.assertEqual(len(self.api.playlist), 12)

    async def test_uncertain_create_never_blindly_retries(self):
        self.api.fail_create = httpx.ReadTimeout("secret")
        key = (await self.plan())["plan_id"]
        result = await self.service.create(key)
        self.assertEqual(result["status"], "creation_uncertain")
        self.assertNotIn("secret", json.dumps(result))
        await self.service.create(key)
        await self.service.status(key, resume=True)
        self.assertEqual(len(self.api.writes), 1)

    async def test_user_edits_block_resume(self):
        self.api.fail_add = "after"
        key = (await self.plan())["plan_id"]
        await self.service.create(key)
        self.api.playlist.reverse()
        result = await self.service.status(key, resume=True)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["error_code"], "playlist_edited")
        self.assertEqual(len(self.api.writes), 2)

    async def test_readback_forbidden_not_complete(self):
        self.api.fail_read = True
        key = (await self.plan())["plan_id"]
        result = await self.service.create(key)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["error_code"], "spotify_403")
        self.assertIn("playlist_url", result)
        self.api.fail_read = False
        self.assertEqual(
            (await self.service.status(key, resume=True))["status"], "complete"
        )

    async def test_relinked_readback_recognized(self):
        original_id = sid(765)
        self.api.tracks[sid(1)][0]["linked_from"] = {"id": original_id}
        key = (await self.plan())["plan_id"]
        self.api.fail_add = "after"
        await self.service.create(key)
        self.api.playlist[0] = "spotify:track:" + original_id
        self.assertEqual(
            (await self.service.status(key, resume=True))["status"], "complete"
        )

    async def test_visibility_change_never_reported_complete(self):
        key = (await self.plan())["plan_id"]
        self.api.public = True
        result = await self.service.create(key)
        self.assertEqual(result["error_code"], "visibility_changed_or_unknown")

    async def test_429_resume_waits_and_keeps_operation(self):
        key = (await self.plan())["plan_id"]
        self.api.fail_add = client.RateLimited(600)
        result = await self.service.create(key)
        self.assertEqual(result["error_code"], "rate_limited")
        count = len(self.api.writes)
        self.assertEqual(await self.service.status(key, resume=True), result)
        self.assertEqual(len(self.api.writes), count)

    async def test_receipt_and_plan_cleanup_preserves_incomplete_operations(self):
        key = (await self.plan())["plan_id"]
        await self.service.create(key)
        receipt = self.store.read(key)
        receipt["receipt_expires_at"] = 0
        self.store.write(receipt)
        key2 = (await self.plan())["plan_id"]
        self.api.fail_create = httpx.ReadTimeout("failure")
        await self.service.create(key2)
        self.store.cleanup()
        self.assertFalse(self.store.path(key).exists())
        self.assertTrue(self.store.path(key2).exists())
        self.store.purge(key2)
        self.assertFalse(self.store.path(key2).exists())

    async def test_symlinks_and_bad_ids_rejected(self):
        key = "a" * 32
        target = Path(self.tmp.name) / "target"
        target.write_text("{}")
        self.store.path(key).symlink_to(target)
        with self.assertRaises(OSError):
            self.store.read(key)
        for bad in (
            "../file",
            "https://evil.invalid/album/" + sid(1),
            "https://open.spotify.com.evil/album/" + sid(1),
            "spotify:track:" + sid(1),
        ):
            with self.assertRaises(ValueError):
                spotify_id(bad)
        self.assertEqual(
            spotify_id("https://open.spotify.com/album/" + sid(1) + "?si=abc"), sid(1)
        )

    async def test_mcp_schema_and_catalog_boundary(self):
        server = build_server(self.service)
        tools = await server.list_tools()
        self.assertEqual(
            {t.name for t in tools},
            {
                "spotify_plan_album_playlist",
                "spotify_create_album_playlist",
                "spotify_album_playlist_status",
            },
        )
        result = await server.call_tool(
            "spotify_plan_album_playlist",
            {"name": "Mix", "selections": [{"artist": "Band", "album": "Album"}]},
        )
        serialized = str(result)
        self.assertNotIn("Private title", serialized)
        self.assertIn("ready", serialized)
        error = await server.call_tool(
            "spotify_create_album_playlist", {"plan_id": "invalid"}
        )
        self.assertIn("invalid_input", str(error))

    async def test_definite_creation_rejection_can_resume_safely(self):
        key = (await self.plan())["plan_id"]
        self.api.fail_create = client.RateLimited(120)
        result = await self.service.create(key)
        self.assertEqual(result["status"], "creation_rejected")
        await self.service.status(key, resume=True)
        self.assertEqual(len(self.api.writes), 1)
        record = self.store.read(key)
        record["retry_at"] = 0
        self.store.write(record)
        self.api.fail_create = None
        self.assertEqual(
            (await self.service.status(key, resume=True))["status"], "complete"
        )
        self.assertEqual(len(self.api.playlist), 12)

    async def test_crash_after_second_batch_reconciles_on_restart(self):
        self.api = FakeSpotify(count=123)
        self.service = AlbumService(self.store, self.api)
        key = (await self.plan())["plan_id"]
        post = self.api._post

        async def interrupted(path, body):
            result = await post(path, body)
            if path.endswith("/items") and len(self.api.playlist) == 123:
                raise asyncio.CancelledError()
            return result

        with patch.object(self.api, "_post", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.create(key)
        record = self.store.read(key)
        self.assertEqual(record["acknowledged"], 100)
        self.assertEqual(record["in_flight"], [100, 123])
        restarted = AlbumService(Store(self.store.root), self.api)
        self.assertEqual(
            (await restarted.status(key, resume=True))["status"], "complete"
        )
        self.assertEqual(len(self.api.playlist), 123)
        self.assertEqual(len(self.api.writes), 3)

    async def test_changed_snapshot_prevents_false_success(self):
        key = (await self.plan())["plan_id"]
        original = self.api._get

        async def changed(path, **params):
            result = await original(path, **params)
            if path.endswith("/items"):
                self.api.snapshot += 1
            return result

        with patch.object(self.api, "_get", side_effect=changed):
            result = await self.service.create(key)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["error_code"], "content_changed")

    async def test_track_limit_and_invalid_schema_prevent_writes(self):
        self.api.albums[sid(1)]["total_tracks"] = 1001
        self.api.tracks[sid(1)] = tracks(count=1001)
        with self.assertRaisesRegex(WorkflowError, "track_limit"):
            await self.plan()
        with self.assertRaises(ValueError):
            PlaylistRequest(
                name="Mix", selections=[Selection(artist="Band", album="Album")] * 21
            )
        self.assertFalse(self.api.writes)

    async def test_profile_separates_tokens_and_uses_minimal_scopes(self):
        with (
            patch.dict(os.environ, {"SPOTIFY_ALBUM_HOME": self.tmp.name}),
            patch.object(client, "CACHE_PATH", "original"),
            patch.object(client, "SCOPES", "original"),
            patch.object(client, "CLIENT_ID", "test"),
            patch.object(client, "_token_cache", {"access_token": "legacy"}),
        ):
            root = client.configure_albums()
            self.assertEqual(
                client.SCOPES, "playlist-modify-private playlist-read-private"
            )
            self.assertEqual(client.CACHE_PATH, str(root / "token.json"))
            self.assertFalse(client._load_token())

    async def test_operation_lock_applies_across_processes(self):
        import subprocess
        import sys

        key = (await self.plan())["plan_id"]
        code = "from pathlib import Path; from spotify_mcp.storage import Store; import sys; s=Store(Path(sys.argv[1]));\nwith s.lock(sys.argv[2]): print('acquired')"
        with self.store.lock(key):
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", code, str(self.store.root), key],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Busy", result.stderr)

    async def test_mutable_user_id_does_not_break_account_binding(self):
        key = (await self.plan())["plan_id"]
        self.api.user = "renamed-account-a"
        with patch.object(
            self.api,
            "account",
            new=AsyncMock(
                return_value={"account_id": "stable-account-a", "id": self.api.user}
            ),
        ):
            self.assertEqual((await self.service.create(key))["status"], "complete")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_retries_but_writes_do_not(self):
        for method, expected in [("GET", 3), ("POST", 1)]:
            calls = []

            async def handle(request):
                calls.append(request)
                return httpx.Response(503, json={})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                with (
                    patch.object(
                        client, "_get_access_token", new=AsyncMock(return_value="token")
                    ),
                    patch.object(client, "_get_http", return_value=http),
                    patch.object(client.asyncio, "sleep", new=AsyncMock()),
                ):
                    with self.assertRaises(httpx.HTTPStatusError):
                        await client._api(method, "test")
            self.assertEqual(len(calls), expected)

    async def test_rate_limit_and_http_auth_errors(self):
        for status in (401, 403, 429):

            async def handle(request):
                return httpx.Response(status, headers={"Retry-After": "120"}, json={})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                with (
                    patch.object(
                        client,
                        "_get_access_token",
                        new=AsyncMock(return_value="secret"),
                    ),
                    patch.object(client, "_get_http", return_value=http),
                ):
                    try:
                        await client._api("GET", "test")
                    except Exception as exc:
                        result = error_result(exc)
                    else:
                        self.fail("expected failure")
            self.assertEqual(
                result["error_code"],
                "rate_limited" if status == 429 else f"spotify_{status}",
            )
            if status == 429:
                self.assertEqual(result["retry_after_seconds"], 120)

    async def test_concurrent_refresh_once(self):
        token = {"access_token": "expired", "refresh_token": "r", "expires_at": 0}

        async def refresh(value):
            await asyncio.sleep(0)
            token.update(access_token="fresh", expires_at=time.time() + 3600)
            return token

        with (
            patch.object(client, "_refresh_lock", asyncio.Lock()),
            patch.object(client, "_load_token", return_value=token),
            patch.object(client, "_refresh_token", side_effect=refresh) as refresh_mock,
        ):
            self.assertEqual(
                await asyncio.gather(
                    client._get_access_token(), client._get_access_token()
                ),
                ["fresh", "fresh"],
            )
            refresh_mock.assert_awaited_once()
