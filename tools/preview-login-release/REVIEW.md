# WB currency code in read-only preview — unpublished candidate

Base: portal-login-recovery (including health, settings UX, connection checks, data preview, login recovery). Do not replace newer production files without merging the small portal.py delta and rerunning validation.

## Scope
- Preserve optional `currencyIsoCode4217` in normalized WB response.
- Display the provider's code for each product, rather than implying all values are rubles.
- Accept only three ASCII uppercase letters for display. This is syntax validation, NOT a local ISO registry check or proof of the provider's truthfulness.
- No guessed currency, coercion, case normalization, names/symbol mapping, conversion or totals. Missing/malformed code is `не указано`.
- Preserve existing exact credential redaction and HTML escaping. A code colliding with credential text remains redacted.
- Existing provider URL/method/request count, paging limits, session/CSRF/bearer boundary, quotas, DB, logs, dependencies and secrets are unchanged.
- Normalized WB API output gets one optional additive field; Ozon output does not change.

## Evidence
Official regional WB documentation: https://dev.wildberries.kg/docs/openapi/item-management documents currencyIsoCode4217 on GET /api/v2/list/goods/filter item. Russian documentation requests returned498 during this review, so live Russian API compatibility is not claimed. No real account/credential/network call was used in tests.

## Validation
118 unit tests PASS, including six new tests: currency preservation on the fixed mocked WB transport; per-product codes without price conversion; absent/malformed values; secret collisions; no invented Ozon currency; authenticated preview integration.
Existing real Gunicorn synthetic smoke PASS using /tmp/portal-offline-venv/bin/python test_native.py. This tests runtime/auth/health/login compatibility, not a live WB response.

## Release constraints
Prepared only; not committed, pushed or deployed. Independent review requested. Code-only rollback, no data migration. Merge after pending preview/login release, preserve its exact changes and use the existing isolated release/rollback workflow.
