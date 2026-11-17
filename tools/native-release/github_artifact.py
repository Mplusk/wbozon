"""Verify a GitHub Actions candidate without trusting the staging account."""
import hashlib
import gzip
import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = 'Mplusk/wbozon'
API = 'https://api.github.com/repos/' + REPO
BOOTSTRAP_HASH = 'f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13'
MAX_ZIP = 40_000_000
MAX_ARCHIVE = 30_000_000
MAX_UNPACKED = 50_000_000
MAX_TAR = 64_000_000
APPROVED_WHEELS = frozenset((
    'c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf',
    '82148ec5bddac30b51a5b3c1945075f896fa022cb93f8e4a01e9f6ee95292c5f',
    'ec400d38950de4dfd418cff8328b2c8faed0edb0d517d3394e457c317908ca4d',
    'd7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c',
    'a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e',
))
REQUIRED_JOBS = frozenset(('Python checks and tests', 'Native release controller tests',
                           'Marketplace egress proxy tests', 'Email auth tests and isolated smoke',
                           'Build and test native portal candidate', 'Deliver tested candidate to server staging'))


class VerificationError(Exception):
    pass


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        if not isinstance(token, str) or not 10 <= len(token) <= 8192 or '\n' in token or '\r' in token:
            raise VerificationError('invalid GitHub credential')
        self._token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, path):
        if not isinstance(path, str) or not path.startswith('/') or '\n' in path or '\r' in path:
            raise VerificationError('invalid GitHub API path')
        return urllib.request.Request(API + path, headers={
            'Authorization': 'Bearer ' + self._token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'wbozon-release-verifier',
        })

    def json(self, path):
        try:
            with self._opener.open(self._request(path), timeout=20) as response:
                raw = response.read(20_000_001)
            if len(raw) > 20_000_000:
                raise VerificationError('GitHub response too large')
            return _json(raw)
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            raise VerificationError('GitHub verification unavailable') from None

    def artifact_zip(self, artifact_id):
        try:
            self._opener.open(self._request('/actions/artifacts/' + artifact_id + '/zip'), timeout=20)
            raise VerificationError('artifact redirect missing')
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise VerificationError('artifact download unavailable') from None
            location = exc.headers.get('Location', '')
        parsed = urllib.parse.urlsplit(location)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 443) or not host.endswith('.blob.core.windows.net')):
            raise VerificationError('untrusted artifact redirect')
        try:
            # Deliberately construct a fresh request without GitHub Authorization.
            request = urllib.request.Request(location, headers={'User-Agent': 'wbozon-release-verifier'})
            with self._opener.open(request, timeout=60) as response:
                raw = response.read(MAX_ZIP + 1)
            if len(raw) > MAX_ZIP:
                raise VerificationError('artifact download too large')
            return raw
        except (OSError, ValueError, urllib.error.URLError):
            raise VerificationError('artifact download unavailable') from None


