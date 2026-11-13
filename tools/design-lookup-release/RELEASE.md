# Reviewed design and exact WB lookup release

Base production2bd638dc682a19e846c2631d9c55395abc7eba38. Merged static SIDZEN interface and exact read-only WB nmID lookup. ui.py is a required new runtime module; include it in every native source copy and manifest. Dockerfile also includes ui.py for packaging consistency; no Docker deployment.

Repeated157unit tests and real Gunicorn synthetic smoke PASS. Independent merged review and final CSS/type=text delta review PASS. Static CSS exact CSP hash8ARn1P9kbrYB4nBqdrYIKJHZXJszMTSaGJRpqOrNO8o=. No unsafe-inline, scripts or external assets. Strict-origin HTML, no-referrer JSON and existing exact Origin/session/CSRF/bearer gates retained. Lookup uses fixed WB endpoint, validated canonical positive nmID, one matching result maximum, shared quota and postcall session/key revalidation. No live marketplace/account compatibility claim.

Visual browser QA NOT completed: cloud Chromium EPERM and Windows Library materialization xattrs blocked the available paths. Synthetic layout/CSP tests are not a substitute for browser rendering. Coordinator authorized manual release with this limitation disclosed. Verify public CSS hash versus inline style and no duplicate conflicting CSP/Referrer-Policy; do not claim visual acceptance.

No schema, dependencies, secret paths, systemd/proxy/network changes. Fresh release with offline venv, source hashes,157tests and synthetic smoke before switch; bounded readiness, public auth/health/CSP probes and neighbours after switch. Code-only rollback to2bd, no DB restore. No real email, API keys, seller requests or production session injection. GitHub Actions not invoked/configured; commit uses[skip ci].
