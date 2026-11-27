"""Versioned, format-validated model configuration for a future n8n reader.

The workflow must be separately migrated and tested before this has any
effect. No provider keys are stored or returned by this module.
"""
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import closing

SLOT = 'content_factory_active_models'
READER_SLOT = 'content_factory_model_reader'
ALLOWED = {'text': frozenset({'gpt-5.4-mini'}),
           'image': frozenset({'models/gemini-3.1-flash-image'})}


def _model_valid(field, value):
    if not isinstance(value,str) or len(value) > 80: return False
    if any(part.lower().startswith(('sk-','aiza')) for part in value.split('/')): return False
    pattern = r'gpt-[A-Za-z0-9][A-Za-z0-9._-]{0,74}' if field == 'text' else r'models/gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,65}'
    return bool(re.fullmatch(pattern,value))


def _valid(record):
    if not isinstance(record, dict) or set(record) != {'revision','models'}: return False
    if type(record['revision']) is not int or not 1 <= record['revision'] <= 2**53-1: return False
    models=record['models']
    return (isinstance(models, dict) and set(models) == set(ALLOWED)
            and all(_model_valid(k,models[k]) for k in ALLOWED))


def read(vault):
    record=vault.read(SLOT)
    if not _valid(record): raise ValueError('Model configuration unavailable')
    return {'revision':record['revision'], 'models':dict(record['models'])}


def compare_and_save(vault, expected_revision, models):
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**53-1:
        raise ValueError('Invalid revision')
    candidate={'revision':expected_revision+1, 'models':models}
    if not _valid(candidate): raise ValueError('Unsupported models')
    with closing(sqlite3.connect(vault.path, timeout=5, isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT blob FROM vault WHERE provider=?', (SLOT,)).fetchone()
        current=0
        if row:
            blob=bytes(row[0])
            previous=json.loads(vault.aead.decrypt(blob[:12],blob[12:],SLOT.encode()))
            if not _valid(previous): raise ValueError('Invalid stored configuration')
            current=previous['revision']
        if current != expected_revision:
            raise ValueError('Configuration changed')
        nonce=secrets.token_bytes(12)
        blob=nonce+vault.aead.encrypt(nonce,json.dumps(candidate,separators=(',', ':')).encode(),SLOT.encode())
        db.execute('INSERT OR REPLACE INTO vault VALUES (?,?)',(SLOT,blob))
        db.commit()
    return candidate['revision']


def create_reader_token(vault):
    """Create once inside the vault; token is never returned or rendered."""
    token=secrets.token_urlsafe(32)
    with closing(sqlite3.connect(vault.path,timeout=5,isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM vault WHERE provider=?',(READER_SLOT,)).fetchone():
            raise ValueError('Reader token already exists')
        nonce=secrets.token_bytes(12)
        blob=nonce+vault.aead.encrypt(nonce,json.dumps({'token':token},separators=(',',':')).encode(),READER_SLOT.encode())
        db.execute('INSERT INTO vault VALUES (?,?)',(READER_SLOT,blob))
        db.commit()


def authenticate(vault, header):
    if not isinstance(header,str) or not header.startswith('Bearer '): return False
    given=header[7:]
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',given): return False
    try:
        saved=vault.read(READER_SLOT)
        token=saved.get('token') if isinstance(saved,dict) else None
        return isinstance(token,str) and hmac.compare_digest(token,given)
    except Exception:
        return False


class Control:
    """Two-phase owner change to the local published configuration only."""
    def __init__(self, vault, fence):
        self.vault, self.fence = vault, fence

    def prepare(self, owner, models, *, acknowledge_estimate=False):
        if not _valid({'revision':1,'models':models}): raise ValueError('Unsupported models')
        if acknowledge_estimate is not True: raise ValueError('Cost and compatibility acknowledgement required')
        current = self.vault.read(SLOT)
        if current is None:
            previous, revision = {'text':'', 'image':''}, 0
        elif _valid(current):
            previous, revision = dict(current['models']), current['revision']
        else: raise ValueError('Invalid stored configuration')
        binding = {'revision':revision,'models':dict(models),'acknowledge_estimate':True}
        proposal = self.fence.prepare(owner,'models','factory-model-config',binding)
        return {'proposal_id':proposal, 'revision':revision,
                'changes':{field:{'before':previous[field],'after':models[field]} for field in ('text','image')}}

    def confirm(self, owner, proposal, revision, models, *, acknowledge_estimate=False):
        if type(revision) is not int or not _valid({'revision':revision+1,'models':models}):
            raise ValueError('Invalid model configuration')
        if acknowledge_estimate is not True: raise ValueError('Cost and compatibility acknowledgement required')
        binding = {'revision':revision,'models':dict(models),'acknowledge_estimate':True}
        def commit():
            saved=compare_and_save(self.vault, revision, models)
            observed=read(self.vault)
            return 'verified' if saved == revision+1 and observed == {'revision':saved,'models':models} else 'unknown'
        return self.fence.dispatch(proposal,owner,binding,commit)


class ReaderProvision:
    """Owner-approved one-time transfer of the reader bearer to n8n."""
    def __init__(self, vault, gateway, transport=None):
        from content_factory_n8n_write import bounded_provision
        self.vault, self.gateway = vault, gateway
        self.transport = bounded_provision if transport is None else transport
        if not callable(self.transport): raise ValueError('Invalid transport')

    def _token(self):
        data=self.vault.read(READER_SLOT)
        token=data.get('token') if isinstance(data,dict) else None
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token) or len(set(token)) < 16:
            raise PermissionError('Reader token unavailable')
        return token

    def prepare(self, owner):
        ident=self.gateway.model_reader_id
        probe=self.gateway.model_reader_probe
        if not self.gateway.ready() or not ident or not callable(probe): raise PermissionError('Gateway unavailable')
        secret=self._token()
        if probe('reader',ident) is not True: raise PermissionError('Isolation unavailable')
        return self.gateway.fence.prepare(owner,'credential','factory-reader',{'id':ident,'token':secret})

    def confirm(self, owner, proposal):
        ident=self.gateway.model_reader_id
        probe=self.gateway.model_reader_probe
        if not ident or not callable(probe): raise PermissionError('Gateway unavailable')
        secret=self._token()
        binding={'id':ident,'token':secret}
        def send():
            if probe('reader',ident) is not True: return 'unknown'
            return self.transport('reader',ident,self.gateway.token_reader(),secret,proposal)
        return self.gateway.fence.dispatch(proposal,owner,binding,send)
