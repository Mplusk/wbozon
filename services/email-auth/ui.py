"""Static, dependency-free interface. CSS is allowed by an exact CSP hash only."""
import base64, hashlib, html
CSS = '''
:root{color-scheme:light;--ink:#172821;--muted:#66736b;--green:#23744f;--line:#dce4dd;--paper:#f3f5ef}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}a{color:var(--green);text-underline-offset:3px}a:hover{color:#145338}a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #cc8a28;outline-offset:4px}.skip{position:absolute;left:20px;top:-80px;background:white;padding:10px;z-index:9}.skip:focus{top:10px}.shell{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}.sidebar{background:#172c24;color:#fff;padding:34px 24px;display:flex;flex-direction:column;gap:42px}.brand{font-size:26px;font-weight:800;letter-spacing:5px;color:inherit;text-decoration:none}.brand small{display:block;letter-spacing:2px;font-size:10px;font-weight:500;color:#b6cabe;margin-top:3px}.nav-label,.eyebrow{font-size:11px;letter-spacing:1.7px;text-transform:uppercase;font-weight:700}.nav-label{color:#b1c4b8;margin-bottom:14px}.sidebar nav{display:grid;gap:8px}.sidebar nav a{padding:12px 15px;border-radius:8px;color:#fff;text-decoration:none;background:#2b4638;font-weight:600}.sidebar-foot{margin-top:auto;font-size:12px;color:#bdd0c3;border-top:1px solid #375343;padding-top:22px}.main{padding:34px clamp(22px,4vw,64px) 54px;min-width:0}.topline{display:flex;justify-content:space-between;gap:20px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:34px;color:var(--muted);font-size:12px}.tag{border:1px solid #c7d7ca;background:#e8f0e6;padding:5px 11px;border-radius:30px;color:#365f44;font-size:11px;font-weight:700;white-space:nowrap}.eyebrow{color:var(--green);margin:0 0 12px}h1{font-size:clamp(28px,3.2vw,42px);line-height:1.15;letter-spacing:-1.4px;margin:0 0 18px;font-weight:700}h2{font-size:22px;line-height:1.25;letter-spacing:-.5px;margin:0 0 16px}p{margin:0 0 16px}.intro{max-width:760px;color:var(--muted);font-size:16px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:28px 0}.step{border-top:2px solid #cbd8ca;padding:13px 2px;font-size:12px;color:var(--muted)}.step b{display:block;color:var(--ink);font-size:14px;margin-bottom:4px}.step span{color:var(--green);margin-right:8px}.market-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px;align-items:start;margin:28px 0}.market-card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:28px;box-shadow:0 6px 18px #162b2005}.market-head{display:flex;gap:14px;align-items:center;margin-bottom:24px;padding-bottom:22px;border-bottom:1px solid #e8ece7}.market-head h2{margin:0}.market-head small{color:var(--muted);font-size:11px}.market-icon{display:grid;place-items:center;flex:none;width:46px;height:46px;border-radius:12px;background:#edf2ff;color:#1c52ed;font-weight:800;font-size:22px;letter-spacing:-1px}.wb .market-icon{background:#f7eaf8;color:#a222a5;font-size:17px}.status{font-size:13px;padding:11px 14px;border-radius:8px;background:#f3f5ef;border:1px solid #e4e9df}.status-saved{background:#eaf4ed;color:#245d3b}.market-card p{font-size:13px;color:#526158}.market-card .status{color:var(--ink)}.field{margin:16px 0!important}label{display:block;font-size:12px;font-weight:650;color:#33483b;margin-bottom:7px}input[type=email],input[type=password],input[type=text]{width:100%;min-height:46px;border:1px solid #cbd6ce;border-radius:8px;background:#fafcf9;padding:11px 13px;font:inherit;color:var(--ink)}button,.button{display:inline-flex;justify-content:center;align-items:center;min-height:44px;border:0;border-radius:8px;padding:12px 18px;background:var(--green);color:white;font-size:13px;line-height:1.4;font-weight:600;font-family:inherit;cursor:pointer;text-decoration:none}button:hover,.button:hover{background:#185f3e;color:white}button:disabled{background:#d8dfd8;color:#6a766d;cursor:not-allowed}.save-form button{width:100%;margin-top:6px}.secondary button{background:#edf2ec;color:#315640;border:1px solid #cfdbcf;margin-bottom:10px}.session-note,.notice{background:#e9efe4;border:1px solid #d5dfcc;border-radius:10px;padding:18px 20px;font-size:13px;margin-top:24px}.session-note{display:flex;gap:24px;align-items:center;justify-content:space-between}.session-note p{margin:0;max-width:750px;color:#516047}.session-note button{background:transparent;color:#315640;border:1px solid #bdccba;white-space:nowrap;font-size:12px}.mcp-card{padding:24px;border-radius:12px;border:1px solid var(--line);background:#f9faf6;display:flex;gap:24px;align-items:start}.mcp-card h2{font-size:17px;min-width:180px}.mcp-card p{font-size:13px;color:var(--muted);margin:0}.details{border-top:1px solid var(--line);padding-top:14px;margin-top:18px;font-size:12px;color:var(--muted)}summary{cursor:pointer;font-weight:600;color:#4b6152;margin-bottom:10px}.footer{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding-top:20px;margin-top:34px;font-size:11px;color:#6b786e;gap:12px}.auth-layout{min-height:100vh;display:grid;grid-template-columns:1fr 1fr}.auth-story{background:#172c24;color:white;padding:50px clamp(30px,7vw,110px);display:flex;flex-direction:column}.auth-story h2{font-size:clamp(30px,4vw,54px);line-height:1.15;max-width:500px;margin-top:auto;padding-top:80px;letter-spacing:-1.5px}.auth-story p{color:#c3d1c7;max-width:420px}.auth-art{height:120px;margin:40px 0;border-top:1px solid #59765f;border-bottom:1px solid #59765f;background:repeating-linear-gradient(0deg,transparent,transparent 12px,#385443 13px,transparent 14px);transform:skewY(-8deg);opacity:.7}.auth-main{display:flex;align-items:center;justify-content:center;padding:42px 28px}.auth-card{max-width:420px;width:100%}.auth-card h1{font-size:32px}.auth-card form{margin:26px 0}.auth-card button{width:100%;margin-top:18px}.auth-card .fineprint{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:18px}.data-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.data-grid section{background:white;border:1px solid var(--line);border-radius:12px;padding:24px}dl{display:grid;grid-template-columns:1fr 1fr;gap:8px 20px;font-size:13px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere;font-weight:600}main{min-width:0}.error-card{max-width:600px;margin:50px auto;padding:32px;background:white;border:1px solid var(--line);border-radius:14px}p,dd{overflow-wrap:anywhere}@media(max-width:1050px){.shell{grid-template-columns:180px 1fr}.sidebar{padding:28px 18px}.market-card{padding:22px}.main{padding:28px}.market-grid{grid-template-columns:1fr}.auth-story{padding:40px}}@media(max-width:650px){.shell{display:block}.sidebar{padding:18px 22px;gap:12px;flex-direction:row;align-items:center;justify-content:space-between}.brand{font-size:20px}.brand small{font-size:8px}.sidebar .nav-label,.sidebar-foot{display:none}.sidebar nav a{padding:8px 12px;font-size:11px}.main{padding:24px 18px}.topline{margin-bottom:26px;padding-bottom:16px}.topline span:first-child{font-size:11px}.steps{gap:10px}.step{font-size:11px}.step b{font-size:12px}.market-grid{gap:16px}.session-note,.mcp-card{display:block}.session-note form{margin-top:14px}.mcp-card h2{margin-bottom:10px}.auth-layout{display:block}.auth-story{padding:24px}.auth-story h2,.auth-story p,.auth-art{display:none}.auth-main{padding:46px 24px}.data-grid{grid-template-columns:1fr}dl{grid-template-columns:1fr 1fr}.footer{flex-direction:column}}
'''
CSS += """
.calculator{max-width:1400px;margin:auto}.calculator h2{margin:28px 0 14px}.calculator h3{margin:24px 0 12px}
.calc-tabs,.calc-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 22px}
.calc-tabs a,.calc-chip{border:1px solid var(--line);background:#fff;border-radius:7px;padding:7px 11px;text-decoration:none}
.calc-tabs a[aria-current=page]{background:#e4efe6;border-color:#a7c4ae;color:#234f37;font-weight:700}
.calc-section-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.calc-section-title h2{margin:0}
.calc-filters,.calc-grid,.calc-input-row{display:grid;gap:12px;align-items:end;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin:14px 0}
.calc-input-row{gap:8px;padding:10px;background:#f3f6ef;border:1px solid var(--line);border-radius:8px}
.calc-input-row [data-cost-remove]{min-height:39px;justify-self:start}
.calculator input:not([type=checkbox]),.calculator select,.calculator textarea{display:block;width:100%;min-height:39px;margin:5px 0 0;border:1px solid #cbd6ce;border-radius:7px;background:#fafcf9;padding:8px 10px;color:var(--ink);font:inherit}
.calculator textarea{min-height:74px;resize:vertical}.calculator label{margin:0}.calc-panel{padding:20px;background:#fff;border:1px solid var(--line);border-radius:12px;margin:12px 0 24px}
.calc-panel>form>button,.calc-panel>button{margin-top:14px}.calc-table{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:#fff;margin:16px 0}
.calc-table table{border-collapse:collapse;width:100%;font-size:14px}.calc-table th,.calc-table td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.calc-table th{background:#eaf0e7;white-space:nowrap;font-size:12px}.calc-table tbody tr:last-child td{border-bottom:0}.calc-muted,.calculator td small,.calc-product small{color:var(--muted);display:block}
.calc-chip{display:inline-block;padding:3px 7px;font-size:11px}.calc-product{display:flex;gap:10px;align-items:center;min-width:160px}
.calc-thumb,.calc-picture{flex:none;width:42px;height:42px;object-fit:cover;border-radius:6px;background:#e7eee6}
.calc-totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:20px 0}
.calc-totals div{background:#e6f0e7;padding:13px;border-radius:9px}.calc-totals strong,.calc-totals small{display:block}
.calc-totals strong{font-size:21px}.calc-secondary{color:var(--ink);background:#e7eee5;border:1px solid #ccd9cd;min-height:36px}
"""
CSS += """
.settings-block.mcp-card{display:block;margin:20px 0;padding:28px;background:#fff}
.settings-block .settings-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.settings-block .settings-heading h2{min-width:0;margin:0;font-size:20px;line-height:1.3}
.settings-block .settings-heading p{max-width:68ch;margin:6px 0 0;color:#4f6255;font-size:14px;line-height:1.55}
.settings-block .settings-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:clamp(24px,3vw,44px);align-items:start;margin-top:24px}
.settings-block .settings-summary,.settings-block .settings-form{display:grid;gap:16px;min-width:0}
.settings-block .settings-summary p,.settings-block .settings-form p{margin:0;color:#465b4e;font-size:14px;line-height:1.55}
.settings-block .settings-caption,.settings-block .settings-meta span{display:block;margin-bottom:4px;color:#52675a;font-size:12px;font-weight:650}
.settings-block .settings-address{display:block;overflow-wrap:anywhere;color:var(--ink);font-size:15px;line-height:1.45}
.settings-block .settings-help{color:#52675a!important;font-size:13px!important;line-height:1.5!important}
.settings-block .settings-field{min-width:0}.settings-block .settings-field label{margin:0 0 6px;font-size:13px}
.settings-block input:is([type=text],[type=url],[type=password]),.settings-block select{display:block;width:100%;min-width:0;min-height:46px;border:1px solid #bdcec0;border-radius:8px;background:#fff;padding:10px 12px;color:var(--ink);font:inherit;font-size:14px}
.settings-block input::placeholder{color:#64766a;opacity:1}.settings-block .settings-field .settings-help{margin-top:6px}
.settings-block input:focus-visible,.settings-block select:focus-visible,.settings-block button:focus-visible,.settings-block summary:focus-visible{outline:3px solid #cc8a28;outline-offset:3px}
.settings-block .settings-form>button,.settings-block .settings-summary form button{justify-self:start;width:auto;max-width:100%}
.settings-block .settings-secondary{background:#fff;color:#245c3f;border:1px solid #b9ccbc}
.settings-block .settings-secondary:hover{background:#edf4ed;color:#194b32}
.settings-block .settings-state{display:inline-flex;align-items:center;min-height:30px;padding:4px 10px;border-radius:6px;background:#edf1eb;color:#405747;font-size:12px;font-weight:700}
.settings-block .settings-state--enabled{background:#e5f2e8;color:#1d633e}.settings-block .settings-state--unknown{background:#fff3dc;color:#76510d}
.settings-block .settings-meta strong{display:block;font-size:14px;font-weight:650}.settings-block .settings-warning{color:#87520d!important}
.settings-block .settings-details{font-size:13px}.settings-block .settings-details summary{margin:0;color:#236b49}.settings-block .settings-details p{margin-top:8px}
.settings-block .settings-empty,.settings-block .settings-key-list{margin-top:24px;padding-top:18px;border-top:1px solid var(--line)}
.settings-block .settings-empty{color:#52675a;font-size:13px}.settings-block .settings-key-list h3{margin:0 0 12px;font-size:15px}
.settings-block .settings-key-row{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:12px 0;border-top:1px solid #e8eee8}
.settings-block .settings-key-row>div{min-width:0}.settings-block .settings-key-row p{margin:3px 0 0;color:#52675a;font-size:13px}
.settings-block ::selection{background:#cfe6d5;color:#172821}
@media(max-width:1050px){.settings-block .settings-grid{grid-template-columns:minmax(0,1fr);gap:24px}}
@media(max-width:650px){.settings-block.mcp-card{padding:20px}.settings-block .settings-heading h2{font-size:18px}.settings-block input:is([type=text],[type=url],[type=password]),.settings-block select{font-size:16px}.settings-block .settings-form>button{width:100%}.settings-block .settings-key-row{align-items:flex-start}}
"""
STYLE_HASH=base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
SCRIPT = '''
document.addEventListener('submit', function (event) {
  const form = event.target;
  if (!form || form.tagName !== 'FORM') return;
  const action = form.getAttribute('action');
  if (!['/settings/check', '/settings/preview', '/settings/wb-lookup'].includes(action)) return;
  const button = form.querySelector('button');
  if (!button || button.disabled) { event.preventDefault(); return; }
  button.dataset.originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = action === '/settings/check' ? 'Проверяем подключение…' : 'Загружаем данные…';
  form.setAttribute('aria-busy', 'true');
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  status.textContent = 'Ожидаем ответ маркетплейса. Повторно нажимать не нужно.';
  form.appendChild(status);
  window.setTimeout(function () {
    status.textContent = 'Ответ задерживается. Вернитесь в настройки и проверьте результат позже.';
  }, 25000);
});
window.addEventListener('pageshow', function () {
  document.querySelectorAll('form[aria-busy="true"]').forEach(function (form) {
    const button = form.querySelector('button');
    if (button && button.dataset.originalLabel) { button.textContent = button.dataset.originalLabel; button.disabled = false; }
    form.removeAttribute('aria-busy');
    form.querySelectorAll('[role="status"]').forEach(function (node) { node.remove(); });
  });
});
'''
SCRIPT_HASH=base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
CALCULATOR_SCRIPT="""
document.addEventListener('click', function (event) {
  const remove = event.target.closest('[data-cost-remove]');
  if (remove) {
    const row = remove.closest('.calc-input-row');
    const neighbor = row.nextElementSibling || row.previousElementSibling;
    const group = row.parentElement.dataset.costRows;
    const owner = remove.closest('form');
    row.remove();
    owner?.dispatchEvent(new Event('input', {bubbles: true}));
    const focus = neighbor?.querySelector('[data-cost-remove]') || document.querySelector('[data-cost-add="'+group+'"]');
    focus?.focus();
    return;
  }
  const add = event.target.closest('[data-cost-add]');
  if (!add) return;
  const group = add.dataset.costAdd;
  const list = document.querySelector('[data-cost-rows="'+group+'"]');
  const template = document.getElementById('cost-'+group);
  if (list && template && list.children.length < 110) {
    list.append(template.content.cloneNode(true));
    list.lastElementChild.querySelector('input,select').focus();
    list.closest('form')?.dispatchEvent(new Event('input', {bubbles: true}));
  }
});
document.addEventListener('change', function (event) {
  if (!event.target.matches('[data-cost-color] select')) return;
  const field = event.target.closest('[data-cost-color]').querySelector('[data-cost-new-color]');
  field.hidden = event.target.value !== 'new';
  if (!field.hidden) field.querySelector('input').focus();
});
const csv = document.getElementById('cost-csv-file');
if (csv) csv.addEventListener('change', async function () {
  const file = csv.files[0];
  if (file && file.size <= 220000) document.querySelector('textarea[name=csv]').value = await file.text();
});
// A tab-local safety copy covers expired sessions or a lost connection. It never
// contains the CSRF token and is only restored after the owner chooses to do so.
const costLocalPrefix = 'sidzen-cost-editor:';
try {
  const cleared = new URLSearchParams(location.search).get('clear_draft');
  if (cleared) {
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith(costLocalPrefix)) continue;
      try { if (JSON.parse(sessionStorage.getItem(key))?.id === cleared) sessionStorage.removeItem(key); } catch (_) {}
    }
  }
} catch (_) {}
document.querySelectorAll('form[data-cost-local-key]').forEach(function (form) {
  const key = costLocalPrefix + form.dataset.costLocalKey;
  const controls = root => Array.from(root.querySelectorAll('input[name],select[name],textarea[name]'))
    .filter(el => !['csrf', 'action', 'draft_id'].includes(el.name) && el.type !== 'file');
  const fields = list => list.map(el => [el.name, el.value]);
  function capture() {
    return {
      main: fields(controls(form).filter(el => !el.closest('[data-cost-rows]'))),
      groups: Array.from(form.querySelectorAll('[data-cost-rows]')).map(list => ({
        name: list.dataset.costRows, rows: Array.from(list.children).map(row => fields(controls(row)))
      }))
    };
  }
  function saveLocal() {
    try { sessionStorage.setItem(key, JSON.stringify({id: form.elements.draft_id.value, data: capture()})); } catch (_) {}
  }
  function applyFields(root, values, outsideRows) {
    if (!Array.isArray(values) || values.length > 3000) return;
    const available = controls(root).filter(el => !outsideRows || !el.closest('[data-cost-rows]'));
    const used = new Set();
    values.forEach(function (pair) {
      if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== 'string' || typeof pair[1] !== 'string') return;
      const el = available.find(item => !used.has(item) && item.name === pair[0]);
      if (!el) return;
      used.add(el);
      if (el.tagName === 'SELECT' && !Array.from(el.options).some(option => option.value === pair[1])) {
        const option = document.createElement('option');
        option.value = pair[1]; option.textContent = 'Недоступное значение: ' + pair[1]; el.append(option);
      }
      el.value = pair[1];
    });
  }
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(key)); } catch (_) {}
  if (saved?.data && JSON.stringify(saved.data) !== JSON.stringify(capture())) {
    const notice = document.createElement('p'); notice.className = 'notice'; notice.setAttribute('role', 'status');
    notice.append('В этой вкладке есть несохранённые данные. ');
    const restore = document.createElement('button'); restore.type = 'button'; restore.className = 'calc-secondary';
    restore.textContent = 'Восстановить данные'; notice.append(restore); form.prepend(notice);
    restore.addEventListener('click', function () {
      applyFields(form, saved.data.main, true);
      if (Array.isArray(saved.data.groups)) saved.data.groups.forEach(function (group) {
        const list = Array.from(form.querySelectorAll('[data-cost-rows]')).find(item => item.dataset.costRows === group.name);
        const template = document.getElementById('cost-' + group.name);
        if (!list || !template || !Array.isArray(group.rows) || group.rows.length > 110) return;
        list.replaceChildren();
        group.rows.forEach(function (row) {
          list.append(template.content.cloneNode(true));
          applyFields(list.lastElementChild, row, false);
        });
      });
      form.querySelectorAll('[data-cost-color]').forEach(function (item) {
        item.querySelector('[data-cost-new-color]').hidden = item.querySelector('select').value !== 'new';
      });
      if (typeof saved.id === 'string' && /^[A-Za-z0-9_-]{24}$/.test(saved.id)) form.elements.draft_id.value = saved.id;
      notice.remove(); saveLocal();
      const unsaved = form.querySelector('[data-cost-unsaved]');
      if (unsaved) unsaved.hidden = false;
    });
  }
  let timer;
  function scheduleLocal() {
    const unsaved = form.querySelector('[data-cost-unsaved]');
    if (unsaved) unsaved.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(function () { timer = undefined; saveLocal(); }, 300);
  }
  form.addEventListener('input', scheduleLocal);
  form.addEventListener('change', scheduleLocal);
  form.addEventListener('submit', function () { clearTimeout(timer); timer = undefined; saveLocal(); });
  window.addEventListener('pagehide', function () { if (timer) { clearTimeout(timer); timer = undefined; saveLocal(); } });
});
"""
CALCULATOR_HASH=base64.b64encode(hashlib.sha256(CALCULATOR_SCRIPT.encode()).digest()).decode()
STATIC_POLICY = "default-src 'none'; style-src 'sha256-" + STYLE_HASH + "'; script-src 'sha256-" + SCRIPT_HASH + "'"
DOCUMENT_POLICY = "; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
CSP = STATIC_POLICY + DOCUMENT_POLICY
CALCULATOR_CSP = (STATIC_POLICY + " 'sha256-" + CALCULATOR_HASH + "'" +
    "; img-src 'self' https://*.ozone.ru https://*.ozon.ru https://*.wbbasket.ru https://*.wbbasket.net "
    "https://*.wildberries.ru https://*.wbstatic.net" + DOCUMENT_POLICY)
