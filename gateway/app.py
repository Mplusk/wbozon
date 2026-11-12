"""Fail-closed HTTP gateway. There are currently no marketplace write adapters."""
import hmac
import os
import re
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .store import GatewayStore

ShortString = Annotated[str, StringConstraints(max_length=256)]
IdString = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    operation: Literal['change_price', 'change_ad', 'publish_listing', 'reply_review', 'print_order']
    item_id: IdString
    # Only primitive parameters are accepted; they are hashed and not stored.
    parameters: dict[ShortString, ShortString | int | float | bool | None] = Field(default_factory=dict, max_length=20)


def _validate_token(value):
    return isinstance(value, str) and 32 <= len(value) <= 256 and value.isascii() and bool(re.fullmatch(r'[!-~]+', value))


def create_app(*, db_path, owner_token, agent_token):
    if not _validate_token(owner_token) or not _validate_token(agent_token) or hmac.compare_digest(owner_token, agent_token):
        raise ValueError('Two distinct gateway tokens of at least 32 printable ASCII characters are required')
    store = GatewayStore(db_path)
    app = FastAPI(title='Action Gateway', docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _exc: RequestValidationError):
        # Default validation errors may echo untrusted values and credentials.
        return JSONResponse(status_code=422, content={'detail': 'Invalid request'})

    def authenticate(authorization):
        supplied = ''
        if isinstance(authorization, str) and authorization.startswith('Bearer '):
            supplied = authorization[7:]
        # Non-ASCII tokens would make compare_digest raise, so reject them.
        if not _validate_token(supplied):
            raise HTTPException(status_code=401, detail='Unauthorized')
        is_owner = hmac.compare_digest(supplied, owner_token)
        is_agent = hmac.compare_digest(supplied, agent_token)
        if is_owner:
            return 'owner'
        if is_agent:
            return 'agent'
        raise HTTPException(status_code=401, detail='Unauthorized')

    @app.get('/v1/status')
    def status(authorization: Annotated[str | None, Header()] = None):
        authenticate(authorization)
        return store.status()

    @app.get('/v1/audit')
    def audit(authorization: Annotated[str | None, Header()] = None,
              after_id: int = 0, limit: int = 100):
        if authenticate(authorization) != 'owner':
            raise HTTPException(status_code=403, detail='Forbidden')
        try:
            return {'events': store.audit(after_id, limit)}
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail='Invalid request') from None

    @app.post('/v1/actions')
    def action(body: Action, authorization: Annotated[str | None, Header()] = None,
               idempotency_key: Annotated[str | None, Header()] = None):
        role = authenticate(authorization)
        try:
            result = store.attempt(idempotency_key, body.operation, body.model_dump(), role)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail='Invalid request') from None
        if result['status'] == 'conflict':
            return JSONResponse(status_code=409, content={'status': 'conflict'})
        return JSONResponse(status_code=403, content=result)

    @app.post('/v1/stop')
    def stop(authorization: Annotated[str | None, Header()] = None,
             idempotency_key: Annotated[str | None, Header()] = None):
        if authenticate(authorization) != 'owner':
            raise HTTPException(status_code=403, detail='Forbidden')
        try:
            result = store.stop_and_probe(idempotency_key)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail='Invalid request') from None
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=503, content={'stopped': False, 'probe_denied': False})
        if not result['stopped'] or not result['probe_denied']:
            return JSONResponse(status_code=503, content={'stopped': False, 'probe_denied': False})
        return result

    return app


def create_from_env():
    """Factory for uvicorn; secrets only from environment, not CLI flags."""
    db = os.environ.get('GATEWAY_DB_PATH', '')
    if not db or not Path(db).is_absolute():
        raise ValueError('GATEWAY_DB_PATH must be an absolute server-side path')
    return create_app(db_path=db, owner_token=os.environ.get('GATEWAY_OWNER_TOKEN'),
                      agent_token=os.environ.get('GATEWAY_AGENT_TOKEN'))
