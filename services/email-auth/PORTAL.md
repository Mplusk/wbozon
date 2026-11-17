# Local integration prototype — NOT deployed

This separate working directory extends the audited stdlib WSGI email-login prototype. No production Site, VPS, DNS, repository or credential has been modified.

## Implemented
- Existing magic-link login, allowlisted owner, one-use hashed tokens, scanner-resistant POST confirmation.
- Authenticated `/settings` UI; values never returned after save. Changes require a session established in the last five minutes, CSRF and exact Origin.
- AES-256-GCM vault, independent random nonce per write, provider name as associated data. Ciphertexts only in the vault SQLite DB. Loss of master key loses access; access to both key and DB permits decryption.
- `/api/read`: server-to-server POST, separate high-entropy bearer token. Only its SHA256 hash is configured at the VPS. Browser session and `oai-authenticated-user-id` do NOT authenticate this API.
- Read-only operations include Ozon products, identifiers, orders and partial accruals; Wildberries products, prices, commissions, tariffs and order analytics. Pagination and filters are validated per operation. Exact static hosts, paths/methods, redirects disabled, TLS verification, a 20-second subprocess deadline including DNS and response reads, bounded response and narrow output field projection. Credentials are sent to the worker through stdin rather than command-line arguments or environment. Standard proxy and Python SSL CA environment settings are inherited; unrelated application environment variables are not. SQLite lock waits for checks/previews are shortened, but the network deadline does not guarantee a total HTTP deadline if process creation, disk I/O or client body reads stall. No generic proxy endpoint, no writes, no credentials returned.
- Credential files loaded by `create_portal`; SMTP password is not put in Docker environment.

## Configuration, not performed
Run `portal:create_portal()` under Gunicorn. Preserve independently checked server deployment configuration. In a container accessible only through its isolated Caddy network, bind an internal container port (not loopback inside the container), with NO published host port.

Environment contains paths only:
- DATABASE_PATH: login SQLite DB.
- VAULT_PATH: encrypted credential SQLite DB.
- SESSION_SECRET_FILE: >=32-character random signing secret.
- VAULT_KEY_FILE: exactly 32 random binary bytes, outside repository and backups unless separately protected.
- READ_TOKEN_HASH_FILE: SHA256 hex of a separately generated random server-to-server token (at least 32 random bytes, token_urlsafe encoded).
- MCP_READ_TOKEN_HASH_FILE (optional): SHA256 hex of a different random MCP bridge token. The existing read token remains valid; the optional hash must differ. Only hashes and secret file paths belong on the portal server.
- SMTP_PASSWORD_FILE: SMTP password for no-reply@3dprinta.shop.
- BASE_URL=https://3dprinta.shop.
- OZON_FINANCE_GATE_DIR (optional): absolute path to one common local finance-gate
  directory for every process/instance of this deployment. By default it is
  `.ozon-finance-gates` beside `VAULT_PATH`. Its parent must already exist and
  be writable by the service UID. Keep the directory and its sibling
  `.initialized`, `.registry.json`, per-client `.epoch-*.json` and `.bootstrap.lock` files on one durable,
  trusted filesystem; back them up as one consistent unit. Paths are configuration,
  not seller credentials. Different gate directories do **not** coordinate.

All secret files must be provisioned outside Git/build context, least-privilege read-only mounts; DB directories private to service UID. Do not log headers, cookies, URL query strings or bodies at proxy/app/APM layers. No secret values in command lines, build args, test fixtures or source.

The portal records only the last completed, authorized and valid `/api/read` operation per credential (MCP-specific or existing bridge), with a timestamp and fixed outcome. Authenticated requests rejected by the rate limiter are also recorded with an unknown operation, without parsing the body. It does not log tokens, request arguments, responses or exception details; browser marketplace checks do not count. Activity writes are best-effort so a temporary storage failure does not prevent a read. An MCP-key request demonstrates use of that credential, not a continuously active MCP connection; no history before this feature was enabled is available.

External MCP clients can use `https://3dprinta.shop/mcp` over Streamable HTTP with a distinct owner-issued Bearer key or via OAuth authorization code with PKCE S256. The owner creates and revokes keys in authenticated settings; plaintext is displayed only once and hashes are stored. OAuth access and refresh tokens are bound to the source key and stop working after revocation. Known client metadata and callback URIs are pinned for ChatGPT, Claude and Grok; dynamic registration supports public clients only, with allowlisted callbacks and no client secret. Clients can obtain the OAuth challenge and metadata at standard discovery endpoints. The endpoint supports 2025/2026 MCP versions and a fixed catalogue of read-only tools; its credentials are not accepted by `/api/read`. Last-use timestamps indicate authenticated requests, not continuous connectivity. In-flight reads may finish after revocation.

## Sites integration
The existing Sites adapter preserves its OAuth/owner-lock and calls fixed `https://3dprinta.shop/api/read` using the narrowly scoped bearer credential stored in Sites secrets. Do not accept the upstream host or credentials as tool parameters. No forwarding Sites OAuth tokens to the VPS. Responses remain untrusted evidence. The `/api/read` route remains a dedicated read API for this trusted bridge; external OAuth uses `/mcp`.

The owner-only Sites connector calls this API with its separately scoped MCP token. Individual operations depend on the seller token rights and provider availability; verify live responses before claiming success.

## Ozon finance rate limiting and recovery