def document(title,content,auth=False,progress=False,calculator=False):
    head='<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>'+html.escape(title)+' — SIDZEN</title><style>'+CSS+'</style></head><body><a class="skip" href="#content">К содержимому</a>'
    brand='<a class="brand" href="/">SIDZEN<small>MARKETPLACE WORKSPACE</small></a>'
    if auth:
        body='<div class="auth-layout"><aside class="auth-story">'+brand+'<h2>Ваши кабинеты.<br>Одна точка входа.</h2><p>Подключайте Wildberries и Ozon, проверяйте доступ и просматривайте данные без изменения товаров.</p><div class="auth-art" aria-hidden="true"></div></aside><main id="content" class="auth-main"><div class="auth-card">'+content+'</div></main></div>'
    else:
        location = 'Калькулятор' if calculator else 'Кабинеты'
        body='<div class="shell"><aside class="sidebar">'+brand+'<div><div class="nav-label">Рабочее пространство</div><nav aria-label="Основная навигация"><a href="/settings">Кабинеты маркетплейсов</a><a href="/settings?tab=calculator">Калькулятор</a></nav></div><div class="sidebar-foot">Только чтение данных<br>Без изменения товаров и цен</div></aside><div class="main"><div class="topline"><span>Рабочее пространство / '+location+'</span><span class="tag">'+('RUB' if calculator else 'READ-ONLY')+'</span></div>'+content+'<footer class="footer"><span>SIDZEN · кабинет маркетплейсов</span><span>Секреты не отображаются после сохранения</span></footer></div></div>'
    return head+body+('<script>'+SCRIPT+'</script>' if progress else '')+('<script>'+CALCULATOR_SCRIPT+'</script>' if calculator else '')+'</body></html>'
