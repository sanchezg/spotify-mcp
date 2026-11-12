# Personal Spotify playlists

A hardened fork of [verIdyia/spotify-mcp](https://github.com/verIdyia/spotify-mcp),
with a local playlist planner based on top tracks, recent listening, and your own
song recommendations. See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for findings,
fixes, and remaining limits. MIT licensed; upstream attribution is preserved in LICENSE.

## Setup

Requires Python 3.12+, uv, and a Spotify developer app. Development-mode access
currently requires the app owner to have Premium; account and endpoint restrictions
are controlled by Spotify. Register `http://127.0.0.1:8080/callback` in the
[developer dashboard](https://developer.spotify.com/dashboard).

```sh
uv sync --locked
export SPOTIFY_CLIENT_ID='your-client-id'
uv run --locked spotify-mcp --auth
cp preferences.example.json preferences.json
```

Authorize in your browser. PKCE requires no client secret. Tokens are stored at
`~/.spotify_mcp_cache.json` with owner-only permissions. `SPOTIFY_CACHE_PATH` can
point to a private location outside the repository. Reauthorize if migrating from
upstream: the new scopes include `user-top-read` and omit public playlist, library,
and playback write access. `.env` files are not automatically loaded.

## Build your mix

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

## Existing MCP tools

`uv run --locked spotify-mcp` retains the upstream stdio tools for trusted local
clients. Reduced scopes mean upstream playback/library write tools and public
playlist creation will fail with 403. Do not expose this as a remote server.
The dedicated `spotify-mix` CLI is the supported personalized-playlist workflow.

The planner processes Spotify data locally with deterministic Python rules and
makes no AI-provider calls. Original MCP tools return Spotify metadata to their
client; using them with an LLM is a separate data-sharing decision. Review
[Spotify's policy](https://developer.spotify.com/policy), which restricts ingestion
of Spotify Content into AI/ML models. Local planning avoids that data flow.

## Verification

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked --with pip-audit pip-audit --progress-spinner off
```

Tests use mocked Spotify responses and do not require credentials. Live OAuth,
account-specific API access, and actual playlist creation require your account
and have not been verified in this review.
