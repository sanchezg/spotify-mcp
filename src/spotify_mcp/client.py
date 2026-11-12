"""Shared local Spotify authentication and HTTP transport."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback")

CACHE_PATH = os.environ.get(
    "SPOTIFY_CACHE_PATH",
    os.path.join(os.path.expanduser("~"), ".spotify_mcp_cache.json"),
)

SCOPES = " ".join(
    [
        "user-top-read",
        "user-library-read",
        "user-read-playback-state",
        "user-read-currently-playing",
        "user-read-recently-played",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
    ]
)

# ── Token Management ─────────────────────────────────────────────────────────
_token_cache: dict = {}
_refresh_lock = asyncio.Lock()


def _load_token() -> dict:
    global _token_cache
    if _token_cache:
        return _token_cache
    try:
        fd = os.open(CACHE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Token cache must be a regular file owned by you.")
            os.fchmod(f.fileno(), 0o600)
            _token_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _token_cache


def _save_token(token: dict):
    global _token_cache
    parent = os.path.dirname(os.path.abspath(CACHE_PATH))
    fd, temporary = tempfile.mkstemp(prefix=".spotify-token-", dir=parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(token, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CACHE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _token_cache = token


async def _get_access_token() -> str:
    async with _refresh_lock:
        token = _load_token()
        if not token:
            raise RuntimeError(
                "No Spotify token found. Run authentication for this profile first."
            )
        if token.get("expires_at", 0) < time.time() + 60:
            token = await _refresh_token(token["refresh_token"])
        return token["access_token"]


async def _refresh_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as c:
        resp = await c.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        data["refresh_token"] = data.get("refresh_token", refresh_token)
        data.setdefault("scope", _load_token().get("scope", ""))
        data["expires_at"] = time.time() + data.get("expires_in", 3600)
        _save_token(data)
        return data


# ── HTTP Helpers ─────────────────────────────────────────────────────────────
_http: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=15)
    return _http


async def _api(method: str, path: str, **kwargs) -> Optional[dict]:
    """Make an authenticated request to the Spotify API."""
    # Paths are built locally, never accept traversal or injected query parameters.
    parsed_path = urlparse(path)
    if (
        parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.fragment
        or "%" in path
        or "\\" in path
        or any(part in (".", "..") for part in parsed_path.path.split("/"))
    ):
        raise ValueError("Invalid Spotify API path")
    if path.startswith("/") or any(ord(c) < 32 for c in path):
        raise ValueError("Invalid Spotify API path")
    token = await _get_access_token()
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            resp = await _get_http().request(
                method,
                f"{SPOTIFY_API}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        except httpx.TransportError:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.25 * 2**attempt)
            continue
        if resp.status_code == 429:
            # Do not occupy the MCP connection for a potentially long quota wait.
            raise RateLimited(retry_after(resp.headers.get("Retry-After")))
        if resp.status_code >= 500 and attempt + 1 < attempts:
            await asyncio.sleep(0.25 * 2**attempt)
            continue
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


class RateLimited(RuntimeError):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__("Spotify rate limited the request")


def retry_after(value: str | None) -> int:
    import math
    from email.utils import parsedate_to_datetime

    try:
        return max(1, math.ceil(float(value)))
    except (TypeError, ValueError, OverflowError):
        try:
            return max(
                1, math.ceil(parsedate_to_datetime(value).timestamp() - time.time())
            )
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 60


async def close():
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


ALBUM_SCOPES = "playlist-modify-private playlist-read-private"


def configure_albums():
    """Profile configuration occurs once before serving tools or authenticating."""
    global SCOPES, CACHE_PATH, _token_cache
    from pathlib import Path

    from .storage import private_directory

    SCOPES = ALBUM_SCOPES
    identity = hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:16]
    root = (
        Path(
            os.environ.get(
                "SPOTIFY_ALBUM_HOME",
                str(Path.home() / ".local/share/spotify-mcp/albums"),
            )
        )
        / identity
    )
    private_directory(root)
    CACHE_PATH = str(root / "token.json")
    _token_cache = {}
    return root


async def account() -> dict:
    if not CLIENT_ID:
        raise RuntimeError("Set SPOTIFY_CLIENT_ID first")
    await _get_access_token()
    missing = set(ALBUM_SCOPES.split()) - set(_load_token().get("scope", "").split())
    if missing:
        raise RuntimeError("Authorize the albums profile to grant its required scopes")
    data = await _get("me")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(field), str) or not data[field]
        for field in ("account_id", "id")
    ):
        raise RuntimeError("Spotify account identity unavailable")
    return {"account_id": data["account_id"], "id": data["id"]}


async def _get(path: str, **params) -> Optional[dict]:
    return await _api("GET", path, params=params)


async def _put(path: str, json_data: dict = None) -> Optional[dict]:
    return await _api("PUT", path, json=json_data)


async def _post(path: str, json_data: dict = None) -> Optional[dict]:
    return await _api("POST", path, json=json_data)


async def _delete(path: str, json_data: dict = None) -> Optional[dict]:
    return await _api("DELETE", path, json=json_data)


def _track_uri(value: str) -> str:
    value = value.removeprefix("spotify:track:")
    if not re.fullmatch(r"[A-Za-z0-9]{22}", value):
        raise ValueError("Invalid track ID")
    return "spotify:track:" + value


def _callback_values(path: str, expected_path: str, state: str):
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    if parsed.path != expected_path or len(qs.get("state", [])) != 1:
        return None
    if not secrets.compare_digest(qs["state"][0].encode(), state.encode()):
        return None
    if "error" in qs:
        return (None, "Authorization denied")
    codes = qs.get("code", [])
    return (codes[0], None) if len(codes) == 1 and codes[0] else None


def run_auth():
    """Local OAuth with state validation and PKCE; no client secret required."""
    import webbrowser

    if not CLIENT_ID:
        raise RuntimeError("Set SPOTIFY_CLIENT_ID first.")
    parsed = urlparse(REDIRECT_URI)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path != "/callback"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Redirect must be http://127.0.0.1:PORT/callback")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    result = None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def do_GET(self):
            nonlocal result
            value = _callback_values(self.path, parsed.path, state)
            if value is not None:
                result = value
            self.send_response(200 if value and value[0] else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            self.wfile.write(b"Return to the terminal to finish authorization.")

        def log_message(self, format, *args):
            pass

    with HTTPServer(("127.0.0.1", parsed.port), Handler) as server:
        server.timeout = 1
        url = SPOTIFY_AUTH_URL + "?" + urlencode(params)
        print("Open this URL to authorize Spotify:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 180
        while result is None and time.monotonic() < deadline:
            server.handle_request()
    if not result or not result[0]:
        raise RuntimeError("Authorization denied or timed out; no token saved.")
    resp = httpx.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result[0],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    _save_token(token)
    print("Spotify authorization saved with owner-only file permissions.")
