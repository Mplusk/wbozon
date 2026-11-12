# Portal + opt-in single-service CD

Local integration based on PR3 CD head `896791b86c6a9d980fc0d87586b5d28ac79f0af5`.
Not deployed; no production activation or Sites integration is implied.

## Runtime contract (replaces previous env-file instructions)

The Docker default runs `portal:create_portal()`. No secret values in env, image,
repository or CI. The separately installed deploy controller takes `secret_dir`
instead of `env_file`. Upgrade the controller/config together only after review.

Operator provisions a dedicated data directory (UID10001, mode0700), separate
from state/config/secrets. `auth.sqlite` and `vault.sqlite` share that data mount.
The dedicated secrets directory must be UID10001 mode0700; its four regular files
UID10001 mode0600: `session` (random text >=32 chars), `vault-key` (32 random binary
bytes), `read-token-hash` (SHA256 hex of a random >=32-byte bridge bearer),
`smtp-password` (SpaceWeb password). Parent directories must be trusted operator
owned and not group/world writable. No symlinks. The directory is mounted readonly
at `/run/secrets/portal`; environment contains only fixed paths and public config.
Do not broadly chown/chmod existing shared directories or reuse another project's
UID or secrets directory. The plaintext bridge bearer is provisioned separately in
the authorized caller's secure secret store, not stored on this server or in Git.

Protected `config.example.json` is the schema example. No env-file is consumed.
Secrets must not be nested inside writable application data. The deploy helper
checks paths/ownership, but does not provision accounts, permissions or Docker
networks. UID10001 uniqueness on the shared host requires operator verification.

## Checks and release

CI installs pinned dependencies, runs authentication/vault/API/deployer tests,
builds the portal image, and starts a `--network none` candidate. `candidate.py`
creates disposable random secrets inside `/data` tmpfs only and starts the SAME
production portal factory. Production never invokes the candidate entrypoint.
Readiness requires `/`200, `/settings`401 and `/api/read`401; no mail/provider
requests. Offline tests exercise authenticated settings, CSRF, encrypted storage,
independent bearer authorization and fixed read-only provider dispatch.

The manual main-only release workflow builds/tests before publishing an image.
It does not deploy or contact the VPS. The separately reviewed controller uses
immutable image digest, isolated candidate, loopback bind, service ownership
labels, serialized durable transition journal, and explicit recovery for uncertain
Docker outcomes. Existing adjacent applications must not be changed.

## Mandatory rollout gates

Docker build and runtime smoke must pass in CI (Docker is unavailable in the local
review environment). Test synthetic login/settings flow in a staging browser.
Confirm trusted proxy IP handling and ensure reverse proxy/APM never logs query
strings, cookies, Authorization or request bodies. Review encrypted backup and key
recovery. Code rollback is NOT a DB schema/key rollback. Retain matching encrypted
DB/key backups. Check SMTP delivery without logging links. Protect and rotate the
independent bridge bearer; no header from Sites is treated as user identity.

See PORTAL.md for restricted API scope (two first-page reads only), outstanding
rotation/revocation UI and other limitations. Do not claim this is a complete MCP
OAuth server. The Sites adapter is a separate change requiring verified platform
configuration and retaining Sites OAuth/owner checks.

## Controller ownership and recovery

Docker authority is effectively privileged. Do not grant Docker membership to
arbitrary agents. Install reviewed `release.py` outside the release image and
review changes independently. `check` performs no Docker/network operation.
Examples only, not commands to run on an unprepared host:

```
python3 release.py --config /etc/3dprinta-auth/deploy.json check
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute deploy --digest sha256:REVIEWED_DIGEST
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute rollback
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute recover
```

Confirm package privacy and immutable digest before activation. CI publishing is
not deployment. No registry credential, daemon, timer, network provisioning or
public route is included here. Mock tests do not replace isolated runtime tests.

A Docker timeout is NOT a confirmed failure. The private `uncertain.json` marker
blocks subsequent mutation. Do not retry/stop/rename/remove while the original
command could still be running. Operator must independently establish its outcome,
inspect only this service's owned containers, reconcile journal, then explicitly
clear the marker and request recovery. No automatic marker-clear option exists.
Candidate intent is fsynced before run; SIGKILL leaves blocking evidence. Markers
are written while the same deployment lock is held. Do not delete evidence merely
to get the next deployment through. Retained failed/previous containers require
separate deliberate cleanup, not broad Docker prune.
