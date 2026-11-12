# Candidate: explicit marketplace connection checks (not deployed)

Base: reviewed health+settings UX release 62f825 (local portal-health-ux-release).
Changes: service/portal.py, new service/test_connection_check.py, expanded test_native.py.

Endpoint POST /settings/check accepts exactly csrf+provider. Same existing browser session, expiry, Host, Origin and constant-time CSRF validation; no bearer/identity-header fallback. Cookies still never authorize /api/read. Session older than five minutes can check an already saved key but cannot replace it, preserving existing save reauthentication.

Fixed provider-read operations only: ozon_products or wb_prices, limit=1. Existing fixed HTTPS endpoints, no redirects, bounded response, timeout and response filtering retained. Client cannot supply operation/url/limit or credentials. POST check returns no marketplace payload. Outcomes use fixed local strings, never exception bodies, provider error details or secrets. Success only means that exact read operation worked at the displayed UTC time, not that all rights or MCP are configured. MCP status unchanged.

Persistent owner-wide rate limiter via existing auth attempts table: one attempt/provider/minute, ten total checks/hour, shared across sessions/workers. GET settings sends no seller requests and does not decrypt keys.

Storage: additive connection_checks table in existing encrypted-vault SQLite file (table itself stores only provider, ciphertext SHA256 version, categorical outcome, timestamp). Save clears outcome atomically. Record check compares ciphertext version within a write transaction; replacing key while request is pending yields409 and cannot confirm the new key. Old application versions ignore the extra table; ciphertext version comparison also invalidates outcome if a rollback-version writer changes a key without clearing checks. No existing table layout, credential files, auth schema, unit, dependencies or network settings changed. Backup existing SQLite per normal safe release before startup creates table. Rollback code can retain additive table, no destructive migration.

Validation: 90 unit tests (69 baseline+21 new) PASS; real local Gunicorn synthetic smoke PASS, including /settings/check method/session/origin guards, missing key and persistent cooldown. All credentials synthetic, no live email or marketplace requests. Official API availability/real permissions require user's actual keys and explicit button use; offline tests cannot establish live account connectivity. Browser visual QA not claimed.

No publication/deployment in this task. Integrate only after independent review, check final unit/service hashes and preserve immutable previous release for rollback. Do not change shared Caddy/Docker, credential paths or other projects. No real seller credentials in artifacts.

Independent security review completed: no blockers; independently90 unit tests and Gunicorn smoke PASS. Auth separation, request protections, fixed URL/operation, sanitized outcomes, version-CAS replacement races and rollback invalidation reviewed. Additive table deployment caveat retained above.
