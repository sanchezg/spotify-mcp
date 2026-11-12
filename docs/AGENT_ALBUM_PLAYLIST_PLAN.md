# Agent-driven album playlists

Status: implemented locally; automated verification recorded in README.
Live acceptance is deferred by the user: "automated tests only for now."
Baseline: fork commit `03f0d6f`.

## Intended experience

> I like band A. Recommend five bands with a similar style, pick their top
> albums, and create a playlist containing those albums.

A connected agent recommends the music, explains its choices, and uses this
local MCP server to resolve the selections and create a private Spotify playlist.
The final response includes the playlist link and any unresolved limitations.

Defaults, unless the user specifies otherwise:

- Five distinct recommended bands, excluding the seed band.
- One essential, critically acclaimed studio album per band. This is an
  editorial recommendation, not a claim about Spotify listening statistics.
- Prefer the standard studio edition over deluxe, anniversary, live, compilation,
  or tribute releases. Do not silently replace the requested release.
- Include complete albums in the agent's selected album order, then disc and
  track order. Preserve tracks shared by distinct albums; remove repeated album
  selections, not intentional repetitions within the album sequence.
- Private playlist. No automatic deletion or replacement of existing playlists.
- Explicit tracks are permitted unless the user requests a clean playlist.
  If filtering would make an album incomplete, ask how to handle that album.

An explicit request to create a playlist authorizes creation. The internal
plan/commit split does not impose a second confirmation on the happy path.
A recommendation-only request must not create a playlist. Ask only to resolve
material ambiguity, unavailable content, or a change to the requested selection.

## Recommendation and Spotify responsibilities

The agent uses the user's stated preferences and independent music knowledge or
web research to select artists and albums. It explains stylistic similarities and
album choices, cites sources when researched, and identifies uncertain judgments.
It must not present guesses as objective popularity rankings. No Spotify-related
artists, Recommendations, Audio Features, or popularity ranking dependency is
needed for this workflow.

The Python service handles Spotify catalog matching, availability, pagination,
authorization, playlist writes, and verification. It contains no embedded LLM
provider and needs no OpenAI/Anthropic API key. The existing listening-history CLI
remains available; this milestone does not feed listening history into a model.

Spotify's policy restricts ingesting Spotify Content into AI/ML models. Design the
new agent profile so catalog responses and listening data remain in local code:
the agent submits independently chosen artist/album names and receives operation
status, opaque handles, echoed input references, and the resulting playlist link.
A human can inspect Spotify-derived match details in a local preview. Do not
claim this architecture alone establishes policy compliance; recheck applicable
terms before release. Raw legacy metadata tools must not be registered in this
profile. Keep them explicitly separate from the new workflow.

## Proposed MCP interface

| Tool | Inputs | Result and behavior |
| --- | --- | --- |
| `spotify_plan_album_playlist` | Playlist name; ordered selections containing artist, album, optional release year/edition or exact Spotify album ID; options | Read-only resolution; account-bound plan ID and per-input status. Full catalog preview stays local. No playlist is created. |
| `spotify_create_album_playlist` | Plan ID | Executes the exact stored selection once; returns operation ID, status and playlist link when known. Rejects expired plans or changed accounts. |
| `spotify_album_playlist_status` | Operation ID; `resume=false` by default | Reports completion/partial/uncertain status. An explicit resume reconciles the current playlist before continuing confirmed missing work. |

Provide a local command to inspect a plan and choose an exact album ID for an
ambiguous selection. An agent may also ask the user for an exact Spotify album
link; parse its ID locally without fetching arbitrary URLs. Accept only validated
Spotify album IDs, URIs, and canonical `open.spotify.com/album/...` links.

MCP tools return structured results with stable status/error codes. Tool
descriptions explain authorization semantics; a client-supplied `confirmed=true`
flag is not proof of human consent. Host approval controls remain the host's
responsibility. Treat all remote strings as data, never instructions.

## Implementation sequence

### 1. Shared API client and account handling

- Extract reusable auth/API operations from `server.py` into a client module;
  keep the existing CLI and legacy tool behavior covered by regression tests.
- Use immutable Spotify `account_id` rather than display name or mutable `id`
  for account binding. Compare the current `id` separately with playlist ownership.
- Serialize token refresh within a process. Use a private account/profile cache
  and a per-operation cross-process lock for durable operation state.
- Add bounded read retries for transient failures and honor `Retry-After` on
  429. Return a retry time rather than blocking a tool indefinitely.
- Use fixed Spotify hosts and validated path segments. Never follow an arbitrary
  pagination URL with a bearer token; construct the next request locally.
- New album profile requests private playlist write and private playlist read
  access for creation and verification. It does not need listening-history,
  saved-library, playback, or public-playlist permissions. Separate cached tokens
  from the existing profile to make the permission boundary explicit.

### 2. Album resolution and complete track collection

- Resolve each artist/album pair using bounded, paginated album search, up to
  100 candidates per selection. Exhaustion produces unresolved status, not an
  invented match. Support 1–20 albums and at most 1,000 tracks per plan; reject
  excess requests explicitly rather than truncating.
- Normalize Unicode, case, whitespace and punctuation for comparison. Preserve
  meaningful edition labels. Compare both artist and album, plus optional year
  and edition. Auto-select only a unique qualifying match; ties need resolution.
