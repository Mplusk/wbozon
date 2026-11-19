"""Per-client local serialization and persisted 429 cooldown for Ozon finance.

The directory must be private to this service and on a local filesystem with
working advisory locks and atomic replacement. No marketplace credentials are
stored; file names are hashes of client IDs. A crash during a request leaves an
uncertain marker and requires explicit local recovery instead of a blind retry.
"""
import contextlib
import errno
import email.utils
import hashlib
import json
import math
import os
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
from datetime import timezone

FINANCE_OPERATIONS = frozenset(('ozon_accrual_types', 'ozon_posting_accruals', 'ozon_article_summary',
                                'ozon_realization_article_month', 'ozon_accrual_day', 'ozon_return_events',
                                'ozon_unallocated_accruals','ozon_cash_flow'))
MAX_RETRY_SECONDS = 2147483647
READ_RECOVERY_SECONDS = 60


class GateError(Exception):
    def __init__(self, reason, retry_after_seconds=None):
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__('Financial access is temporarily unavailable')


class KnownProviderResponseError(Exception):
    """No 429 was sent: request was not started or response status is known."""


class ProviderResponseTooLarge(ValueError, KnownProviderResponseError):
    pass


class ProviderResponseInvalid(ValueError, KnownProviderResponseError):
    pass


class ProviderBodyTimeout(TimeoutError, KnownProviderResponseError):
    pass


class LocalPreparationError(ValueError, KnownProviderResponseError):
    pass


class LocalPreparationTimeout(TimeoutError, KnownProviderResponseError):
    pass


def _single_header(headers, name):
    if headers is None: return None
    try:
        values = headers.get_all(name) if hasattr(headers, 'get_all') else None
        if values is not None:
            return values[0] if len(values) == 1 else None
        return headers.get(name)
    except (AttributeError, TypeError):
        return None


def _http_date(value):
    if not isinstance(value, str) or len(value) > 128 or '\r' in value or '\n' in value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None: return None
        return parsed.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError, TypeError, OSError):
        return None


def parse_retry_after(headers, now=None):
    """Return (seconds, kind); unknown/oversized is never shortened to a guess."""
    value = _single_header(headers, 'Retry-After')
    if not isinstance(value, str) or len(value) > 128 or '\r' in value or '\n' in value:
        return None, 'unknown'
    value = value.strip()
    if value.isascii() and value.isdigit():
        if len(value) > 10: return None, 'unknown'
        delay = int(value)
        return (delay, 'delta_seconds') if delay <= MAX_RETRY_SECONDS else (None, 'unknown')
    target = _http_date(value)
    if target is None: return None, 'unknown'
    current = time.time() if now is None else now
    server_time = _http_date(_single_header(headers, 'Date'))
    # An HTTP-date without a trusted server Date cannot be converted safely
    # when the host clock is ahead; do not guess a zero-second cooldown.
    if server_time is None: return None, 'unknown'
    # If clocks disagree, never choose the shorter interpretation.
    delay = max(0, math.ceil(target - current),
                math.ceil(target - server_time))
    return (delay, 'http_date') if delay <= MAX_RETRY_SECONDS else (None, 'unknown')


def _private_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GateError('storage_unavailable')
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise GateError('storage_unavailable')


@contextlib.contextmanager
def _lock(root, key):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = None
    try:
        path = os.path.join(root, key + '.lock')
        try:
            fd = os.open(path, flags | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, flags, 0o600)
            created = False
        _private_file(fd)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0: os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            try: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise GateError('busy', 1) from None
                raise
        else:
            import fcntl
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise GateError('busy', 1) from None
    except GateError:
        if fd is not None: os.close(fd)
        raise
    except OSError:
        if fd is not None: os.close(fd)
        raise GateError('storage_unavailable') from None
    try:
        yield created
    finally:
        os.close(fd)