def _blob_hash(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def verify_request(client, request):
    expected_fields = {'commit', 'run_id', 'build_attempt', 'artifact_id', 'tar_sha256'}
    if not isinstance(request, dict) or set(request) != expected_fields:
        raise VerificationError('invalid deployment request')
    commit, run, attempt, artifact_id, digest = (request[k] for k in
        ('commit', 'run_id', 'build_attempt', 'artifact_id', 'tar_sha256'))
    if (not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit) or
            not isinstance(run, str) or not re.fullmatch('[1-9][0-9]{0,19}', run) or
            not isinstance(attempt, str) or not re.fullmatch('[1-9][0-9]{0,4}', attempt) or
            not isinstance(artifact_id, str) or not re.fullmatch('[1-9][0-9]{0,19}', artifact_id) or
            not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise VerificationError('invalid deployment request')
    run_info = client.json('/actions/runs/' + run)
    repository = run_info.get('repository', {})
    head_repository = run_info.get('head_repository', {})
    if (str(run_info.get('id')) != run or type(repository.get('id')) is not int or
            repository.get('id') != head_repository.get('id') or repository.get('full_name') != REPO or
            run_info.get('event') != 'push' or run_info.get('path') != '.github/workflows/ci.yml' or
            run_info.get('head_branch') != 'main' or run_info.get('head_sha') != commit or
            head_repository.get('full_name') != REPO):
        raise VerificationError('untrusted workflow run')
    jobs = client.json('/actions/runs/' + run + '/attempts/' + attempt + '/jobs?per_page=100')
    if not isinstance(jobs.get('jobs'), list) or jobs.get('total_count') != len(jobs['jobs']):
        raise VerificationError('incomplete workflow jobs')
    found = [j for j in jobs['jobs'] if j.get('name') in REQUIRED_JOBS]
    if (len(found) != len(REQUIRED_JOBS) or {j['name'] for j in found} != REQUIRED_JOBS or
            any(j.get('status') != 'completed' or j.get('conclusion') != 'success' for j in found)):
        raise VerificationError('required workflow checks did not succeed')
    ref = client.json('/git/ref/heads/main')
    if ref.get('object', {}).get('sha') != commit:
        raise VerificationError('commit is not current main')
    meta = client.json('/actions/artifacts/' + artifact_id)
    name = 'native-portal-tested-' + commit + '-' + run + '-' + attempt
    workflow = meta.get('workflow_run', {})
    zip_hash = meta.get('digest', '')
    if (str(meta.get('id')) != artifact_id or meta.get('name') != name or meta.get('expired') is not False or
            str(workflow.get('id')) != run or workflow.get('head_sha') != commit or
            workflow.get('head_branch') != 'main' or
            workflow.get('repository_id') != repository['id'] or
            workflow.get('head_repository_id') != repository['id'] or
            not isinstance(zip_hash, str) or not re.fullmatch('sha256:[0-9a-f]{64}', zip_hash)):
        raise VerificationError('artifact identity mismatch')
    archive_zip = client.artifact_zip(artifact_id)
    if hashlib.sha256(archive_zip).hexdigest() != zip_hash[7:]:
        raise VerificationError('artifact download digest mismatch')
    try:
        with zipfile.ZipFile(io.BytesIO(archive_zip)) as bundle:
            infos = bundle.infolist()
            archive_name = 'native-portal-' + commit + '.tar.gz'
            if (len(infos) != 2 or {x.filename for x in infos} != {archive_name, 'SHA256SUMS'} or
                    any(x.is_dir() or x.file_size > MAX_ARCHIVE for x in infos) or
                    next(x.file_size for x in infos if x.filename == 'SHA256SUMS') > 256):
                raise VerificationError('invalid artifact ZIP inventory')
            archive = bundle.read(archive_name)
            sums = bundle.read('SHA256SUMS')
        if (hashlib.sha256(archive).hexdigest() != digest or
                sums.strip() != (digest + '  ' + archive_name).encode()):
            raise VerificationError('candidate tar digest mismatch')
        files = _read_candidate(archive)
        provenance = _json(files['PROVENANCE.json'])
        if (provenance.get('repository') != REPO or provenance.get('source_commit') != commit or
                str(provenance.get('run_id')) != run or str(provenance.get('run_attempt')) != attempt):
            raise VerificationError('candidate provenance mismatch')
        tree = client.json('/git/trees/' + commit + '?recursive=1')
        if tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
            raise VerificationError('incomplete Git tree')
        tracked = {}
        for item in tree['tree']:
            path = item.get('path')
            if (isinstance(path, str) and path.startswith('services/email-auth/') and
                    item.get('type') != 'tree' and
                    (item.get('type') != 'blob' or item.get('mode') not in ('100644', '100755'))):
                raise VerificationError('untrusted source file type')
            if item.get('type') == 'blob' and item.get('mode') in ('100644', '100755'):
                if path in tracked:
                    raise VerificationError('duplicate Git tree path')
                tracked[path] = item.get('sha')
        service = {p[len('services/email-auth/'):]: h for p, h in tracked.items()
                   if isinstance(p, str) and p.startswith('services/email-auth/')}
        candidate = {p[len('service/'):]: _blob_hash(value) for p, value in files.items() if p.startswith('service/')}
        if not service or service != candidate:
            raise VerificationError('candidate source differs from trusted commit')
        for name in ('requirements-offline.lock', 'requirements-bootstrap.lock', 'test_native.py'):
            if tracked.get('tools/native-test-bundle/' + name) != _blob_hash(files[name]):
                raise VerificationError('candidate build inputs differ from trusted commit')
        verify_wheels(files)
        return files
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, tarfile.TarError):
        raise VerificationError('invalid candidate artifact') from None


