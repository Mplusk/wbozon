# Native portal fallback: preparation only, no VPS changes

Based on reviewed portal integration GitHub commit
`14b958081157b1c4b5f15ac67cb1f2cb45477a8f`.
The existing Docker CD helper is NOT used by this alternative.

## Recommendation

Use system Python 3.12 to create a dedicated **isolated venv**, install prebuilt
PyPI wheels offline with hashes, then run the same Gunicorn/portal application
under a separately reviewed systemd service. No Docker daemon, image build/import,
compiler, distro Python replacement or global pip install is needed. Wheelhouse is
about 5.1 MB. No automatic service installer/activator is included.

The prepared wheelhouse targets CPython3.12, x86_64, glibc>=2.34. Confirm those
facts on the actual VPS, plus Python venv/ensurepip and systemd>=250. If absent or
different, STOP and prepare the correct platform bundle. Do not fall back to a
source build or silently install system packages. Existing VPS architecture,
glibc, venv availability and systemd version have not been verified by this task.

All direct and transitive wheels are pinned and SHA256 locked. Downloaded from
standard PyPI in the preparation environment. SHA256 protects transfer integrity;
it does not replace trusting the source/reviewed manifest. Never use system-site-
packages or inherit the VPS cryptography package into this runtime.

## Tested locally (Python3.12, synthetic data only)

- An isolated venv with cryptography41.0.7 and gunicorn23.0.0 passes all59 tests and
  a real Gunicorn HTTP smoke including session settings, CSRF, encrypted storage
  and separate bearer authentication. Thus installed41.0.7 has no demonstrated API
  incompatibility with the portal, but this is NOT a security endorsement or a
  reason to downgrade the reviewed dependency.
- Recommended offline wheelhouse installation with `--no-index --require-hashes`
  succeeds; all59 tests and the same native HTTP smoke pass with crypto50.0.0.
- No SMTP or seller API call is made. All secrets/session fixtures temporary.
- Real systemd sandbox/credential activation is NOT tested here. The local
  environment prevents systemd-analyze verification before it loads the unit.

## Isolated preparation commands (not production activation)

Run only in a NEW owned staging directory, never over another project's venv:

```
python3 -m venv venv
venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements-offline.lock
(cd service && ../venv/bin/python -m unittest -v)
venv/bin/python test_native.py
```

Use immutable root-owned release directories on the host and create the venv at
its final path: virtualenv console-script shebangs embed absolute paths, so do not
copy/relocate an already-created venv from this computer. Verify the file manifest
before installation. Keep a previous immutable release for an explicit rollback.
Switching a service's `current` symlink needs controlled stop/start and a journaled
release procedure, not a change while workers are running. The Docker CD rollback
logic is not an automated native deployer; do not claim equivalent rollback tests.

## Native service draft and approval gates

`3dprinta-portal.service.example` uses DynamicUser (no collision with fixedUID10001),
private systemd StateDirectory, readonly LoadCredential files, strict filesystem,
capability/namespace restrictions, private tmp/devices, process/memory/CPU quotas,
and only127.0.0.1:18731. That port/path are proposals, NOT verified reservations.
Systemd exposes credentials at its private `%d` directory; env contains file paths,
never values. Four source files under a NEW dedicated root-only directory:
`session` random text>=32 chars; `vault-key`32 random bytes; `read-token-hash` SHA256
hex of a separately protected random bearer; `smtp-password` SpaceWeb credential.
No actual secrets supplied/generated for production. Do not print credential files.

Before any activation: inventory existing apps/listeners/resources; confirm new
service name,paths,port do not collide; obtain approval for service/security-setting
changes; independently verify unit syntax, actual credential delivery/permissions,
no logs of auth query/body/cookie/Authorization, and restart/rollback behavior on an
isolated test instance. Ingress remains unchanged until separately approved. Keep
existing shared-server projects untouched. Never reuse Docker UID/data mounts for
DynamicUser without a separately approved ownership/migration plan.

StateDirectory holds BOTH auth.sqlite and encrypted vault.sqlite. Back up vault
with matching key; code rollback is not schema/key rollback. DynamicUser state can
be stored under systemd's private directory; backup tooling must account for that.
Protect source credential directory backups as well. A native launch is not a
Sites bridge: OAuth/owner authentication on Sites remains separate and unchanged.

Production ingress trusted-client-IP handling, TLS/SMTP test, bounded mail timeout,
key rotation/revocation UX, backup/restore and bridge configuration remain rollout
gates. No claims of production-ready or automatic deployment are made.