`ozon_accrual_types`, `ozon_posting_accruals`, `ozon_accrual_day`,
`ozon_realization_article_month` and the partial `ozon_article_summary` use a per-Client-ID lock owned
by the actual provider worker, so a parent process dying does not release a lock
still held by an orphaned worker. Different clients can proceed separately.
The gate uses local OS advisory locks, atomic file replacement, a durable client
registry, external identity marker and per-client generation counters. A 429
persists its valid `Retry-After` unless the interval could reproduce an API
key, in which case it stores `retry_unknown` instead;
both delta seconds and HTTP-date are supported. HTTP-date also requires a valid
provider `Date`, and the longer clock interpretation is used. A later request
is refused until the interval has elapsed on both wall and monotonic clocks in
the same boot. No provider call is retried automatically. MCP reports a safe
`retry_after_seconds` in the tool error; `/api/read` may return `Retry-After`.
An interval that could reproduce a seller key is not returned to the caller.
The article summary performs two identity lookups and one posting-accrual read
sequentially within the same overall worker deadline; its user-supplied posting
set is not a complete-period assertion. See `OZON_ARTICLE_SUMMARY.md`.
The daily accrual tool defaults to a compact `summary` by observed SKU, type,
source component and currency; its `coverage_by_sku` records missing delivery
and commission containers. `details` retains per-posting and per-item fields.
The monthly realization tool projects only the chosen offer ID and labels the
report month distinctly from order or accrual dates. Neither tool establishes
complete buyouts or per-unit profit. The upstream response limit is 512,000 bytes;
SKU filtering is applied after reading and does not reduce the provider response.
See `OZON_REAL_DATA_STATUS.md` for the dated real-API checks and limitations.

The guarantee is limited to processes using the **same gate directory**, with
working local filesystem locks, durable atomic writes and the same boot identity.
It does not control other applications, other directories/volumes/hosts or
calls made directly outside this portal. Ozon's limits may be shared with them.
The Unix implementation requires a readable Linux boot ID; Windows uses the OS
boot identifier. Clock or boot identity uncertainty fails closed. Windows ACLs,
Linux/container mounts and actual concurrent production requests have not been
verified. Rewinding only the gate directory or an individual client state is
detected by the external generation counter. A coordinated loss or rollback of
the directory **and its corresponding external state** cannot be distinguished
from a fresh or earlier consistent state without a separate trusted anti-rollback
authority. Do not treat a consistent old snapshot as the latest state.

An unavailable/malformed/oversized `Retry-After`, timeout, cancellation, crash
mid-request, loss of reliable boot identity, or a failed durable state write can
leave that client's financial reads blocked with `retry_time_unknown` or
`storage_unavailable`. This is deliberate: an unseen 429 may have specified an
arbitrary wait. It is not proof that a provider request failed or succeeded.
Do not remove/recreate gate files or restore an older snapshot to bypass it.
Local request/TLS preparation failures before a network call, and invalid or
oversized bodies after receiving a known non-429 HTTP response, release the gate
instead. An oversized raw Ozon response is `provider_response_too_large`:
reduce the batch; `view=summary` alone does not reduce the upstream body. An
oversized projected MCP result is `response_too_large`: `view=summary` can help.

Local recovery is an operator action, not exposed via MCP or HTTP:

1. Stop new financial calls and check no worker still owns the lock. Independently
   establish Ozon's earliest safe retry time for this Client ID, considering all
   integrations; the application cannot infer it from a missing response.
2. Only for an existing `in_flight`, `retry_unknown` or retry state whose boot
   identity changed, run `finance_gate.schedule_verified_recovery(root, client_id,
   wait_seconds)` as the service UID. `root` is the configured absolute gate
   directory; `wait_seconds` is a positive interval at least as long as the
   independently verified remaining wait at execution time. The function does
   not verify the evidence. It refuses to shorten a known same-boot cooldown and
   requires the existing registry, markers and per-client files.
3. Allow the entire scheduled interval to expire before another read. If no
   trustworthy provider reset time or intact current gate state is available,
   the safe result remains blocked; an old backup is not proof of later 429s.

For example, from the installed service directory an operator can run the
following command and supply one JSON object on stdin with `root`, `client_id`
and `wait_seconds`; the last field must already reflect the verified wait:

```text
python -c "import json,sys,finance_gate; p=json.load(sys.stdin); finance_gate.schedule_verified_recovery(p['root'],p['client_id'],p['wait_seconds'])"
```

The command has no access to, and must not be given, the seller API key. It
does not prove that the interval supplied by the operator is correct. A
`storage_unavailable` state, mismatched generations or a missing registry is
not repaired by this command; stop and recover only from trustworthy current
state. The `retry_time_unknown` case has no automatic retry or automatic reset.

Never pass seller API keys in recovery arguments, logs or reports. There is no
automatic recovery from an unknown wait and no production recovery was run.

## Known limits / production gates
- 34 local unit tests passed, including a clean virtual-environment install from requirements.txt. No live seller request, SMTP send, browser QA or deploy occurred.
- No delete/revoke UI or self-service key rotation history; overwrite only. API token rotation needs operator action.
- Login rate-limiter sees REMOTE_ADDR. Configure validated client-address handling only for a trusted proxy; otherwise all browser requests may share Caddy's address. Never trust arbitrary forwarded headers.
- SMTP remains synchronous, full-operation timeout/observability not yet implemented.
- Authentication DB and vault DB migrations/backups/permissions and schema-version rollback need review before production.
- State change requires re-login within five minutes. Settings provides a CSRF-protected logout control, followed by a new magic-link request.
- Service-to-service bearer can read configured seller data and must be treated as a secret with rotation/revocation procedures. Read-only does not make seller data public.
- Do not claim this supplies competitor sales or complete FBO economics.

Test: `python -m unittest -v`.
