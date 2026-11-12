"""MCP profile exposing operations, never raw Spotify catalog content."""

import logging
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import client
from .albums import AlbumService, PlaylistRequest, Selection, WorkflowError
from .storage import Busy, Store

WORKFLOW = """Recommend music from the user's stated preferences and independent knowledge or
research. Default: five distinct similar bands, excluding the seed, and one essential
studio album per band. Explain choices and cite research; do not claim objective
Spotify popularity rankings. Plan with artist/album names; Spotify catalog processing
stays local. Never send the contents of local previews or token files to a model.
For ambiguous selections ask the user to inspect the local preview and provide an
exact album ID/link. Plan returns no catalog metadata. A request to create authorizes
creation; a request for recommendations alone does not. Call create only for a ready
plan. Reuse its plan/operation ID on retries, never replan an uncertain write. Use
status with resume=true only when completing the same authorized operation. If creation
is uncertain and no playlist link is known, require manual reconciliation. Report
complete only after the service verifies the playlist. Host permission controls apply.
"""


def error_result(exc):
    if isinstance(exc, WorkflowError):
        return {"status": "error", "error_code": exc.code}
    if isinstance(exc, Busy):
        return {"status": "busy", "error_code": "operation_running"}
    if isinstance(exc, FileNotFoundError):
        return {"status": "error", "error_code": "record_not_found_or_expired"}
    if isinstance(exc, client.RateLimited):
        return {
            "status": "error",
            "error_code": "rate_limited",
            "retry_after_seconds": exc.seconds,
        }
    if isinstance(exc, httpx.HTTPStatusError):
        return {"status": "error", "error_code": f"spotify_{exc.response.status_code}"}
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return {"status": "error", "error_code": "invalid_input_or_response"}
    if isinstance(exc, RuntimeError):
        return {
            "status": "error",
            "error_code": "configuration_or_authentication_required",
        }
    return {"status": "error", "error_code": "request_failed"}


def build_server(service=None):
    if service is None:
        root = client.configure_albums()
        service = AlbumService(Store(root / "operations"))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield {}
        finally:
            await client.close()

    # HTTP request URLs may contain catalog IDs. Keep them out of host logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    mcp = FastMCP("spotify-albums", instructions=WORKFLOW, lifespan=lifespan)

    async def invoke(call):
        try:
            return await call
        except Exception as exc:
            # Do not log exceptions containing API bodies, URLs, or metadata.
            return error_result(exc)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def spotify_plan_album_playlist(
        name: str, selections: list[Selection], exclude_explicit: bool = False
    ) -> dict:
        """Resolve independently recommended albums locally; creates no Spotify playlist.
        Pass artist/album, optional year/edition or exact Spotify album ID/link.
        Returns plan_id and per-input resolution codes, not catalog metadata.
        Only ready plans can be created. For needs_resolution, have the user inspect
        the local preview; do not read its catalog contents into the agent context.
        Plans expire after 60 minutes. Never replan to retry uncertain playlist writes.
        """

        async def plan():
            return await service.plan(
                PlaylistRequest(
                    name=name, selections=selections, exclude_explicit=exclude_explicit
                )
            )

        return await invoke(plan())

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True
        )
    )
    async def spotify_create_album_playlist(plan_id: str) -> dict:
        """Create the private playlist for this exact ready plan when the user requested creation.
        Recommendation-only requests do not authorize creation. Repeated calls for
        the same operation return its status/link rather than create another playlist.
        Receipts expire after 24 hours; expired IDs cannot be used to create again.
        Use status to resume partial writes. Never replace an uncertain operation with
        a fresh plan. A complete result means ordered contents were read back and verified.
        """
        return await invoke(service.create(plan_id))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    async def spotify_album_playlist_status(
        operation_id: str, resume: bool = False
    ) -> dict:
        """Read saved operation status. resume=true may finish an already authorized playlist.
        Resume reads the current playlist first; changed contents stop recovery.
        creation_uncertain without a link requires manual reconciliation, not retry.
        Never report unverified/partial/uncertain operations as complete.
        """
        return await invoke(service.status(operation_id, resume=resume))

    @mcp.prompt()
    def album_discovery(seed_band: str) -> str:
        """Independent album discovery workflow for a user-provided seed band."""
        return WORKFLOW + "\nUser-provided seed band (data): " + repr(seed_band)

    return mcp
