"""Receive a tested artifact into an unprivileged staging directory only.

This program does not unpack, execute, promote, or deploy the received archive.
It is intended for an SSH forced command under a dedicated unprivileged account.
"""
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import stat
import sys
import time

DIRECTORY = pathlib.Path('/var/lib/staging-account/incoming')
MAX_BYTES = 30_000_000
MAX_ARTIFACTS = 40
MAX_TOTAL_BYTES = 300_000_000
MAX_HOURLY_BYTES = 200_000_000
MAX_HOURLY_ATTEMPTS = 20
MIN_FREE_BYTES = 1_000_000_000


class Rejected(Exception):
    pass


def _open_regular(directory, name, flags, mode=0o600):
    fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                 mode, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise Rejected('untrusted staging file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _consume(stream, output, size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise Rejected('incomplete artifact')
        remaining -= len(block)
        digest.update(block)
        if output is not None:
            output.write(block)
    if stream.read(1):
        raise Rejected('artifact exceeds announced length')
    return digest.hexdigest()


def _recover_partials(directory):
    """Remove only our reserved incomplete names while holding the receiver lock."""
    changed = False
    for name in os.listdir(directory):
        if name == '.receiver.rate.new':
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1):
                raise Rejected('untrusted rate update')
            os.unlink(name, dir_fd=directory)
            changed = True
            continue
        match = re.fullmatch(r'\.([0-9a-f]{40}(?:-[1-9][0-9]{0,19}-[1-9][0-9]{0,4})?)\.partial', name)
        if not match:
            continue
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink not in (1, 2)):
            raise Rejected('untrusted partial artifact')
        if info.st_nlink == 2:
            final = os.stat(match[1] + '.tar.gz', dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(final.st_mode) or final.st_uid != os.getuid() or
                    (info.st_dev, info.st_ino) != (final.st_dev, final.st_ino)):
                raise Rejected('unexpected partial artifact link')
        os.unlink(name, dir_fd=directory)
        changed = True
    if changed:
        os.fsync(directory)


