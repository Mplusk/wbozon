"""Exercise the exact marketplace release and its production rollback, with synthetic data only."""

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

BASE = "824d84a053134e06dac96aacb7a527401e50ab74"
REPORTS = "b78835f2adf6a09df5828bb2cf6730c93349a96f"
BASE_INVENTORY = "b543e1568394095a20f14213feb3a194a456716c70d43b95c06b33c98cd32aed"
REPORT_INVENTORY = "8edfed4f0ed0cc783cc919da4a38b7ef348daa56112f91ce6f4af72f2903b596"
HOST_POLICY = "4c9d8de80ad612f5ab226dcd8619391407dfffc9f83aca21b91d1cf7bb14dadd"


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), "-c", "core.autocrlf=false", *args])


def export_service(revision, destination):
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(git("archive", revision + ":services/email-auth"))) as tree:
        tree.extractall(destination, filter="data")


def rollout_check(temp, python, packaged):
    from check_finance_transition import exercise, PROBE
    from release_host import service_digest

    old, new = temp / "baseline", temp / "candidate"
    export_service(BASE, old / "service")
    if packaged:
        shutil.copytree(packaged, new / "service")
    else:
        export_service(REPORTS, new / "service")
    baseline_digest, candidate_digest = service_digest(old), service_digest(new)
    if (baseline_digest, candidate_digest) != (BASE_INVENTORY, REPORT_INVENTORY):
        raise ValueError(
            f"Unexpected service inventories. Baseline {baseline_digest}, candidate {candidate_digest}"
        )

    source = git("show", BASE + ":tools/native-release/release_host.py")
    if hashlib.sha256(source).hexdigest() != HOST_POLICY:
        raise ValueError("Unexpected production controller source")
    path = temp / "installed_release_host.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("installed_release_host", path)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    admission = {}
    for direction, first, second in (("upgrade", old, new), ("rollback", new, old)):
        try:
            policy.compatible(first, second)
        except policy.ReleaseError as exc:
            admission[direction] = str(exc)
        else:
            admission[direction] = "admitted"

    # The data simulation runs in separate release processes, without activating either service.
    exercise(python, old, new, temp)
    data = temp / "seller-fixture"
    data.mkdir(mode=0o700)

    def probe(release, action, stage):
        return subprocess.check_output(
            [str(python), "-B", "-I", str(PROBE), action, str(data), stage],
            cwd=release / "service",
            text=True,
            timeout=20,
        )

    probe(old, "activity", "seed")
    probe(new, "activity", "candidate")
    probe(new, "report-state", "seed")
    reports = data / ".market-reports" / "reports.db"
    before = hashlib.sha256(reports.read_bytes()).digest()
    probe(old, "activity", "restored")
    legacy_limits = json.loads(probe(old, "report-state", "legacy"))
    db_preserved = before == hashlib.sha256(reports.read_bytes()).digest()
    resumed = json.loads(probe(new, "report-state", "resume"))
    if not db_preserved or not resumed["confirmed_task_reused_and_unknown_creation_blocked"]:
        raise AssertionError("Report recovery lost durable state")
    ready = all(v == "admitted" for v in admission.values()) and all(legacy_limits.values())
    return {
        "eligibility": "ready" if ready else "blocked",
        "baseline": BASE,
        "candidate_service": REPORTS,
        "candidate_inventory_sha256": REPORT_INVENTORY,
        "tested_controller_sha256": HOST_POLICY,
        "controller": admission,
        "finance_gate_preserved": True,
        "session_key_and_vault_preserved": True,
        "reports_db_preserved": db_preserved,
        "report_tasks_resumed": True,
        "legacy_methods_honor_persisted_429": legacy_limits,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, help="Python with the release dependencies")
    parser.add_argument("--packaged-service", type=Path)
    parser.add_argument(
        "--evidence-only", action="store_true", help="Collect results regardless of eligibility"
    )
    args = parser.parse_args()
    with TemporaryDirectory() as temp:
        report = rollout_check(Path(temp), args.python.absolute(), args.packaged_service)
    print(json.dumps(report, indent=2))
    return 0 if args.evidence_only or report["eligibility"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
