# Deployment status and proposed CD

The canonical service source is this directory. It is the standard-library WSGI
prototype, not a separate Flask experiment on the VPS. CI runs its 17 regression
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
