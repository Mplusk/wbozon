# Production deployment from GitHub Actions

A push to main runs the repository, portal, egress and release tests. The native
candidate job tests its packaged source and wheels. Its separate systemd fixture
opens a synthetic finance request during deployment, kills controllers at five
transaction phases and starts a candidate that deliberately fails readiness.
The final production job waits for server activation and checks the public HTTPS
health response. Branch builds do not activate production.

The SSH deploy key runs only dispatch.py. It submits the GitHub job's temporary
read token on stdin to a root-owned transient systemd service. The host verifies
the run, successful jobs, current main, artifact digest, Git source and pinned
wheels. It installs the native runtime offline and executes candidate tests under
the dedicated wbozon-test identity with private networking and bounded tmpfs.
The controller and its trust policy are installed separately by the administrator.

Before stopping the portal, the controller fences new connections on its two
listener addresses, waits for established requests and provider children to exit,
then holds every existing Ozon finance lock. The deadline leaves a busy portal
serving its original version. Two marked connections verify that both sync workers
have finished their handlers, including requests whose client disconnected.
The controller backs up and checks both quiescent SQLite databases before stop.
The release link changes atomically. Readiness checks cover the schema, process
directory, two workers, real HTTP response and the existing finance gate in the
running service's namespace.

A synced pending.json records the rollback target before ingress changes.
The recovery service reconciles process state and the actual release link after
failure. The timer also runs after boot and every 30 seconds. During an unfinished
transaction the portal's ExecCondition requires a live controller holding the
deployment lock. Recovery preserves the databases, finance epochs, provider
cooldowns and uncertain outcomes. After the durable commit it completes activation
of the healthy new version. A failed recheck after admission reopens also triggers
a fresh drain and rollback. The controller reserves recovery disk blocks before
fencing, keeps the five most recent managed builds and snapshots plus protected
release targets, and discards temporary wheelhouses after each successful build.

For operations, inspect the GitHub job **Deploy and verify production portal**,
the journal of the corresponding wbozon-deploy-RUN-ATTEMPT.service and
wbozon-cd-recover.service, and the root-private active.json under
/var/lib/3dprinta-native-deployer. The single-host adoption script verifies the
original unit and baseline before installing the units in systemd/. Revisions
that change the pinned dependency set, database-owning modules, SQL access or
finance gate contract require a compatible rollout plan. The running DB schema
must match the quiescent snapshots on every path that reopens ingress. Unexpected
external schema changes retain the journal and backups for reconciliation.

## FBO finance release and rollback

The controller admits the exact baseline service from `c8b2257` paired with
either the first FBO service from `11bca6b` or the revised unit rates from
`023c4c1`, in either direction. Each pair has its own SHA-256 inventory.
CI verifies the packaged service byte for byte when its Git subtree matches
`023c4c1`. Commit and Actions run provenance belong to the enclosing archive
and may change while the service inventory stays identical. The read operations share
the existing client locks, epochs and retry deadlines. The portal saves the time,
source and outcome of each new bridge operation under the original activity code
`unknown`, which both versions can display after rollback.

CI exercises the two immutable service trees with separate provider processes and
an isolated systemd portal. It checks concurrent reads, interruption, known and
unknown retry times, rollback after admission reopens, and continued access with
the original session and MCP key. The provider transport uses synthetic replies.

To put this release on the production path, update the existing root-owned
`/usr/local/libexec/wbozon-cd/release_host.py` atomically from the reviewed commit
under the controller's `deploy.lock`, with no pending transaction. Verify the
installed file digest against Git and both service tree digests against the
approved pair before promoting the tested changes to `main`. The production job
verifies its own main-branch artifact and public readiness. The one-time adoption
installer applies to new hosts.

## Historical manual controller

The remaining notes describe the earlier manual prototype and staging design.

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
and retains a private `native-portal-tested-<commit>-<run>-<attempt>` artifact for seven days.
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

## Restricted artifact staging

`stage_receiver.py` accepts an archive over a forced SSH command for a dedicated
unprivileged account. It checks the announced length and digest and writes only
to that account's private staging directory. It never unpacks, executes or activates
the archive. The SSH key cannot invoke a shell or write to the live release. The
staged file and sender-supplied digest are **not** independent proof of CI origin;
any future privileged promoter must obtain trusted provenance and verify the
bytes again after copying them to a root-controlled location.
The staging filename includes commit, workflow run and attempt so a rerun of
the same commit does not overwrite a different artifact.

The CI job stages only tested artifacts from `main` and the explicitly named
release branch. A separate, fresh GitHub runner downloads the exact artifact ID
and compares its tarball digest with the builder's job output; the build and tests
do not run in the job containing the staging key. Uploads use
pinned SSH host keys and encrypted GitHub secrets.
The transfer retries a network interruption up to twice with the same build
attempt, archive and digest. Each upload has a 150-second deadline. The receiver
checks an existing file under the same identity before acknowledging delivery.
Host-key, authentication and receiver validation errors end the job immediately.
Staging is bounded to 40 artifacts or 300 MB. After a new upload is durable, it
keeps the twelve newest versioned cache entries and preserves legacy names.
Upload rate limits use the host wall clock. The free-space precheck
does not reserve disk blocks against other services. Staging does **not** enable
automatic deployment or switch the running service.
Concurrent staging jobs are serialized by GitHub; a newer queued job may replace
an older queued job. If a staging job needs rerunning, rerun the entire workflow
until isolated job-only retry has been verified.

The controller does not maintain a permanent guarantee about root operators or
systemd configuration changes outside its lock. Passing local tests does not
constitute host acceptance or enable automatic deployment.