def _read_candidate(archive):
    files, seen = {}, set()
    with gzip.GzipFile(fileobj=io.BytesIO(archive), mode='rb') as decompressor:
        raw_tar = decompressor.read(MAX_TAR + 1)
    if len(raw_tar) > MAX_TAR:
        raise VerificationError('candidate archive limits exceeded')
    total = count = 0
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode='r:') as bundle:
        for member in bundle:
            count += 1
            total += member.size
            if count > 500 or total > MAX_UNPACKED:
                raise VerificationError('candidate archive limits exceeded')
            name = member.name[2:] if member.name.startswith('./') else member.name
            name = name.rstrip('/')
            if name in ('', '.'):
                continue
            if (not re.fullmatch('[A-Za-z0-9_./-]+', name) or
                    any(x in ('', '.', '..') for x in name.split('/')) or name in seen or
                    member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or
                    member.sparse is not None or not 0 <= member.size <= MAX_ARCHIVE):
                raise VerificationError('unsafe candidate archive')
            seen.add(name)
            if member.isfile():
                files[name] = bundle.extractfile(member).read()
    if not count:
        raise VerificationError('empty candidate archive')
    lines = files.pop('FILES.sha256').decode('ascii').splitlines()
    inventory = {}
    for line in lines:
        digest, name = line.split('  ', 1)
        if not re.fullmatch('[0-9a-f]{64}', digest) or name in inventory:
            raise VerificationError('invalid candidate inventory')
        inventory[name] = digest
    if set(inventory) != set(files) or any(hashlib.sha256(files[n]).hexdigest() != h for n, h in inventory.items()):
        raise VerificationError('candidate inventory mismatch')
    allowed = {'PROVENANCE.json', 'requirements-offline.lock', 'requirements-bootstrap.lock', 'test_native.py'}
    for name in files:
        if not (name in allowed or name.startswith('service/') or
                re.fullmatch(r'wheelhouse/[A-Za-z0-9_.-]+\.whl', name) or
                re.fullmatch(r'bootstrap/[A-Za-z0-9_.-]+\.whl', name)):
            raise VerificationError('unexpected candidate file')
    return files


def verify_wheels(files):
    expected = set()
    text = files['requirements-offline.lock'].decode('ascii')
    for line in text.splitlines():
        match = re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+ --hash=sha256:([0-9a-f]{64})', line)
        if not match or match[1] in expected:
            raise VerificationError('unsafe offline requirements')
        expected.add(match[1])
    actual = [hashlib.sha256(data).hexdigest() for name, data in files.items() if name.startswith('wheelhouse/')]
    if expected != APPROVED_WHEELS or len(actual) != len(expected) or set(actual) != expected:
        raise VerificationError('wheelhouse digest mismatch')
    if {name for name in files if name.startswith('bootstrap/')} != {'bootstrap/pip-26.3-py3-none-any.whl'}:
        raise VerificationError('invalid bootstrap inventory')
    if hashlib.sha256(files['bootstrap/pip-26.3-py3-none-any.whl']).hexdigest() != BOOTSTRAP_HASH:
        raise VerificationError('untrusted pip bootstrap')
