"""Fail early when the workflow omits or reorders a reviewed service release."""

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]


def inventory(ref):
    ref = "HEAD" if ref == '"$GITHUB_SHA"' else ref
    raw = subprocess.check_output(["git", "-C", str(ROOT), "-c", "core.autocrlf=false",
                                   "archive", ref + ":services/email-auth"])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        files = {member.name: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                 for member in archive if member.isfile()}
    if not files:
        raise ValueError("empty service archive")
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def main():
    module = ast.parse((ROOT / "tools/native-release/cd.py").read_text(encoding="utf-8"))
    policy = next(ast.literal_eval(node.value) for node in module.body
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                  target.id == "MARKET_REPORT_SUCCESSORS" for target in node.targets))
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r'(?m)^\s+--successor\s+([0-9a-f]{40}|"\$GITHUB_SHA")', workflow)
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = tuple(pool.map(inventory, refs))
    if not observed or observed != policy:
        raise SystemExit("workflow successors differ from the reviewed deployment chain")
    print("Reviewed deployment chain:", len(observed), "inventories verified")


if __name__ == "__main__":
    main()
