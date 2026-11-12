# Deployment status and proposed CD

The canonical service source is this directory. It is the standard-library WSGI
prototype, not a separate Flask experiment on the VPS. CI runs its 31 application/deployment regression
tests and builds/smoke-tests its container. CI never connects to the VPS, sends
email, or reads production credentials. Existing repository CI is unchanged.

CD is NOT enabled. This service does not replace Sites ChatGPT OAuth, implement
marketplace routes, or store marketplace API credentials.

## Proposed production pipeline

1. Review and merge the service only after CI and independent review.
2. Build an immutable image in CI for the selected commit, not on the 2-GB VPS.
   Pin the base image digest before production. Publish to a private registry.
3. Use a dedicated read-only registry identity on the VPS. Do not put root SSH,
   SMTP, or marketplace passwords in GitHub Actions. Do not install a privileged
   self-hosted runner on the shared VPS.
4. A separately reviewed, locally trusted deploy helper may activate only this
   service's image digest, under a lock and with a last-good digest retained.
   It must never execute arbitrary shell commands from a workflow or restart
   another Compose project. Registry authentication and this limited privilege
   need explicit setup; no credential or permission has been created here.
5. Before activation: check free disk/memory, record neighboring service health,
   start an isolated candidate with synthetic data, verify HTTP readiness, and
   reject it on failure. Production database/SMTP secrets must not enter the
   CI candidate. Back up the service's SQLite database consistently before
   migrations; never assume a code rollback reverses a schema migration.
6. Activate only this service, verify health, and restore the previous image if
   unhealthy. Keep exact commit/digest and redacted deployment result. Do not
   run global prune, compose down, or restart the shared proxy.
7. Public HTTPS routing must be a separate reviewed change: the existing Caddy
   is in a bridge container, so its localhost is not the host localhost. Choose
   a narrowly scoped container network; preserve other virtual hosts. Never
   expose the backend directly to the Internet. Ensure auth query tokens and
   cookies are absent from every proxy, access, error and tracing log.

## Remaining gates

- Confirm protected main/review rules and the private image registry.
- Provision a least-privilege registry/deploy credential through approved storage.
- Review service identity, resource limits, persistent data permissions and ingress.
- Configure SMTP through protected runtime configuration, rotate previously
  exposed credentials, and test one authorized real email.
- Complete live browser, HTTPS, restart, rollback and neighboring-project checks.

The existing repository deploy/deploy.py is only a file-release template and
does not supply container health checks, activation or rollback. Do not point
the production service at it until those controls are implemented and tested.

## Implemented opt-in CD controls (not installed or activated)

`deploy/release.py` now provides `check`, `deploy`, `rollback`, and `recover`.
No invocation is automatic. All mutating commands require `--execute`.

- Only an approved GHCR repository plus `sha256:` digest is accepted, never a tag.
- Fixed container name `3dprinta-auth`; refusal to manipulate an existing container
  unless its service label matches. No global Docker/Compose commands or proxy edits.
- Dedicated network must already exist. The deployer does not create or attach
  networks to Caddy. Backend publication is only `127.0.0.1:<configured-port>`.
- Synthetic candidate has no network, runtime secrets or production database.
- Bounded resources, read-only container root, no capabilities or host Docker socket.
- Journal persisted before activation; concurrent activation is locked. A failed
  runtime is retained stopped, previous runtime restored. After an interruption,
  `recover` is required before another release. Uncertain Docker outcomes are not
  blindly retried. The control state and images must be inspected on failure.
- No automatic image/container deletion or database migrations. Old stopped
  containers may retain environment secrets; their inventory is privileged.
- Config/state/runtime env must be private and owned by the deploy identity.
  The separate database directory must be private and owned by UID 10001 (the
  image's non-root user). Provisioning these paths/permissions is deliberately
  not implemented by the deployer and must be reviewed before installation.
- The deployer can call Docker, so its identity is effectively privileged. Do not
  give Docker membership to arbitrary agents. Install this reviewed control file
  outside the release directory; changes to it require a separate review.

Examples below are documentation, not an instruction to run against an
unprepared server:

```
python3 release.py --config /etc/3dprinta-auth/deploy.json check
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute deploy --digest sha256:REVIEWED_DIGEST
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute rollback
python3 release.py --config /etc/3dprinta-auth/deploy.json --execute recover
```

The manually dispatched `email-auth-release.yml` runs only on `main`, tests the
exact commit and publishes a commit-tagged GHCR image with the repository's
short-lived GITHUB_TOKEN. It does not connect to the VPS. Confirm package privacy,
permissions and immutable digest before use. It has NOT been dispatched here.
There is no automatic deploy workflow, registry credential, server installation
or public route in this change. The release-image workflow requires the owner to
merge reviewed code first; a draft PR is not a production release.

Tests cover deployment state, idempotency, candidate/activation failure, rollback,
interrupted recovery, concurrency, foreign-container refusal, paths and secret-free
candidate arguments. These mocks are not a substitute for real isolated VPS tests.