- Prefer a standard release when the request leaves edition unspecified. If
  only another edition exists, report that choice for resolution.
- Fetch album metadata and every `/albums/{id}/tracks` page. Preserve disc and
  track positions, account-market availability and relinked IDs.
- Stop before any write if an album is unresolved, pagination is incomplete,
  or a known unavailable track prevents the requested complete-album playlist.
  Unknown playability is a visible limitation, not proof of availability.
- Freeze ordered track URIs and selection options in a short-lived plan. Use a
  60-minute lifetime for uncommitted plans; edits produce a new plan ID.

### 3. Playlist execution and recovery

- Persist a private operation record before the first write. Bind it to the
  account, plan and intended ordered content. Use owner-only permissions and
  atomic updates. Never put tokens or operation state in Git.
- Create via `POST /me/playlists` with `public=false`; add tracks through
  `/playlists/{id}/items` in batches of at most 100, preserving order.
- Repeated calls for a completed plan return the existing result. Concurrent
  calls for one plan must not create two playlists.
- Persist the playlist ID as soon as creation is acknowledged. Record each
  batch as in-flight before sending, then acknowledged after a valid response.
- Do not blindly retry a write after a timeout, disconnect, or ambiguous server
  error. Spotify does not provide a general exactly-once guarantee here.
- If the playlist ID is known, read back every playlist page on resume. Continue
  only if its content is an exact recognized prefix of the intended sequence
  (accounting for documented relinking). Unexpected edits require review.
- If creation may have succeeded but no playlist ID was received, return
  `creation_uncertain` and require manual reconciliation. Never automatically
  create a replacement and claim that duplicates are impossible.
- Read back the final ordered contents before reporting `complete`. If readback
  is forbidden or fails, return `unverified` with the known playlist link.
- Clear catalog plans after completion/expiry. Retain a minimal completion receipt
  for 24 hours for duplicate-call protection, then delete it. Retain incomplete
  operation data only for recovery, with a documented purge command. Purging a
  pending operation never deletes its Spotify playlist and ends local recovery.

### 4. Agent integration and local deployment

- Add a dedicated stdio profile, proposed command:
  `uv run --locked spotify-mcp --profile albums`.
- Register only the three album-workflow tools in this profile. Make
  authentication accept the same profile so scopes and cache locations agree.
- Supply a provider-neutral agent workflow document and an optional MCP prompt
  describing recommendation criteria, tool sequence, and error behavior.
  Tool descriptions must be sufficient for clients that do not support prompts.
- Supply a generic MCP configuration example with an absolute executable/project
  path, client ID, and account-specific cache path. Check it with two local MCP
  clients available during implementation; document the versions actually tested.
- Add a startup diagnostic command that checks configuration and scopes without
  printing tokens or starting interactive login inside an agent tool call.
- Support local agents capable of spawning stdio MCP servers. Browser-only agents
  without local MCP support need a later hosted authenticated transport; remote
  deployment, multi-user OAuth and a web UI are outside this milestone.
- No additional Spotify SDK or third-party recommendation service is required.

### 5. Verification and release

Automated tests must cover behavior, including:

- Unique matches, ambiguous artist names, missing releases, accents, edition
  differences, exact album IDs and malicious URLs/path input.
- Albums with more than 50 tracks, multiple discs, repeated songs across albums,
  repeated album inputs, unavailable tracks, relinking and incomplete pagination.
- Preview performing zero writes, expired/account-mismatched plans, and an
  explicit content filter that would otherwise silently shorten an album.
- Playlists above 100 tracks, correct batch boundaries and exact final order.
- Duplicate/concurrent create calls, crash/restart between batches, timeouts
  before/after acknowledged writes, user edits, 401/403/429, and failed readback.
- MCP initialization, tool schemas, actual tool calls and structured error
  propagation over stdio. Ensure the album profile exposes no legacy metadata tools.
- Existing OAuth/token-security tests and the listening-history CLI regressions.

Live acceptance requires the user's authenticated Spotify development app and
an explicitly requested test playlist. Run the seed-band prompt through a real
connected agent; check five distinct recommended bands, defensible album choices,
the exact selected album editions, complete track order, private visibility, and
the returned link. Repeat the same create call and verify that it returns the
same playlist. Record account restrictions or unverified cases honestly.

Release gates: automated checks pass; live acceptance passes or is explicitly
reported as blocked; README explains setup and limitations; the security review
covers the new data and operation storage. Prepare a reviewable commit. Git push
remains manual if the environment retains its current restriction.

## Sources checked while planning

- [Spotify search](https://developer.spotify.com/documentation/web-api/reference/search):
  current search page size is at most 10.
- [Album tracks](https://developer.spotify.com/documentation/web-api/reference/get-an-albums-tracks):
  pagination supports at most 50 tracks per page; account market takes priority.
- [Add playlist items](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist):
  current items endpoint and batches of at most 100.
- [Spotify developer policy](https://developer.spotify.com/policy): content and
  AI/ML restrictions require a deliberate boundary between catalog processing
  and agent recommendations.

## Definition of done

From a supported configured local agent, the user can issue the seed-band request
and receive a verified private playlist containing the selected full albums,
with explanations grounded in independent recommendations. The common path needs
no manual track copying or terminal playlist-building step. Ambiguity and partial
failure are actionable and never disguised as a completed playlist.
