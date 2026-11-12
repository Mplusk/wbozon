# Manual native release controller (review-only, not installed)

Only `3dprinta-portal.service`, `/opt/3dprinta-portal/current` and its releases,
and private `/var/lib/3dprinta-native-deployer` are in scope. No deployment workflow,
service installation/enablement, daemon reload, firewall, proxy, other unit,
credentials, database, migration or backup code is included. There is no scheduler.

## Before production use

This is a **separate manual controller**, not activation authorization. Host adoption
is blocked until both existing and candidate releases satisfy the contract below.
The deployed PR3 release is NOT assumed to already have a complete sealed manifest.
Its current venv may contain symlinks, which this strict first version refuses.
Do not run it against the current production layout until separately reviewing a
migration to compatible immutable releases. Do not mutate a live release to pass checks.

1. Verify Python3.12 and trusted package hashes. Prepare candidate under its **final**
   root-owned `/opt/3dprinta-portal/releases/<release-name>` path with an isolated venv,
   no system site packages, only approved hash-locked offline wheels. Venv executable
   shebangs depend on this path. Run portal regression and synthetic native smoke
   before sealing. Runtime must not write pycache, secrets, logs or DB inside release.
2. Release tree and all parent paths must be root-owned, not group/other writable,
   regular files/directories only, **no symlinks** (including Python/lib64 venv links).
   Prepare Python executable copies; resolve nonessential venv links in staging only
   under separately reviewed preparation. No writable state belongs in release.
3. Create `MANIFEST.json` as a JSON object mapping every regular file's relative path
   to its SHA256, except MANIFEST.json itself. Include all venv files and code. Pin the
   manifest's SHA256 in the reviewed change request outside the release. This manifest
   must be generated from trusted tested bytes, not accepted from an untrusted server.
   Preserve the previous release and its reviewed manifest hash for rollback.
4. Have an independently reviewed consistent DB backup and verify that both versions
   can safely use the unchanged existing auth/vault schema. This controller does not
   implement schema migration, backup or restore; `--data-compatible` is an explicit
   operator assertion, not evidence or an automatic check. Applications can still
   perform their ordinary database writes when started.
5. Inventory unit dependencies/hooks/aliases and drop-ins: start/stop must not affect
   other projects. Pin SHA256 of exact `systemctl cat --no-pager 3dprinta-portal.service`
   output, **not just the unit file's bytes**. Inspect loaded `systemctl show`, and
   reconcile any pending systemd jobs before beginning. The controller refuses a
   daemon-reload-needed unit, wrong working directory or non-release gunicorn path.
6. Provision `/var/lib/3dprinta-native-deployer` root-owned0700, all root-owned parents.
   Controller must run through a reviewed root-owned absolute path using trusted
   system Python. Do not execute scripts from an app-owned directory or altered PATH.
   No provisioning command is bundled or automatically executed.

## Commands (templates, no implied production approval)

```
python3 native_release.py check --release RELEASE --manifest-sha256 HASH --unit-sha256 UNITCAT_HASH
python3 native_release.py deploy --release RELEASE --manifest-sha256 HASH --current-manifest-sha256 OLD_HASH --unit-sha256 UNITCAT_HASH --execute --data-compatible
python3 native_release.py rollback --unit-sha256 UNITCAT_HASH --execute --data-compatible
```

`check` is read-only except private lock creation. Readiness is HTTP200 at
127.0.0.1:18731 with Host3dprinta.shop, proxies/redirects disabled, followed by no
external API/SMTP call. It proves basic HTTP readiness, not email delivery or
marketplace connectivity. Readiness retries up to20 times (0.5s apart, each HTTP request max5s),
checking unit activity on every attempt; exhaustion preserves the journal. Never expose new ports for this check.

`rollback` switches to the previous immutable release through the same checks;
it does NOT roll back credentials or database contents. Stop/switch/start creates a
short planned outage. This is not a zero-downtime deployer.

## Fail-closed recovery

An exclusive flock covers checks, intent journaling, stop/switch/start and health.
Intent is fsynced BEFORE stopping anything. `current` changes by atomic symlink
replacement + directory fsync. Previous releases are never deleted. Confirmed errors,
CLI timeouts, process crash or health failure leave durable intent; no automatic retry,
rollback or speculative cancellation is attempted. Original systemd jobs may continue
following a client timeout. Stop/start commands target only the fixed unit.

After failure, inspect systemd status and jobs, processes, current symlink, candidate
health and pending.json. Wait for/cancel an unsettled job only through a separately
reviewed operator action; do not just repeat the command. When outcome is settled,
no competing service manager/operator remains, and reverting is data-compatible:

```
python3 native_release.py recover --execute --data-compatible --confirmed-no-systemd-jobs
```

Recovery verifies both release manifests and pinned unit, stops the service,
confirms inactive, restores old symlink, starts and checks readiness. It retains the
journal on any error. Recovery removes a crash-created `current.pending` only if it is a root-owned
symlink with the exact journaled old/new target, after confirmed stop and under lock.
Unexpected links and private `.new` files need manual reconciliation and are never
removed automatically. Both deploy and recover additionally query list-jobs for
the fixed unit and refuse any result; operator acknowledgement also covers other
competing management processes not visible in that snapshot.
The system cannot fence unrelated root operators who ignore the lock.

## Verification limits

Unit tests use a fake systemd runner and temporary file trees; production ownership
checks are tested separately. No real systemd/VPS activation is performed. Tests cover
success, rollback, recovery, corrupt artifacts, foreign current, stale intent,
timeout stop/start, readiness failure, lock exclusion and single-unit command scope.
Production acceptance must additionally verify actual systemd sandbox, venv layout,
unit hooks/dependencies, DB compatibility, health identity and operational recovery.
