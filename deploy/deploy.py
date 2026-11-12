#!/usr/bin/env python3
"""Pull main, test a candidate release, then atomically switch the current link.

Run this trusted control script as a dedicated unprivileged deploy user. It
never installs a timer, starts application services, or accesses marketplace
credentials. See deploy/README.md before enabling on a real server.
"""

import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

GITHUB_REPO = re.compile(r"(?:git@github\.com:|ssh://git@github\.com/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?\Z")


class DeployError(Exception):
    pass


def safe_environment():
    """No application credentials or forwarded SSH agent are passed to tests."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
           "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "PYTHONDONTWRITEBYTECODE": "1"}
    return env


def command(args, cwd, env):
    result = subprocess.run(args, cwd=cwd, env=env, check=False)
    if result.returncode:
        raise DeployError(f"Команда {args[0]} завершилась с кодом {result.returncode}")


def deploy(repo, base, allow_local=False):
    if os.geteuid() == 0:
        raise DeployError("Не запускайте выкладку от root")
    if not GITHUB_REPO.fullmatch(repo) and not (allow_local and repo.startswith("file:///")):
        raise DeployError("Ожидается SSH-адрес репозитория GitHub; локальный file:// доступен только для тестов")
    base = Path(base)
    if not base.is_absolute() or base == Path("/"):
        raise DeployError("Каталог развёртывания должен быть абсолютным и не корнем")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases = base / "releases"
    releases.mkdir(mode=0o700, exist_ok=True)
    if releases.is_symlink() or not releases.is_dir():
        raise DeployError("releases должен быть обычным каталогом")
    current = base / "current"
    lock_path = base / ".deploy.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("Другая выкладка уже выполняется") from None
        if current.exists() and not current.is_symlink():
            raise DeployError("current существует и не является символической ссылкой")
        env = safe_environment()
        # git receives a separate read-only deployment key via the deploy user's
        # SSH configuration; application credentials are not in this environment.
        with tempfile.TemporaryDirectory(prefix=".candidate-", dir=base) as temp:
            candidate = Path(temp) / "checkout"
            git_env = dict(env, GIT_ALLOW_PROTOCOL="file" if allow_local else "ssh",
                           GIT_SSH_COMMAND="ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no")
            command(["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-local",
                     "--depth=1", "--single-branch", "--branch", "main", "--", repo, str(candidate)], base, git_env)
            commit = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=candidate, env=env,
                                    check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                raise DeployError("Некорректный идентификатор коммита")
            if current.is_symlink():
                active = current.resolve()
                if (active.parent == releases.resolve() and active.is_dir()
                        and re.fullmatch(re.escape(commit) + r"-[0-9a-f]{12}", active.name)):
                    return commit, False
            test_env = dict(env, WBANDOZON_DEPLOY_TEST_STAGE="1")
            # Explicit suites prevent a zero-test checkout from being deployed.
            command([sys.executable, "-m", "unittest", "-v", "test_economics", "test_candidate_pipeline"], candidate, test_env)
            command([sys.executable, "-m", "unittest", "discover", "-v"], candidate, test_env)
            release = releases / (commit + "-" + uuid.uuid4().hex[:12])
            os.replace(candidate, release)
            link = base / (".current-" + uuid.uuid4().hex)
            try:
                os.symlink(os.path.relpath(release, base), link)
                os.replace(link, current)
            finally:
                if link.is_symlink():
                    link.unlink()
        return commit, True
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверить и активировать снимок main")
    parser.add_argument("--repo", required=True, help="SSH-адрес GitHub с read-only deploy key")
    parser.add_argument("--base", required=True, help="абсолютный каталог выкладки")
    parser.add_argument("--allow-local-repository", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        commit, changed = deploy(args.repo, args.base, args.allow_local_repository)
    except (DeployError, OSError, subprocess.SubprocessError) as exc:
        print(f"Выкладка не выполнена: {exc}", file=sys.stderr)
        return 1
    status = "Активирован проверенный" if changed else "Уже активен проверенный"
    print(f"{status} коммит {commit}. Сервисы не перезапускались.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
