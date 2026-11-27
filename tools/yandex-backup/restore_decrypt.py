"""Decrypt only into a bounded private work directory, keeping identities in RAM."""
import os
from pathlib import Path
import re
import subprocess


class DecryptionError(ValueError):
    pass


def decrypt(encrypted, identity, output):
    if (not hasattr(os, "memfd_create") or not isinstance(identity, bytes) or
            not 32 <= len(identity) <= 8192 or b"\0" in identity):
        raise DecryptionError("invalid_private_key")
    if not (identity.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----") or
            re.search(rb"(?m)^AGE-SECRET-KEY-[A-Z0-9]{20,200}\r?$", identity)):
        raise DecryptionError("invalid_private_key")
    output = Path(output)
    key_fd = os.memfd_create("wbozon-restore-key", flags=getattr(os, "MFD_CLOEXEC", 0))
    created = False
    try:
        os.write(key_fd, identity)
        os.lseek(key_fd, 0, os.SEEK_SET)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with open(encrypted, "rb") as source, os.fdopen(descriptor, "wb") as destination:
            try:
                result = subprocess.run(["/usr/bin/age", "--decrypt", "--identity", "/proc/self/fd/" + str(key_fd)],
                                        stdin=source, stdout=destination, stderr=subprocess.DEVNULL,
                                        pass_fds=(key_fd,), timeout=120, check=False,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
            except (OSError, subprocess.TimeoutExpired):
                raise DecryptionError("decryption_failed") from None
            if result.returncode != 0:
                raise DecryptionError("decryption_failed")
            destination.flush()
            os.fsync(destination.fileno())
        if output.stat().st_size > 512 * 1024 * 1024:
            raise DecryptionError("archive_too_large")
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    finally:
        os.close(key_fd)