def _root(path):
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise GateError('storage_unavailable')
    parent, leaf = os.path.split(path)
    if not parent or not leaf: raise GateError('storage_unavailable')
    marker = path + '.initialized'
    registry = path + '.registry.json'
    try:
        with _lock(parent, leaf + '.bootstrap') as created:
            if created:
                if any(os.path.lexists(item) for item in (path,marker,registry)): raise GateError('storage_unavailable')
                os.mkdir(path, 0o700)
                identity=secrets.token_hex(32).encode('ascii')
                for target in (os.path.join(path,'.store-id'),marker):
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0), 0o600)
                    try:
                        os.write(fd,identity)
                        os.fsync(fd)
                    finally: os.close(fd)
                _write_state(parent,leaf+'.registry',{'version':1,'clients':[]})
                if os.name != 'nt':
                    fd=os.open(parent,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
                    try: os.fsync(fd)
                    finally: os.close(fd)
            _validate_store(path)
    except OSError:
        raise GateError('storage_unavailable') from None
    return path


def _validate_store(root):
    try:
        def read_identity(target):
            fd=os.open(target,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
            try:
                _private_file(fd)
                return os.read(fd,65)
            finally: os.close(fd)
        outer=read_identity(root+'.initialized')
        inner=read_identity(os.path.join(root,'.store-id'))
        if (len(outer)!=64 or outer!=inner or
                any(character not in b'0123456789abcdef' for character in outer)):
            raise GateError('storage_unavailable')
        info=os.lstat(root)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise GateError('storage_unavailable')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise GateError('storage_unavailable')
    except OSError:
        raise GateError('storage_unavailable') from None


def _registry(root):
    parent, leaf = os.path.split(root)
    path = root + '.registry.json'
    try:
        fd = os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        try:
            _private_file(fd)
            raw=os.read(fd,1048577)
        finally: os.close(fd)
        if len(raw)>1048576: raise ValueError()
        def unique(pairs):
            result={}
            for field,value in pairs:
                if field in result: raise ValueError()
                result[field]=value
            return result
        data=json.loads(raw,object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data,dict) or set(data)!={'version','clients'} or type(data['version']) is not int or data['version']!=1:
            raise ValueError()
        clients=data['clients']
        if not isinstance(clients,list) or len(clients)>10000 or len(clients)!=len(set(clients)):
            raise ValueError()
        if any(not isinstance(item,str) or len(item)!=64 or any(c not in '0123456789abcdef' for c in item) for item in clients):
            raise ValueError()
        return data
    except (OSError,ValueError,TypeError,UnicodeError,json.JSONDecodeError):
        raise GateError('storage_unavailable') from None


def _ensure_client(root, key, *, create):
    parent,leaf=os.path.split(root)
    with _lock(parent,leaf+'.bootstrap') as bootstrap_created:
        if bootstrap_created: raise GateError('storage_unavailable')
        _validate_store(root)
        registry=_registry(root)
        registered=key in registry['clients']
        lock=os.path.join(root,key+'.lock')
        state=os.path.join(root,key+'.json')
        epoch=root+'.epoch-'+key+'.json'
        if registered:
            if not all(os.path.lexists(item) for item in (lock,state,epoch)): raise GateError('storage_unavailable')
            return
        if not create: raise ValueError('No uncertain cooldown to recover')
        if any(os.path.lexists(item) for item in (lock,state,epoch)) or len(registry['clients']) >= 10000:
            raise GateError('storage_unavailable')
        # Register before creating per-client state. A crash during setup will
        # then fail closed instead of being mistaken for an unseen client.
        registry['clients'].append(key)
        _write_state(parent,leaf+'.registry',registry)
        with _lock(root,key) as new_lock:
            if not new_lock: raise GateError('storage_unavailable')
            _write_state(parent,leaf+'.epoch-'+key,{'version':1,'generation':0})
            _write_state(root,key,{'version':1,'status':'idle','generation':0})


def _boot_identity():
    """A same-boot anchor; failure is closed, never silently use wall time."""
    try:
        if sys.platform.startswith('linux'):
            with open('/proc/sys/kernel/random/boot_id','rb') as file:
                value=file.read(128).strip()
            if not value or len(value)>64: raise ValueError()
        elif os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class BootInfo(ctypes.Structure):
                _fields_=[('boot',ctypes.c_ubyte*16),('firmware',wintypes.ULONG),('flags',ctypes.c_ulonglong)]
            data=BootInfo()
            if ctypes.windll.ntdll.NtQuerySystemInformation(90,ctypes.byref(data),ctypes.sizeof(data),None)!=0:
                raise ValueError()
            value=bytes(data.boot)
            if not any(value): raise ValueError()
        else:
            raise ValueError()
        return hashlib.sha256(value).hexdigest()
    except (OSError,ValueError,AttributeError):
        raise GateError('storage_unavailable') from None


def _read_state(root, key, created):
    if created: raise GateError('storage_unavailable')
    path = os.path.join(root, key + '.json')
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise GateError('storage_unavailable') from None
    except OSError:
        raise GateError('storage_unavailable') from None
    try:
        _private_file(fd)
        raw = os.read(fd, 4097)
        if len(raw) > 4096: raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        state = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1 or
                type(state.get('generation')) is not int or not 0 <= state['generation'] <= 9007199254740991 or
                state['generation'] != _read_epoch(root,key)):
            raise ValueError()
        if state.get('status') in ('idle', 'in_flight', 'retry_unknown'):
            if set(state) != {'version', 'status', 'generation'}: raise ValueError()
        elif state.get('status') == 'retry':
            if set(state) != {'version','status','at_wall','at_monotonic','delay','source','boot','generation'}:
                raise ValueError()
            if (type(state['delay']) is not int or not 0 <= state['delay'] <= MAX_RETRY_SECONDS or
                    state['source'] not in ('delta_seconds','http_date','verified_recovery') or
                    not isinstance(state['boot'],str) or len(state['boot']) != 64 or
                    any(c not in '0123456789abcdef' for c in state['boot']) or
                    any(type(state[k]) not in (int,float) or not math.isfinite(state[k]) or state[k] < 0
                        for k in ('at_wall','at_monotonic'))): raise ValueError()
        else: raise ValueError()
        return state
    except (ValueError, TypeError, UnicodeError, OSError, json.JSONDecodeError):
        raise GateError('storage_unavailable') from None
    finally:
        os.close(fd)


def _write_state(root, key, state):
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix='.finance-', dir=root)
        with os.fdopen(fd, 'wb') as file:
            file.write(json.dumps(state, separators=(',', ':'), allow_nan=False).encode('ascii'))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, os.path.join(root, key + '.json'))
        temp_name = None
        if os.name != 'nt':
            dir_fd = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
    except (OSError, ValueError, TypeError):
        raise GateError('storage_unavailable') from None
    finally:
        if temp_name is not None:
            try: os.unlink(temp_name)
            except OSError: pass