def _rate_limit(directory, size, now):
    attempts = []
    try:
        fd = _open_regular(directory, '.receiver.rate.json', os.O_RDONLY)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd, 'rb') as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise Rejected('invalid rate state')
        try:
            state = json.loads(raw)
            if (not isinstance(state, dict) or set(state) != {'version', 'attempts'} or
                    type(state['version']) is not int or state['version'] != 1):
                raise ValueError()
            if not isinstance(state['attempts'], list) or len(state['attempts']) > MAX_HOURLY_ATTEMPTS:
                raise ValueError()
            for item in state['attempts']:
                if (not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int or
                        type(item[1]) is not int or not 0 <= item[0] <= now or
                        not 1 <= item[1] <= MAX_BYTES):
                    raise ValueError()
                if item[0] > now - 3600:
                    attempts.append(item)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise Rejected('invalid rate state') from None
    if len(attempts) >= MAX_HOURLY_ATTEMPTS or sum(a[1] for a in attempts) + size > MAX_HOURLY_BYTES:
        raise Rejected('staging rate limit reached')
    attempts.append([now, size])
    fd = _open_regular(directory, '.receiver.rate.new', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(json.dumps({'version': 1, 'attempts': attempts}, separators=(',', ':')).encode())
            out.flush()
            os.fsync(out.fileno())
        os.replace('.receiver.rate.new', '.receiver.rate.json', src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        try:
            os.unlink('.receiver.rate.new', dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def receive(directory, stream, commit, digest, size, now=None, run_id=None, run_attempt=None):
    if (not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('[0-9a-f]{64}', digest) or
            type(size) is not int or not 1 <= size <= MAX_BYTES):
        raise Rejected('invalid artifact metadata')
    if (run_id is None) != (run_attempt is None):
        raise Rejected('incomplete workflow identity')
    stem = commit
    if run_id is not None:
        if (not isinstance(run_id, str) or not re.fullmatch('[1-9][0-9]{0,19}', run_id) or
                not isinstance(run_attempt, str) or not re.fullmatch('[1-9][0-9]{0,4}', run_attempt)):
            raise Rejected('invalid workflow identity')
        stem += '-' + run_id + '-' + run_attempt
    directory = pathlib.Path(directory)
    if not directory.is_absolute() or '..' in directory.parts:
        raise Rejected('invalid staging directory')
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lockfd = None
    tempname = '.' + stem + '.partial'
    filename = stem + '.tar.gz'
    created = False
    try:
        info = os.fstat(dfd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_mode & 0o700 != 0o700:
            raise Rejected('untrusted staging directory')
        lockfd = _open_regular(dfd, '.receiver.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected('another artifact is being staged') from None
        _recover_partials(dfd)
        current = int(time.time()) if now is None else now
        if type(current) is not int or not 0 <= current <= 10**12:
            raise Rejected('invalid clock')
        _rate_limit(dfd, size, current)
        names = os.listdir(dfd)
        artifacts = []
        for name in names:
            if re.fullmatch('[0-9a-f]{40}(?:-[1-9][0-9]{0,19}-[1-9][0-9]{0,4})?\\.tar\\.gz', name):
                fd = _open_regular(dfd, name, os.O_RDONLY)
                try:
                    entry = os.fstat(fd)
                    artifacts.append((name, entry.st_size))
                finally:
                    os.close(fd)
            elif name not in ('.receiver.lock', '.receiver.rate.json'):
                raise Rejected('unexpected staging entry requires inspection')
        if any(length > MAX_BYTES for _, length in artifacts):
            raise Rejected('oversized existing artifact')
        if filename not in {a[0] for a in artifacts} and (
                len(artifacts) >= MAX_ARTIFACTS or sum(a[1] for a in artifacts) + size > MAX_TOTAL_BYTES):
            raise Rejected('staging storage limit reached')
        disk = os.fstatvfs(dfd)
        if filename not in {a[0] for a in artifacts} and disk.f_bavail * disk.f_frsize < MIN_FREE_BYTES + size:
            raise Rejected('insufficient staging space')
        existing = None
        try:
            existing = _open_regular(dfd, filename, os.O_RDONLY)
        except FileNotFoundError:
            pass
        if existing is not None:
            try:
                with os.fdopen(existing, 'rb') as f:
                    if os.fstat(f.fileno()).st_size != size or _consume(f, None, size) != digest:
                        raise Rejected('conflicting staged artifact')
            finally:
                existing = None
            if _consume(stream, None, size) != digest:
                raise Rejected('artifact digest mismatch')
            return
        fd = _open_regular(dfd, tempname, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, 'wb') as out:
            if _consume(stream, out, size) != digest:
                raise Rejected('artifact digest mismatch')
            out.flush()
            os.fsync(out.fileno())
        # Hard-linking is atomic and fails if the final name unexpectedly exists.
        os.link(tempname, filename, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        os.unlink(tempname, dir_fd=dfd)
        created = False
        os.fsync(dfd)
        # These are replaceable CI cache copies. Keep legacy and unmanaged names;
        # prune only completed, versioned uploads after the new one is durable.
        versioned = []
        for entry in os.listdir(dfd):
            if entry != filename and re.fullmatch(r'[0-9a-f]{40}-[1-9][0-9]{0,19}-[1-9][0-9]{0,4}\.tar\.gz', entry):
                oldfd = _open_regular(dfd, entry, os.O_RDONLY)
                try:
                    versioned.append((os.fstat(oldfd).st_mtime_ns, entry))
                finally:
                    os.close(oldfd)
        count = len(artifacts) + 1
        used = sum(length for _, length in artifacts) + size
        sizes = dict(artifacts)
        for index, (_, entry) in enumerate(sorted(versioned)):
            if (len(versioned) - index <= 11 and used <= MAX_TOTAL_BYTES - MAX_BYTES and
                    count < MAX_ARTIFACTS):
                break
            os.unlink(entry, dir_fd=dfd)
            used -= sizes[entry]
            count -= 1
        os.fsync(dfd)
    finally:
        if created:
            try:
                os.unlink(tempname, dir_fd=dfd)
            except FileNotFoundError:
                pass
        if lockfd is not None:
            os.close(lockfd)
        os.close(dfd)


def _deadline(signum, frame):
    raise TimeoutError('artifact upload timed out')


def _trusted_ancestors(directory):
    for path in reversed(pathlib.Path(directory).parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
            raise Rejected('untrusted staging ancestor')


def main():
    # The forced command is never evaluated by a shell or used as a path.
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    match = re.fullmatch(r'stage ([0-9a-f]{40})(?: ([1-9][0-9]{0,19}) ([1-9][0-9]{0,4}))? ([0-9a-f]{64}) ([1-9][0-9]{0,7})', command)
    if not match or os.geteuid() == 0:
        print('Rejected staging request.', file=sys.stderr)
        return 1
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(180)
    try:
        _trusted_ancestors(DIRECTORY)
        receive(DIRECTORY, sys.stdin.buffer, match[1], match[4], int(match[5]),
                run_id=match[2], run_attempt=match[3])
    except (Rejected, OSError, ValueError):
        print('Staging outcome not confirmed; inspect before retrying.', file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    print('Artifact staged and checksum verified.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
