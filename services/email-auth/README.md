# SIDZEN isolated email-login prototype

## State
Local prototype only. No email was sent, no real secret was used, no server was changed.
This is NOT the existing Sites MCP and does not grant access to that MCP. A successful
email session only opens an explicit prototype page. Marketplace credential storage,
MCP OAuth, and marketplace routes are NOT implemented here.

## Implemented
- Only `maksim@kuchermg.ru` can receive a login link.
- Sender `no-reply@3dprinta.shop`, verified-TLS SMTP to `smtp.spaceweb.ru:465`.
- 256-bit random login token, SHA-256 hash stored, 15-minute TTL.
- GET from an email scanner does not consume a token or authenticate.
- Explicit same-origin POST confirmation with signed double-submit CSRF cookie.
- Atomic one-time token consumption and server-side session creation in SQLite.
- Random session identifier stored only as a hash, 12-hour TTL, Secure/HttpOnly/
  SameSite=Strict __Host cookie. POST logout with session CSRF protection.
- Durable limits: 5 request attempts per source IP per 15 minutes, 5 actual sends
  per hour for the sole recipient. Behind a proxy all users may intentionally share
  one limit. Forwarded IP headers are NOT trusted.
- No-referrer, no-store, restrictive CSP, Host/Origin checks, bounded input.
- Generic response for unknown recipient and mail failure. Mail failure invalidates
  the created token and does not log SMTP exceptions.

## Tests (no dependencies, no network)
`python -m unittest -v`

## Configuration (never paste values into chat)
Required runtime variables:
- DATABASE_PATH: dedicated writable SQLite file outside source/public directories.
- SESSION_SECRET: independently generated cryptographic secret, >=32 characters.
- SMTP_USER: no-reply@3dprinta.shop
- SMTP_PASSWORD: new password entered via approved protected secret mechanism.
- BASE_URL: https://3dprinta.shop
- PORT: confirmed free loopback-only port, example 18081 is NOT reserved.

`gunicorn --config gunicorn.conf.py 'app:create_app()'`

Do not run Python's development server on a public interface. Do not run as root.
The database has security-sensitive token/session state and must be owned only by
the dedicated service identity. Secrets must not be in Git, command arguments,
access logs, shell history, screenshots or exported artifacts.

## Shared VPS deployment checklist — NOT executed
1. Establish trusted SSH access. Check host identity; never disable host validation.
2. Read-only inventory first: OS/resources, listening ports, running containers,
   service names, nginx/Caddy/Traefik configuration and configured domains. Do not
   read unrelated app environment files or secret values. Do not stop existing apps.
3. Verify public DNS A/AAAA and any api subdomain; user-provided DNS changes have
   not yet been verified. Determine whether 3dprinta.shop is already serving a project.
4. Choose a new directory and dedicated service identity, a free loopback port,
   and separate writable data path. Do not overwrite another project or repurpose
   its account, database, ports, virtualenv, container network or TLS certificate.
5. Independently review this prototype before any real credentials or deployment.
   Install pinned dependency into an isolated environment after registry/source review.
6. Prepare exact changes for approval where required: service identity/permissions,
   reverse proxy route, TLS issuance, security/network settings. No firewall, DNS,
   SSH or proxy changes have been authorized by this artifact. Do not run a
   global upgrade, stack restart, `docker compose down`, or generic nginx replacement.
7. Use the existing reverse proxy (after confirming its owner/configuration) for
   HTTPS on the explicit domain only. Keep the new backend bound to loopback.
   Preserve all other virtual hosts. Disable query-string access logging for auth
   routes and ensure error/observability systems do not retain tokens or cookies.
8. Check existing configuration before and after the isolated change; back up only
   the files being changed with protected access, syntax-check, then controlled
   reload rather than restart. Verify all pre-existing project health checks.
9. Test with synthetic mail first, then one explicitly authorised real email.
   Verify SMTP TLS, SPF/DKIM/DMARC and delivery. A successful SMTP response is not
   proof of inbox delivery. Never reveal the login token in a report.
10. Verify expiry, one-time use, scanner GET, CSRF, forged Host, rate limits,
    restart persistence and concurrent use on the deployed service.
11. Rollback: remove only this newly created service/route after controlled validation;
    retain the original applications unchanged. Do not delete persistent data blindly.

## Known limits before production
- SMTP delivery is synchronous; authenticated send failures are deliberately generic.
  A protected, redacted operational health/metrics mechanism is still needed.
- IP rate limiting is deliberately conservative behind a proxy; deployments with
  trusted ingress may need a carefully reviewed IP extraction strategy.
- No reauthentication/revocation dashboard, idle timeout, or multi-user support.
- SQLite and WSGI deployment have only local tests, not a live shared-VPS test.
- Browser UI and email clients have not been independently tested.
- This does not solve current cloud-to-VPS networking: SSH remained unreachable.

## Mandatory ingress/logging and timeout review
- Standard nginx `$request` and `$request_uri` contain the full query string and
  therefore the login token. Do not use them in access logs for `/auth/link`.
  Use an explicitly reviewed path-only format or disable that location's access log.
- Review proxy error logs, tracing, WAF/CDN, analytics, monitoring and crash reporting
  separately: disabling application logs does NOT stop an upstream from recording URLs.
  Do not record POST bodies, Authorization/Cookie/Set-Cookie headers, SMTP errors,
  mail body/link, or environment values. A reviewer must verify this on the actual VPS.
- SMTP's 15-second socket timeout applies to individual blocking operations, not a
  guaranteed whole-request deadline. DNS, TLS, login and send may cumulatively exceed
  the sample Gunicorn 30-second worker timeout. A worker killed during sending could
  leave delivery uncertain; automatic retries must not send duplicate login emails.
- Before production, select and test compatible ingress/worker/send deadlines or use
  a separately reviewed bounded mail queue with explicit delivery state. Do not simply
  raise all shared-server timeouts. Test SMTP stalls and client disconnects with fake
  transports before any real account, preserving the other projects' settings.
