# Manual release: non-sensitive liveness endpoint

## What changes

Add GET/HEAD `/healthz` returning minimal `{"status":"ok"}` for validated Host.
Other methods405 with Allow; no-store and nosniff. The endpoint intentionally does
not read sessions, databases, saved keys or credentials, and makes no SMTP/seller
requests. It is process liveness only, NOT proof of database/integration health.
Existing login, settings and bearer API authorization are unchanged. No DB schema,
requirements, network, proxy, unit or credential changes are included.

## Evidence

64 portal tests PASS:59 existing plus5 liveness tests. Real native Gunicorn synthetic
smoke PASS: health GET/HEAD/405 plus existing session/CSRF/encrypted-vault/API guards.
These use temporary synthetic files/DB, no production credentials. Source baseline:
14b958081157b1c4b5f15ac67cb1f2cb45477a8f (already deployed and hash-verified).
Only portal.py runtime content changes; test_health.py is added; test_native.py is
release acceptance tooling. Review SHA256SUMS and independently approved patch hash.

## Manual-only rollout contract (operator must verify before mutation)

1. Compare current target/code and approved config hashes to inventory. Ensure no
   pending jobs or concurrent deployment. Preserve exact old current target in a
   root-owned durable record. Existing known-good target:
   `/opt/3dprinta-portal/releases/14b958081157b1c4b5f15ac67cb1f2cb45477a8f`.
2. Create a NEW unique release directory; copy only approved baseline source and
   apply verified portal.py/test_health.py. Never write the running release. Build
   a new isolated venv at its final path with existing reviewed offline wheels and
   requirements hashes; no global pip, registry pulls, Docker operations or packages.
3. Test64 suite and supplied smoke under bounded CPU/memory/time, temporary synthetic
   DB/credentials, unused local ephemeral port. These synthetic files must not be
   replaced by production credentials. Verify source hashes again after transfer.
4. Confirm same schema/dependencies and local consistent backup policy before short
   authorized portal-only downtime. Existing credential mounts remain unchanged;
   never read/output/transfer actual secrets or bypass any declined secret handoff.
5. Journal old/new target+config hash; stop ONLY3dprinta-portal.service; confirm inactive
   and no unsettled jobs; atomic current-link switch; start ONLY that service. No
   unit edits, daemon-reload, enable, Docker/Caddy/network or neighbour restart.
6. Check loopback `/healthz` Host3dprinta.shop GET200 exact JSON, HEAD200 empty body,
   POST405; `/settings`401 and unauthenticated `/api/read`401. Check public HTTPS
   health/TLS and neighbour-domain status without authentication/external API calls.

## Rollback

On failed verification, preserve evidence. If command outcome uncertain, inspect
jobs/processes first, do not blindly retry. Once settled, stop ONLY portal, atomically
restore recorded old target, start and verify existing homepage/auth guards (old
release has no `/healthz`; do not use it as rollback criterion). No DB or credential
rollback needed: schema and storage format unchanged. Keep both releases and journal;
no deletion/prune. Failure to restore is a user-visible blocker, not silent success.

## Not done by this preparation

No production mutation, Git push, CI dispatch, runner setup, secret transmission or
integration configuration was performed. Independent review passed64/64 tests and real Gunicorn smoke; no runtime blockers.
