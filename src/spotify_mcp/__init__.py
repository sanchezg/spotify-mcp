"""Local CLI entry point; authentication is always explicit."""

import argparse
import asyncio
import json


def main():
    parser = argparse.ArgumentParser(description="Local Spotify MCP server")
    parser.add_argument("--profile", choices=["legacy", "albums"], default="legacy")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--auth", action="store_true")
    actions.add_argument(
        "--doctor",
        action="store_true",
        help="Check local configuration without network calls",
    )
    actions.add_argument(
        "--inspect-plan",
        metavar="ID",
        help="Print catalog preview locally; do not send it to an LLM",
    )
    actions.add_argument(
        "--select-album",
        metavar="PLAN_ID",
        help="Create a revised plan using an exact album selection",
    )
    actions.add_argument(
        "--purge-operation",
        metavar="ID",
        help="Delete local state only; never deletes a Spotify playlist",
    )
    parser.add_argument("--selection-index", type=int)
    parser.add_argument("--album-id")
    args = parser.parse_args()
    from . import client
    from .album_server import error_result
    from .albums import AlbumService, PlaylistRequest
    from .storage import Store

    try:
        root = client.configure_albums() if args.profile == "albums" else None
        if args.auth:
            client.run_auth()
        elif args.doctor:
            token = client._load_token()
            missing = sorted(
                set(client.SCOPES.split()) - set(token.get("scope", "").split())
            )
            report = {
                "profile": args.profile,
                "client_id_configured": bool(client.CLIENT_ID),
                "token_present": bool(token),
                "missing_scopes": missing,
                "status": "ready"
                if client.CLIENT_ID and token and not missing
                else "setup_required",
                "network_verified": False,
            }
            print(json.dumps(report, indent=2))
            if report["status"] != "ready":
                raise SystemExit(1)
        elif args.inspect_plan or args.select_album or args.purge_operation:
            if root is None:
                parser.error("Local album operations require --profile albums")
            store = Store(root / "operations")
            if args.purge_operation:
                store.purge(args.purge_operation)
                print('{"status": "purged_local_state_only"}')
            elif args.inspect_plan:
                store.cleanup()
                with store.lock(args.inspect_plan):
                    print(
                        json.dumps(
                            store.read(args.inspect_plan), indent=2, ensure_ascii=True
                        )
                    )
            else:
                if args.selection_index is None or args.album_id is None:
                    parser.error(
                        "--select-album requires --selection-index and --album-id"
                    )

                async def select():
                    try:
                        service = AlbumService(store)
                        with store.lock(args.select_album):
                            record = await service.load(args.select_album)
                            if record["status"] not in ("ready", "needs_resolution"):
                                raise ValueError(
                                    "Only uncommitted plans can be revised"
                                )
                            request = record["request"]
                            if (
                                not 0
                                <= args.selection_index
                                < len(request["selections"])
                            ):
                                raise ValueError("Selection index out of range")
                            request["selections"][args.selection_index]["album_id"] = (
                                args.album_id
                            )
                            return await service.plan(
                                PlaylistRequest.model_validate(request)
                            )
                    finally:
                        await client.close()

                print(json.dumps(asyncio.run(select()), indent=2))
        elif args.profile == "albums":
            from .album_server import build_server

            build_server(AlbumService(Store(root / "operations"))).run()
        else:
            from .server import mcp

            mcp.run()
    except Exception as exc:
        parser.exit(1, json.dumps(error_result(exc)) + "\n")


__all__ = ["main"]
