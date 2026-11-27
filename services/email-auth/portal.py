"""Local integration prototype. Browser sessions never authenticate the read API."""
import contextvars
import market_cooldown
import hashlib, json, math, os, re, sys, urllib.request, urllib.error
import threading, time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import finance_gate
import market_specs
from market_https import VerifiedHTTPS
import market_diagnostics

# Provider children are fresh interpreters with a single request and a hard
# deadline. Load their marketplace implementation only when dispatching it.
_worker_cli = __name__ == '__main__' and sys.argv[1:2] == ['--provider-worker']
if not _worker_cli:
    import hmac, html, secrets, sqlite3, ssl, subprocess
    from urllib.parse import parse_qs, urlsplit
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from ui import document, CSP
    from app import LoginApp, smtp_mailer, send_login_email
    import ozon_mcp, ozon_details, ozon_article_summary, wb_mcp, avito_mcp
    import external_keys, external_mcp, oauth_mcp, market_actions, market_catalog, ozon_review_reply
    import expansion_gateway, market_health, market_history, market_archive, posting_discovery
    import cost_catalog, cost_integration

FIELDS = {'ozon': ('client_id', 'api_key'), 'wb': ('api_token',)}
CHECK_OPERATIONS = {'ozon':'ozon_products', 'wb':'wb_prices', 'avito':'avito_get_user_info_self'}
CHECK_PROVIDER_LIMIT, CHECK_PROVIDER_WINDOW = 1, 60
CHECK_GLOBAL_LIMIT, CHECK_GLOBAL_WINDOW = 10, 3600
CHECK_MESSAGES = {
    'ok':'Проверка чтения данных выполнена успешно.',
    'denied':'Маркетплейс отклонил доступ. Проверьте ключ, срок действия и права чтения.',
    'rate_limited':'Маркетплейс временно ограничил запросы. Повторите проверку позже.',
    'unavailable':'Маркетплейс временно недоступен. Повторите проверку позже.',
    'failed':'Не удалось подтвердить соединение. Проверьте настройки и повторите позже.',
}
# Stored in SQLite and read by the previous release after a rollback.
BRIDGE_OPERATIONS = {'unknown':'операция не определена', 'ozon_products':'товары Ozon',
    'ozon_prices':'цены Ozon', 'ozon_orders_analytics':'заказы Ozon', 'ozon_posting_status':'статусы отправлений Ozon', 'wb_products':'товары Wildberries',
    'ozon_product_info':'идентификаторы товаров Ozon', 'ozon_accrual_types':'виды начислений Ozon',
    'ozon_posting_accruals':'начисления по отправлениям Ozon',
    'ozon_article_summary':'частичная сводка артикула Ozon',
    'ozon_posting_candidates':'номера отправлений Ozon',
    'ozon_realization_article_month':'месячный отчёт реализации артикула Ozon',
    'ozon_accrual_day':'начисления Ozon за день по SKU',
    'ozon_return_events':'события возвратов Ozon',
    'wb_prices':'цены Wildberries', 'wb_commissions':'комиссии Wildberries',
    'wb_box_tariffs':'тарифы Wildberries', 'wb_sales_summary':'заказы и выкупы Wildberries'}
OAUTH_PATHS = frozenset(('/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-authorization-server',
    '/oauth/register', '/oauth/authorize', '/oauth/token'))
BRIDGE_OUTCOMES = {'ok':'данные получены', 'not_configured':'ключ не добавлен', 'failed':'не удалось получить данные', 'rate_limited':'превышен лимит запросов'}

def public_retry_delay(delay, creds):
    """Do not reproduce a seller secret through a numeric retry interval."""
    if type(delay) is not int or not 0 <= delay <= finance_gate.MAX_RETRY_SECONDS:
        return None
    if isinstance(creds,dict) and any(public_retry_delay(delay,value) is None for value in creds.values() if isinstance(value,dict)):
        return None
    for field in ('api_key','api_token','client_secret','digital_token'):
        secret=creds.get(field) if isinstance(creds,dict) else None
        if isinstance(secret,str) and secret and secret in str(delay):
            return None
    return delay


def finance_request_preflight(operation, args, creds):
    """Reject local input errors before reserving a finance request."""
    import ozon_mcp, ozon_details, ozon_article_summary, posting_discovery
    if operation not in finance_gate.FINANCE_OPERATIONS or not isinstance(creds,dict):
        raise ValueError('Invalid financial request')
    client_id, api_key = creds.get('client_id'), creds.get('api_key')
    if (not isinstance(client_id,str) or not re.fullmatch(r'[0-9]{1,32}',client_id) or
            not isinstance(api_key,str) or not re.fullmatch(r'[!-~]{1,2048}',api_key)):
        raise ValueError('Invalid financial credentials')
    if operation in ('ozon_article_summary','ozon_posting_accruals') and posting_discovery.active(args):
        posting_discovery.validate(operation,args)
        ozon_details._text(args['offer_id'],256,secret=api_key)
    elif operation == 'ozon_cash_flow':
        market_specs.validate(operation,args)
    elif operation == ozon_article_summary.OPERATION:
        ozon_article_summary.validate_args(args)
        ozon_details.build_request('ozon_product_info',{'offer_ids':[args['offer_id']]},creds)
        ozon_details.build_request('ozon_posting_accruals',{
            'posting_numbers':args['posting_numbers'],'date_from':args['date_from'],
            'date_to':args['date_to'],'view':'summary'},creds)
    else:
        if operation == 'ozon_accrual_day' and isinstance(args,dict):
            if 'posting_number' in args:
                ozon_details._text(args['posting_number'],128,secret=api_key)
            if 'last_id' in args:
                ozon_details._text(args['last_id'],512,empty=True,secret=api_key)
        if operation == 'ozon_return_events' and isinstance(args,dict):
            ozon_details._text(args.get('offer_id'),256,secret=api_key)
            if 'last_id' in args: ozon_details._id(args['last_id'],api_key)
        ozon_mcp.build_request(operation,args,creds)


class Vault:
    def __init__(self, path, key):
        if len(key) != 32: raise ValueError('32-byte vault key required')
        self.path, self.aead = path, AESGCM(key)
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS connection_checks(provider TEXT PRIMARY KEY, version TEXT NOT NULL, outcome TEXT NOT NULL, checked_at REAL NOT NULL)')
            c.execute("CREATE TABLE IF NOT EXISTS bridge_activity(source TEXT PRIMARY KEY CHECK(source IN ('bridge','mcp')), operation TEXT NOT NULL, outcome TEXT NOT NULL, completed_at REAL NOT NULL)")
    def save(self, provider, values):
        nonce = secrets.token_bytes(12)
        blob = nonce + self.aead.encrypt(nonce, json.dumps(values).encode(), provider.encode())
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', (provider, blob))
            c.execute('DELETE FROM connection_checks WHERE provider=?',(provider,))
    def snapshot(self, provider):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row = c.execute('SELECT blob FROM vault WHERE provider=?', (provider,)).fetchone()
        if not row: return None
        blob = bytes(row[0])
        values=json.loads(self.aead.decrypt(blob[:12], blob[12:], provider.encode()))
        return values, hashlib.sha256(blob).hexdigest()
    def read(self, provider):
        snapshot=self.snapshot(provider)
        return snapshot[0] if snapshot else None
    def record_check(self, provider, version, outcome, checked_at):
        if outcome not in CHECK_MESSAGES: raise ValueError('Invalid outcome')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
            if not row or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version): return False
            c.execute('INSERT OR REPLACE INTO connection_checks VALUES (?,?,?,?)',(provider,version,outcome,checked_at))
        return True
    def version_matches(self, provider, version):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
        return bool(row) and hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version)
    def check_status(self, provider):
        # Read storage metadata only; rendering never decrypts credentials.
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT v.blob,c.version,c.outcome,c.checked_at FROM vault v JOIN connection_checks c ON v.provider=c.provider WHERE v.provider=?',(provider,)).fetchone()
        if not row or row[2] not in CHECK_MESSAGES or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),row[1]): return None
        return row[2],row[3]
    def present(self):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            return {r[0] for r in c.execute('SELECT provider FROM vault')}
    def record_bridge_activity(self, source, operation, outcome, completed_at):
        if source not in ('bridge','mcp') or operation not in BRIDGE_OPERATIONS or outcome not in BRIDGE_OUTCOMES: raise ValueError('Invalid bridge activity')
        # A delayed writer must not overwrite a more recently completed request.
        with sqlite3.connect(self.path, timeout=0.5) as c:
            c.execute('INSERT INTO bridge_activity VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET operation=excluded.operation, outcome=excluded.outcome, completed_at=excluded.completed_at WHERE excluded.completed_at >= bridge_activity.completed_at', (source,operation,outcome,completed_at))
    def bridge_activity(self, source):
        if source not in ('bridge','mcp'): raise ValueError('Invalid bridge source')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT operation,outcome,completed_at FROM bridge_activity WHERE source=?',(source,)).fetchone()
        if row and (row[0] not in BRIDGE_OPERATIONS or row[1] not in BRIDGE_OUTCOMES): raise ValueError('Invalid bridge activity')
        return row

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def decode_provider_payload(raw, preserve_decimal=False, reject_duplicates=False):
    """Reject non-JSON constants and numeric overflow before normalizing data."""
    def reject_constant(value): raise ValueError('Invalid provider JSON number')
    def finite_float(value):
        if preserve_decimal:
            # Restrict both precision and exponent before constructing/summing a
            # provider-controlled decimal. Other operations keep their old types.
            if len(value) > 80: raise ValueError('Invalid provider JSON number')
            try: number=Decimal(value)
            except InvalidOperation: raise ValueError('Invalid provider JSON number') from None
            if (not number.is_finite() or len(number.as_tuple().digits) > 80
                    or abs(number.as_tuple().exponent) > 80 or abs(number.adjusted()) > 80):
                raise ValueError('Invalid provider JSON number')
            return number
        number=float(value)
        if not math.isfinite(number): raise ValueError('Invalid provider JSON number')
        return number
    def unique_pairs(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('Duplicate provider JSON key')
            result[key]=value
        return result
    payload=json.loads(raw,parse_constant=reject_constant,parse_float=finite_float,
                       object_pairs_hook=unique_pairs if reject_duplicates else None)
    if not isinstance(payload,dict): raise ValueError('Invalid provider response')
    return payload

def _provider_read(operation, args, creds, *, request_timeout=15, absolute_deadline=None):
    """Bounded, read-only provider requests, within one deadline worker."""
    started=time.monotonic()
    if operation.startswith('wb_'):
        import wb_mcp
        return wb_mcp.read(operation, args, creds)
    import ozon_mcp, ozon_details, ozon_article_summary
    if operation not in ozon_mcp.OPERATIONS:
        raise ValueError('Unknown operation')
    if operation == ozon_mcp.ozon_supply_allocation.OPERATION:
        deadline = started + PROVIDER_DEADLINE_SECONDS - 2 if absolute_deadline is None else absolute_deadline
        def read_supply(name, arguments):
            return _provider_read(name, arguments, creds, request_timeout=request_timeout, absolute_deadline=deadline)
        result = ozon_mcp.ozon_supply_allocation.calculate(args, read_supply)
        return _guard_ozon_result(result, creds)
    if operation == ozon_article_summary.OPERATION:
        deadline=(started+max(0,PROVIDER_DEADLINE_SECONDS-2)
                  if absolute_deadline is None else absolute_deadline)
        return _read_article_summary(args,creds,deadline)
    try:
        req=ozon_mcp.build_request(operation,args,creds)
        opener=urllib.request.build_opener(NoRedirect, VerifiedHTTPS)
    except Exception:
        raise finance_gate.LocalPreparationError('Provider request could not be prepared') from None
    if absolute_deadline is not None:
        remaining=absolute_deadline-time.monotonic()
        if remaining <= 0.2:
            raise finance_gate.LocalPreparationTimeout('Summary deadline reached before request')
        request_timeout=min(request_timeout,remaining)
    with market_cooldown.legacy_open(opener, req, request_timeout) as resp:
        try:
            raw=resp.read(512001)
            if len(raw)>512000: raise finance_gate.ProviderResponseTooLarge('Provider response too large')
            data=decode_provider_payload(raw,preserve_decimal=operation in ('ozon_orders_analytics','ozon_realization_article_month',ozon_mcp.ozon_fbo.BUNDLE),
                                         reject_duplicates=operation in ('ozon_products','ozon_posting_status','ozon_posting_candidates','ozon_realization_article_month','ozon_accrual_day','ozon_return_events',ozon_mcp.ozon_unallocated.OPERATION) or operation in ozon_mcp.ozon_details.OPERATIONS or operation in ozon_mcp.ozon_fbo.ENDPOINTS)
        except (finance_gate.ProviderResponseTooLarge, urllib.error.HTTPError): raise
        except TimeoutError: raise finance_gate.ProviderBodyTimeout('Provider response timed out') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason,TimeoutError): raise finance_gate.ProviderBodyTimeout('Provider response timed out') from None
            raise finance_gate.ProviderResponseInvalid('Provider response could not be read') from None
        except ValueError as exc:
            message='Duplicate provider JSON key' if str(exc)=='Duplicate provider JSON key' else 'Invalid provider response'
            raise finance_gate.ProviderResponseInvalid(message) from None
        except Exception: raise finance_gate.ProviderResponseInvalid('Invalid provider response') from None
    try: result=ozon_mcp.project_response(operation,args,data,creds)
    except Exception: raise finance_gate.ProviderResponseInvalid('Invalid provider response') from None
    if operation == ozon_mcp.ozon_unallocated.OPERATION and args.get('include_types', True):
        deadline = started + PROVIDER_DEADLINE_SECONDS - 2 if absolute_deadline is None else absolute_deadline
        try:
            types = _provider_read('ozon_accrual_types', {}, creds, request_timeout=request_timeout, absolute_deadline=deadline)
        except urllib.error.HTTPError as exc:
            if exc.code != 403:
                raise
            exc.close()
            result['type_dictionary_status'] = 'provider_access_denied'
        else:
            result = ozon_mcp.ozon_unallocated.attach_types(result, types)
    if operation=='ozon_products':
        result['stock_tool']='ozon_stocks'
    if operation=='ozon_products' and args.get('include_identity',True):
        # A single page is enriched by product ID and exact offer_id; neither
        # SKU ordering nor a successful catalog lookup proves historical coverage.
        try: enrichment_args=ozon_mcp.product_enrichment_request(result)
        except Exception: raise finance_gate.ProviderResponseInvalid('Invalid product page') from None
        identity_error=None
        if enrichment_args is None:
            info={'items':[]}
        else:
            deadline=(started+max(0,PROVIDER_DEADLINE_SECONDS-2)
                      if absolute_deadline is None else absolute_deadline)
            try:
                info=_provider_read('ozon_product_info',enrichment_args,creds,
                                    request_timeout=request_timeout,absolute_deadline=deadline)
            except urllib.error.HTTPError as exc:
                if exc.code not in (401,403): raise
                identity_error='provider_access_denied'
                exc.close()
                info={'items':[]}
        try: result=ozon_mcp.enrich_product_page(result,info)
        except Exception: raise finance_gate.ProviderResponseInvalid('Invalid product identity response') from None
        if identity_error:result['identity_enrichment']['error']=identity_error
    if operation=='ozon_posting_accruals' and args.get('include_types',True):
        deadline=(started+max(0,PROVIDER_DEADLINE_SECONDS-2)
                  if absolute_deadline is None else absolute_deadline)
        types=_provider_read('ozon_accrual_types',{},creds,
                             request_timeout=request_timeout,absolute_deadline=deadline)
        try: result=ozon_details.label_posting_accruals(result,types)
        except Exception: raise finance_gate.ProviderResponseInvalid('Invalid accrual type response') from None
    if operation=='ozon_orders_analytics':
        # Seller info is optional. Use a daemon thread with a hard wait bound:
        # DNS resolution may ignore urllib's socket timeout. Never let an
        # unavailable profile discard an otherwise valid analytics response.
        deadline = started + PROVIDER_DEADLINE_SECONDS
        if absolute_deadline is not None:
            deadline = min(deadline, absolute_deadline)
        remaining = min(3.0, deadline - time.monotonic() - 1.5)
        if remaining>0:
            found=[]
            def read_company_currency():
                try:
                    profile=ozon_mcp.build_seller_info_request(creds)
                    profile_opener = urllib.request.build_opener(NoRedirect, VerifiedHTTPS)
                    with market_cooldown.legacy_open(profile_opener, profile, remaining) as response:
                        raw_profile=response.read(65537)
                    if len(raw_profile)>65536: return
                    currency=ozon_mcp.project_company_currency(decode_provider_payload(raw_profile),creds)
                    if currency: found.append(currency)
                except urllib.error.HTTPError as exc:
                    exc.close()
                except Exception:
                    # This optional lookup never logs an upstream error or credentials.
                    pass
            worker=threading.Thread(target=contextvars.copy_context().run,args=(read_company_currency,),daemon=True)
            worker.start(); worker.join(remaining)
            if not worker.is_alive() and found:
                result['company_currency']=found[0]
                result['company_currency_source']='Ozon Seller API /v1/seller/info company.currency; company currency, not a guaranteed analytics currency.'
    # The provider key accepts arbitrary printable ASCII. Numeric fields,
    # derived totals, cursors or adjacent JSON values can reproduce it even
    # when individual text fields were redacted. Fail closed rather than
    # altering an amount or cursor. Client ID is an identifier, not this key.
    return _guard_ozon_result(result,creds)


