# Origin/referrer-policy fix — unpublished candidate

Base: manually released3bf8fd906fec783f03b2f9e126cea2c6906d2516 (/workspace/shared/portal-wb-currency). Budget hints are deliberately NOT included.

## Diagnosis
Screenshots show plain Forbidden after the login form; live root GET has Referrer-Policy:no-referrer. app.py strictly requires HTTP_ORIGIN == configured HTTPS origin on all POST requests. Per WHATWG Fetch3.2, for non-CORS non-GET/HEAD requests the no-referrer policy serializes Origin as null. This explains normal HTML form navigation being rejected before the email code executes.
Primary source: https://fetch.spec.whatwg.org/#append-a-request-origin-header
Referrer policy source: https://w3c.github.io/webappsec-referrer-policy/#referrer-policy-strict-origin

## Change
Use strict-origin on HTML documents in both LoginApp and Portal. Keep no-referrer on JSON API responses. No change to the exact Origin allowlist, CSRF validation, token/session consumption, cookies, email recipients/quotas or authorization.

strict-origin retains the real origin for HTTPS form POST and sends only the origin in Referer, never the path/query containing a magic-link token. HTTPS-to-HTTP downgrade suppresses referrer. Do NOT use same-origin or strict-origin-when-cross-origin here: their same-origin Referer can contain the full token URL. Do NOT allow Origin:null or treat missing Origin as trustworthy.

## Validation
124 unit tests PASS, including six new tests for form-document headers, API no-referrer, legitimate synthetic form request, continued null/missing/foreign rejection and null-origin rejection for confirm/save/check/preview/logout. Existing recovery test updated to new HTML header.
Existing real Gunicorn synthetic smoke PASS. No real email was sent.
Browser-specific header-generation test is prepared as test_browser_origin.cjs. It uses reserved .invalid origins and intercepts every request; it makes no production/API/mail calls. It asserts no-referrer -> Origin:null and strict-origin -> actual Origin with origin-only Referer for a page containing a synthetic query token. It has NOT been run by this agent because another agent reported Chromium launch EPERM in this environment; screenshots has been asked to run it in an available permitted browser.

## Deployment checks for owner/coordinator
Independent review requested; no publication/commit/push/Actions/server changes by this agent.
After an authorized deploy, verify public response headers are strict-origin on / and authenticated /settings (proxy must not override or append no-referrer). Existing open documents retain the old policy until reloaded; tell the user to reopen/reload the login form before resubmitting. Confirm with synthetic/browser regression before relying on email flow. API guards must stay401 without bearer; forged/null Origins must remain403.
Rollback code-only; no DB/dependency/configuration migration.

Independent settings_ux review PASS:124 unit tests and native Gunicorn smoke repeated; no blockers, runtime diff confirmed header-only, exact Origin/CSRF/session gates unchanged. Browser-generated header regression remains required separately.
