# Sequential manual release: clearer connection settings, retaining healthz

Only after health-only release b06b7819b5cd21a3e4f0547be44487972441a6e5 is confirmed
healthy and its deployment journal settled. No parallel production switch.

Runtime changes relative to that release: rendering of authenticated GET/settings
only. Distinguishes encrypted-key storage from unverified marketplace connectivity;
MCP connectivity explicitly not asserted; Russian field labels and permission hints;
explains existing five-minute recent-login window and logout/re-authentication.
Healthz remains byte-for-byte logic-equivalent. No JS/external assets, CSP weakening,
new network calls, auth/CSRF/secret/schema/requirements/unit changes.

69 unit tests pass (59 original+5 health+5 UX). Real Gunicorn synthetic smoke passes
for health GET/HEAD/405, rendered labels/statuses and original session/CSRF/encrypted
storage/independent API authorization. No real credentials or seller API requests.
Independent combined review passed69/69 tests and real Gunicorn smoke, no blockers. No production browser screenshot/visual QA
claimed; semantic HTML and HTTP rendering are tested.

Use the same manual separate-release/new-final-path-offline-venv workflow as health
release. Retain exact previously healthy current target from the completed health
rollout in a root-owned durable record. Validate artifact/source hashes and69tests,
run synthetic smoke on unused loopback port under resource bounds before switch.
Do not change credential mounts/read or transmit secrets. Do not package preview.html.

Switch only portal service during short approved downtime; no Docker/Caddy/unit/
network/neighbour operations. Confirm healthz/root200 and protected settings/API401
unauthenticated. Authenticated page verification should use the existing authorized
user session only; do not create a production session by editing DB. If no session
is available, rely on pre-release synthetic HTTP tests and state that limitation.

Rollback: reconcile uncertain jobs first; stop portal only, restore recorded previous
health-only target, start and verify healthz/auth guards. No DB/credential restore or
schema rollback required because no schema/storage changes. Preserve release and
journal evidence. No automatic deployment or GitHub Actions dependency.
