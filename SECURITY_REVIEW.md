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
