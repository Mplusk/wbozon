"""Fail-closed, read-only lock of an *existing* Ozon finance gate.

This is an adoption building block, not a deployment controller. While held,
new finance calls are rejected by the existing gate. No state is created, reset,
or recovered. It does not drain non-finance HTTP requests or stop any service.
"""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import resource
import stat
import threading
import time


class GuardError(Exception):
    pass


def _json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate JSON key')
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _valid_clock(value):
    return type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value)


def _boot_id(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GuardError('invalid boot identity file')
        raw = os.read(fd, 66)
        value = raw.strip()
        if len(raw) > 65 or not value or len(value) > 64:
            raise GuardError('invalid boot identity')
        return value
    finally:
        os.close(fd)


class FinanceGuard:
    def __init__(self, root, uid, wait_seconds=25, *, allow_uncertain=False):
        self.root = pathlib.Path(root)
        self.uid = uid
        if not self.root.is_absolute() or '..' in self.root.parts or not self.root.name:
            raise GuardError('absolute gate path required')
        if type(uid) is not int or uid < 0 or not 0 <= wait_seconds <= 60 or type(allow_uncertain) is not bool:
            raise GuardError('invalid guard settings')
        self.wait_seconds = wait_seconds
        self.allow_uncertain = allow_uncertain
        self.stack = None
        self.locks = []
        self._used = False
        self._lifecycle = threading.Lock()

    def _file(self, directory, name, writable=False):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            required = stat.S_IRUSR | (stat.S_IWUSR if writable else 0)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or
                    info.st_mode & 0o077 or info.st_mode & required != required or info.st_nlink != 1):
                raise GuardError('untrusted gate file')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read(self, directory, name, limit):
        fd = self._file(directory, name)
        try:
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                raise GuardError('gate file is too large')
            return raw
        finally:
            os.close(fd)

    def _lock(self, directory, name, deadline):
        fd = self._file(directory, name, writable=True)
        self.stack.callback(os.close, fd)
        while True:
            if self.wait_seconds and time.monotonic() >= deadline:
                raise GuardError('finance guard deadline exceeded')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not self.wait_seconds or time.monotonic() >= deadline:
                    raise GuardError('finance operation is busy') from None
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        if self.wait_seconds and time.monotonic() >= deadline:
            raise GuardError('finance guard deadline exceeded')
        self.locks.append((directory, name, fd))

    def __enter__(self):
        with self._lifecycle:
            if self._used:
                raise GuardError('guard instances are single-use')
            self._used = True
        self.stack = contextlib.ExitStack()
        try:
            parent = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.stack.callback(os.close, parent)
            self.parent = parent
            parent_info = os.fstat(parent)
            if (parent_info.st_uid != self.uid or parent_info.st_mode & 0o077 or
                    parent_info.st_mode & 0o700 != 0o700):
                raise GuardError('untrusted gate parent')
            deadline = time.monotonic() + self.wait_seconds
            leaf = self.root.name
            self._lock(parent, leaf + '.bootstrap.lock', deadline)
            store = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=parent)
            self.stack.callback(os.close, store)
            self.store = store
            info = os.fstat(store)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid or
                    info.st_mode & 0o077 or info.st_mode & 0o700 != 0o700):
                raise GuardError('untrusted gate directory')
            marker = self._read(parent, leaf + '.initialized', 64)
            if not re.fullmatch(b'[0-9a-f]{64}', marker) or marker != self._read(store, '.store-id', 64):
                raise GuardError('gate identity mismatch')
            registry_raw = self._read(parent, leaf + '.registry.json', 1048576)
            registry = _json(registry_raw)
            if (not isinstance(registry, dict) or set(registry) != {'version', 'clients'} or
                    type(registry['version']) is not int or registry['version'] != 1 or
                    not isinstance(registry['clients'], list)):
                raise GuardError('invalid gate registry')
            clients = registry['clients']
            if (len(clients) > 10000 or any(not isinstance(k, str) or
                    not re.fullmatch('[0-9a-f]{64}', k) for k in clients) or
                    len(set(clients)) != len(clients)):
                raise GuardError('invalid gate registry')
            limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if limit != resource.RLIM_INFINITY and len(clients) + len(os.listdir('/proc/self/fd')) + 16 >= limit:
                raise GuardError('insufficient file descriptors')
            for client in sorted(clients):
                self._lock(store, client + '.lock', deadline)
            self.marker, self.registry_raw = marker, registry_raw
            self.clients = clients
            self.states = None
            self.verify()
            if self.wait_seconds and time.monotonic() >= deadline:
                raise GuardError('finance guard deadline exceeded')
            return self
        except (OSError, ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError):
            self.stack.close()
            self.stack = None
            self.locks.clear()
            raise GuardError('gate verification failed') from None
        except BaseException:
            self.stack.close()
            self.stack = None
            self.locks.clear()
            raise

    def verify(self, namespace_root=None, time_namespace=None):
        if self.stack is None:
            raise GuardError('guard is not active')
        leaf = self.root.name
        held_parent = os.fstat(self.parent)
        current_parent = os.stat(self.root.parent)
        if ((held_parent.st_dev, held_parent.st_ino) != (current_parent.st_dev, current_parent.st_ino) or
                current_parent.st_uid != self.uid or current_parent.st_mode & 0o077 or
                current_parent.st_mode & 0o700 != 0o700):
            raise GuardError('gate parent replaced')
        if namespace_root is not None:
            namespace_parent = os.stat(pathlib.Path(namespace_root) / str(self.root.parent).lstrip('/'))
            if (held_parent.st_dev, held_parent.st_ino) != (namespace_parent.st_dev, namespace_parent.st_ino):
                raise GuardError('different gate mount namespace')
            if time_namespace is None:
                raise GuardError('time namespace is required with mount namespace')
            host_boot = _boot_id('/proc/sys/kernel/random/boot_id')
            other_boot = _boot_id(pathlib.Path(namespace_root) / 'proc/sys/kernel/random/boot_id')
            if host_boot != other_boot:
                raise GuardError('different boot identity in mount namespace')
            namespace_gate = pathlib.Path(namespace_root) / str(self.root).lstrip('/')
            paths = [(self.parent, leaf, namespace_gate),
                     (self.parent, leaf + '.bootstrap.lock', namespace_gate.parent / (leaf + '.bootstrap.lock')),
                     (self.parent, leaf + '.initialized', namespace_gate.parent / (leaf + '.initialized')),
                     (self.parent, leaf + '.registry.json', namespace_gate.parent / (leaf + '.registry.json')),
                     (self.store, '.store-id', namespace_gate / '.store-id')]
            for key in self.clients:
                paths.extend(((self.parent, leaf + '.epoch-' + key + '.json',
                               namespace_gate.parent / (leaf + '.epoch-' + key + '.json')),
                              (self.store, key + '.json', namespace_gate / (key + '.json')),
                              (self.store, key + '.lock', namespace_gate / (key + '.lock'))))
            for directory, name, other_path in paths:
                host_stat = os.stat(name, dir_fd=directory, follow_symlinks=False)
                other_stat = os.stat(other_path, follow_symlinks=False)
                if (host_stat.st_dev, host_stat.st_ino) != (other_stat.st_dev, other_stat.st_ino):
                    raise GuardError('different gate file in mount namespace')
        if time_namespace is not None:
            own_time = os.stat('/proc/self/ns/time')
            other_time = os.stat(time_namespace)
            if (own_time.st_dev, own_time.st_ino) != (other_time.st_dev, other_time.st_ino):
                raise GuardError('different time namespace')
        for directory, name, fd in self.locks:
            held = os.fstat(fd)
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or current.st_uid != self.uid or
                    current.st_mode & 0o077 or current.st_mode & 0o600 != 0o600 or current.st_nlink != 1 or
                    (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                raise GuardError('gate lock replaced')
        held_store = os.fstat(self.store)
        current_store = os.stat(leaf, dir_fd=self.parent, follow_symlinks=False)
        if ((held_store.st_dev, held_store.st_ino) != (current_store.st_dev, current_store.st_ino) or
                not stat.S_ISDIR(current_store.st_mode) or current_store.st_uid != self.uid or
                current_store.st_mode & 0o077 or current_store.st_mode & 0o700 != 0o700):
            raise GuardError('gate directory replaced')
        if (self._read(self.parent, leaf + '.initialized', 64) != self.marker or
                self._read(self.store, '.store-id', 64) != self.marker or
                self._read(self.parent, leaf + '.registry.json', 1048576) != self.registry_raw):
            raise GuardError('gate identity or registry changed')
        expected_store = {'.store-id'} | {key + ext for key in self.clients for ext in ('.json', '.lock')}
        if set(os.listdir(self.store)) != expected_store:
            raise GuardError('unexpected gate file')
        expected_epoch = {leaf + '.epoch-' + key + '.json' for key in self.clients}
        actual_epoch = {name for name in os.listdir(self.parent) if name.startswith(leaf + '.epoch-')}
        if expected_epoch != actual_epoch:
            raise GuardError('gate epoch inventory mismatch')
        states = {}
        for key in self.clients:
            state_raw = self._read(self.store, key + '.json', 4096)
            epoch_raw = self._read(self.parent, leaf + '.epoch-' + key + '.json', 256)
            states[key] = (state_raw, epoch_raw)
            state = _json(state_raw)
            epoch = _json(epoch_raw)
            if (not isinstance(state, dict) or not isinstance(epoch, dict) or
                    set(epoch) != {'version', 'generation'} or type(epoch['version']) is not int or
                    epoch['version'] != 1 or type(epoch['generation']) is not int or
                    not 0 <= epoch['generation'] <= 9007199254740991 or
                    type(state.get('version')) is not int or state['version'] != 1 or
                    type(state.get('generation')) is not int or state['generation'] != epoch['generation']):
                raise GuardError('gate generation mismatch')
            status = state.get('status')
            if status == 'idle':
                if set(state) != {'version', 'status', 'generation'}:
                    raise GuardError('invalid gate state')
            elif status in ('in_flight', 'retry_unknown') and self.allow_uncertain:
                # Recovery may restore service availability, but never resets an
                # uncertain provider outcome. The application keeps it blocked.
                if set(state) != {'version', 'status', 'generation'}:
                    raise GuardError('invalid uncertain gate state')
            elif status == 'retry':
                if (set(state) != {'version','status','at_wall','at_monotonic','delay','source','boot','generation'} or
                        type(state['delay']) is not int or not 0 <= state['delay'] <= 2147483647 or
                        state['source'] not in ('delta_seconds','http_date','verified_recovery') or
                        not isinstance(state['boot'], str) or not re.fullmatch('[0-9a-f]{64}', state['boot']) or
                        any(not _valid_clock(state[k]) for k in ('at_wall', 'at_monotonic'))):
                    raise GuardError('invalid retry state')
                boot = _boot_id('/proc/sys/kernel/random/boot_id')
                if (hashlib.sha256(boot).hexdigest() != state['boot'] or
                        time.monotonic() < state['at_monotonic']):
                    if not self.allow_uncertain:
                        raise GuardError('retry state has unknown remaining time')
            else:
                raise GuardError('uncertain finance state')
        if self.states is None:
            self.states = states
        elif states != self.states:
            raise GuardError('gate state changed while locked')

    def __exit__(self, *exc):
        self.stack.close()
        self.stack = None
        self.locks.clear()
