"""Exercise old/new MCP grants and OAuth across rollback in isolated processes."""
import base64
import hashlib
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


ORIGIN = 'https://3dprinta.shop'
RESOURCE = ORIGIN + '/mcp'
REDIRECT = 'https://claude.ai/api/mcp/auth_callback'
NOW = 1000


def admission(service, work, phase, state):
    sys.path.insert(0, str(service.resolve()))
    from app import LoginApp
    from oauth_mcp import OAuthError
    from portal import Portal, Vault

    login = LoginApp(str(work/'auth.db'), 'x'*32, ORIGIN, clock=lambda:NOW)
    app = Portal(login, Vault(str(work/'vault.db'), b'v'*32), '0'*64)
    keys, oauth = app.external_keys, app.oauth

    def listed(token):
        body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
        env = {'REQUEST_METHOD':'POST', 'PATH_INFO':'/mcp', 'HTTP_HOST':'3dprinta.shop',
               'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),
               'HTTP_ACCEPT':'application/json, text/event-stream','HTTP_AUTHORIZATION':'Bearer '+token,
               'wsgi.input':io.BytesIO(body)}
        statuses = []
        result = json.loads(b''.join(app(env, lambda status,headers:statuses.append(status))))
        if statuses == ['401 Unauthorized']:
            assert result['error'] == 'unauthorized'
            return None
        assert statuses == ['200 OK']
        return {tool['name'] for tool in result['result']['tools']}

    def code_for(key_id):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(state['verifier'].encode()).digest()).rstrip(b'=').decode()
        ticket, nonce, _, _ = oauth.begin(dict(client_id=state['client'], redirect_uri=REDIRECT,
            response_type='code', code_challenge=challenge, code_challenge_method='S256',
            state='fixture', scope='mcp:read mcp:propose' if key_id.startswith('cp_') else 'mcp:read', resource=RESOURCE), RESOURCE)
        return oauth.approve(ticket, nonce, key_id)[0]

    def redeem(code):
        return oauth.exchange(dict(grant_type='authorization_code',client_id=state['client'],code=code,
            redirect_uri=REDIRECT,code_verifier=state['verifier'],resource=RESOURCE), RESOURCE)

    def refresh(tokens):
        return oauth.exchange(dict(grant_type='refresh_token',client_id=state['client'],
                                   refresh_token=tokens['refresh_token']), RESOURCE)

    if phase == 'legacy':
        state['client'] = oauth.register({'redirect_uris':[REDIRECT],'token_endpoint_auth_method':'none',
            'grant_types':['authorization_code','refresh_token']})['client_id']
        state['verifier'] = secrets.token_urlsafe(32)
        key_id, bearer = keys.create('Original',secrets.token_urlsafe(16),NOW)
        state['legacy'] = [key_id,bearer]
        state['old_oauth'] = redeem(code_for(key_id))
        state['legacy_names'] = sorted(listed(bearer))
    legacy = set(state['legacy_names'])
    assert 'ozon_products' in legacy and not any(n.startswith('avito_') for n in legacy)
    assert listed(state['legacy'][1]) == listed(state['old_oauth']['access_token']) == legacy
    if phase == 'issue':
        state['scoped'] = {}
        for access in (('catalog_read','catalog_operations') if state.get('catalog') else ('avito','all')):
            key_id, bearer = keys.create(access,secrets.token_urlsafe(16),NOW,access)
            state['scoped'][access] = [key_id,bearer,redeem(code_for(key_id)),code_for(key_id)]
    for access, (key_id,bearer,tokens,unused) in state.get('scoped',{}).items():
        if phase == 'rollback':
            assert listed(bearer) is None and listed(tokens['access_token']) is None
            for func, arg in ((refresh,tokens),(redeem,unused)):
                try:
                    func(arg)
                except OAuthError as exc:
                    assert exc.code == 'invalid_grant'
                else:
                    raise AssertionError('previous service accepted a scoped grant')
        else:
            if state.get('catalog'):
                import market_catalog
                expected=set(market_catalog.available(access))
            else:
                import avito_mcp
                expected = set(avito_mcp.CONTRACTS) | (legacy if access == 'all' else set())
            assert listed(bearer) == listed(tokens['access_token']) == expected
            if phase == 'resume':
                new = refresh(tokens)
                unspent = redeem(unused)
                assert listed(new['access_token']) == listed(unspent['access_token']) == expected
                assert keys.revoke(key_id,NOW)
                assert listed(new['access_token']) is None and listed(bearer) is None
    return state


def main():
    if len(sys.argv) == 5 and sys.argv[1] == '--child':
        state = json.load(sys.stdin)
        json.dump(admission(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], state), sys.stdout)
        return
    old, candidate = (Path(path).resolve(strict=True) for path in sys.argv[1:3])
    catalog=sys.argv[3:]==['--catalog']
    if len(sys.argv)>3 and not catalog:
        raise ValueError('Invalid arguments')
    with tempfile.TemporaryDirectory() as work:
        state = json.dumps({'catalog':catalog})
        for service, phase in ((old,'legacy'),(candidate,'issue'),(old,'rollback'),(candidate,'resume')):
            command = [sys.executable,'-B','-I',__file__,'--child',str(service),work,phase]
            state = subprocess.run(command,input=state,text=True,stdout=subprocess.PIPE,check=True).stdout
    print('Verified raw keys, OAuth access, refresh, unused codes and revocation across rollback.')


if __name__ == '__main__':
    main()