def _read_epoch(root, key):
    path=root+'.epoch-'+key+'.json'
    try:
        fd=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        try:
            _private_file(fd)
            raw=os.read(fd,257)
        finally: os.close(fd)
        if len(raw)>256: raise ValueError()
        def unique(pairs):
            result={}
            for field,value in pairs:
                if field in result: raise ValueError()
                result[field]=value
            return result
        data=json.loads(raw,object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(data,dict) or set(data)!={'version','generation'} or
                type(data['version']) is not int or data['version']!=1 or
                type(data['generation']) is not int or not 0 <= data['generation'] <= 9007199254740991):
            raise ValueError()
        return data['generation']
    except (OSError,ValueError,TypeError,UnicodeError,json.JSONDecodeError):
        raise GateError('storage_unavailable') from None


def _write_client_state(root, key, state):
    generation=_read_epoch(root,key)
    if generation >= 9007199254740991: raise GateError('storage_unavailable')
    generation+=1
    parent,leaf=os.path.split(root)
    # Persist outside the client directory first. A crash between writes leaves
    # a mismatch and is blocked. Restoring only inner files cannot rewind state.
    _write_state(parent,leaf+'.epoch-'+key,{'version':1,'generation':generation})
    _write_state(root,key,{**state,'generation':generation})


def _remaining(state, wall, mono, boot):
    if boot != state['boot'] or mono < state['at_monotonic']:
        raise GateError('retry_time_unknown')
    remaining = state['at_wall'] + state['delay'] - wall
    # Same-boot monotonic time prevents a forward wall-clock jump from ending
    # the cooldown early. A changed boot identity is deliberately unknown.
    remaining = max(remaining, state['at_monotonic'] + state['delay'] - mono)
    return max(0, math.ceil(remaining))


