# Local integration prototype — NOT deployed

This separate working directory extends the audited stdlib WSGI email-login prototype. No production Site, VPS, DNS, repository or credential has been modified.

## Implemented
- Existing magic-link login, allowlisted owner, one-use hashed tokens, scanner-resistant POST confirmation.
- Authenticated `/settings` UI; values never returned after save. Changes require a session established in the last five minutes, CSRF and exact Origin.
- AES-256-GCM vault, independent random nonce per write, provider name as associated data. Ciphertexts only in the vault SQLite DB. Loss of master key loses access; access to both key and DB permits decryption.
- `/api/read`: server-to-server POST, separate high-entropy bearer token. Only its SHA256 hash is configured at the VPS. Browser session and `oai-authenticated-user-id` do NOT authenticate this API.
- Read-only operations: Ozon products and prices; Wildberries products, prices, commissions and box tariffs. Pagination and filters are validated per operation. Exact static hosts, paths/methods, redirects disabled, TLS verification, a 20-second subprocess deadline including DNS and response reads, bounded response and narrow output field projection. Credentials are sent to the worker through stdin rather than command-line arguments or environment. Standard proxy and Python SSL CA environment settings are inherited; unrelated application environment variables are not. SQLite lock waits for checks/previews are shortened, but the network deadline does not guarantee a total HTTP deadline if process creation, disk I/O or client body reads stall. No generic proxy endpoint, no writes, no credentials returned.
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

All secret files must be provisioned outside Git/build context, least-privilege read-only mounts; DB directories private to service UID. Do not log headers, cookies, URL query strings or bodies at proxy/app/APM layers. No secret values in command lines, build args, test fixtures or source.

The portal records only the last completed, authorized and valid `/api/read` operation per credential (MCP-specific or existing bridge), with a timestamp and fixed outcome. Authenticated requests rejected by the rate limiter are also recorded with an unknown operation, without parsing the body. It does not log tokens, request arguments, responses or exception details; browser marketplace checks do not count. Activity writes are best-effort so a temporary storage failure does not prevent a read. An MCP-key request demonstrates use of that credential, not a continuously active MCP connection; no history before this feature was enabled is available.

External MCP clients can use `https://3dprinta.shop/mcp` over Streamable HTTP with a distinct owner-issued Bearer key or via OAuth authorization code with PKCE S256. The owner creates and revokes keys in authenticated settings; plaintext is displayed only once and hashes are stored. OAuth access and refresh tokens are bound to the source key and stop working after revocation. Known client metadata and callback URIs are pinned for ChatGPT, Claude and Grok; dynamic registration supports public clients only, with allowlisted callbacks and no client secret. Clients can obtain the OAuth challenge and metadata at standard discovery endpoints. The endpoint supports 2025/2026 MCP versions and six read-only tools; its credentials are not accepted by `/api/read`. Last-use timestamps indicate authenticated requests, not continuous connectivity. In-flight reads may finish after revocation.

## Sites integration
The existing Sites adapter preserves its OAuth/owner-lock and calls fixed `https://3dprinta.shop/api/read` using the narrowly scoped bearer credential stored in Sites secrets. Do not accept the upstream host or credentials as tool parameters. No forwarding Sites OAuth tokens to the VPS. Responses remain untrusted evidence. The `/api/read` route remains a dedicated read API for this trusted bridge; external OAuth uses `/mcp`.

The owner-only Sites connector calls this API with its separately scoped MCP token. Individual operations depend on the seller token rights and provider availability; verify live responses before claiming success.

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
