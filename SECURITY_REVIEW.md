# Security review — 2026-11-12

Reviewed upstream commit `b86d58933dd44ac05a4746b5e3b6dd1928de24bf`.
Scope: both Python modules, packaging/lockfile, configuration, documented flows,
and current working-tree behavior. This is a source review and focused regression
testing, not a penetration test, security certification, or full Git-history audit.

**Verdict:** do not use the upstream version unchanged. The patched fork is
reasonable for trusted local, single-user testing after the fixes below. No obvious
malicious execution, telemetry, third-party exfiltration, or shell execution was
found in the reviewed application code. Network destinations are Spotify API and
authorization hosts; dependency installation separately contacts package registries.

| Finding | Risk | Fix in this fork |
| --- | --- | --- |
| OAuth lacked state validation | Medium: login CSRF/account confusion | Random state, constant-time comparison, exact callback path and single-value checks |
| OAuth lacked PKCE | Defense in depth against intercepted authorization codes | S256 PKCE, no stored client secret |
| Callback listened on configurable host, reflected error HTML, opened browser before binding, and waited indefinitely | Medium: network exposure/reflected markup; reliability failures | Loopback-only callback, static plain text, bind first, bounded waiting and socket timeout |
| Token cache used default file permissions and non-atomic overwrite | Medium: other local users could read refresh tokens under permissive umask | Atomic owner-only replacement; no-follow reads; owner/type checks |
| Public playlist default and unnecessary write scopes | Privacy/least privilege | Private default; removed public-playlist, playback-write and library-write scopes |
| IDs interpolated into API paths/queries without validation | Medium: tool caller could alter intended API path/parameters | Track and playlist IDs validated; traversal rejected; queue query encoded |
| Stale lockfile described v0.2.0, not v0.4.0 source | Supply-chain/reproducibility issue | Regenerated lock; current MCP 1.x and httpx constraints; locked install |
| Generic exception strings exposed implementation details | Low | Generic non-HTTP errors; controlled input errors |

Dependency audit: `uv run --with pip-audit pip-audit --progress-spinner off`
reported **no known vulnerabilities** for resolved packages. The local spotify-mcp
package is not on PyPI and was skipped by the advisory tool; its code was reviewed
manually. This result is point-in-time, not proof that dependencies are vulnerability-free.
No installed third-party package source was exhaustively reviewed.

Regression tests cover callback rejection, token permissions and symlinks, URI
and path injection, error sanitization, ranking/deduplication/exclusions, no-write
preview, missing-scope reporting, private creation, empty selection, and partial
write failure reporting. Live Spotify login and API behavior remain untested.

## Remaining trust boundaries

- Refresh tokens remain plaintext on disk behind filesystem permissions. Protect
  your user account, disk, and backups. Same-user malware can read them.
- MCP clients can invoke authorized tools. There is no remote authentication or
  per-tool approval policy; use only trusted local stdio clients. Spotify metadata
  is untrusted content and must not be treated as instructions by an agent.
- Original MCP tools expose metadata to their client. The dedicated CLI keeps
  listening data out of AI models; see Spotify policy before integrating an LLM.
- Broad read scopes support inherited tools. Write privileges are restricted to
  private playlists, but a trusted client can still modify accessible private playlists.
- Concurrent processes sharing a token cache can race during token refresh.
  Run one instance per cache; do not share the cache with other applications.
- Account restrictions, rate limits, token revocation and network failures may
  prevent completion. Writes are not automatically retried; partial creation is
  explicitly reported. Some inherited tools still have upstream API limitations.
- Review future upstream updates and run the dependency audit again before upgrading.

References: [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow),
[top items](https://developer.spotify.com/documentation/web-api/reference/get-users-top-artists-and-tracks),
[API migration](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
[developer policy](https://developer.spotify.com/policy).


## Album workflow update — version 0.5.0

The new profile is implemented in `album_server.py`, `albums.py`, and `storage.py`.
Shared OAuth/HTTP code moved into `client.py`; the original MCP surface remains in
`server.py`. Existing token/auth regression tests now target the shared client.

- Album-profile tokens are isolated by profile and Client ID under a private
  configurable home. Its scopes are private playlist read/write only. Operation
  records bind to a fingerprint of Spotify's immutable `account_id`, not display name or mutable `id`.
- Only three workflow tools are registered. Spotify catalog metadata and track
  lists are not returned to agent tools; local human inspection remains available.
  HTTP-client request logging is suppressed in this profile to avoid catalog URLs
  in host logs. Agent recommendations use independently supplied artist/album names.
- A random plan ID is a local reference, not an authentication or consent token.
  Hosts must enforce their own tool authorization. The user's explicit create
  request authorizes the normal flow; the server cannot prove conversational consent.
- State directories are owner-only; JSON files use atomic replacement, file and
  directory fsync, owner-only permissions, and no-follow reads. Nonblocking process
  locks prevent concurrent execution of the same plan. Lock files remain after
  purging to avoid replacing an inode that another process already opened.
- Ready plans expire after one hour; completed catalog plans become minimal
  24-hour receipts. Expiry cleanup runs on planning/inspection, not a background
  timer. Incomplete operation state persists for recovery until completed/purged.
- Planning validates input limits and exact Spotify link forms. All API hosts are
  fixed; pagination uses local offsets and never follows response URLs with tokens.
- Writes are journaled before sending. Known creation rejections may be retried
  explicitly; ambiguous creation is never retried automatically. Uncertain add
  operations reconcile only a recognized ordered prefix, preserving user edits.
- Final verification reads owner, private visibility, ordered playlist items and
  a stable snapshot. Completion means verified at that moment; a cached receipt
  is not a fresh check of later user edits. A user can still modify Spotify during
  the narrow interval between a readback and a subsequent write; Spotify offers
  no conditional append primitive used here. Unexpected final contents are reported
  as unverified rather than silently rewritten.
- Read requests have bounded transient retries; 429 returns a retry deadline.
  Mutation requests are never blindly retried. Token refresh is serialized within
  a process; multiple processes sharing a token home may still race on refresh.
  Prefer one running host per token home, and restart hosts after reauthorization.
- Limits and matching favor explicit failure over silently incomplete albums.
  Playback availability may be unknown even when catalog membership is verified.

Automated verification covers both successful full-album creation and protocol,
filesystem, account-binding, expiry, concurrency, crash/restart, rate-limit,
ambiguity, relinking, and partial-write cases. Two stdio clients exercise the actual
MCP protocol with a fake Spotify backend. These are not a live penetration test or
proof of compatibility with every agent host. Per the user's instruction, live
Spotify authorization and playlist creation are deferred (automated tests only).
The dependency versions are unchanged from the earlier audited lockfile; only the
local package version changes to 0.5.0. The prior audit remains point-in-time.