def _key(client_id):
    if not isinstance(client_id, str) or not client_id or len(client_id) > 256:
        raise GateError('storage_unavailable')
    # Numeric forms with leading zeroes can refer to the same seller. Taking
    # the same lock is conservative even if the provider rejects one spelling.
    canonical = (client_id.lstrip('0') or '0') if client_id.isascii() and client_id.isdigit() else client_id
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def schedule_verified_recovery(root, client_id, wait_seconds):
    """Local operator-only action after independently verifying Ozon's reset.

    This method does not verify the evidence itself. It is never exposed by
    MCP/HTTP and cannot shorten a still-valid, same-boot provider cooldown.
    """
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= MAX_RETRY_SECONDS:
        raise ValueError('A positive verified wait is required')
    key = _key(client_id)
    if not isinstance(root,str) or not os.path.isabs(root): raise GateError('storage_unavailable')
    if not all(os.path.lexists(path) for path in (root, os.path.join(root,key+'.lock'),os.path.join(root,key+'.json'))):
        raise ValueError('No uncertain cooldown to recover')
    root = _root(root)
    _ensure_client(root,key,create=False)
    with _lock(root,key) as created:
        state = _read_state(root,key,created)
        boot = _boot_identity()
        allowed = state['status'] in ('in_flight','retry_unknown')
        if state['status'] == 'retry':
            try: _remaining(state,time.time(),time.monotonic(),boot)
            except GateError as exc:
                if exc.reason == 'retry_time_unknown': allowed = True
                else: raise
        if not allowed: raise ValueError('No uncertain cooldown to recover')
        _write_client_state(root,key,{'version':1,'status':'retry','at_wall':time.time(),
                               'at_monotonic':time.monotonic(),'delay':wait_seconds,
                               'source':'verified_recovery','boot':boot})


def run(root, client_id, callback, secret_values=(), *, before_send=None, read_recovery=False):
    """Execute one request or fail closed; never sleep or retry the provider."""
    key = _key(client_id)
    root = _root(root)
    _ensure_client(root,key,create=True)
    with _lock(root, key) as created:
        state = _read_state(root, key, created)
        boot = _boot_identity()
        if state['status'] in ('in_flight', 'retry_unknown'):
            if read_recovery:
                _write_client_state(root,key,{'version':1,'status':'retry','at_wall':time.time(),
                    'at_monotonic':time.monotonic(),'delay':READ_RECOVERY_SECONDS,'source':'verified_recovery','boot':boot})
                raise GateError('busy',READ_RECOVERY_SECONDS)
            raise GateError('retry_time_unknown')
        if state['status'] == 'retry':
            try:
                remaining = _remaining(state, time.time(), time.monotonic(), boot)
            except GateError:
                if not read_recovery: raise
                # Re-anchor the remaining provider deadline to this boot under the same lock.
                remaining = max(0,min(state['delay'],math.ceil(max(state['at_wall']+state['delay']-time.time(),
                                                                   state['delay']-time.monotonic()))))
                _write_client_state(root,key,dict(version=1,status='retry',at_wall=time.time(),
                    at_monotonic=time.monotonic(),delay=remaining,source=state['source'],boot=boot))
            if remaining: raise GateError('cooldown', remaining)
        # Local quotas and report reservations can reject without sending any HTTP.
        # A crash after a successful reservation still leaves its durable job marker.
        if before_send is not None:
            before_send()
        _write_client_state(root, key, {'version': 1, 'status': 'in_flight'})
        try:
            result = callback()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                now_wall, now_mono = time.time(), time.monotonic()
                delay, source = parse_retry_after(exc.headers, now_wall)
                if delay is None and read_recovery and _single_header(exc.headers,'Retry-After') is None:
                    delay, source = READ_RECOVERY_SECONDS, 'verified_recovery'
                # A provider key can be entirely numeric. Do not persist a
                # retry value whose decimal representation reproduces it.
                if delay is not None and any(isinstance(secret,str) and secret and secret in str(delay)
                                             for secret in secret_values):
                    delay, source = None, 'unknown'
                next_state = ({'version':1,'status':'retry','at_wall':now_wall,
                               'at_monotonic':now_mono,'delay':delay,'source':source,'boot':boot}
                              if delay is not None else {'version':1,'status':'retry_unknown'})
                _write_client_state(root, key, next_state)
                exc.retry_after_seconds, exc.retry_after_source = delay, source
            else:
                _write_client_state(root, key, {'version':1,'status':'idle'})
            raise
        except KnownProviderResponseError:
            # The request was not sent, or its status is known not to be 429.
            _write_client_state(root, key, {'version':1,'status':'idle'})
            raise
        except BaseException:
            # A timeout, cancellation or crash may hide a provider response.
            # Retain in_flight, so the next process does not retry blindly.
            raise
        _write_client_state(root, key, {'version': 1, 'status': 'idle'})
        return result
