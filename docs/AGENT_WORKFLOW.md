# Instructions for connected agents

Use the `spotify-albums` MCP server for album discovery playlists. Its tools work
with any local MCP host that can start a stdio process; no provider API key is
configured in the server. Recommendations come from the agent, not a built-in
Spotify similarity/ranking API.

When the user names a band and asks for similar music:

1. Choose five distinct similar bands, excluding the seed, unless a different
   count was requested. Use independent knowledge or web research. Explain which
   musical characteristics connect them. Cite researched claims; do not invent
   popularity statistics or pretend an editorial judgment is a measured ranking.
2. Pick one essential studio album per band unless the user specifies another
   criterion. Prefer standard editions. Explain your choices before executing.
3. Call `spotify_plan_album_playlist` with the playlist name and ordered
   `{artist, album}` selections. Optional fields: `year`, `edition`, `album_id`.
   The last accepts an exact Spotify album ID, URI or canonical album link.
   `exclude_explicit=true` requires complete clean albums; it never silently drops
   explicit songs. All selections must be resolved before writing anything.
4. A `ready` plan may be created when the user explicitly requested a playlist.
   Call `spotify_create_album_playlist` using the returned `plan_id`. An internal
   preview does not require asking for a redundant confirmation. A request only
   for recommendations does not authorize creation.
5. Report success only for `complete`, and give the returned `playlist_url`.
   `warnings` describe limitations such as unknown track playability. Verification
   proves the private playlist's ordered contents, not that every track will play
   on every device or that the musical recommendations are objectively best.

## Resolving ambiguity

`needs_resolution` gives an `input_index` and status for each submitted selection.
Ask the user to inspect the local preview or supply an exact album link. The user
can run:

```sh
uv run --locked spotify-mcp --profile albums --inspect-plan PLAN_ID
uv run --locked spotify-mcp --profile albums --select-album PLAN_ID \
  --selection-index 0 --album-id 'https://open.spotify.com/album/ALBUM_ID'
```

The second command produces a new plan ID. Selection indices start at zero.
The user can provide that ID or supply an exact album link to include in a revised
planning call. Editing is permitted only for uncommitted plans. Do not substitute
an album or edition without resolving the user's intended choice.

Do not read local catalog previews or token files into the model context. Catalog
responses remain inside Python; the MCP profile returns control statuses, IDs,
input indices and the final playlist link. There are no raw search/history tools
in this profile. The optional `album_discovery` MCP prompt contains the same
workflow; it is not required for clients that do not support MCP prompts.

## Failure and recovery

- `configuration_or_authentication_required`: ask the user to run local `--doctor`
  and `--auth` with the albums profile. Tools never launch a login browser.
- `spotify_401` / `spotify_403`: account authorization, scopes or app access need
  attention. Do not switch to a less constrained API or silently skip an album.
- `rate_limited`: wait the returned duration / until `retry_at`. Avoid tight loops.
- `busy`: another process is working on this operation; poll its existing ID.
- `write_uncertain`, `writing`, `partial` or `unverified` with a known playlist:
  use `spotify_album_playlist_status` with the same operation ID. Set `resume=true`
  to complete the existing authorized operation after reconciliation. This may
  write missing batches; default status calls do not write to Spotify.
- `creation_rejected`: Spotify explicitly rejected creation (for example 403 or
  429). After fixing the cause or waiting until `retry_at`, resume the same
  operation. The service can safely attempt creation again for this definite rejection.
- `creation_uncertain` without a playlist link: the original creation may have
  succeeded, or its rejection needs investigation. Ask the user to check Spotify.
  Never automatically replan/recreate to bypass uncertainty. Manual reconciliation
  can mean keeping/completing the discovered playlist, or explicitly starting over.
- `playlist_edited` / visibility or owner changes: stop. Recovery never overwrites
  the user's edits or changes a public playlist back to private without instruction.
- Expired/missing IDs: explain expiry. Do not recreate an already-completed playlist
  just because its local receipt has expired.

Keep the same operation ID across retries. Local receipts provide duplicate-call
protection for 24 hours. Network failures cannot provide an absolute exactly-once
Spotify guarantee; unresolved creation outcomes require human reconciliation.
