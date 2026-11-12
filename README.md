# Personal Spotify playlists

A hardened fork of [verIdyia/spotify-mcp](https://github.com/verIdyia/spotify-mcp)
with an **agent-driven album playlist workflow** and a separate listening-history
mix CLI. MIT licensed; upstream attribution is preserved in LICENSE.

## Start using album playlists with an agent

The agent chooses similar bands and essential albums using independent knowledge
or research. This app matches those choices on Spotify and creates a verified
private playlist containing the full albums in disc/track order. It does not use
an embedded LLM or require an AI-provider API key.

Requirements: macOS or Linux, Python 3.12+, uv, a local MCP-capable agent host, and
a Spotify developer app. Register `http://127.0.0.1:8080/callback` in the
[Spotify dashboard](https://developer.spotify.com/dashboard). Development-mode
access requires the app owner to have Premium and the listening account to have
app access. Spotify controls account quotas and endpoint availability.

Run from this checkout (on the original machine, the directory is
`/Users/gonzalo/orca/projects/spotify-recom/upstream-review`):

```sh
uv sync --locked
export SPOTIFY_CLIENT_ID='YOUR_SPOTIFY_CLIENT_ID'
# Use the same private location here and in the MCP configuration.
export SPOTIFY_ALBUM_HOME="$HOME/.local/share/spotify-albums-personal"
uv run --locked spotify-mcp --profile albums --auth
uv run --locked spotify-mcp --profile albums --doctor
```

Login uses PKCE; no client secret is required. Authorize this profile even if you
already authenticated the older CLI: it uses a separate token and requests only
`playlist-modify-private` and `playlist-read-private`. The diagnostic command
checks local configuration and scopes; it makes no Spotify requests and does not
claim the token is still valid online. `.env` files are not automatically loaded.

Copy [the MCP configuration example](docs/mcp-config.example.json) into your host's
MCP settings. Replace all absolute paths and the Client ID. `command -v uv` shows
your uv executable. Use the same `SPOTIFY_ALBUM_HOME` value as authentication.
Restart/reconnect the host; it should expose exactly these tools:

| Tool | Purpose |
| --- | --- |
| `spotify_plan_album_playlist` | Resolve albums without creating a playlist |
| `spotify_create_album_playlist` | Create the private playlist from a ready plan |
| `spotify_album_playlist_status` | Inspect an operation; optionally resume it |

Give your agent [the workflow instructions](docs/AGENT_WORKFLOW.md), or use its
optional `album_discovery` MCP prompt. Then ask:

> I like Porcupine Tree. Recommend five similar bands and one essential studio
> album from each. Explain your choices and create a private playlist containing
> the complete albums in track order.

No cloud deployment or public server is required. The host starts the process
when it connects. Running the bare server in a terminal waits for MCP protocol
messages; it is not an interactive chat interface. Browser-only agents without a
local MCP bridge require a separate future hosted integration.

## Matching, limits and recovery

Defaults: standard studio editions, complete albums, explicit songs allowed,
private playlist. Ambiguous matches and missing/known-unplayable tracks stop before
creation. Exact album links can resolve edition ambiguity. The service preserves
repeated tracks across distinct albums, but removes duplicate album selections.
Search is bounded to 100 candidates per selection; requests support 1–20 albums
and at most 1,000 tracks. No request is silently truncated. Unknown playability is
reported as a warning; content verification does not guarantee playback everywhere.

Read-only plans expire after 60 minutes. Once creation starts, a durable record
allows recovery after process restarts. Repeated create calls for that ID return
its existing status/link. Completed receipts last 24 hours. Readback verifies the
ordered contents, private visibility, owner and stable snapshot before reporting
`complete`. A failed verification returns `unverified`, never success.

If a write times out, resume reads the playlist before adding anything. Changed
contents stop recovery. If the initial create request has an unknown outcome and
no playlist ID is known, check Spotify manually before starting over. No system
can guarantee exactly-once Spotify creation across that failure boundary.

Private state is stored under `SPOTIFY_ALBUM_HOME/<client-id-hash>/`; the default
home is `~/.local/share/spotify-mcp/albums`. Use a separate home per Spotify account.
Never place it in a Git repository. Files have owner-only permissions; tokens are
still plaintext, so protect your account and backups. Restart clients after changing
accounts or reauthorizing. Concurrent operation execution is locked across processes;
token refresh is serialized within one process. Prefer one running host per token
home to avoid cross-process refresh races.

Inspect a plan locally, resolve an edition, or purge local state:

```sh
uv run --locked spotify-mcp --profile albums --inspect-plan PLAN_ID
uv run --locked spotify-mcp --profile albums --select-album PLAN_ID \
  --selection-index 0 --album-id 'spotify:album:ALBUM_ID'
uv run --locked spotify-mcp --profile albums --purge-operation OPERATION_ID
```

Purging removes only local recovery state, not the Spotify playlist. Uncommitted
plans and completed receipts are cleaned up on planning/inspection; incomplete
operations remain until resolved or purged. Tiny lock files remain to avoid lock
inode races. Do not feed local catalog preview output to a model.

The album profile exposes no Spotify catalog/history metadata tools. Recommendations
use independent inputs; catalog data stays in local Python. Review
[Spotify's policy](https://developer.spotify.com/policy) before deployment; this
architecture is not a legal compliance guarantee. See [the security review](SECURITY_REVIEW.md).

## Verification

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked --with ruff ruff check --isolated --select E4,E7,E9,F,I src tests
uv run --locked --with ruff ruff format --isolated --check src tests
```

The 52 automated tests use fake Spotify responses and exercise actual stdio communication
with the official Python MCP client (SDK 1.30.0) and an independent JSON-RPC test
client (protocol 2025-03-26). These are protocol tests, not claims of testing every
agent product. Live Spotify authentication and playlist creation are deferred at
the user's request: **automated tests only for now**.

## Separate listening-history mix CLI

This CLI keeps the earlier ranking workflow. It does not perform band/album
recommendations through an agent. Authenticate its separate legacy profile first:

```sh
uv run --locked spotify-mcp --auth
cp -n preferences.example.json preferences.json
```

Edit `preferences.json`:

- `recommendations`: Spotify track IDs or `spotify:track:ID` URIs you want included.
- `favorite_artists`: artist names to boost (case insensitive).
- `excluded_artists`: artist names to exclude, including collaborations.
- `exclude_explicit`: omit explicit tracks and tracks with unknown explicit status.
- `time_range`: `short_term`, `medium_term`, or `long_term` for top tracks.
- `size`: 1–100; `max_per_artist`: artist diversity cap.

Preview without writing to Spotify:

```sh
uv run --locked spotify-mix --preferences preferences.json
```

Preview and create after typing `yes`:

```sh
uv run --locked spotify-mix --preferences preferences.json --create
```

The preview includes track names, artists, selection reasons, and warnings. Creation
uses exactly that preview and always creates a **private** playlist. It never edits
an existing playlist. A failed add operation reports the created playlist URL;
inspect it before retrying to avoid duplicates.

Ranking uses up to 50 top tracks and the last 50 plays: top-track rank gives 0.1–5
points, each recent play gives 2, your recommendations give 100, and favorite
artists add 10. Duplicates merge; exclusions and artist caps still apply to your
recommendations. It can return fewer tracks than requested. This is a personal
mix, not Spotify's discovery algorithm; it does not call deprecated Recommendations
or Audio Features endpoints. Recommended songs must be supplied as IDs/URIs, not
natural-language descriptions. No history or preferences are persisted by the
planner; terminal output may remain in your terminal history/logging system.

## Legacy MCP tools

`uv run --locked spotify-mcp` still starts the eight original tools for explicitly
trusted local clients. This profile returns Spotify metadata to its client and
has broader read scopes; the album profile above does not expose these tools.
Reduced write scopes mean playback/library mutations and public playlist creation
remain unavailable. The legacy token uses `SPOTIFY_CACHE_PATH` or
`~/.spotify_mcp_cache.json`; that variable does not override album-profile storage.
