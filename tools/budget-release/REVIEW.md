# Budget hints integrated onto production origin hotfix

Base production: 826bf049198ea74f791381c3e7264b4f4a4fb0e4. Local candidate only; no commit, push, Actions or VPS changes.

Copied origin-fix source and applied only reviewed request-budget delta from its old 3bf baseline with patch --fuzz=0. app.py and origin/recovery tests remain exact hotfix bytes. Changes vs production: portal.py request-budget hints, new test_request_budget.py, expanded synthetic test_native.py. HTML strict-origin and JSON no-referrer retained; no Origin/CSRF weakening.

134 unit tests and real Gunicorn synthetic smoke PASS. Budget reads existing attempts with bounded queries; no credential reads, provider calls, writes or quota reservations for settings GET. No schema/dependency/config changes; code-only rollback to826bf. No real mail or seller calls. Independent settings_ux merge review repeated: no blockers; exact portal/app hashes and 134 tests plus native smoke PASS.

Publication deferred to next authorized grouped window; no release claimed. Future rollout must verify public headers, auth/health and neighbour services; browser-real acceptance is separate from synthetic tests.