def _guard_ozon_result(result,creds):
    key=creds.get('api_key')
    if isinstance(key,str) and key:
        escaped_key=json.dumps(key,ensure_ascii=True)[1:-1]
        try:
            variants=(json.dumps(result,ensure_ascii=True,allow_nan=False),
                      json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')))
        except (ValueError,TypeError): raise finance_gate.ProviderResponseInvalid('Invalid provider response') from None
        if any(key in value or escaped_key in value for value in variants):
            raise finance_gate.ProviderResponseInvalid('Unsafe provider response')
    return result


def _read_article_summary(args,creds,deadline):
    """Sequential bounded reads; a supplied posting set is partial."""
    import ozon_article_summary
    ozon_article_summary.validate_args(args)
    def read(operation,arguments):
        remaining=deadline-time.monotonic()
        if remaining <= 0.2:
            raise finance_gate.LocalPreparationTimeout('Summary deadline reached before request')
        return _provider_read(operation,arguments,creds,request_timeout=min(15,remaining),
                              absolute_deadline=deadline)
    forward=read('ozon_product_info',{'offer_ids':[args['offer_id']]})
    try: reverse_args,blocked=ozon_article_summary.prepare_identity(args,forward)
    except Exception: raise finance_gate.ProviderResponseInvalid('Invalid identity projection') from None
    if blocked is not None:
        blocked.update(observed_at=datetime.now(timezone.utc).isoformat(),
                       sources=['Ozon Seller API /v3/product/info/list'])
        return _guard_ozon_result(blocked,creds)
    reverse=read('ozon_product_info',reverse_args)
    try: identity,blocked=ozon_article_summary.confirm_identity(args,forward,reverse)
    except Exception: raise finance_gate.ProviderResponseInvalid('Invalid identity projection') from None
    if blocked is not None:
        blocked.update(observed_at=datetime.now(timezone.utc).isoformat(),
                       sources=['Ozon Seller API /v3/product/info/list'])
        return _guard_ozon_result(blocked,creds)
    try: posting_args=ozon_article_summary.accrual_args(args,identity)
    except Exception: raise finance_gate.ProviderResponseInvalid('Invalid identity projection') from None
    accruals=read('ozon_posting_accruals',posting_args)
    try: result=ozon_article_summary.finish(args,identity,accruals,datetime.now(timezone.utc).isoformat())
    except Exception: raise finance_gate.ProviderResponseInvalid('Invalid accrual projection') from None
    return _guard_ozon_result(result,creds)

PROVIDER_DEADLINE_SECONDS = 20
PROVIDER_STARTUP_SECONDS = 30
FAST_READ_TOTAL_SECONDS = 24  # Reserve time for the enclosing 30-second HTTP worker.
REPORT_WORKERS = (frozenset(market_specs.TOOLS) - finance_gate.FINANCE_OPERATIONS - {'mcp_health'}) | {'archive_market_day'}
PREPARED_OPERATIONS = REPORT_WORKERS | frozenset(('market_api_read','market_api_propose','ozon_products',
    'ozon_prices','wb_products','wb_prices','wb_commissions','wb_box_tariffs'))
PROVIDER_WORKER = os.path.realpath(__file__)

def _prepare_provider_worker(operation):
    # No request body or credential has been sent at this point. Use exactly
    # the same TLS context and module cache as the subsequent real request.
    if operation.startswith('market_api_'):
        import market_catalog
        if operation == 'market_api_propose':
            import market_actions
    elif operation in REPORT_WORKERS:
        # Only code, trust roots and the cache cipher are loaded before readiness.
        # No vault, database, credentials or outbound request is needed here.
        import expansion_gateway
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if _worker_cli:
            # Composite finance calls reuse the read functions in this worker
            # instead of importing the web application a second time.
            sys.modules.setdefault('portal', sys.modules[__name__])
        if operation == 'archive_market_day':
            import market_archive
    elif operation.startswith('ozon_'):
        import ozon_mcp
    else:
        import wb_mcp
    import market_transport  # Also used by the allowlisted request diagnostics.
    VerifiedHTTPS()

def provider_read(operation, args, creds, finance_gate_dir=None, *, dispatch=None, report_mappings=None, report_deadline=None):
    """Bound the entire provider operation, including DNS and response reads.

    Credentials go over a private stdin pipe, never argv or the environment.
    Killing the subprocess on timeout also stops a stuck resolver/socket call.
    """
    started=time.monotonic()
    prepare = (os.name == 'posix' and operation in PREPARED_OPERATIONS
               and os.environ.get('MCP_PREPARE_MARKET_WORKERS') == '1')
    # A resumed read is observed through its task receipt. Its cold startup
    # and execution do not consume the external client's HTTP deadline.
    # A previous public job can still be resumed after a view moves to the fast path.
    managed_report = report_mappings is not None
    startup_seconds = 75 if managed_report else PROVIDER_STARTUP_SECONDS
    execution_seconds = 45 if managed_report else PROVIDER_DEADLINE_SECONDS
    hard_deadline=started+execution_seconds+(startup_seconds if prepare else 0)
    if operation in market_specs.FAST_SELLER_READS and not managed_report:
        hard_deadline=min(hard_deadline,started+FAST_READ_TOTAL_SECONDS)
    if report_deadline is not None:
        hard_deadline=min(hard_deadline,report_deadline)
    if finance_gate_dir is None and operation not in finance_gate.FINANCE_OPERATIONS:
        finance_gate_dir = os.environ.get('OZON_FINANCE_GATE_DIR')
        if finance_gate_dir is None and os.environ.get('VAULT_PATH'):
            finance_gate_dir = os.path.join(os.path.dirname(os.path.abspath(os.environ['VAULT_PATH'])), '.ozon-finance-gates')
    if operation in finance_gate.FINANCE_OPERATIONS and (not isinstance(finance_gate_dir,str) or not os.path.isabs(finance_gate_dir)):
        raise finance_gate.GateError('storage_unavailable')
    if operation in finance_gate.FINANCE_OPERATIONS:
        finance_request_preflight(operation,args,creds)
    env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
    # Preserve standard outbound network settings; never pass unrelated app secrets.
    for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HTTPS_PROXY', 'HTTP_PROXY',
                 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy',
                 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
        if name in os.environ: env[name] = os.environ[name]
    if time.monotonic()>=hard_deadline:
        raise market_diagnostics.timeout(operation,time.monotonic()-started,None)
    command=[sys.executable, PROVIDER_WORKER, '--provider-worker']
    if prepare:
        command.append(operation)
    try:
        with subprocess.Popen(command, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                if prepare:
                    import select
                    remaining_startup=min(started+startup_seconds,hard_deadline)-time.monotonic()
                    if remaining_startup<=0 or not select.select([process.stdout],[],[],remaining_startup)[0]:
                        raise subprocess.TimeoutExpired(process.args,startup_seconds)
                    if os.read(process.stdout.fileno(),1) != b'R':
                        raise finance_gate.LocalPreparationError('Marketplace worker could not prepare')
                    ready_at=time.monotonic()
                    if ready_at>=started+startup_seconds:
                        raise subprocess.TimeoutExpired(process.args,startup_seconds)
                    hard_deadline=min(hard_deadline,ready_at+execution_seconds)
                deadline=hard_deadline-min(2,PROVIDER_DEADLINE_SECONDS)
                envelope={'operation':operation, 'args':args, 'creds':creds,
                          'finance_gate_dir':finance_gate_dir,'deadline_monotonic':deadline,'dispatch':dispatch}
                if managed_report:
                    envelope['report_mappings'] = report_mappings
                payload=json.dumps(envelope).encode()
                remaining=hard_deadline-time.monotonic()
                if remaining<=0:
                    # A delayed launch must never receive credentials or dispatch work after expiry.
                    raise subprocess.TimeoutExpired(process.args,PROVIDER_DEADLINE_SECONDS)
                stdout,stderr=process.communicate(payload,timeout=remaining)
            except BaseException:
                process.kill()
                raise
    except subprocess.TimeoutExpired as exc:
        raise market_diagnostics.timeout(operation,time.monotonic()-started,exc.stderr) from None
    if process.returncode:
        raise urllib.error.URLError('Marketplace worker failed')
    try:
        result=json.loads(stdout)
        if result.get('kind') == 'http' and type(result.get('status')) is int:
            delay = result.get('retry_after_seconds')
            if type(delay) is not int or not 0 <= delay <= finance_gate.MAX_RETRY_SECONDS: delay = None
            headers = {'Retry-After':str(delay)} if delay is not None and result['status'] == 429 else {}
            error = urllib.error.HTTPError('', result['status'], 'Marketplace request failed', headers, None)
            error.retry_after_seconds = delay
            error.retry_after_source = result.get('retry_after_source') if result.get('retry_after_source') in ('delta_seconds','http_date') else 'unknown'
            raise error
        if result.get('kind') == 'gate':
            reason = result.get('reason')
            if reason not in ('busy','cooldown','retry_time_unknown','storage_unavailable'): reason = 'storage_unavailable'
            delay = result.get('retry_after_seconds')
            if type(delay) is not int or not 0 <= delay <= finance_gate.MAX_RETRY_SECONDS: delay = None
            raise finance_gate.GateError(reason, delay)
        if result.get('kind') == 'provider_response_too_large':
            raise finance_gate.ProviderResponseTooLarge('Provider response too large')
        if result.get('kind') == 'provider_response_invalid':
            raise finance_gate.ProviderResponseInvalid('Invalid provider response')
        if result.get('kind') == 'local_preparation_failed':
            raise finance_gate.LocalPreparationError('Provider request could not be prepared')
        if result.get('kind') == 'timeout': raise market_diagnostics.timeout(operation,time.monotonic()-started,stderr)
        if result.get('kind') == 'unavailable': raise urllib.error.URLError('Marketplace unavailable')
        if result.get('kind') != 'ok' or not isinstance(result.get('result'),dict):
            raise ValueError('Marketplace response failed')
        return result['result']
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        raise ValueError('Invalid marketplace worker response') from None

def _finance_read_with_retry(root, creds, read, deadline, *, allow_retry=True):
    """One bounded retry only after a known short 429 and the shared cooldown."""
    def attempt():
        return finance_gate.run(root,creds.get('client_id'),read,(creds.get('api_key'),),read_recovery=True)
    try:
        return attempt()
    except urllib.error.HTTPError as exc:
        delay=getattr(exc,'retry_after_seconds',None)
        source=getattr(exc,'retry_after_source',None)
        wait=max(1,delay) if type(delay) is int else None
        if (not allow_retry or exc.code != 429 or source not in ('delta_seconds','http_date') or
                wait is None or wait > 2 or deadline-time.monotonic() < wait+3):
            raise
        original_error=exc
        exc.close()
    # Do not hold the shared lock while waiting; the second attempt must
    # acquire the same gate, which preserves provider and concurrency limits.
    time.sleep(wait)
    if deadline-time.monotonic() < 3:
        raise original_error
    return attempt()


def _provider_worker():
    """Private child entry point; exceptions and upstream bodies are not logged."""
    try:
        request=json.load(sys.stdin)
        market_diagnostics.enable()
        if request['operation'] in finance_gate.FINANCE_OPERATIONS:
            finance_request_preflight(request['operation'],request['args'],request['creds'])
        is_finance=request['operation'] in finance_gate.FINANCE_OPERATIONS
        if is_finance:
            import ozon_article_summary, ozon_unallocated
            deadline=request.get('deadline_monotonic')
            if type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline < 0:
                raise ValueError('Invalid provider deadline')
        def read():
            if request['operation'] in ('market_api_read','market_api_propose'):
                from contextlib import nullcontext
                import market_catalog
                gate=nullcontext()
                if request['operation']=='market_api_propose':
                    import market_actions
                    gate=market_actions.transmission(request.get('dispatch'),request['args'])
                with gate:
                    return market_catalog.transfer(request['operation'],request['args'],request['creds'],
                                                   request['finance_gate_dir'],request['deadline_monotonic'])
            if request['operation'].startswith('avito_'):
                import avito_mcp
                return avito_mcp.read(request['operation'], request['args'], request['creds'],
                                      request['finance_gate_dir'], request['deadline_monotonic'])
            if (request['operation']=='wb_sales_summary' and 'group_by' in request['args'] or
                    request['operation']=='wb_prices' and 'vendor_code' in request['args']):
                import expansion_gateway, wb_legacy_reports, wb_mcp
                wire=expansion_gateway.make_wire('wb',request['creds'],request['finance_gate_dir'],request['deadline_monotonic'])
                fn=wb_legacy_reports.sales if request['operation']=='wb_sales_summary' else wb_legacy_reports.prices
                return fn(wb_mcp.validate_args(request['operation'],request['args']),wire)
            if request['operation']=='archive_market_day':
                import market_archive
                return market_archive.tick(request['args']['dataset'],request['creds'],request['finance_gate_dir'],request['deadline_monotonic'])
            if request['operation']=='ozon_orders_analytics' and set(request['args'])!={'date_from','date_to'}:
                import expansion_gateway
                return expansion_gateway.execute('ozon_sku_daily',{k:v for k,v in request['args'].items() if k!='dimension'},
                    request['creds'],request['finance_gate_dir'],request['deadline_monotonic'])
            if request['operation'] in market_specs.TOOLS:
                import expansion_gateway
                snapshot = request.get('report_mappings')
                if snapshot is not None and (request['operation'] not in (market_specs.RESUMABLE_READS | market_specs.FAST_SELLER_READS) or
                                             not isinstance(snapshot, dict)):
                    raise ValueError('Invalid report mapping')
                token = expansion_gateway.request_seller_mappings.set(snapshot)
                try:
                    return expansion_gateway.execute(request['operation'],request['args'],request['creds'],
                        request['finance_gate_dir'],request['deadline_monotonic'])
                finally:
                    expansion_gateway.request_seller_mappings.reset(token)
            with market_cooldown.worker(request.get('finance_gate_dir'), request['creds']):
                if request['operation'] in ('ozon_article_summary','ozon_posting_accruals'):
                    import posting_discovery
                    if posting_discovery.active(request['args']):
                        import expansion_gateway
                        wire=expansion_gateway.Wire('ozon',request['creds'],request['finance_gate_dir'],deadline)
                        return posting_discovery.read(request['operation'],request['args'],wire,
                            lambda name,args:_provider_read(name,args,request['creds'],absolute_deadline=deadline))
                if is_finance:
                    return _provider_read(request['operation'],request['args'],request['creds'],absolute_deadline=deadline)
                if request['operation'] == 'ozon_orders_analytics':
                    return _provider_read(request['operation'], request['args'], request['creds'],
                                          absolute_deadline=request.get('deadline_monotonic'))
                return _provider_read(request['operation'],request['args'],request['creds'])
        value = (_finance_read_with_retry(request.get('finance_gate_dir'),request['creds'],read,deadline,
                  allow_retry=(request['operation'] != ozon_article_summary.OPERATION and
                    (request['operation'] not in ('ozon_posting_accruals',ozon_unallocated.OPERATION) or
                     not request['args'].get('include_types',True))))
                 if is_finance else read())
        if request['operation'] in market_specs.TOOLS or request['operation'] in ('wb_sales_summary','wb_prices','archive_market_day'):
            def amounts(obj):
                if isinstance(obj,Decimal) and obj.is_finite(): return str(obj)
                raise TypeError('Unexpected projected value')
            encoded=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False,default=amounts)
            def check(payload):
                for k,v in payload.items():
                    if isinstance(v,dict): check(v)
                    elif k in ('api_key','api_token','client_secret','digital_token') and isinstance(v,str) and v and json.dumps(v,ensure_ascii=False)[1:-1] in encoded:
                        raise finance_gate.ProviderResponseInvalid('Unsafe response')
            check(request['creds'])
            value=json.loads(encoded)
        result={'kind':'ok', 'result':value}
    except urllib.error.HTTPError as exc:
        result={'kind':'http', 'status':exc.code}
        if exc.code == 429:
            delay = getattr(exc,'retry_after_seconds',None)
            source = getattr(exc,'retry_after_source',None)
            if source is None: delay, source = finance_gate.parse_retry_after(exc.headers)
            result.update(retry_after_seconds=delay,retry_after_source=source)
        exc.close()
    except finance_gate.GateError as exc:
        result={'kind':'gate','reason':exc.reason,'retry_after_seconds':exc.retry_after_seconds}
    except finance_gate.ProviderResponseTooLarge:
        result={'kind':'provider_response_too_large'}
    except finance_gate.ProviderResponseInvalid:
        result={'kind':'provider_response_invalid'}
    except finance_gate.LocalPreparationError:
        result={'kind':'local_preparation_failed'}
    except TimeoutError:
        result={'kind':'timeout'}
    except urllib.error.URLError as exc:
        # urllib can wrap socket.timeout (an alias of TimeoutError) as its
        # reason. Preserve the timeout classification without forwarding the
        # exception or its possibly sensitive message across the worker pipe.
        result={'kind':'timeout' if isinstance(exc.reason, TimeoutError) else 'unavailable'}
    except Exception:
        result={'kind':'failed'}
    sys.stdout.write(json.dumps(result,ensure_ascii=True,allow_nan=False))

def render_preview(provider, result, fetched_at, sensitive_values=(), lookup_nm_id=None):
    """Render allowlisted normalized data only; upstream strings are untrusted text."""
    rows=result.get('items')
    if not isinstance(rows,list): raise ValueError('Invalid items')
    if lookup_nm_id is not None and (provider!='wb' or type(lookup_nm_id) is not int or not 1<=lookup_nm_id<=9223372036854775807): raise ValueError('Invalid WB article')
    title='Товар Wildberries по артикулу' if lookup_nm_id is not None else 'Первые товары Ozon' if provider=='ozon' else 'Первые цены Wildberries'
    stamp=datetime.fromtimestamp(fetched_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    def text(value, maximum=256):
        if value is None: return 'не указано'
        if type(value) is bool: return 'да' if value else 'нет'
        if not isinstance(value,(str,int,float)): return 'не указано'
        rendered=str(value)
        for secret in sensitive_values:
            if isinstance(secret,str) and secret: rendered=rendered.replace(secret,'[REDACTED]')
        return html.escape(rendered[:maximum],quote=True)
    def field_text(key,value):
        if key=='name': return text(value,1024)
        if key=='currencyIsoCode4217':
            # Display the provider's code verbatim, never assume RUB or convert prices.
            if not isinstance(value,str) or not re.fullmatch(r'[A-Z]{3}',value): return 'не указано'
        if key in ('price','discountedPrice','clubDiscountedPrice','discount','clubDiscount'):
            # WB prices are JSON numbers, not truth values or numeric strings.
            # Reject nonfinite values accepted by Python's permissive JSON parser.
            if type(value) not in (int,float): return 'не указано'
            if type(value) is float and not math.isfinite(value): return 'не указано'
            if value<0 or (key in ('discount','clubDiscount') and value>100): return 'не указано'
        return text(value)
    def fields(row,labels):
        return '<dl>'+''.join('<dt>'+label+'</dt><dd>'+field_text(key,row.get(key))+'</dd>' for key,label in labels)+'</dl>'
    content='<main id="content"><p class="eyebrow">Данные маркетплейса</p><h1>'+title+'</h1><p>Получено: '+stamp+'.</p>'
    if lookup_nm_id is not None:
        content+='<p>Запрошен артикул Wildberries: '+str(lookup_nm_id)+'. Показан ответ только по этому артикулу, до одного товара. Это не отчёт об остатках или продажах. Данные только для чтения и могут измениться после запроса.</p>'
    else:
        content+='<p>Только первая страница, до 20 товаров. Это не полный ассортимент и не отчёт об остатках или продажах. Данные отображаются только для чтения; значения могут измениться после запроса.</p>'
    content+='<p><a href="/settings">Вернуться к настройкам</a></p>'
    if not rows:
        content+='<p>Ответ API не содержит товар с этим артикулом. Проверьте артикул и выбранный кабинет; этот ответ не доказывает удаление товара.</p>' if lookup_nm_id is not None else '<p>В первой странице ответа нет товаров. Это не доказывает отсутствие всех товаров в кабинете.</p>'
    content+='<div class="data-grid">'
    for index,row in enumerate(rows[:20],1):
        if not isinstance(row,dict): raise ValueError('Invalid row')
        content+='<section><h2>Товар '+str(index)+'</h2>'
        if provider=='ozon':
            content+=fields(row,(('product_id','ID товара'),('offer_id','Артикул продавца'),('name','Название товара'),('archived','В архиве'),('is_fbo_visible','Видимость FBO по API'),('is_fbs_visible','Видимость FBS по API')))
            skus=row.get('sku_candidates')
            if skus is not None and not isinstance(skus,list):raise ValueError('Invalid product SKUs')
            if skus:
                content+='<p>Наблюдаемые SKU: '+', '.join(text(sku) for sku in skus)+'.</p>'
            else:content+='<p>SKU карточки не получены или сопоставление не подтверждено.</p>'
            statuses={'observed':'Название и SKU сопоставлены с текущим ответом Ozon.',
                      'incomplete':'В карточке отсутствуют некоторые данные; список SKU может быть неполным.',
                      'sku_conflict':'Обнаружен конфликт SKU; сопоставление не показано.',
                      'sku_owner_incomplete':'Владельца SKU нельзя определить; сопоставление не показано.',
                      'offer_mismatch':'Артикул карточки не совпал; сопоставление не показано.',
                      'sku_mismatch':'SKU списка и карточки не совпали; сопоставление не показано.',
                      'page_sku_unverifiable':'Формат SKU списка не удалось проверить.',
                      'invalid_list_sku':'Формат SKU списка не удалось проверить.',
                      'unavailable':'Данные карточки не получены или неоднозначны.'}
            if row.get('identity_status') in statuses:
                content+='<p>'+statuses[row['identity_status']]+'</p>'
        else:
            content+=fields(row,(('nmID','Артикул Wildberries'),('vendorCode','Артикул продавца'),('discount','Скидка по API, %'),('clubDiscount','Скидка WB Клуба по API, %'),('currencyIsoCode4217','Код валюты из API')))
            sizes=row.get('sizes',[])
            if not isinstance(sizes,list): raise ValueError('Invalid sizes')
            content+='<p>Цены приведены без пересчёта, в валюте указанного кода из API. Если код не указан, валюту нужно уточнить в кабинете; рубли по умолчанию не подставляются. Показано не более 10 размеров на товар.</p>'
            if not sizes: content+='<p>Информация о ценах и размерах не получена.</p>'
            for size in sizes[:10]:
                if not isinstance(size,dict): raise ValueError('Invalid size')
                content+=fields(size,(('sizeID','ID размера'),('techSizeName','Размер'),('price','Цена'),('discountedPrice','Цена со скидкой'),('clubDiscountedPrice','Цена для WB Клуба')))
        content+='</section>'
    content+='</div></main>'
    return document(title,content)

class Portal:
    native_provider = staticmethod(provider_read)
    def __init__(self, login, vault, read_token_hash, gateway=provider_read, mcp_read_token_hash=None, external_key_store=None, oauth_store=None, finance_gate_dir=None, ai_gateway=None, budget_gateway=None, write_token_setup=False, ai_gateway_inspector=None, model_config_enabled=False, ai_cutover=None, ai_cutover_jobs=None):
        if not re.fullmatch(r'[0-9a-f]{64}',read_token_hash): raise ValueError('Expected SHA256 token hash')
        if mcp_read_token_hash is not None and (not re.fullmatch(r'[0-9a-f]{64}',mcp_read_token_hash) or hmac.compare_digest(read_token_hash,mcp_read_token_hash)):
            raise ValueError('Expected distinct SHA256 MCP token hash')
        self.login,self.vault,self.token_hash,self.gateway=login,vault,read_token_hash,gateway
        self.ai_gateway=ai_gateway
        self.ai_gateway_inspector=ai_gateway_inspector
        self.model_config_enabled=model_config_enabled is True
        self.ai_cutover=ai_cutover
        self.ai_cutover_jobs=ai_cutover_jobs
        self.budget_gateway=budget_gateway
        self.write_token_setup = write_token_setup is True
        self.finance_gate_dir=(finance_gate_dir if finance_gate_dir is not None else
            os.path.join(os.path.dirname(os.path.abspath(vault.path)),'.ozon-finance-gates'))
        if not isinstance(self.finance_gate_dir,str) or not os.path.isabs(self.finance_gate_dir):
            raise ValueError('Absolute finance gate directory required')
        self.mcp_token_hash=mcp_read_token_hash
        self.external_keys=external_key_store if external_key_store is not None else external_keys.KeyStore(vault.path)
        self.oauth=oauth_store if oauth_store is not None else oauth_mcp.OAuthStore(vault.path,signing_secret=login.secret,clock=login.clock)
    def ai_gateway_available(self):
        try:
            return self.ai_gateway is not None and self.ai_gateway.ready() is True
        except Exception:
            return False
    def budget_gateway_available(self):
        try:
            return self.budget_gateway is not None and self.budget_gateway.ready() is True
        except Exception:
            return False
    def read_gateway(self, operation, args, creds, *, include_avito=False):
        if operation in cost_catalog.TOOLS:
            market_specs.validate(operation, args)
            calculator = cost_catalog.Store(cost_catalog.database_path(self.vault.path))
            return cost_catalog.read(calculator, operation, args)
        if operation=='mcp_health':
            from functools import partial
            configured=partial(expansion_gateway.credentials,include_avito=include_avito)

            def accounts(vault, name):
                return {} if name in cost_catalog.TOOLS else configured(vault, name)
            detail=None
            if 'probe_arguments' in args:
                name,probe=args['tool_name'],args['probe_arguments']
                if name not in external_mcp.TOOL_NAMES or name=='mcp_health': raise ValueError('Unknown probe')
                if name in market_specs.TOOLS: market_specs.validate(name,probe)
                elif name in ozon_mcp.OPERATIONS: ozon_mcp.validate_args(name,probe)
                else: wb_mcp.validate_args(name,probe)
                account=accounts(self.vault,name)
                if account or name in cost_catalog.TOOLS:
                    try:
                        data=self.read_gateway(name,probe,account)
                        detail={k:data.get(k) for k in ('status','complete','data_as_of','http_status','task_id',
                                                        'source','retry_after_seconds','refresh_status')}
                        if detail['status'] is None and isinstance(detail['complete'], bool):
                            detail['status'] = 'ready' if detail['complete'] else 'partial'
                        if isinstance(data.get('source_issues'), list):
                            detail['source_issues'] = data['source_issues']
                    except finance_gate.GateError as exc:
                        detail=dict(status=exc.reason, probe_origin='local_gate', http_status=None,
                                    reason='local_limiter', retry_after_seconds=exc.retry_after_seconds)
                    except urllib.error.HTTPError as exc:
                        detail=dict(status='rate_limited' if exc.code == 429 else 'http_error',
                                    http_status=exc.code, retry_after_seconds=getattr(exc,'retry_after_seconds',None))
                        exc.close()
                    except TimeoutError as exc:
                        detail = dict(status='provider_timeout', http_status=None, **{
                            k:v for k,v in getattr(exc,'diagnostic',{}).items()
                            if k in ('provider_method','retry_after_seconds','retry_basis')})
                    except urllib.error.URLError:
                        detail={'status':'transport_error','http_status':None}
                else:
                    detail=self._unconfigured_read(name)
            report=market_health.read(self.vault,self.finance_gate_dir,args,external_mcp.TOOL_NAMES,accounts)
            calculator_path = cost_catalog.database_path(self.vault.path) if isinstance(self.vault, Vault) else None
            if calculator_path and cost_catalog.Store.exists(calculator_path):
                import cost_sync
                report["calculator_sync"] = cost_sync.sync_summary(cost_catalog.Store(calculator_path))
                for item in report["items"]:
                    if item["tool"] in cost_catalog.TOOLS:
                        item.update(status="available", source="cost_calculator")
            elif calculator_path:
                report["calculator_sync"] = {"status": "waiting_for_first_sync", "time_zone": "Europe/Moscow", "recent_runs": []}
            report["seller_capabilities"]=market_specs.seller_reports.capabilities(self.vault,self.finance_gate_dir,
                    args.get("refresh_capabilities",False),self.read_gateway)
            caps=report["seller_capabilities"]
            for token in report.get("tokens",[]):
                if token["category"]=="Ozon Seller":
                    token.update(allowed_methods=caps["ozon_roles"].get("allowed_methods"),roles_status=caps["ozon_roles"]["status"])
                if token["category"]=="WB общий":
                    token["subscriptions"]={"jam":caps["wb_jam"]}
            if args.get("refresh_capabilities"):
                from market_composites import observed
                name="wb_seller_information"
                route,_=market_specs.seller_reports.request(name,{"report":"news"})
                account=market_catalog.read_credentials(self.vault,route)
                if account:
                    day=(datetime.now(timezone.utc)-market_specs.timedelta(days=30)).date().isoformat()
                    news=observed(lambda:self.read_gateway(name,{"report":"news","input":{"query":{"from":day}}},account))
                    items=(news.get("data") or {}).get("data") or []
                    report.setdefault("api_updates",{})["wb_news"]={"status":news.get("status"),"source":route["path"],
                        "data_as_of":news.get("data_as_of"),"items":[{k:item.get(k) for k in ("id","date","header")} for item in items
                            if "api" in (str(item.get("header"))+" "+str(item.get("content"))).casefold()][:50]}
            if detail: report['probe']=detail
            return report
        cost_card, calculator = None, None
        if (isinstance(self.vault, Vault) and isinstance(args, dict)
                and {"article", "offer_id", "vendor_code", "offer_ids", "vendor_codes"} & args.keys()):
            calculator_path = cost_catalog.database_path(self.vault.path)
            if cost_catalog.Store.exists(calculator_path):
                calculator = cost_catalog.Store(calculator_path)
                args, cost_card, resolved = cost_integration.prepare(calculator, operation, args)
                if resolved is not None:
                    if operation in cost_integration.COST_REPORTS:
                        return cost_integration.enrich(calculator, operation, args, cost_card, resolved)
                    return resolved
        try:
            value=self._dispatch_read(operation,args,creds)
        except Exception as exc:
            status=('provider_timeout' if isinstance(exc,TimeoutError) else 'http_'+str(exc.code) if isinstance(exc,urllib.error.HTTPError)
                    else exc.reason if isinstance(exc,finance_gate.GateError)
                    else 'provider_response_invalid' if isinstance(exc,finance_gate.ProviderResponseInvalid)
                    else 'transport_error' if isinstance(exc,urllib.error.URLError) else 'failed')
            self._observe(operation,creds,status)
            raise
        if isinstance(value,dict):
            if cost_card or operation in cost_integration.COST_REPORTS:
                value = cost_integration.enrich(calculator, operation, args, cost_card, value)
            # A managed worker records its own admission and completion in order.
            # A foreground poll can finish just after the worker has recorded ready.
            in_progress = (value.get("status") == "pending" and
                           str(value.get("task_id", "")).startswith("mcpjob_"))
            if not in_progress:
                self._observe(operation,creds,value.get('status') or ('partial' if value.get('complete') is False else 'ok'))
            if self.gateway is provider_read:
                value.setdefault('data_as_of',value.get('as_of') or (None if operation in market_specs.TOOLS else market_history.utc()))
                if operation.startswith('wb_') and 'date_to' in args:
                    boundary=(datetime.now(market_specs.MOSCOW).date()-market_specs.timedelta(days=1)).isoformat()
                    value.setdefault('provisional_dates',[d for d in market_history.days_between(args['date_from'],args['date_to']) if d>=boundary])
        return value

    def _observe(self,operation,creds,status):
        try:
            if not isinstance(status,str) or not re.fullmatch(r'[a-z0-9_]{1,65}',status): status='provider_replied'
            market_history.History(self.finance_gate_dir).record(creds,operation,status)
        except (OSError,sqlite3.Error):
            pass

    def _unconfigured_read(self, operation):
        self._observe(operation, None, 'not_configured')
        return dict(status='not_configured', complete=False, data_as_of=None, http_status=None)

    def _dispatch_read(self, operation, args, creds):
        if operation not in finance_gate.FINANCE_OPERATIONS:
            if self.gateway is provider_read:
                legacy_job = (operation in market_specs.FAST_SELLER_READS and
                              isinstance(args.get("task_id"), str) and args["task_id"].startswith("mcpjob_"))
                batch_acts = operation == "ozon_supply_acts" and "supply_id" not in args
                if ((operation in market_specs.RESUMABLE_READS or operation == "wb_sales_summary" or legacy_job or batch_acts) and os.name == "posix"
                        and os.environ.get("MCP_PREPARE_MARKET_WORKERS") == "1"):
                    import report_jobs
                    try:
                        return report_jobs.run(operation, args, creds, self.finance_gate_dir,
                            lambda query, snapshot, deadline: provider_read(operation, query, creds,
                                finance_gate_dir=self.finance_gate_dir, report_mappings=snapshot,
                                report_deadline=deadline),
                            observe=lambda status: self._observe(operation, creds, status),
                            deadline_aware=True)
                    except report_jobs.InvalidTask:
                        raise external_mcp.MCPToolError("invalid_arguments") from None
                try:
                    return provider_read(operation,args,creds,finance_gate_dir=self.finance_gate_dir)
                except (TimeoutError, finance_gate.GateError) as exc:
                    if operation != 'wb_supplies' or (isinstance(exc, finance_gate.GateError)
                            and exc.reason not in ('busy', 'cooldown', 'retry_time_unknown')):
                        raise
                    import supply_history
                    market_specs.validate(operation, args)
                    wire = expansion_gateway.make_wire('wb', creds, self.finance_gate_dir, time.monotonic())
                    page = supply_history.saved_wb_page(args, wire)
                    if page is None:
                        raise
                    return dict(page, tool=operation, read_only=True, untrusted_data=True,
                                refresh_reason=exc.reason if isinstance(exc, finance_gate.GateError) else 'provider_timeout',
                                retry_after_seconds=getattr(exc, 'retry_after_seconds', None))
            return self.gateway(operation,args,creds)
        finance_request_preflight(operation,args,creds)
        if self.gateway is provider_read:
            # The worker, not its parent, owns the lock. If the parent dies,
            # an orphaned worker cannot overlap another financial request.
            return provider_read(operation,args,creds,finance_gate_dir=self.finance_gate_dir)
        # Test/injected gateways use the same contract and serialization.
        return finance_gate.run(self.finance_gate_dir,creds.get('client_id'),
                                lambda:self.gateway(operation,args,creds),(creds.get('api_key'),),read_recovery=True)
    def request_budget(self, provider, now):
        """Read-only local limiter snapshot. It neither reserves nor grants a request."""
        if provider not in CHECK_OPERATIONS: raise ValueError('Invalid provider')
        with self.login.connect() as c:
            c.execute('BEGIN')
            local=[row[0] for row in c.execute(
                'SELECT at FROM attempts WHERE bucket=? AND at>? ORDER BY at DESC LIMIT ?',
                ('connection-check:'+provider,now-CHECK_PROVIDER_WINDOW,CHECK_PROVIDER_LIMIT))]
            shared=[row[0] for row in c.execute(
                'SELECT at FROM attempts WHERE bucket=? AND at>? ORDER BY at DESC LIMIT ?',
                ('connection-check:all',now-CHECK_GLOBAL_WINDOW,CHECK_GLOBAL_LIMIT))]
        ready_at=now
        if len(local)==CHECK_PROVIDER_LIMIT: ready_at=max(ready_at,local[-1]+CHECK_PROVIDER_WINDOW)
        if len(shared)==CHECK_GLOBAL_LIMIT: ready_at=max(ready_at,shared[-1]+CHECK_GLOBAL_WINDOW)
        return {'retry_seconds':math.ceil(ready_at-now),
                'hourly_remaining':CHECK_GLOBAL_LIMIT-len(shared)}

    def __call__(self,e,start):
        path=e.get('PATH_INFO','/'); method=e.get('REQUEST_METHOD','GET')
        if path == '/' and method == 'GET' and e.get('HTTP_HOST') == urlsplit(self.login.origin).netloc:
            sid=self.login.cookies(e).get('__Host-sid','')
            if re.fullmatch(r'[A-Za-z0-9_-]{43}',sid):
                with self.login.connect() as c:
                    present=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                if present:
                    start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path not in OAUTH_PATHS and path not in ('/settings','/settings/catalog','/settings/check','/settings/preview','/settings/wb-lookup','/settings/mcp/create','/settings/mcp/revoke','/settings/yandex-backup','/settings/content-factory/connect','/settings/content-factory/models','/settings/content-factory/budget/prepare','/settings/content-factory/budget/confirm','/settings/content-factory/budget/refresh','/settings/content-factory/ai/write-token','/settings/content-factory/ai/reader-token','/settings/content-factory/ai/reader-credential/prepare','/settings/content-factory/ai/reader-credential/confirm','/settings/content-factory/ai/gateway','/settings/content-factory/ai/models/prepare','/settings/content-factory/ai/models/confirm','/settings/content-factory/ai/credentials/prepare','/settings/content-factory/ai/credentials/commit','/settings/content-factory/ai/credentials/provision/prepare','/settings/content-factory/ai/credentials/provision/commit','/settings/content-factory/ai/reconcile/inspect','/settings/content-factory/ai/reconcile/confirm','/settings/content-factory/ai/cutover/job','/settings/content-factory/ai/cutover/pause/prepare','/settings/content-factory/ai/cutover/pause/confirm','/settings/content-factory/ai/cutover/restore/prepare','/settings/content-factory/ai/cutover/restore/confirm','/settings/content-factory/ai/cutover/draft/prepare','/settings/content-factory/ai/cutover/draft/confirm','/settings/content-factory/ai/cutover/publish/prepare','/settings/content-factory/ai/cutover/publish/confirm','/api/content-factory/model-config','/mcp','/api/read','/healthz'):
            return self.login(e,start)
        def reply(text,code='200 OK',json_response=False,extra_headers=(),*,csp=CSP):
            if not json_response and not text.lstrip().lower().startswith('<!doctype'): text=document('Кабинет', '<main id="content" class="error-card"><h1>Кабинет</h1><p>'+text+'</p></main>')
            body=(json.dumps(text) if json_response else text).encode()
            start(code,[('Content-Type','application/json' if json_response else 'text/html; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('Referrer-Policy','no-referrer' if json_response else 'strict-origin'),('X-Content-Type-Options','nosniff'),('Content-Security-Policy',csp)]+list(extra_headers)); return [body]
        if e.get('HTTP_HOST')!=urlsplit(self.login.origin).netloc: return reply('Invalid host','400 Bad Request')
        if path == '/api/content-factory/model-config':
            if not self.model_config_enabled: return reply({'error':'unavailable'},'404 Not Found',True)
            if method != 'GET' or e.get('QUERY_STRING',''): return reply({'error':'invalid_request'},'400 Bad Request',True)
            import content_factory_model_config
            if not content_factory_model_config.authenticate(self.vault,e.get('HTTP_AUTHORIZATION','')):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            try:
                if not self.login.permit('content-factory-model-reader',60,60): return reply({'error':'rate_limited'},'429 Too Many Requests',True)
                return reply(content_factory_model_config.read(self.vault),json_response=True)
            except Exception:
                return reply({'error':'unavailable'},'503 Service Unavailable',True)
        if path in OAUTH_PATHS:
            return oauth_mcp.handle(e,start,self.oauth,self.external_keys,self.login.origin)
        if path=='/mcp':
            current_access=None
            def authenticate(token):
                nonlocal current_access
                key_id=self.external_keys.check_token(token)
                grant=(key_id,external_keys.access_for_key(key_id)) if key_id else self.oauth.verify_access(token,self.login.origin+'/mcp')
                if grant and self.external_keys.mark_used(grant[0],self.login.clock()):
                    current_access=grant[1]
                    return grant
                return None
            def execute(key_id,operation,args):
                creds={}
                try:
                    if operation in market_catalog.PROPOSAL_TOOLS and not self.login.permit('external-mcp:'+key_id,30,60):
                        raise external_mcp.MCPToolError('rate_limited')
                    if operation=='market_api_search':
                        return market_catalog.search(args)
                    if operation=='market_api_describe':
                        return market_catalog.describe(args['operation_id'])
                    if operation=='market_api_propose':
                        return market_actions.Store(self).propose(key_id,args)
                    if operation==ozon_review_reply.TOOL:
                        return market_actions.Store(self).propose_review_reply(key_id,args)
                    if operation=='market_api_status':
                        return market_actions.Store(self).status(key_id,args['proposal_id'])
                    if operation=="mcp_health" and current_access=="all":
                        selected=args.get("tool_name")
                        if not selected or selected in avito_mcp.PUBLIC_CONTRACTS:
                            account=self.vault.read("avito")
                            hist=market_history.History(self.finance_gate_dir)
                            probe=None
                            if selected and "probe_arguments" in args:
                                avito_mcp.validate(selected,args["probe_arguments"])
                                if account:
                                    from market_composites import observed
                                    probe=observed(lambda:self.read_gateway(selected,args["probe_arguments"],account))
                                else:
                                    probe=self._unconfigured_read(selected)
                            items=[dict(tool=n,category="Avito",**hist.check(account or None,n))
                                   for n in sorted(avito_mcp.PUBLIC_CONTRACTS) if selected in (None,n)]
                            if selected:
                                result=market_specs.result(items,tool_count=len(items))
                                if probe is not None:
                                    result["probe"]={k:probe.get(k) for k in ("status","complete","data_as_of","http_status")}
                            else:
                                result=self.read_gateway(operation,args,{},include_avito=True)
                                result["items"].extend(items);result["tool_count"]+=len(items)
                            return result
                    if operation=='mcp_health':
                        return self.read_gateway(operation,args,{},include_avito=current_access=="all")
                    creds=(market_catalog.read_credentials(self.vault,market_catalog.routes()[args['operation_id']]) if operation=='market_api_read'
                       else self.vault.read('avito') if operation in avito_mcp.PUBLIC_CONTRACTS else
                       expansion_gateway.credentials(self.vault,operation,include_avito=current_access=="all"))
                    if not creds and operation not in cost_catalog.TOOLS and operation != "article_map":
                        self._unconfigured_read(operation)
                        raise external_mcp.MCPToolError('not_configured')
                    return self.read_gateway(operation,args,creds)
                except market_actions.ActionError as exc:
                    return {'status':'action_unavailable','reason':str(exc)}
                except urllib.error.HTTPError as exc:
                    if operation=='market_api_read' and exc.code!=429:
                        status=exc.code
                        exc.close()
                        return {'status':'provider_error','http_status':status,'reason':{400:'invalid_provider_parameters',
                                401:'provider_credentials',402:'provider_subscription_required',403:'provider_access_denied',
                                404:'provider_resource_not_found',422:'provider_validation_failed'}.get(status,'provider_replied')}
                    code='provider_access_denied' if exc.code in (401,402,403) else 'rate_limited' if exc.code==429 else 'unavailable'
                    delay=getattr(exc,'retry_after_seconds',None) if exc.code==429 else None
                    if exc.code==429 and delay is None: delay,_=finance_gate.parse_retry_after(exc.headers)
                    raw_delay=delay
                    delay=public_retry_delay(delay,creds)
                    exc.close()
                    guarded=operation in finance_gate.FINANCE_OPERATIONS or operation in avito_mcp.PUBLIC_CONTRACTS
                    reason='retry_time_unknown' if exc.code==429 and delay is None and guarded else None
                    if raw_delay is not None and delay is None: reason='retry_interval_unavailable'
                    raise external_mcp.MCPToolError(code,retry_after_seconds=delay,reason=reason,http_status=exc.code) from None
                except finance_gate.GateError as exc:
                    code='rate_limited' if exc.reason in ('busy','cooldown','retry_time_unknown') else 'unavailable'
                    delay=public_retry_delay(exc.retry_after_seconds,creds)
                    reason=('retry_interval_unavailable' if exc.retry_after_seconds is not None and delay is None else
                            'local_limiter' if code=='rate_limited' and operation!='market_api_propose' else exc.reason)
                    raise external_mcp.MCPToolError(code,retry_after_seconds=delay,reason=reason) from None
                except finance_gate.ProviderResponseTooLarge:
                    raise external_mcp.MCPToolError('provider_response_too_large') from None
                except finance_gate.ProviderResponseInvalid:
                    raise external_mcp.MCPToolError('provider_response_invalid') from None
                except TimeoutError as exc:
                    error=external_mcp.MCPToolError('provider_timeout')
                    error.diagnostic=getattr(exc,'diagnostic',{})
                    raise error from None
            return external_mcp.handle(e,start,authenticate,execute,self.login.origin)
        if path=='/healthz':
            # Liveness only: no session, credential, database, SMTP or seller access.
            # This does not assert that downstream integrations are configured/healthy.
            status='200 OK' if method in ('GET','HEAD') else '405 Method Not Allowed'
            payload=b'{"status":"ok"}' if status=='200 OK' else b'{"error":"method"}'
            headers=[('Content-Type','application/json'),('Content-Length',str(len(payload))),
                     ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]
            if status!='200 OK': headers.append(('Allow','GET, HEAD'))
            start(status,headers)
            return [b''] if method=='HEAD' else [payload]
        def body(kind, maximum=4096):
            if e.get('CONTENT_TYPE','').split(';')[0]!=kind: raise ValueError()
            n=int(e.get('CONTENT_LENGTH','0'))
            if not 0<n<=maximum: raise ValueError()
            raw=e['wsgi.input'].read(n)
            if len(raw)!=n: raise ValueError()
            return raw.decode('utf-8')
        if path=='/api/read':
            if method!='POST': return reply({'error':'method'},'405 Method Not Allowed',True)
            # Browser cookies, Origin and Sites identity header confer no API authority.
            auth=e.get('HTTP_AUTHORIZATION','')
            if not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43,128}',auth):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            supplied_hash=hashlib.sha256(auth[7:].encode()).hexdigest()
            bridge_authorized=hmac.compare_digest(supplied_hash,self.token_hash)
            mcp_authorized=hmac.compare_digest(supplied_hash,self.mcp_token_hash or '0'*64)
            if not (bridge_authorized or (self.mcp_token_hash is not None and mcp_authorized)):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            source='mcp' if self.mcp_token_hash is not None and mcp_authorized else 'bridge'
            def record_bridge(operation,outcome):
                # Observability must not change the response of an authorised read.
                stored_operation = operation if operation in BRIDGE_OPERATIONS else 'unknown'
                try: self.vault.record_bridge_activity(source,stored_operation,outcome,self.login.clock())
                except Exception: pass
            if source!='mcp' and not self.login.permit('api',30,60):
                # The body has not been validated, so do not trust or record its operation.
                record_bridge('unknown','rate_limited')
                return reply({'error':'rate_limited'},'429 Too Many Requests',True)
            try:
                data=json.loads(body('application/json'))
                if not isinstance(data,dict) or set(data)!={'operation','arguments'}: raise ValueError()
                op=data['operation']; args=data['arguments']
                if op=='sync_cost_catalog':
                    if not bridge_authorized or args != {}: raise ValueError()
                elif op=='archive_market_day':
                    if not bridge_authorized or not isinstance(args,dict) or set(args)!={'dataset'} or args['dataset'] not in market_archive.DATASETS: raise ValueError()
                elif op in market_specs.seller_reports.GROUPS:
                    if not bridge_authorized: raise ValueError()
                    market_specs.validate(op,args)
                elif op in market_specs.TOOLS: market_specs.validate(op,args)
                elif op in ozon_mcp.OPERATIONS: ozon_mcp.validate_args(op,args)
                elif op in wb_mcp.OPERATIONS:
                    wb_mcp.validate_args(op,args)
                    # Exact-article lookup is reserved for the browser flow.
                    if op=='wb_prices' and 'nm_id' in args: raise ValueError()
                else: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply({'error':'invalid_request'},'400 Bad Request',True)
            try:
                if op == "sync_cost_catalog":
                    import cost_sync
                    run_id = cost_sync.kick(self, scheduled=True)
                    return reply({"status": "scheduled" if run_id else "up_to_date"}, json_response=True)
                selection=args['dataset'] if op=='archive_market_day' else op
                if op == "archive_market_day" and selection == "wb_orders":
                    import cost_sync
                    try:
                        cost_sync.kick(self)
                    except (sqlite3.Error, OSError):
                        pass
                if op in market_specs.seller_reports.GROUPS:
                    route, inputs=market_specs.seller_reports.request(op,args)
                    if inputs is None:
                        return reply(market_specs.seller_reports.execute(op,args,{},self.finance_gate_dir,time.monotonic()),json_response=True)
                    creds=market_catalog.read_credentials(self.vault,route)
                else:
                    creds=expansion_gateway.credentials(self.vault,{'ozon_availability':'ozon_stocks_analytics','wb_availability':'wb_stocks'}.get(selection,selection))
                if not creds and op not in cost_catalog.TOOLS | {"mcp_health", "article_map"}:
                    record_bridge(op,'not_configured')
                    return reply({'error':'not_configured'},'409 Conflict',True)
                result=self.read_gateway(op,args,creds)
                if op in market_specs.TOOLS and result.get('status')=='provider_access_denied':
                    record_bridge(op,'failed')
                    return reply(result,'403 Forbidden',True)
                record_bridge(op,'ok')
                return reply(result,json_response=True)
            except urllib.error.HTTPError as exc:
                record_bridge(op,'failed')
                if exc.code in (401,403): return reply({'error':'provider_access_denied'},'403 Forbidden',True)
                if exc.code == 429:
                    delay=getattr(exc,'retry_after_seconds',None)
                    if delay is None: delay,_=finance_gate.parse_retry_after(exc.headers)
                    delay=public_retry_delay(delay,creds)
                    headers=[('Retry-After',str(delay))] if delay is not None else []
                    exc.close()
                    return reply({'error':'provider_rate_limited','retry_after_seconds':delay},'429 Too Many Requests',True,headers)
                return reply({'error':'read_failed'},'502 Bad Gateway',True)
            except finance_gate.GateError as exc:
                record_bridge(op,'failed')
                limited=exc.reason in ('busy','cooldown','retry_time_unknown')
                delay=public_retry_delay(exc.retry_after_seconds,creds)
                headers=[('Retry-After',str(delay))] if delay is not None else []
                return reply({'error':'provider_rate_limited' if limited else 'read_failed',
                              'reason':exc.reason if exc.retry_after_seconds is None or delay is not None else 'retry_interval_unavailable',
                              'retry_after_seconds':delay},
                             '429 Too Many Requests' if limited else '502 Bad Gateway',True,headers)
            except finance_gate.ProviderResponseTooLarge:
                record_bridge(op,'failed')
                return reply({'error':'provider_response_too_large'},'502 Bad Gateway',True)
            except finance_gate.ProviderResponseInvalid:
                record_bridge(op,'failed')
                return reply({'error':'provider_response_invalid'},'502 Bad Gateway',True)
            except TimeoutError as exc:
                record_bridge(op,'failed')
                return reply({'error':'provider_timeout',**getattr(exc,'diagnostic',{})},'504 Gateway Timeout',True)
            except Exception:
                record_bridge(op,'failed')
                return reply({'error':'read_failed'},'502 Bad Gateway',True)
        if path in ('/settings/check','/settings/preview','/settings/wb-lookup') and method!='POST': return reply('Method not allowed','405 Method Not Allowed')
        if method not in ('GET','POST'): return reply('Method not allowed','405 Method Not Allowed')
        sid=self.login.cookies(e).get('__Host-sid','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',sid): return reply('Войдите заново на главной странице.','401 Unauthorized')
        now=self.login.clock()
        try:
            with self.login.connect() as c:
                if path in ('/settings/check','/settings/preview','/settings/wb-lookup'):
                    c.execute('PRAGMA busy_timeout=500')
                session=c.execute('SELECT expires,csrf FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),now)).fetchone()
        except sqlite3.Error:
            return reply('Сервис временно недоступен. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
        if not session: return reply('Войдите заново на главной странице.','401 Unauthorized')
        if path=='/settings/yandex-backup':
            if method!='POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            import yandex_backup_admin
            try:
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=8192),max_num_fields=6,keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if form.get('action')=='disable' and set(form)=={'csrf','action'}:
                    yandex_backup_admin.disable(self.vault)
                elif form.get('action')=='save' and set(form)=={'csrf','action','public_url','disk_folder','token','enabled'}:
                    if session[0]-43200 < now-300: return reply('Для изменения данных Яндекс Диска войдите заново по письму.','403 Forbidden')
                    yandex_backup_admin.save(self.vault,public_url=form['public_url'],disk_folder=form['disk_folder'],token=form['token'],enabled=form['enabled'])
                else: raise ValueError()
            except BlockingIOError:
                return reply('Идёт резервное копирование. Повторите изменение после его завершения. <a href="/settings">Настройки</a>','409 Conflict')
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте ссылку, путь к папке и токен Яндекс Диска. <a href="/settings">Настройки</a>','400 Bad Request')
            except (sqlite3.Error,OSError):
                return reply('Не удалось сохранить настройки резервирования. <a href="/settings">Настройки</a>','503 Service Unavailable')
            start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path == '/settings/content-factory/connect':
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключа n8n войдите заново по письму.','403 Forbidden')
            try:
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=4096),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                if set(form) != {'csrf','api_key'} or any(len(v) != 1 for v in form.values()): raise ValueError()
                csrf, api_key = form['csrf'][0], form['api_key'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if not re.fullmatch(r'[!-~]{16,2048}',api_key): raise ValueError()
                self.vault.save('n8n', {'api_key':api_key})
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте ключ доступа n8n.', '400 Bad Request')
            except (sqlite3.Error,OSError):
                return reply('Не удалось сохранить ключ n8n.', '503 Service Unavailable')
            start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path == '/settings/content-factory/ai/reader-token':
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключа войдите заново по письму.','403 Forbidden')
            if not self.model_config_enabled: return reply('Настройка ключа недоступна.','503 Service Unavailable')
            try:
                if not self.login.permit('cf-reader-token:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=512),max_num_fields=1,keep_blank_values=True,strict_parsing=True)
                if set(form) != {'csrf'} or any(len(v) != 1 for v in form.values()): raise ValueError()
                csrf=form['csrf'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                import content_factory_model_config
                content_factory_model_config.create_reader_token(self.vault)
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте ключ чтения моделей.','400 Bad Request')
            except Exception: return reply('Не удалось сохранить ключ чтения моделей.','503 Service Unavailable')
            start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path == '/settings/content-factory/ai/write-token':
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключа войдите заново по письму.','403 Forbidden')
            if not self.write_token_setup: return reply('Настройка ключа управления недоступна.','503 Service Unavailable')
            try:
                if not self.login.permit('cf-write-token:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=4096),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                if set(form) != {'csrf','api_key'} or any(len(v) != 1 for v in form.values()): raise ValueError()
                csrf, api_key = form['csrf'][0], form['api_key'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                import content_factory_gateway_token
                content_factory_gateway_token.save(self.vault, api_key)
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте ключ управления n8n.','400 Bad Request')
            except Exception:
                return reply('Не удалось сохранить ключ управления n8n.','503 Service Unavailable')
            start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path in ('/settings/content-factory/budget/prepare', '/settings/content-factory/budget/confirm', '/settings/content-factory/budget/refresh'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения бюджета войдите заново по письму.','403 Forbidden')
            if not self.budget_gateway_available(): return reply('Изменение бюджета пока недоступно.','503 Service Unavailable')
            try:
                import content_factory_budget_ui
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=2048),max_num_fields=3,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form = {k:v[0] for k,v in form.items()}
                csrf = form.get('csrf', '')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                owner = self.login.digest(sid)
                action = path.rsplit('/',1)[-1]
                stable_owner = self.login.digest('cf-owner:' + self.login.recipient)
                if (not self.login.permit('cf-budget-owner:'+action+':'+stable_owner,10,300)
                        or not self.login.permit('cf-budget-global:'+action,30,300)):
                    return reply('Слишком много попыток.','429 Too Many Requests')
                if path.endswith('/refresh'):
                    if set(form) != {'csrf'} or getattr(self.budget_gateway, 'source', None) != 'n8n_live': raise ValueError()
                    self.budget_gateway.refresh()
                    start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
                if path.endswith('/prepare'):
                    if set(form) != {'csrf','revision','monthly_limit_usd'} or not re.fullmatch(r'(?:0|[1-9][0-9]{0,7})(?:[.,][0-9]{1,2})?', form['monthly_limit_usd']) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})',form['revision']): raise ValueError()
                    parts = form['monthly_limit_usd'].replace(',','.').split('.')
                    cents = int(parts[0])*100 + int((parts[1] if len(parts)>1 else '').ljust(2,'0'))
                    if cents > content_factory_budget_ui.MAX_CENTS: raise ValueError()
                    proposal = self.budget_gateway.prepare(owner, int(form['revision']), cents)
                    if not isinstance(proposal, dict) or set(proposal) != {'proposal_id','before_cents','after_cents'} or proposal['after_cents'] != cents: raise ValueError()
                    page = content_factory_budget_ui.render_budget_confirmation(csrf, proposal['proposal_id'], proposal['before_cents'], proposal['after_cents'], source=getattr(self.budget_gateway, 'source', None))
                    return reply(document('Подтверждение бюджета', '<main id="content">' + page + '</main>', content_factory=True))
                if set(form) != {'csrf','proposal_id'} or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте значение бюджета.','400 Bad Request')
            except Exception:
                return reply('Не удалось подготовить изменение бюджета.','503 Service Unavailable')
            try:
                if self.budget_gateway.confirm(owner, form['proposal_id']) is not True: raise RuntimeError()
            except Exception:
                return reply('Результат сохранения неизвестен. Проверьте настройки перед повтором.','503 Service Unavailable')
            if getattr(self.budget_gateway, 'source', None) == 'n8n_live':
                return reply('Оценочный лимит n8n изменён. Фактические списания поставщиков могут отличаться.')
            return reply('Бюджет кабинета сохранён. Ограничение n8n не включается автоматически.')
        if path == '/settings/content-factory/ai/gateway':
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            selected_gateway = self.ai_gateway or self.budget_gateway
            if not self.write_token_setup or selected_gateway is None: return reply('Шлюз недоступен.','503 Service Unavailable')
            try:
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=512),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                if set(form) != {'csrf','action'} or any(len(v) != 1 for v in form.values()): raise ValueError()
                csrf, action = form['csrf'][0], form['action'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if action not in ('arm','halt'): raise ValueError()
                if action == 'arm' and session[0]-43200 < now-300: return reply('Для включения войдите заново по письму.','403 Forbidden')
                if not self.login.permit('cf-gateway:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                if action == 'halt': selected_gateway.fence.halt()
                else:
                    selected_gateway.token_reader()  # Require a separate stored write token.
                    selected_gateway.fence.arm(lambda: True)
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте запрос.','400 Bad Request')
            except Exception: return reply('Шлюз недоступен или требуется сверка неопределённого исхода.','503 Service Unavailable')
            start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path == '/settings/content-factory/ai/cutover/job':
            if method != 'GET': return reply('Method not allowed','405 Method Not Allowed')
            if self.ai_cutover_jobs is None: return reply('Задача недоступна.','503 Service Unavailable')
            try:
                query=parse_qs(e.get('QUERY_STRING',''),max_num_fields=1,keep_blank_values=True,strict_parsing=True)
                if set(query) != {'job'} or len(query['job']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',query['job'][0]): raise ValueError()
                ident=query['job'][0]; owner=self.login.digest('cf-owner:' + self.login.recipient)
                job=self.ai_cutover_jobs.status(owner,ident)
                import content_factory_cutover_ui
                if job['state'] in ('queued','running'):
                    page='<h1>Идёт проверка</h1><p>Обновите страницу через некоторое время.</p><a href="/settings/content-factory/ai/cutover/job?job='+html.escape(ident,quote=True)+'">Обновить</a>'
                elif job['state'] == 'unknown':
                    page='<h1>Результат не подтверждён</h1><p>Не повторяйте действие до сверки состояния n8n.</p>'
                elif job['state'] == 'completed' and isinstance(job['result'],dict):
                    result=job['result']; kind=result.get('kind')
                    if kind == 'pause_preview': page=content_factory_cutover_ui.pause_preview(session[1],result['proposal_id'],result['version'])
                    elif kind == 'pause_result' and result.get('status') == 'verified' and result.get('receipt'):
                        page=content_factory_cutover_ui.pause_done(session[1],result['receipt'])
                    elif kind == 'restore_preview': page=content_factory_cutover_ui.restore_preview(session[1],result['proposal_id'],result['receipt'])
                    elif kind == 'restore_result' and result.get('status') == 'verified':
                        page='<h1>Прежняя версия восстановлена</h1><p>n8n подтвердил прежнюю активную версию и настройки.</p>'
                    elif kind == 'draft_preview': page=content_factory_cutover_ui.draft_preview(session[1],result['proposal_id'],result['version'],result['changes'],result['pause_receipt'])
                    elif kind == 'draft_result' and result.get('status') == 'verified' and result.get('receipt'):
                        page=content_factory_cutover_ui.draft_saved(session[1],result['receipt'])
                    elif kind == 'publish_preview': page=content_factory_cutover_ui.publish_preview(session[1],result['proposal_id'],result['receipt'])
                    elif kind == 'publish_result' and result.get('status') == 'verified':
                        page='<h1>Версия опубликована</h1><p>n8n подтвердил указанную версию. Работоспособность ключей и стоимость моделей отдельно не проверены.</p>'
                    elif kind == 'rejected_before_send': page='<h1>Проверка не пройдена</h1><p>Изменение не отправлено. Проверьте настройки, наличие ожидающих выполнений и доступ к n8n.</p>'
                    else: page='<h1>Результат не подтверждён</h1><p>Не повторяйте действие до сверки состояния n8n.</p>'
                else: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте номер задачи.','400 Bad Request')
            except Exception: return reply('Задача недоступна.','503 Service Unavailable')
            return reply(document('Подключение сценария','<main id="content">'+page+'</main>',content_factory=True))
        if path in ('/settings/content-factory/ai/cutover/pause/prepare','/settings/content-factory/ai/cutover/pause/confirm', '/settings/content-factory/ai/cutover/restore/prepare','/settings/content-factory/ai/cutover/restore/confirm','/settings/content-factory/ai/cutover/draft/prepare','/settings/content-factory/ai/cutover/draft/confirm', '/settings/content-factory/ai/cutover/publish/prepare','/settings/content-factory/ai/cutover/publish/confirm'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения сценария войдите заново по письму.','403 Forbidden')
            if not self.model_config_enabled or self.ai_cutover is None or self.ai_cutover_jobs is None or not self.ai_gateway_available(): return reply('Подключение сценария недоступно.','503 Service Unavailable')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=8192),max_num_fields=4,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if not self.login.permit('cf-cutover:'+self.login.digest(sid),12,300): return reply('Слишком много попыток.','429 Too Many Requests')
                import content_factory_model_config, content_factory_cutover_jobs
                content_factory_model_config.read(self.vault)
                if not isinstance(self.vault.read(content_factory_model_config.READER_SLOT),dict): raise ValueError()
                owner=self.login.digest('cf-owner:' + self.login.recipient)
                if path.endswith('/pause/prepare'):
                    if set(form) != {'csrf'}: raise ValueError()
                    operation,payload='pause_prepare',{}
                elif path.endswith('/pause/confirm'):
                    if set(form) != {'csrf','proposal_id','ack_pause'} or form['ack_pause'] != 'understood': raise ValueError()
                    if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
                    operation,payload='pause_confirm',{'proposal_id':form['proposal_id']}
                elif path.endswith('/restore/prepare'):
                    if set(form) != {'csrf','receipt'}: raise ValueError()
                    receipt=json.loads(form['receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='restore_prepare',{'receipt':receipt}
                elif path.endswith('/restore/confirm'):
                    if set(form) != {'csrf','proposal_id','receipt'} or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
                    receipt=json.loads(form['receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='restore_confirm',{'proposal_id':form['proposal_id'],'receipt':receipt}
                elif path.endswith('/draft/prepare'):
                    if set(form) != {'csrf','pause_receipt'}: raise ValueError()
                    receipt=json.loads(form['pause_receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='draft_prepare',{'pause_receipt':receipt}
                elif path.endswith('/draft/confirm'):
                    if set(form) != {'csrf','proposal_id','pause_receipt'} or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
                    receipt=json.loads(form['pause_receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='draft_confirm',{'proposal_id':form['proposal_id'],'pause_receipt':receipt}
                elif path.endswith('/publish/prepare'):
                    if set(form) != {'csrf','receipt'}: raise ValueError()
                    receipt=json.loads(form['receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='publish_prepare',{'receipt':receipt}
                else:
                    if set(form) != {'csrf','proposal_id','receipt','ack_cost'} or form['ack_cost'] != 'understood': raise ValueError()
                    if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
                    receipt=json.loads(form['receipt'])
                    if not isinstance(receipt,dict): raise ValueError()
                    operation,payload='publish_confirm',{'proposal_id':form['proposal_id'],'receipt':receipt}
                ident=content_factory_cutover_jobs.start(self.ai_cutover_jobs,owner,operation,payload)
                page=('<h1>Проверка запущена</h1><p>Задача выполняется отдельно от кабинета.</p>'
                      '<a href="/settings/content-factory/ai/cutover/job?job='+html.escape(ident,quote=True)+'">Открыть состояние</a>')
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте запрос или состояние сценария.','400 Bad Request')
            except Exception: return reply('Не удалось подтвердить состояние сценария. Не повторяйте запись вслепую.','503 Service Unavailable')
            return reply(document('Подключение сценария', '<main id="content">'+page+'</main>', content_factory=True))
        if path in ('/settings/content-factory/ai/reader-credential/prepare','/settings/content-factory/ai/reader-credential/confirm'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для подключения войдите заново по письму.','403 Forbidden')
            if not self.model_config_enabled or not self.ai_gateway_available(): return reply('Подключение недоступно.','503 Service Unavailable')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=1024),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if not self.login.permit('cf-reader-credential:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                owner=self.login.digest('cf-owner:' + self.login.recipient)
                import content_factory_model_config, content_factory_ai_controls
                provisioner=content_factory_model_config.ReaderProvision(self.vault,self.ai_gateway)
                if path.endswith('/prepare'):
                    if set(form) != {'csrf'}: raise ValueError()
                    proposal=provisioner.prepare(owner)
                    page=content_factory_ai_controls.render_reader_confirmation(csrf,proposal)
                else:
                    if set(form) != {'csrf','proposal_id'} or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']): raise ValueError()
                    outcome=provisioner.confirm(owner,form['proposal_id'])
                    if outcome not in ('accepted_unconfirmed','unknown'): outcome='unknown'
                    page=content_factory_ai_controls.render_result(outcome,csrf,form['proposal_id'])
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте запрос подключения.','400 Bad Request')
            except Exception: return reply('Подключение недоступно; проверьте состояние шлюза.','503 Service Unavailable')
            return reply(document('Подключение чтения', '<main id="content">'+page+'</main>', content_factory=True))
        if path in ('/settings/content-factory/ai/models/prepare','/settings/content-factory/ai/models/confirm'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения моделей войдите заново по письму.','403 Forbidden')
            if not self.model_config_enabled or not self.ai_gateway_available(): return reply('Настройка моделей недоступна.','503 Service Unavailable')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=2048),max_num_fields=6,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if not self.login.permit('cf-model-settings:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                owner=self.login.digest('cf-owner:' + self.login.recipient)
                models={'text':form.get('text_model'), 'image':form.get('image_model')}
                if form.get('ack_estimate') != 'understood': raise ValueError()
                import content_factory_model_config, content_factory_ai_controls
                control=content_factory_model_config.Control(self.vault,self.ai_gateway.fence)
                if path.endswith('/prepare'):
                    if set(form) != {'csrf','text_model','image_model','ack_estimate'}: raise ValueError()
                    proposal=control.prepare(owner,models,acknowledge_estimate=True)
                    page=content_factory_ai_controls.render_model_confirmation(csrf,proposal['proposal_id'],proposal['changes'],revision=proposal['revision'])
                else:
                    if set(form) != {'csrf','proposal_id','revision','text_model','image_model','ack_estimate'}: raise ValueError()
                    if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',form['proposal_id']) or not re.fullmatch(r'0|[1-9][0-9]{0,15}',form['revision']): raise ValueError()
                    outcome=control.confirm(owner,form['proposal_id'],int(form['revision']),models,acknowledge_estimate=True)
                    page=('<h1>Конфигурация сохранена</h1><p>Сценарий n8n начнёт её читать только после отдельного подключения и проверки контроля расходов.</p>'
                          if outcome == 'verified' else '<h1>Результат не подтверждён</h1><p>Повторное изменение заблокировано до выяснения состояния.</p>')
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте настройки моделей.','400 Bad Request')
            except Exception: return reply('Изменение недоступно; проверьте состояние шлюза.','503 Service Unavailable')
            return reply(document('Настройки моделей', '<main id="content">'+page+'</main>', content_factory=True))
        if path in ('/settings/content-factory/ai/reconcile/inspect', '/settings/content-factory/ai/reconcile/confirm'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для сверки войдите заново по письму.','403 Forbidden')
            if not self.write_token_setup or self.ai_gateway is None or not callable(self.ai_gateway_inspector): return reply('Сверка недоступна.','503 Service Unavailable')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded',maximum=1024),max_num_fields=3,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                proposal=form.get('proposal_id','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',proposal): raise ValueError()
                owner=self.login.digest('cf-owner:' + self.login.recipient)
                if not self.login.permit('cf-reconcile:'+self.login.digest(sid),5,300): return reply('Слишком много попыток.','429 Too Many Requests')
                import content_factory_ai_controls
                if path.endswith('/inspect'):
                    if set(form) != {'csrf','proposal_id'}: raise ValueError()
                    self.ai_gateway.fence.halt()
                    observation=self.ai_gateway.fence.inspect_unknown(proposal,owner,lambda: True,self.ai_gateway_inspector)
                    page=content_factory_ai_controls.render_reconciliation(csrf,proposal,observation)
                else:
                    if set(form) != {'csrf','proposal_id','risk'} or form['risk'] != 'understood': raise ValueError()
                    self.ai_gateway.fence.confirm_unknown(proposal,owner,lambda: True,self.ai_gateway_inspector,risk_confirmed=True)
                    page='<h1>Сверка записана</h1><p>Неопределённый исход зафиксирован. Шлюз остаётся остановлен до выяснения результата.</p>'
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте запрос сверки.','400 Bad Request')
            except Exception: return reply('Сверка недоступна или состояние изменилось. Шлюз остаётся остановлен.','503 Service Unavailable')
            return reply(document('Сверка изменения', '<main id="content">'+page+'</main>', content_factory=True))
        if path in ('/settings/content-factory/ai/credentials/prepare', '/settings/content-factory/ai/credentials/commit', '/settings/content-factory/ai/credentials/provision/prepare', '/settings/content-factory/ai/credentials/provision/commit'):
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключей ИИ войдите заново по письму.','403 Forbidden')
            # Refuse before reading the body when the real gateway is unavailable.
            if not self.ai_gateway_available(): return reply('Шлюз изменения ключей ИИ недоступен.','503 Service Unavailable')
            try:
                import content_factory_ai_controls
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=8192),max_num_fields=4,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form = {k:v[0] for k,v in form.items()}
                csrf = form.get('csrf', '')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                provider = form.get('provider')
                if provider not in ('openai', 'gemini'): raise ValueError()
                owner = self.login.digest('cf-owner:' + self.login.recipient)
                provision = '/provision/' in path
                if path.endswith('/prepare'):
                    if set(form) != {'csrf','provider'}: raise ValueError()
                    proposal = (self.ai_gateway.prepare_provision(owner, provider) if provision else self.ai_gateway.prepare_credential(owner, provider))
                    page = content_factory_ai_controls.render_key_confirmation(csrf, proposal, provider, provision=provision)
                    return reply(document('Подтверждение ключа', '<main id="content">' + page + '</main>', content_factory=True))
                if set(form) != {'csrf','provider','proposal_id','api_key'}: raise ValueError()
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', form['proposal_id']): raise ValueError()
                if not re.fullmatch(r'[!-~]{16,2048}', form['api_key']): raise ValueError()
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте запрос изменения ключа ИИ.','400 Bad Request')
            except Exception:
                return reply('Не удалось подготовить изменение ключа ИИ.','503 Service Unavailable')
            try:
                outcome = (self.ai_gateway.commit_provision(owner, form['proposal_id'], provider, form['api_key']) if provision
                           else self.ai_gateway.commit_credential(owner, form['proposal_id'], provider, form['api_key']))
            except Exception:
                # Any exception after dispatch could conceal a successful write.
                outcome = 'unknown'
            if outcome == 'cancelled': outcome = 'rejected_before_send'
            if outcome not in ('verified','accepted_unconfirmed','rejected_before_send','unknown'): outcome = 'unknown'
            page = content_factory_ai_controls.render_result(outcome, csrf, form['proposal_id'])
            return reply(document('Результат изменения ключа', '<main id="content">' + page + '</main>', content_factory=True))
        if path == '/settings/content-factory/models':
            if method != 'POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN') != self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения настроек ИИ войдите заново по письму.','403 Forbidden')
            try:
                import content_factory_ai
                form = parse_qs(body('application/x-www-form-urlencoded',maximum=8192),max_num_fields=4,keep_blank_values=True,strict_parsing=True)
                if any(len(v) != 1 for v in form.values()): raise ValueError()
                form = {k:v[0] for k,v in form.items()}
                csrf = form.get('csrf', '')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if set(form) == {'csrf','text_model','image_model','video_model'}:
                    content_factory_ai.save_models(self.vault, form['text_model'], form['image_model'], form['video_model'])
                else: raise ValueError()
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте настройки ИИ.', '400 Bad Request')
            except (sqlite3.Error,OSError):
                return reply('Не удалось сохранить настройки ИИ.', '503 Service Unavailable')
            except Exception:
                # Do not expose errors from encryption or storage, or the submitted values.
                return reply('Не удалось сохранить настройки ИИ.', '503 Service Unavailable')
            start('303 See Other',[('Location','/settings?tab=content-factory'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path == '/settings' and 'tab=content-factory' in e.get('QUERY_STRING', ''):
            if method != 'GET': return reply('Method not allowed','405 Method Not Allowed')
            try:
                parsed = parse_qs(e.get('QUERY_STRING', ''), max_num_fields=4, keep_blank_values=True)
                if parsed != {'tab': ['content-factory']}: raise ValueError()
                import content_factory, content_factory_ui
                snapshot = content_factory.snapshot(self.vault)
                import content_factory_ai
                snapshot.update(content_factory_ai.snapshot(self.vault))
                budget_ready = self.budget_gateway_available()
                budget_settings = self.budget_gateway.settings() if budget_ready else None
                token_present = self.write_token_setup and 'n8n_content_factory_write' in self.vault.present()
                return reply(document('Контент-фабрика', content_factory_ui.render(snapshot, csrf=session[1], ai_gateway_ready=self.ai_gateway_available(), budget_gateway_ready=budget_ready, budget_settings=budget_settings, write_token_setup=self.write_token_setup, write_token_present=token_present, budget_gateway_configured=self.budget_gateway is not None, ai_gateway_configured=self.ai_gateway is not None, model_config_enabled=self.model_config_enabled, cutover_configured=self.ai_cutover is not None), content_factory=True))
            except (ValueError, TypeError, UnicodeError):
                return reply('Проверьте параметры запроса.', '400 Bad Request')
            except (sqlite3.Error, OSError):
                return reply('Данные контент-фабрики временно недоступны.', '503 Service Unavailable')
            except Exception:
                return reply('Данные контент-фабрики временно недоступны.', '503 Service Unavailable')
        if path == '/settings' and 'tab=calculator' in e.get('QUERY_STRING', ''):
            import calculator_ui
            try:
                parsed = parse_qs(e.get('QUERY_STRING', ''), max_num_fields=20)
                if parsed.get("tab") == ["calculator"] and all(len(v) == 1 for v in parsed.values()):
                    query = {k: value[0] for k, value in parsed.items()}
                    return calculator_ui.handle(self, e, method, session, query, body, reply, start)
            except cost_catalog.CatalogError as exc:
                return reply(html.escape(str(exc)) + ' <a href="/settings?tab=calculator">Калькулятор</a>', '400 Bad Request')
            except (ValueError, TypeError, UnicodeError, KeyError):
                return reply('Проверьте поля калькулятора.', '400 Bad Request')
            except (sqlite3.Error, OSError):
                return reply('Хранилище калькулятора временно недоступно.', '503 Service Unavailable')
        if path=='/settings/catalog':
            try:
                return market_actions.handle(self,e,method,session,now,body,reply,start)
            except market_actions.ActionError as exc:
                return reply(html.escape(str(exc))+' <a href="/settings/catalog">Операции</a>','409 Conflict')
            except (ValueError,TypeError,UnicodeError):
                return reply('Проверьте параметры запроса.','400 Bad Request')
            except (sqlite3.Error,OSError):
                return reply('Хранилище операций временно недоступно.','503 Service Unavailable')
        if path in ('/settings/mcp/create','/settings/mcp/revoke'):
            if method!='POST': return reply('Method not allowed','405 Method Not Allowed')
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=4,keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if path=='/settings/mcp/create':
                    if set(form) not in ({'csrf','request_id','name'}, {'csrf','request_id','name','access'}): raise ValueError()
                    access=form.get('access','marketplaces')
                    if access.startswith('catalog_') and session[0]-43200 < now-300:
                        return reply('Для выдачи доступа к полному каталогу войдите заново по ссылке из письма.','403 Forbidden')
                    name=form['name'].strip()
                    _,token=self.external_keys.create(name,form['request_id'],now,access)
                    return reply(document('Ключ создан',external_keys.render_created(name,token,self.login.origin,access)))
                if set(form)!={'csrf','key_id'}: raise ValueError()
                if not self.external_keys.revoke(form['key_id'],now): return reply('Ключ уже отозван или не найден. <a href="/settings">Настройки</a>','409 Conflict')
            except external_keys.DuplicateRequestError:
                return reply('Этот запрос уже выполнен. Создайте новый ключ в настройках.','409 Conflict')
            except external_keys.KeyLimitError:
                return reply('Достигнут лимит активных ключей для этих площадок. Отзовите ненужный ключ.','409 Conflict')
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте введённые значения.','400 Bad Request')
            except sqlite3.Error: return reply('Не удалось изменить ключ. Повторите позже.','503 Service Unavailable')
            start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]); return [b'']
        if path in ('/settings/check','/settings/preview','/settings/wb-lookup'):
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                lookup=path=='/settings/wb-lookup'
                if set(form)!=({'nm_id','csrf'} if lookup else {'provider','csrf'}) or any(len(v)!=1 for v in form.values()): raise ValueError()
                provider='wb' if lookup else form['provider'][0]; csrf=form['csrf'][0]
                nm_id=None
                if lookup:
                    article=form['nm_id'][0]
                    if not re.fullmatch(r'[1-9][0-9]{0,18}',article) or int(article)>9223372036854775807: raise ValueError()
                    nm_id=int(article)
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if provider not in CHECK_OPERATIONS or (provider=='avito' and path!='/settings/check'): raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply('Некорректный запрос.','400 Bad Request')
            # Global owner-level buckets persist across workers/sessions and prevent click storms.
            try:
                allowed=self.login.permit('connection-check:'+provider,CHECK_PROVIDER_LIMIT,CHECK_PROVIDER_WINDOW,busy_timeout_ms=500) and self.login.permit('connection-check:all',CHECK_GLOBAL_LIMIT,CHECK_GLOBAL_WINDOW,busy_timeout_ms=500)
            except sqlite3.Error:
                return reply('Проверка временно недоступна. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
            if not allowed:
                return reply('Слишком много запросов. Для проверки или просмотра одного кабинета доступен один запрос в минуту, всего — не более 10 в час. <a href="/settings">Настройки</a>','429 Too Many Requests')
            try:
                snapshot=self.vault.snapshot(provider)
                if not snapshot: return reply('Сначала сохраните ключ. <a href="/settings">Настройки</a>','409 Conflict')
                creds,version=snapshot
            except Exception: return reply('Не удалось прочитать настройки. <a href="/settings">Настройки</a>','503 Service Unavailable')
            try:
                arguments={} if provider=='avito' else {'limit':20 if path=='/settings/preview' else 1}
                if provider=='ozon' and path=='/settings/check': arguments['include_identity']=False
                if lookup: arguments['nm_id']=nm_id
                result=self.read_gateway(CHECK_OPERATIONS[provider],arguments,creds)
                if not isinstance(result,dict) or not isinstance(result.get('items'),list): raise ValueError('Invalid response')
                if lookup and (len(result['items'])>1 or any(not isinstance(row,dict) or type(row.get('nmID')) is not int or row['nmID']!=nm_id for row in result['items'])):
                    raise ValueError('Unexpected article response')
                outcome='ok'
            except urllib.error.HTTPError as exc:
                outcome='denied' if exc.code in (401,403) else 'rate_limited' if exc.code==429 else 'unavailable' if 500<=exc.code<=599 else 'failed'
                exc.close()
            except finance_gate.GateError as exc:
                outcome='rate_limited' if exc.reason in ('busy','cooldown','retry_time_unknown') else 'unavailable'
            except (TimeoutError,urllib.error.URLError): outcome='unavailable'
            except Exception: outcome='failed'
            if path in ('/settings/preview','/settings/wb-lookup'):
                # Revalidate after the bounded upstream call: logout/expiry or key replacement
                # must not return a snapshot from an old session/account.
                try:
                    with self.login.connect() as c:
                        c.execute('PRAGMA busy_timeout=500')
                        live=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                    unchanged=self.vault.version_matches(provider,version)
                except Exception:
                    return reply('Не удалось проверить доступ к данным. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
                if not live: return reply('Войдите заново на главной странице.','401 Unauthorized')
                if not unchanged:
                    return reply('Ключ изменился во время запроса. Повторите просмотр. <a href="/settings">Настройки</a>','409 Conflict')
                if outcome!='ok': return reply(CHECK_MESSAGES[outcome]+' <a href="/settings">Настройки</a>','502 Bad Gateway')
                try:
                    return reply(render_preview(provider,result,self.login.clock(),creds.values(),lookup_nm_id=nm_id if lookup else None))
                except (ValueError,TypeError,KeyError):
                    return reply('Ответ маркетплейса не удалось отобразить. <a href="/settings">Настройки</a>','502 Bad Gateway')
            try:
                if not self.vault.record_check(provider,version,outcome,self.login.clock()):
                    return reply('Ключ был заменён во время проверки. Проверьте новый ключ. <a href="/settings">Настройки</a>','409 Conflict')
            except Exception: return reply('Не удалось сохранить результат проверки. <a href="/settings">Настройки</a>','503 Service Unavailable')
            # Never render/log upstream payloads or exception strings. PRG prevents refresh resubmission.
            start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]); return [b'']
        if method=='POST':
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключей войдите заново по письму.','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=6,keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf',''); provider=form.get('provider','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                fields=FIELDS.get(provider)
                if provider in ('avito','performance'):
                    fields=('client_id','client_secret')
                elif provider=='wb_digital':
                    fields=('digital_token',)
                elif provider in market_health.WB_CATEGORIES or provider=='wb_write' or provider in {v[0] for v in market_catalog.WB_EXTRA.values()}:
                    fields=('api_token',)
                elif provider=='ozon_write':
                    fields=FIELDS['ozon']
                if not fields or set(form)!=set(fields)|{'csrf','provider'}: raise ValueError()
                values={k:form[k] for k in fields}
                if provider=='wb_digital':
                    values['digital_token']=values['digital_token'].strip().removeprefix('Bearer ')
                if any(not re.fullmatch(r'[!-~]{1,2048}',v) for v in values.values()): raise ValueError()
                if provider in ('ozon','ozon_write') and not re.fullmatch(r'[0-9]{1,32}',values['client_id']): raise ValueError()
                if provider=='avito' and len(values['client_id']) > 128: raise ValueError()
                if provider == 'wb' or provider in market_health.WB_CATEGORIES:
                    from market_transport import remember_wb_feed_credentials
                    remember_wb_feed_credentials(self.finance_gate_dir,self.vault.read(provider),values)
                self.vault.save(provider,values)
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте введённые значения.','400 Bad Request')
            except Exception: return reply('Не удалось сохранить настройки.','503 Service Unavailable')
            return reply('Ключ сохранён зашифрованно. Соединение ещё не проверено. <a href="/settings">Настройки</a>')
        try:
            configured=self.vault.present()
        except sqlite3.Error:
            return reply('Настройки временно недоступны. Повторите позже.','503 Service Unavailable')
        content='<main id="content"><p class="eyebrow">Интеграции</p><h1>Подключение кабинетов</h1>'
        content+='<p class="intro">Подключите кабинеты Ozon, Wildberries и Авито. Для каждого кабинета используйте ключ с нужными правами чтения.</p>'
        content+='<p>Ключи хранятся зашифрованно и не отображаются после сохранения. Сохранение ключа не подтверждает его действительность и не подключает MCP автоматически.</p>'
        content+='<div class="steps" aria-label="Порядок подключения"><div class="step"><b><span>01</span>Добавьте ключ</b>Доступ только на чтение</div><div class="step"><b><span>02</span>Проверьте связь</b>Один безопасный запрос</div><div class="step"><b><span>03</span>Откройте данные</b>Первые товары и цены</div></div>'
        content+='<div class="session-note"><p>Сохранить или заменить ключи Ozon, Wildberries и Авито можно в течение первых 5 минут после входа по письму. Затем выйдите и запросите новую ссылку. Ключи для подключения ИИ можно создавать и отзывать в течение всего сеанса.</p>'
        content+='<form method="post" action="/logout"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Выйти для повторного входа</button></form></div>'
        if session[0]-43200 < now-300:
            content+='<p role="status">Время для изменения ключей маркетплейсов истекло. Для их сохранения сначала выйдите и получите новую ссылку входа. Создание и отзыв ключей для ИИ по-прежнему доступны.</p>'
        names={'ozon':'Ozon','wb':'Wildberries'}
        labels={'client_id':'Идентификатор продавца (Client ID)', 'api_key':'API-ключ Ozon', 'api_token':'API-токен Wildberries'}
        content+='<div class="market-grid">'
        for provider,fields in FIELDS.items():
            saved=provider in configured
            content+=f'<section class="market-card {provider}" aria-labelledby="{provider}-title"><div class="market-head"><span class="market-icon" aria-hidden="true">{"O" if provider=="ozon" else "WB"}</span><div><h2 id="{provider}-title">{names[provider]}</h2><small>КАБИНЕТ ПРОДАВЦА</small></div></div>'
            content+='<p class="status '+('status-saved' if saved else '')+'">Ключ: '+('сохранён зашифрованно' if saved else 'не добавлен')+'.</p>'
            try:
                check=self.vault.check_status(provider) if saved else None
            except sqlite3.Error:
                return reply('Настройки временно недоступны. Повторите позже.','503 Service Unavailable')
            if check:
                outcome,checked_at=check
                stamp=datetime.fromtimestamp(checked_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                scope='список товаров Ozon' if provider=='ozon' else 'цены и скидки Wildberries'
                content+='<p>Последняя проверка: '+stamp+'. '+CHECK_MESSAGES[outcome]+'</p>'
                content+='<p>Область проверки — только операция чтения: '+scope+'. Это результат на указанное время, не подтверждение всех прав или подключения MCP.</p>'
            else:
                content+='<p class="status">Соединение с маркетплейсом: не проверено. '+('Срок действия и права ключа пока не подтверждены.' if saved else 'Сначала добавьте ключ.')+'</p>'
            if saved:
                content+='<details class="details"><summary>Лимиты запросов и условия проверки</summary>'
                try:
                    budget=self.request_budget(provider,self.login.clock())
                    content+='<p>Локальный лимит портала на момент загрузки страницы: осталось до '+str(budget['hourly_remaining'])+' из '+str(CHECK_GLOBAL_LIMIT)+' запросов за скользящий час для обоих кабинетов вместе.</p>'
                    if budget['retry_seconds']:
                        content+='<p>Следующая попытка для этого кабинета по локальному лимиту — не раньше чем через '+str(budget['retry_seconds'])+' с после загрузки этой страницы.</p>'
                    else:
                        content+='<p>Локальный лимит сейчас допускает попытку запроса.</p>'
                    content+='<p>Это не резервирование запроса и не гарантия доступности API. Другая вкладка может израсходовать лимит. <a href="/settings">Обновить сведения о лимите</a>. Обновление страницы не обращается к маркетплейсам.</p>'
                except (sqlite3.Error,ValueError,TypeError,OverflowError):
                    content+='<p>Не удалось оценить локальный лимит. Он будет проверен при отправке запроса. <a href="/settings">Обновить страницу</a>.</p>'
                content+='</details>'
                content+='<form class="secondary" method="post" action="/settings/check"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Проверить подключение</button></form>'
                content+='<p>Проверка прочитает до одной записи через API маркетплейса, не меняя товары, цены или заказы.</p>'
                label='Показать первые товары Ozon' if provider=='ozon' else 'Показать первые цены Wildberries'
                content+='<form class="secondary" method="post" action="/settings/preview"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>'+label+'</button></form>'
                content+='<p>Просмотр загрузит только первую страницу, до 20 товаров. Для Ozon название и SKU запрашиваются дополнительно из карточек. Данные не сохраняются в кабинете. Проверка и просмотр используют общий лимит: один запрос кабинета в минуту, всего не более 10 запросов в час.</p>'
                if provider=='wb':
                    content+='<form class="save-form" method="post" action="/settings/wb-lookup"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><label for="wb-lookup-id">Артикул Wildberries (nmID), не артикул продавца</label><input id="wb-lookup-id" type="text" name="nm_id" inputmode="numeric" pattern="[1-9][0-9]{0,18}" maxlength="19" required><button>Показать цены этого товара</button></form><p>Поиск читает до одного товара по точному артикулу и использует тот же общий лимит проверки и просмотра. Товары, цены и заказы не изменяются.</p>'
            if provider=='ozon': content+='<p>Введите Client ID и API-ключ из кабинета продавца Ozon. Оба значения нужно сохранить вместе.</p>'
            else: content+='<p>Введите API-токен из кабинета продавца Wildberries с правами чтения цен и скидок.</p><p>Для инструментов MCP выберите нужные категории доступа WB: Поставки, Статистика, Аналитика, Финансы, Продвижение, Вопросы и отзывы. Категория Контент нужна для поиска по артикулу.</p>'
            content+='<form class="save-form" method="post" action="/settings"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'">'
            for field in fields:
                ident=provider+'-'+field
                content+=f'<p class="field"><label for="{ident}">{labels[field]}</label><input id="{ident}" type="password" name="{field}" autocomplete="off" required maxlength="2048"></p>'
            content+='<button>'+('Заменить сохранённый ключ' if saved else 'Сохранить ключ')+'</button></form></section>'
        content+='</div><section class="mcp-card"><h2>Ключи Wildberries по категориям</h2>'
        if session[0]-43200 >= now-300:
            content+='<form class="save-form" method="post" action="/settings"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><label for="wb-category">Категория токена</label><select name="provider" id="wb-category">'
            for key,label in market_health.WB_CATEGORIES.items():
                content+='<option value="'+key+'">'+label+(' (сохранён)' if key in configured else '')+'</option>'
            content+='</select><label for="category-token">Токен WB</label><input id="category-token" name="api_token" type="password" autocomplete="off" maxlength="2048" required><button>Сохранить токен категории</button></form>'
        else: content+='<p>Для сохранения отдельного токена войдите по новой ссылке из письма.</p>'
        content+='</section><section class="market-card" aria-labelledby="avito-title"><h2 id="avito-title">Авито</h2>'
        content+='<p>Объявления, заказы, переписка и статистика своего кабинета через Avito API.</p>'
        csrf_value=html.escape(session[1], quote=True)
        if 'avito' in configured:
            check=self.vault.check_status('avito')
            content+='<p class="status">Данные приложения сохранены зашифрованно.</p>'
            if check:
                content+='<p>Последняя проверка '+external_keys._stamp(check[1])+' — '+CHECK_MESSAGES[check[0]]+'</p>'
            content+='<form method="post" action="/settings/check"><input type="hidden" name="provider" value="avito"><input type="hidden" name="csrf" value="'+csrf_value+'"><button>Проверить подключение Авито</button></form>'
        else:
            content+='<p>Для подключения нужны Client ID и Client Secret приложения Авито.</p>'
        if session[0]-43200 >= now-300:
            content+='<form class="save-form" method="post" action="/settings"><input type="hidden" name="provider" value="avito"><input type="hidden" name="csrf" value="'+csrf_value+'">'
            content+='<label for="avito-client">Client ID Авито</label><input id="avito-client" name="client_id" type="text" autocomplete="off" maxlength="128" required>'
            content+='<label for="avito-secret">Client Secret Авито</label><input id="avito-secret" name="client_secret" type="password" autocomplete="off" maxlength="2048" required><button>Сохранить данные Авито</button></form>'
        else:
            content+='<p>Для изменения данных Авито войдите по новой ссылке из письма.</p>'
        content+='</section><section class="mcp-card"><h2>Реклама Ozon Performance</h2>'
        content+='<p>'+('Рекламные ключи сохранены.' if 'performance' in configured else 'Для статистики рекламы нужны отдельные Client ID и Client Secret Ozon Performance.')+'</p>'
        if session[0]-43200 >= now-300:
            content+='<form class="save-form" method="post" action="/settings"><input type="hidden" name="provider" value="performance"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'">'
            for key,label in (('client_id','Client ID Performance'),('client_secret','Client Secret Performance')):
                content+='<label for="performance-'+key+'">'+label+'</label><input id="performance-'+key+'" name="'+key+'" type="password" autocomplete="off" required maxlength="2048">'
            content+='<button>Сохранить рекламные ключи</button></form>'
        content+='</section><section class="mcp-card" aria-labelledby="mcp-title"><h2 id="mcp-title">Связь с MCP</h2><div>'
        try:
            for source,label in (('mcp','MCP'),('bridge','другого подключённого клиента')):
                activity=self.vault.bridge_activity(source)
                if activity:
                    operation,outcome,completed_at=activity
                    stamp=datetime.fromtimestamp(completed_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                    content+='<p><strong>Последнее обращение '+label+':</strong> '+stamp+' — '+BRIDGE_OPERATIONS[operation]+', '+BRIDGE_OUTCOMES[outcome]+'.</p>'
                else:
                    content+='<p><strong>Обращений '+label+' пока не зафиксировано.</strong></p>'
        except (sqlite3.Error,ValueError,TypeError,OverflowError,OSError):
            content+='<p>Не удалось загрузить сведения об обращениях к API кабинета.</p>'
        content+='<p>Показаны последние завершённые обращения к API кабинета с момента включения учёта. MCP определяется по отдельному ключу доступа и появится здесь, только если интеграция использует этот API; проверки и просмотр в кабинете не учитываются. Это не проверка текущего подключения MCP. <a href="/settings">Обновить сведения</a>.</p></div></section>'
        try:
            content+=external_keys.render_section(self.external_keys,session[1],self.login.origin)
        except (sqlite3.Error,ValueError,TypeError,OverflowError,OSError):
            content+='<p>Не удалось загрузить ключи для других ИИ. Повторите позже.</p>'
        try:
            import yandex_backup_admin
            content+=yandex_backup_admin.render(self.vault,session[1],session[0]-43200 >= now-300)
        except (sqlite3.Error,ValueError,TypeError,OverflowError,OSError):
            content+='<p>Не удалось загрузить настройки резервирования Яндекс Диска.</p>'
        content+='</main>'
        return reply(document('Подключение кабинетов',content,progress=True))

def secret_file(name, binary=False):
    path=os.environ[name]
    with open(path,'rb') as f: raw=f.read(8193)
    if len(raw)>8192: raise ValueError('Invalid secret file')
    return raw if binary else raw.decode().strip()

def file_smtp_mailer(recipient, link):
    send_login_email(recipient,link,'no-reply@3dprinta.shop',secret_file('SMTP_PASSWORD_FILE'))

def create_portal():
    # Production file mounts are configured by the operator, never by request input.
    login=LoginApp(os.environ['DATABASE_PATH'],secret_file('SESSION_SECRET_FILE'),os.environ.get('BASE_URL','https://3dprinta.shop'),mailer=file_smtp_mailer)
    vault=Vault(os.environ['VAULT_PATH'],secret_file('VAULT_KEY_FILE',True))
    mcp_hash=secret_file('MCP_READ_TOKEN_HASH_FILE') if 'MCP_READ_TOKEN_HASH_FILE' in os.environ else None
    import content_factory_gateway_config
    control, inspector, write_token_setup = content_factory_gateway_config.configure(os.environ, vault, secret_file)
    import content_factory_budget_config
    budget_control = content_factory_budget_config.configure_budget(os.environ, vault, secret_file, control)
    write_token_setup = write_token_setup or budget_control is not None
    model_config_flag = os.environ.get('CF_MODEL_CONFIG_ENABLED','0')
    if model_config_flag not in ('0','1'): raise ValueError('Invalid model configuration flag')
    if model_config_flag == '1' and (os.environ.get('CF_MODEL_CONFIG_STAGING_VERIFIED') != '1' or control is None
                                     or control.model_reader_id is None):
        raise ValueError('Model configuration staging and gateway required')
    cutover_flag=os.environ.get('CF_CUTOVER_ENABLED','0')
    if cutover_flag not in ('0','1'): raise ValueError('Invalid cutover flag')
    cutover=None
    cutover_jobs=None
    if cutover_flag == '1':
        if (model_config_flag != '1' or control is None or os.environ.get('CF_CUTOVER_STAGING_VERIFIED') != '1'
                or os.environ.get('CF_CUTOVER_JOB_STAGING_VERIFIED') != '1'):
            raise ValueError('Verified model configuration cutover required')
        from content_factory_cutover import Cutover, BoundedWire
        from content_factory_cutover_jobs import Store
        from content_factory_gateway_wiring import bounded_cutover_preflight
        ids={**control.ids,'reader':control.model_reader_id}
        cutover=Cutover(control.fence,BoundedWire(control.token_reader),ids,
                        lambda stage, targets: bounded_cutover_preflight(control.token_reader(),stage,targets))
        cutover_jobs=Store(control.fence.path,control.fence.key)
    return Portal(login,vault,secret_file('READ_TOKEN_HASH_FILE'),mcp_read_token_hash=mcp_hash,
                  finance_gate_dir=os.environ.get('OZON_FINANCE_GATE_DIR'), ai_gateway=control,
                  ai_gateway_inspector=inspector, budget_gateway=budget_control, write_token_setup=write_token_setup,
                  model_config_enabled=model_config_flag == '1', ai_cutover=cutover, ai_cutover_jobs=cutover_jobs)

if _worker_cli:
    if sys.argv[2:]:
        if len(sys.argv)!=3 or sys.argv[2] not in PREPARED_OPERATIONS:
            raise SystemExit(2)
        try:
            _prepare_provider_worker(sys.argv[2])
        except Exception:
            raise SystemExit(1) from None
        os.write(1,b'R')
    _provider_worker()
