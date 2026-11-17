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

## CI native candidate

In the branch containing this document, the `CI` workflow builds a native
candidate only after the application tests, isolated Docker smoke, controller
tests and egress tests succeed for the same commit. It reconstructs the tracked
service from that commit, downloads hash-locked Linux wheels, installs them in a
separate CI venv, runs the portal tests and a synthetic native Gunicorn smoke,
and retains a private `native-portal-tested-<commit>` artifact for seven days.
The artifact contains source, the wheelhouse, a separately hash-locked pip
bootstrap wheel, `PROVENANCE.json`, per-file
`FILES.sha256`, and an outer `SHA256SUMS` for the archive. Verify the hashes and
the exact repository, commit and successful workflow run from a trusted GitHub
channel; a checksum inside the artifact alone is not proof of origin.

This archive is **not** an installed or manifest-sealed release: it contains no
prepared server venv, trusted current-release manifest, unit fingerprint, or
server deployment. It must not be pointed at the controller unchanged. Host
adoption and the controller checks below remain necessary before any CD step.

1. Verify Python3.12 and trusted package hashes. Prepare candidate under its **final**
   root-owned `/opt/3dprinta-portal/releases/<release-name>` path with an isolated venv,
   no system site packages, only approved hash-locked offline wheels. Venv executable
   shebangs depend on this path. Run portal regression and synthetic native smoke
   before sealing. Runtime must not write pycache, secrets, logs or DB inside release.
   The CI test uses `venv --without-pip --copies` and runs pip from the pinned
   bootstrap wheel. A host still needs a compatible Python and the `venv` module;
   when those are already present, missing `ensurepip` does not by itself require
   package updates. Verify the source and both lockfiles against the trusted CI
   artifact before use.
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

## Current production hook contract (local controller not activated)

The separately authorized host correction removed only Wants=docker.service from
20-wait-bridge.conf. Ordering, readiness helper and automatic retries remain.
`check_unit` requires secure root-owned regular files and exact hashes:

- `/usr/local/libexec/3dprinta-wait-local-address.py`:
  `8ddb5c61cc2c3ee8e3e0e7c4978053ce7df42ad4424f2687a4f2c2f7a2370aa5`
- `/etc/systemd/system/3dprinta-portal.service.d/20-wait-bridge.conf`:
  `1160cc65d4b0f762d2f1de85802fc84f4ea370b5ac3654d3a54df6aa346cbebb`

Loaded ExecStartPre must contain exactly one `/usr/bin/python3` command running the
helper with `172.30.31.1 60`. Full systemctl-cat fingerprint must be independently
captured and approved, not inferred from the main-unit file hash. No helper is
executed by check. Changes require another review, not copying live hashes blindly.

## Dependency races and bounded timeouts

Docker checks use only is-active/list-jobs, each max5s, before stopping portal,
immediately before start and after readiness. Inactive/pending/unknown refuses;
loss after mutation leaves journal without retries, rollback or Docker restart.
Portal start uses `--job-mode=ignore-dependencies` to avoid dependency expansion in
its initial transaction. Host acceptance must account for suppressed ordering.

Automatic Restart=on-failure is a new systemd transaction that can expand
activation dependencies. Thus loaded Wants/Requires/BindsTo containing docker.service
or missing dependency properties refuse BEFORE intent/stop, even when Docker is
active. This guards against regression to the former unsafe drop-in. It is not a
complete graph proof. The host operator reports a99-unit recursive graph inspection
across Wants/Requires/BindsTo/Upholds/OnSuccess/OnFailure/Triggers with no Docker or
containerd reachable. This supplied host snapshot must be rechecked after unit or
dependency changes and before first activation. Ordering-only After does not pull
Docker in; Docker must already be running for this workflow.

Client start budget100s exceeds verified loaded systemd75s; stop60s exceeds40s;
read-only portal queries max10s. Missing/different/infinite systemd budgets refuse
before mutation. A timeout still means UNKNOWN because systemd may continue:
preserve pending journal, inspect actual jobs and reconcile before explicit recovery.

## Remaining adoption gates

- Fresh full systemctl-cat fingerprint including corrected drop-in.
- Sealed manifest-compatible baseline/candidate, not copied or modified live venv.
- Independently reviewed bootstrap from old unsealed release, consistent backup
  and explicit data compatibility; no automatic migration/restore.
- Actual systemd sandbox/lifecycle/recovery acceptance during authorized window.
- Recheck activation graph for concurrent host/dependency changes.

## Experimental finance lock guard

`finance_guard.py` is a separately tested building block. It opens only an existing
gate, takes its bootstrap and registered client locks, checks identities,
generations and cooldowns, and refuses uncertain or changed state. It does not
create or reset any gate data. It is **not integrated into the controller**,
installed on the host, or sufficient to authorize a deployment. Holding the locks
temporarily causes concurrent finance calls to fail busy; it does not drain OAuth,
SMTP, other marketplace calls, or general HTTP requests.

Any future integration must derive the gate path, UID, PID and invocation identity
from the trusted actual unit, not from a CI client; compare every relevant gate
inode and time namespace with the running service before and after switching; keep
the guard outside the service cgroup; and account for the fact that locks disappear
when the guard process dies. It must independently establish secure path ownership,
writer and mount-namespace assumptions, a durable recovery plan and bounded disk
I/O supervision. Synthetic tests do not prove real systemd drain or recovery.

The controller does not maintain a permanent guarantee about root operators or
systemd configuration changes outside its lock. Passing local tests does not
constitute host acceptance or enable automatic deployment.
