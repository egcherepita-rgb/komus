const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Execute the real client upload handlers with an isolated DOM/API adapter.
// No browser session, customer workbook, or production data is involved.
function client() {
  const elements = new Map(), calls = [], replies = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', files: [], hidden: false, disabled: false, open: false,
      innerHTML: '', textContent: '', classList: { add() {}, remove() {}, toggle() {} },
      reset() {}, showModal() { this.open = true; }, close() { this.open = false; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    URLSearchParams, Intl, Date, console,
    location: { pathname: '/' }, localStorage: { getItem() { return null; } },
    document: {
      getElementById: element, addEventListener() {},
      querySelectorAll: selector => selector === '.mutation' ? [element('applyImport'), element('previewButton')] : [],
      body: { classList: { add() {} } },
    },
    fetch: async (url, options) => {
      if (url === '/api/session') return { ok: true, json: async () => ({ authenticated: false }) };
      calls.push({ url, options });
      if (!replies.length) throw Error('Unexpected request: ' + url);
      const reply = replies.shift();
      return { ok: true, status: 200, json: async () => await reply };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8'), context);
  vm.runInContext("showUpload('initial')", context);
  element('uploadFile').files = [{ name: 'initial.xlsx', size: 100 }];
  element('uploadYear').value = '2026'; element('initialMode').value = 'full';
  return { element, calls, replies, run: code => vm.runInContext(code, context),
    check: () => vm.runInContext("$('uploadForm').onsubmit({preventDefault(){}})", context) };
}
const good = () => ({ token: 'reviewed-token', count: 1, errors: [], warnings: [], rows: [
  { order: 'QA-1', position: '10', name: 'Synthetic product', qty: 260, shipped: 0, due: '2026-11-20' },
] });

test('one check discovers the known initial sheet and validates its rows before enabling apply', async () => {
  const c = client();
  c.replies.push({ sheets: [{ name: 'отгруженные целиком' }, { name: 'отгрузка с УЗМК' }, { name: 'Лист1' }] }, good());
  await c.check();
  assert.equal(c.calls.length, 2);
  assert.equal(new URL('http://test' + c.calls[1].url).searchParams.get('sheet'), 'отгрузка с УЗМК');
  assert.equal(c.element('uploadSheet').value, 'отгрузка с УЗМК');
  assert.equal(c.element('applyImport').disabled, false);
  assert.match(c.element('uploadStatus').textContent, /Проверка завершена/);
  assert.ok(c.calls.every(call => !call.url.includes('import-apply')));
});

test('invalid rows keep apply disabled and explain the exact row and reason', async () => {
  const c = client(); const result = good(); result.errors = [{ line: 7, reason: 'Нет номера заказа' }];
  c.replies.push({ sheets: [{ name: 'отгрузка с УЗМК' }] }, result);
  await c.check();
  assert.equal(c.element('applyImport').disabled, true);
  assert.match(c.element('uploadPreview').innerHTML, /Строка 7: Нет номера заказа/);
  assert.match(c.element('uploadStatus').textContent, /1 ошибок/);
});

test('an ambiguous workbook requires an explicit sheet and never saves or guesses', async () => {
  const c = client(); c.replies.push({ sheets: [{ name: 'Current A' }, { name: 'Current B' }] });
  await c.check(); assert.equal(c.calls.length, 1); assert.equal(c.element('applyImport').disabled, true);
  assert.equal(c.element('uploadSheet').value, ''); assert.match(c.element('uploadStatus').textContent, /не выбран/);
  c.element('uploadSheet').value = 'Current B'; c.run("$('uploadSheet').onchange()");
  c.replies.push(good()); await c.check();
  assert.equal(new URL('http://test' + c.calls[1].url).searchParams.get('sheet'), 'Current B');
  assert.equal(c.element('applyImport').disabled, false);
});

test('missing quantity mode explains why apply is blocked without uploading the file', async () => {
  const c = client(); c.element('initialMode').value = ''; await c.check();
  assert.equal(c.calls.length, 0); assert.equal(c.element('applyImport').disabled, true);
  assert.match(c.element('uploadMessage').textContent, /исходные количества или чистые остатки/);
});

test('changing the file while a preview is in flight cannot enable an outdated apply', async () => {
  const c = client(); let finish;
  c.replies.push(new Promise(resolve => { finish = resolve; }));
  const checking = c.check();
  c.element('uploadFile').files = [{ name: 'different.xlsx', size: 100 }];
  c.run("$('uploadFile').onchange()"); finish(good()); await checking;
  assert.equal(c.element('applyImport').disabled, true);
  assert.equal(c.element('uploadPreview').innerHTML, '');
});

test('apply sends only the successfully checked token, then closes the dialog', async () => {
  const c = client(); c.replies.push({ sheets: [{ name: 'отгрузка с УЗМК' }] }, good()); await c.check();
  c.run('render=()=>{}'); c.replies.push({ revision: 1, state: {}, result: { message: 'Saved' } });
  await c.run("$('applyImport').onclick()");
  const apply = c.calls.find(call => call.url === '/api/v2/import-apply');
  assert.deepEqual(JSON.parse(apply.options.body), { revision: 0, token: 'reviewed-token' });
  assert.equal(c.element('uploadDialog').open, false);
});
