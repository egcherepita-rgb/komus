const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Execute the real client handlers with an isolated DOM/API adapter.
// No browser session, customer workbook, or production data is involved.
function client({ upload = true } = {}) {
  const elements = new Map(), events = new Map(), calls = [], replies = [], downloads = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', files: [], hidden: false, disabled: false, open: false,
      innerHTML: '', textContent: '', classList: { add() {}, remove() {}, toggle() {} },
      reset() {}, showModal() { this.open = true; }, close() { this.open = false; },
      focus() { this.focused = true; }, scrollIntoView() { this.scrolled = true; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    URLSearchParams, Intl, Date, console,
    URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
    setTimeout: callback => callback(),
    location: { pathname: '/' }, localStorage: { getItem() { return null; } },
    document: {
      getElementById: element, addEventListener(type, listener) { events.set(type, listener); },
      createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }),
      querySelectorAll: selector => selector === '.mutation' ? [element('applyImport'), element('previewButton')] : [],
      body: { classList: { add() {} }, append() {} },
    },
    fetch: async (url, options) => {
      if (url === '/api/session') return { ok: true, json: async () => ({ authenticated: false }) };
      calls.push({ url, options });
      if (!replies.length) throw Error('Unexpected request: ' + url);
      const reply = replies.shift();
      return { ok: true, status: 200, json: async () => await reply, blob: async () => await reply };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8'), context);
  if (upload) vm.runInContext("showUpload('initial')", context);
  element('uploadFile').files = [{ name: 'initial.xlsx', size: 100 }];
  element('uploadYear').value = '2026'; element('initialMode').value = 'full';
  return { element, calls, replies, downloads, run: code => vm.runInContext(code, context),
    click: dataset => events.get('click')({ target: { closest: () => ({ dataset }) } }),
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

const allLayouts = JSON.parse(require('node:child_process').execFileSync('python', [
  '-c', 'from planner import LAYOUTS; import json; print(json.dumps(LAYOUTS))',
], { cwd: path.join(__dirname, '..'), encoding: 'utf8' }));

test('pallet calculator uploads a workbook and renders calculations and unmatched rows', async () => {
  const c = client({ upload: false });
  const workbook = { name: 'pallets.xlsx', size: 100 };
  c.element('palletsFile').files = [workbook];
  const result = { sheet: 'Паллеты', columns: [['name','Товар'],['sku','Артикул ПРОМЕТ'],['warehouse','Площадка'],['redHalf','Красная 0,5'],['note','Примечание']], rows: [
    { line: 2, name: 'Шкаф', sku: '0007', quantity: 67, capacity: 22, redFull: 1, blackFull: 0, redHalf: 1, remainder: 1 },
    { line: 3, name: 'Другая позиция', quantity: 1, error: 'Нет в справочнике' },
    { line: 4, name: 'Ключница', quantity: 20, order: 'К-1', warehouse: 'МСК' },
  ] };
  c.replies.push(result);
  await c.run("$('palletsForm').onsubmit({preventDefault(){}})");
  assert.equal(c.calls[0].url, '/api/v2/pallets');
  assert.equal(c.calls[0].options.body, workbook);
  assert.equal(c.element('palletsResults').hidden, false);
  assert.match(c.element('palletsResults').innerHTML, /Красная 0,5/);
  assert.match(c.element('palletsResults').innerHTML, /Артикул ПРОМЕТ/);
  assert.match(c.element('palletsResults').innerHTML, /0007/);
  assert.match(c.element('palletsResults').innerHTML, /Нет в справочнике/);
  assert.match(c.element('palletsResults').innerHTML, /<span class="tag purple">МСК<\/span>/);
  assert.match(c.element('palletsStatus').textContent, /Рассчитано 1 из 3 позиций. МСК: 1/);
  assert.equal(c.element('exportPallets').disabled, false);
  c.replies.push('xlsx');
  await c.run("$('exportPallets').onclick()");
  assert.equal(c.calls[1].url, '/api/v2/pallets/export');
  assert.deepEqual(JSON.parse(c.calls[1].options.body), { rows: result.rows });
  assert.deepEqual(c.downloads, ['komus-pallets.xlsx']);
  c.run("$('palletsFile').onchange()");
  assert.equal(c.element('exportPallets').disabled, true);
});

test('history groups manual shipments by date and displays known trucks separately', () => {
  const c = client({ upload: false });
  c.run(`state={shipments:[{id:3,actualDate:'2026-11-19',slot:1,layout:{id:1,doubles:0,singles:0,length:0},pallets:[],loose:[]}],manualShipments:[
    {rowId:'one',order:'11',name:'Товар один',qty:5,actualDate:'2026-11-19'},
    {rowId:'one',order:'11',name:'Товар один',qty:7,actualDate:'2026-11-19'},
    {rowId:'two',order:'12',name:'Товар два',qty:2,actualDate:'2026-11-19'},
    {rowId:'one',order:'11',name:'Товар один',qty:3,actualDate:'2026-11-18'},
  ]}; renderHistory()`);
  const html = c.element('shipments').innerHTML;
  assert.equal((html.match(/Ручные отметки/g) || []).length, 2);
  assert.match(html, /Машина № 3/);
  assert.match(html, /2 поз. · 14 шт./);
  assert.match(html, /<strong>12 шт.<\/strong>/);
  assert.ok(html.indexOf('19 ноября') < html.indexOf('18 ноября'));
});

test('shipment history searches name, both articles, order and invoice, including older manual shipments', () => {
  const c = client({ upload: false });
  c.run(`state={rows:[{id:'old',sku:'P-888',komusSku:'K-444'}],shipments:[
    {id:3,actualDate:'2026-11-19',slot:1,layout:{id:1,doubles:0,singles:0,length:0},pallets:[],
     loose:[{rowId:'a',order:'45001',invoice:'50501',name:'Стеллаж',sku:'P-11',komusSku:'K-22',qty:1}]}
  ],manualShipments:[{rowId:'old',order:'45002',invoice:'50502',name:'Ключница',qty:2,actualDate:'2026-11-18'}]}`);
  for (const [query, expected, absent] of [
    ['стеллаж', 'Стеллаж', 'Ключница'], ['p-11', 'Стеллаж', 'Ключница'],
    ['K-22', 'Стеллаж', 'Ключница'], ['45001', 'Стеллаж', 'Ключница'],
    ['50501', 'Стеллаж', 'Ключница'], ['K-444', 'Ключница', 'Стеллаж'],
  ]) {
    c.element('historySearch').value = query;
    c.run("$('historySearch').oninput()");
    assert.ok(c.element('shipments').innerHTML.includes(expected));
    assert.ok(!c.element('shipments').innerHTML.includes(absent));
  }
});

test('leftover searches the journal article and uses the same shipment dialog', async () => {
  const c = client({ upload: false });
  c.run(`state={rows:[{id:'a',order:'45001',name:'Стеллаж',sku:'P-11',qty:40,shipped:10},{id:'b',name:'Ключница',qty:10,shipped:0}],
    plan:{issues:[],leftovers:[{rowId:'a',name:'Стеллаж',qty:5,reason:'Перенос'},{rowId:'b',name:'Ключница',qty:10,reason:'Перенос'}]}};
    leftoverQuery='p-11';revision=4;renderLeftovers();render=()=>{}`);
  assert.match(c.element('leftoverTable').innerHTML, /Стеллаж/);
  assert.doesNotMatch(c.element('leftoverTable').innerHTML, /Ключница/);
  assert.match(c.element('leftoverTable').innerHTML, /data-ship-row="a" data-leftover-quantity="5"/);
  c.click({ shipRow: 'a', leftoverQuantity: '5' });
  assert.equal(c.element('rowTransitionQty').value,5);
  c.replies.push({ revision: 5, state: {}, result: {} });
  await c.run("$('rowTransitionForm').onsubmit({preventDefault(){}})");
  assert.equal(JSON.parse(c.calls[0].options.body).fromPlan,true);
});

test('manual shipment uses the quantity for this shipment and confirms before saving', async () => {
  const c = client({ upload: false });
  c.run("state={rows:[{id:'row1',order:'QA-1',name:'Товар',qty:50,shipped:10,reserved:20}]}; revision=4; render=()=>{}");
  c.run("openRowTransition('row1','ship')");
  assert.equal(c.element('rowTransitionQty').max, 40);
  assert.equal(c.element('rowTransitionQty').value, 20);
  assert.equal(c.element('rowTransitionDialog').open, true);
  assert.equal(c.calls.length, 0);
  c.element('rowTransitionQty').value = '10';
  c.element('rowTransitionDate').value = '2026-11-17';
  c.replies.push({ revision: 5, state: {}, result: {} });
  await c.run("$('rowTransitionForm').onsubmit({preventDefault(){}})");
  assert.equal(c.calls[0].url, '/api/v2/row-transition');
  assert.deepEqual(JSON.parse(c.calls[0].options.body), {
    id: 'row1', revision: 4, operation: 'ship', quantity: 10, actualDate: '2026-11-17',
  });
  assert.equal(c.element('rowTransitionDialog').open, false);
});

test('an active reservation can be removed from the order row without entering a quantity', async () => {
  const c = client({ upload: false });
  c.run("state={rows:[{id:'row1',order:'QA-1',name:'Товар',qty:50,shipped:10,reserved:20}],plan:{id:'plan-A'}}; revision=4; render=()=>{}");
  assert.match(c.run('orderRowActions(state.rows[0])'), /data-release-row="row1">Снять отметку/);
  assert.doesNotMatch(c.run('orderRowActions({...state.rows[0],reserved:0})'), /data-release-row/);
  c.click({ releaseRow: 'row1' });
  assert.equal(c.element('rowTransitionDialog').open, true);
  assert.equal(c.element('rowTransitionQuantityLabel').hidden, true);
  assert.equal(c.element('rowTransitionDateLabel').hidden, true);
  assert.match(c.element('rowTransitionHint').textContent, /вернутся 20 шт/);
  assert.equal(c.calls.length, 0);
  c.replies.push({ revision: 5, state: { plan: null }, result: {} });
  await c.run("$('rowTransitionForm').onsubmit({preventDefault(){}})");
  assert.equal(c.calls[0].url, '/api/v2/row-transition');
  assert.deepEqual(JSON.parse(c.calls[0].options.body), {
    id: 'row1', revision: 4, operation: 'reserve', quantity: 0,
  });
  assert.equal(c.element('rowTransitionDialog').open, false);
});

test('clear plan is sent only after confirmation and targets the selected plan', async () => {
  const c = client({ upload: false });
  c.run("state={plan:{id:'plan-A'}}; revision=4; render=()=>{}; $('clearPlan').onclick()");
  assert.equal(c.element('clearPlanDialog').open, true);
  assert.equal(c.calls.length, 0);
  c.replies.push({ revision: 5, state: { plan: null }, result: {} });
  await c.run("$('clearPlanForm').onsubmit({preventDefault(){}})");
  assert.equal(c.calls[0].url, '/api/v2/clear-plan');
  assert.deepEqual(JSON.parse(c.calls[0].options.body), { revision: 4, planId: 'plan-A' });
  assert.equal(c.element('clearPlanDialog').open, false);
});

test('layouts tab renders all twelve planner layouts and reuses its read-only response', async () => {
  const c = client({ upload: false }); c.replies.push(allLayouts);
  await c.run("tab('layouts'); loadLayouts()");
  const html = c.element('layoutGrid').innerHTML;
  assert.equal((html.match(/<article id="layout-/g) || []).length, 12);
  for (const l of allLayouts) {
    assert.ok(html.includes(`id="layout-${l.id}"`));
    assert.ok(html.includes(`Схема ${l.id}: сдвоенных паллет ${l.doubles}, одиночных ${l.singles}`));
  }
  await c.run('loadLayouts()');
  assert.equal(c.calls.length, 1); assert.equal(c.calls[0].url, '/api/layouts');
  assert.equal(c.calls[0].options.method, 'GET');
});

test('a truck layout link focuses the matching scheme without changing the plan', async () => {
  const c = client({ upload: false }); c.replies.push(allLayouts);
  await c.run('openLayout(12)');
  assert.equal(c.element('layout-12').focused, true);
  assert.equal(c.element('layout-12').scrolled, true);
  assert.equal(c.calls.length, 1); assert.equal(c.calls[0].options.method, 'GET');
});

test('failed layout loading explains the problem and can be retried', async () => {
  const c = client({ upload: false }); c.replies.push([]);
  await assert.rejects(c.run('loadLayouts()'), /12 схем/);
  assert.equal(c.element('retryLayouts').hidden, false);
  assert.match(c.element('layoutStatus').textContent, /Не удалось загрузить схемы/);
  c.replies.push(allLayouts); await c.run("$('retryLayouts').onclick()");
  assert.equal(c.element('retryLayouts').hidden, true);
  assert.match(c.element('layoutStatus').textContent, /Все 12 вариантов/);
});
