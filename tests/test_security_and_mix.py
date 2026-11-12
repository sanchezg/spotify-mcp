import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from spotify_mcp import client, server
from spotify_mcp.recommend import Preferences, create, plan


def track(letter, artist="Artist", **kw):
    return {
        "id": letter * 22,
        "name": letter,
        "artists": [{"name": artist}],
        "explicit": False,
        **kw,
    }


class SecurityTests(unittest.TestCase):
    def test_callback_requires_state_and_path(self):
        for path in (
            "/callback?code=x",
            "/callback?code=x&state=wrong",
            "/callback?code=x&state=%C3%A9",
            "/other?code=x&state=s",
            "/callback?code=x&state=s&state=s",
            "/callback?code=x&code=y&state=s",
        ):
            self.assertIsNone(client._callback_values(path, "/callback", "s"))
        self.assertEqual(
            client._callback_values("/callback?code=x&state=s", "/callback", "s"),
            ("x", None),
        )

    def test_denial_does_not_reflect_input(self):
        self.assertEqual(
            client._callback_values(
                "/callback?error=%3Cscript%3E&state=s", "/callback", "s"
            ),
            (None, "Authorization denied"),
        )

    def test_cache_owner_only_and_symlink_not_followed(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(client, "CACHE_PATH", d + "/token"),
            patch.object(client, "_token_cache", {}),
        ):
            target = Path(d) / "target"
            target.write_text("untouched")
            Path(client.CACHE_PATH).symlink_to(target)
            client._save_token({"access_token": "test"})
            self.assertEqual(target.read_text(), "untouched")
            self.assertEqual(stat.S_IMODE(os.stat(client.CACHE_PATH).st_mode), 0o600)
            client._token_cache = {}
            self.assertEqual(client._load_token()["access_token"], "test")

    def test_load_rejects_symlink(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(client, "CACHE_PATH", d + "/token"),
            patch.object(client, "_token_cache", {}),
        ):
            Path(d, "target").write_text("{}")
            Path(client.CACHE_PATH).symlink_to(Path(d, "target"))
            with self.assertRaises(OSError):
                client._load_token()

    def test_uri_injection_rejected(self):
        for value in (
            "../me",
            "x?uri=evil",
            "spotify:album:" + "a" * 22,
            "https://example.com",
        ):
            with self.assertRaises(ValueError):
                client._track_uri(value)

    def test_pkce_flow_and_callback_rejection(self):
        import base64
        import hashlib
        import io
        from urllib.parse import parse_qs, urlparse

        captured = {}

        class FakeServer:
            timeout = 0

            def __init__(self, address, handler):
                self.handler = handler
                captured["bound"] = address

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def handle_request(self):
                handler = object.__new__(self.handler)
                handler.send_response = lambda status: captured.update(status=status)
                handler.send_header = lambda *args: None
                handler.end_headers = lambda: None
                handler.wfile = io.BytesIO()
                handler.path = "/callback?code=bad&state=wrong"
                handler.do_GET()
                assert captured["status"] == 400
                handler.path = (
                    "/callback?code=good&state=" + captured["params"]["state"][0]
                )
                handler.do_GET()

        def open_browser(url):
            self.assertEqual(captured["bound"], ("127.0.0.1", 8080))
            captured["params"] = parse_qs(urlparse(url).query)

        response = httpx.Response(
            200,
            json={"access_token": "test", "refresh_token": "refresh"},
            request=httpx.Request("POST", client.SPOTIFY_TOKEN_URL),
        )
        with (
            patch.object(client, "CLIENT_ID", "test"),
            patch.object(client, "REDIRECT_URI", "http://127.0.0.1:8080/callback"),
            patch.object(client, "HTTPServer", FakeServer),
            patch("webbrowser.open", side_effect=open_browser),
            patch.object(client.httpx, "post", return_value=response) as post,
            patch.object(client, "_save_token") as save,
            patch("builtins.print"),
        ):
            client.run_auth()
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["code"], "good")
        self.assertNotIn("client_secret", data)
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(data["code_verifier"].encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        self.assertEqual(captured["params"]["code_challenge"], [challenge])
        save.assert_called_once()

    def test_scopes_and_errors(self):
        self.assertIn("user-top-read", client.SCOPES)
        self.assertNotIn("playlist-modify-public", client.SCOPES)
        self.assertNotIn("user-modify-playback-state", client.SCOPES)
        self.assertNotIn("secret", server._handle_error(RuntimeError("secret")))


class MixTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_ranking_filtering_and_no_writes(self):
        async def get(path, **params):
            if path == "me/top/tracks":
                return {
                    "items": [
                        track("a"),
                        track("b", "Blocked"),
                        track("c", explicit=True),
                    ]
                }
            if path == "me/player/recently-played":
                return {
                    "items": [
                        {"track": track("a")},
                        {"track": None},
                        {"track": track("e", is_local=True)},
                    ]
                }
            return track("d", "Recommended")

        with (
            patch.object(server, "_get", side_effect=get),
            patch.object(server, "_post", new_callable=AsyncMock) as post,
        ):
            mix = await plan(
                Preferences(
                    recommendations=["d" * 22],
                    excluded_artists=["blocked"],
                    exclude_explicit=True,
                )
            )
        post.assert_not_called()
        self.assertEqual([t["name"] for t in mix["tracks"]], ["d", "a"])
        self.assertEqual(mix["tracks"][1]["score"], 7)

    async def test_artist_cap(self):
        with patch.object(
            server,
            "_get",
            new=AsyncMock(
                side_effect=[{"items": [track("a"), track("b")]}, {"items": []}]
            ),
        ):
            mix = await plan(Preferences(max_per_artist=1))
        self.assertEqual(len(mix["tracks"]), 1)

    async def test_missing_scope_warning(self):
        error = httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("GET", "https://api.spotify.com"),
            response=httpx.Response(403),
        )
        with patch.object(
            server, "_get", new=AsyncMock(side_effect=[error, {"items": []}])
        ):
            mix = await plan(Preferences())
        self.assertIn("403", mix["warnings"][0])

    async def test_invalid_preferences_fail_before_network(self):
        with patch.object(server, "_get", new_callable=AsyncMock) as get:
            with self.assertRaises(ValueError):
                await plan(Preferences(recommendations=["../me"]))
        get.assert_not_called()

    async def test_create_private_with_exact_selection(self):
        with patch.object(
            server, "_post", new=AsyncMock(side_effect=[{"id": "p" * 22}, {}])
        ) as post:
            result = await create(
                {"name": "Mix", "tracks": [{"uri": "spotify:track:" + "a" * 22}]}
            )
        self.assertFalse(post.call_args_list[0].args[1]["public"])
        self.assertEqual(
            post.call_args_list[1].args[1], {"uris": ["spotify:track:" + "a" * 22]}
        )
        self.assertEqual(result["count"], 1)

    async def test_partial_failure_preserves_playlist_link(self):
        with patch.object(
            server,
            "_post",
            new=AsyncMock(side_effect=[{"id": "p" * 22}, RuntimeError("failure")]),
        ) as post:
            with self.assertRaisesRegex(
                RuntimeError, "https://open.spotify.com/playlist/"
            ):
                await create({"name": "Mix", "tracks": [{"uri": "a" * 22}]})
        self.assertEqual(post.await_count, 2)

    async def test_empty_plan_cannot_create(self):
        with patch.object(server, "_post", new_callable=AsyncMock) as post:
            with self.assertRaises(ValueError):
                await create({"name": "Mix", "tracks": []})
        post.assert_not_called()

    async def test_api_path_rejected_before_auth(self):
        with patch.object(client, "_get_access_token", new_callable=AsyncMock) as token:
            for path in ("tracks/../../me", "https://evil.test", "tracks/%2e%2e/me"):
                with self.assertRaises(ValueError):
                    await client._api("GET", path)
        token.assert_not_called()
