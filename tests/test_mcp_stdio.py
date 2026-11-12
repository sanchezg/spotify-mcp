"""Exercise the wire protocol with the official SDK and an independent JSON-RPC client."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/mcp_fixture.py"
ARGS = {
    "name": "Agent discovery",
    "selections": [{"artist": "Band", "album": "Album"}]
    + [{"artist": f"Band {index}", "album": f"Album {index}"} for index in range(2, 6)],
}


def payload(result):
    # Use text for compatibility with clients lacking structuredContent support.
    return json.loads(result.content[0].text)


class SDKStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_workflow_with_official_python_client(self):
        with tempfile.TemporaryDirectory() as directory:
            params = StdioServerParameters(
                command=sys.executable, args=[str(FIXTURE), directory]
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 3)
                    plan = payload(
                        await session.call_tool("spotify_plan_album_playlist", ARGS)
                    )
                    self.assertEqual(plan["status"], "ready")
                    key = plan["plan_id"]
                    result = payload(
                        await session.call_tool(
                            "spotify_create_album_playlist", {"plan_id": key}
                        )
                    )
                    self.assertEqual(result["status"], "complete")
                    again = payload(
                        await session.call_tool(
                            "spotify_create_album_playlist", {"plan_id": key}
                        )
                    )
                    self.assertEqual(again, result)
                    status = payload(
                        await session.call_tool(
                            "spotify_album_playlist_status", {"operation_id": key}
                        )
                    )
                    self.assertEqual(status, result)
                    prompts = await session.list_prompts()
                    self.assertEqual(prompts.prompts[0].name, "album_discovery")
                    self.assertNotIn("Private title", json.dumps(plan))

    async def test_real_cli_profile_without_credentials_returns_safe_setup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-c",
                    "from spotify_mcp import main; main()",
                    "--profile",
                    "albums",
                ],
                env={
                    **os.environ,
                    "SPOTIFY_CLIENT_ID": "",
                    "SPOTIFY_ALBUM_HOME": directory,
                },
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 3)
                    result = payload(
                        await session.call_tool("spotify_plan_album_playlist", ARGS)
                    )
                    self.assertEqual(
                        result["error_code"], "configuration_or_authentication_required"
                    )

    async def test_independent_jsonrpc_stdio_client(self):
        with tempfile.TemporaryDirectory() as directory:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(FIXTURE),
                directory,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            counter = 0

            async def send(method, params=None, notification=False):
                nonlocal counter
                message = {"jsonrpc": "2.0", "method": method}
                if params is not None:
                    message["params"] = params
                if not notification:
                    counter += 1
                    message["id"] = counter
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()
                if notification:
                    return
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
                    if not line:
                        self.fail("MCP server closed before replying")
                    reply = json.loads(line)
                    if reply.get("id") == counter:
                        self.assertNotIn("error", reply)
                        return reply["result"]

            try:
                initialized = await send(
                    "initialize",
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "independent-jsonrpc-test",
                            "version": "1.0",
                        },
                    },
                )
                self.assertIn("serverInfo", initialized)
                await send("notifications/initialized", notification=True)
                tools = await send("tools/list")
                self.assertEqual(len(tools["tools"]), 3)
                plan = await send(
                    "tools/call",
                    {"name": "spotify_plan_album_playlist", "arguments": ARGS},
                )
                plan = json.loads(plan["content"][0]["text"])
                result = await send(
                    "tools/call",
                    {
                        "name": "spotify_create_album_playlist",
                        "arguments": {"plan_id": plan["plan_id"]},
                    },
                )
                self.assertEqual(
                    json.loads(result["content"][0]["text"])["status"], "complete"
                )
                error = await send(
                    "tools/call",
                    {
                        "name": "spotify_create_album_playlist",
                        "arguments": {"plan_id": "../bad"},
                    },
                )
                self.assertEqual(
                    json.loads(error["content"][0]["text"])["error_code"],
                    "invalid_input_or_response",
                )
            finally:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


class CLITests(unittest.TestCase):
    def test_doctor_does_not_contact_spotify_or_print_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from spotify_mcp import main; main()",
                    "--profile",
                    "albums",
                    "--doctor",
                ],
                env={
                    **os.environ,
                    "SPOTIFY_CLIENT_ID": "test-client-identifier",
                    "SPOTIFY_ALBUM_HOME": directory,
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "setup_required")
            self.assertFalse(report["network_verified"])
            self.assertNotIn("test-client-identifier", result.stdout + result.stderr)
