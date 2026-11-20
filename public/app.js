'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow'}).format(new Date());
const datefmt=v=>v?new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric'}).format(new Date(v.slice(0,10)+'T12:00:00')):'—';
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
const readOnly=location.pathname==='/demo';
const types={single:'Красная 0,5',double:'Чёрная 1',split:'Красная 1',loose:'0 · без места'};
const importNames={initial:'Первоначальный файл отгрузок',catalog:'Кратность Тула',moscow:'Кратность Москвы',order:'Новый заказ Комуса',response:'Ответ Донцовой'};
let state=null,revision=0,busy=false,month=today().slice(0,7),uploadKind='',uploadToken=null,uploadGeneration=0,editing=null,dispatching=null;
let layouts=[],layoutsLoading=null;
let rowTransition=null,planToClear=null;
function banner(message,error=false){$('banner').hidden=!message;$('banner').textContent=message||'';$('banner').classList.toggle('error',error);}
function lock(value){busy=value;document.querySelectorAll('.mutation').forEach(b=>b.disabled=value||readOnly);if(!value&&$('uploadDialog').open)$('applyImport').disabled=readOnly||!uploadToken;}
async function request(path,{body,raw=false}={}){
 if(readOnly&&body!==undefined)throw Error('Демонстрация доступна только для просмотра. Для своего журнала войдите в приложение.');
 const headers=body===undefined?{}:{'X-Komus-Client':'web',...(raw?{'X-Komus-Revision':String(revision)}:{'Content-Type':'application/json'})};
 const res=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:raw?body:JSON.stringify(body)});
 const result=await res.json().catch(()=>({error:'Некорректный ответ сервера'}));
 if(!res.ok){if(res.status===401&&path!=='login'){document.querySelectorAll('dialog[open]').forEach(d=>d.close());$('shell').hidden=true;$('login').hidden=false;}
 if(res.status===409){await loadState();uploadToken=null;}
 throw Error(result.error||'Не удалось выполнить действие');}return result;
}
function accept(data){state=data.state;revision=data.revision;render();}
async function loadState(){const data=await request(readOnly?'v2/demo':'v2/state');accept(data);}
async function action(path,data={}){if(busy)return;lock(true);try{const r=await request('v2/'+path,{body:{revision,...data}});accept(r);banner(r.result?.message||'Изменения сохранены на сервере.');return r;}finally{lock(false);}}
function tab(name){document.querySelectorAll('section[id^="view-"]').forEach(s=>s.hidden=s.id!=='view-'+name);document.querySelectorAll('nav [data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));if(name==='layouts')loadLayouts().catch(()=>{});}
function table(headers,rows,empty='Пока нет данных'){return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join(''):`<tr><td colspan="${headers.length}">${esc(empty)}</td></tr>`}</tbody></table>`;}
function download(path){if(readOnly){banner('Войдите в приложение, чтобы скачать рабочий шаблон.');return;}const a=document.createElement('a');a.href=path;a.download='';document.body.append(a);a.click();a.remove();}
function renderPalletCalculation(result){
 const cols=['Строка Excel','Заказ','Товар','Склад','Количество, шт.','Шт. в паллете','Красная 1','Чёрная 1','Красная 0,5','Остаток, шт.','Примечание'];
 const values=result.rows.map(r=>[
  fmt(r.line),esc(r.order||'—'),esc(r.name),r.warehouse==='МСК'?'<span class="tag purple">МСК</span>':esc(r.warehouse||'—'),
  esc(r.quantity),r.capacity?fmt(r.capacity):'—',
  ...['redFull','blackFull','redHalf','remainder'].map(key=>r.error||r.warehouse==='МСК'?'—':fmt(r[key])),
  r.error?`<span class="tag amber">${esc(r.error)}</span>`:esc(r.note||'')
 ]);
 const errors=result.rows.filter(r=>r.error).length;
 const moscow=result.rows.filter(r=>r.warehouse==='МСК').length;
 $('palletsResults').innerHTML=table(cols,values);
 $('palletsResults').hidden=false;
 $('palletsStatus').textContent=`Лист «${result.sheet}». Рассчитано ${result.rows.length-errors-moscow} из ${result.rows.length} позиций.`+
  (moscow?` МСК: ${moscow}.`:'')+
  (errors?` Требуют проверки: ${errors}.`:'');
}
$('palletsFile').onchange=()=>{
 $('palletsResults').hidden=true;
 $('palletsStatus').textContent='';
};
$('palletsForm').onsubmit=async event=>{
 event.preventDefault();
 const input=$('palletsFile'),button=$('calculatePallets'),file=input.files[0];
 if(readOnly||button.disabled||!file)return;
 input.disabled=button.disabled=true;
 $('palletsResults').hidden=true;
 $('palletsStatus').textContent='Считаем паллеты…';
 try{
  renderPalletCalculation(await request('v2/pallets',{body:file,raw:true}));
 }catch(error){
  $('palletsStatus').textContent=error.message;
 }finally{
  input.disabled=button.disabled=false;
 }
};
function fileTable(files){return table(['Тип','Файл / лист','Когда загружен','Строк','Исходник'],files.slice().reverse().map(f=>[esc(importNames[f.kind]||f.kind),esc(f.filename)+(f.sheet?`<small class="row-note">${esc(f.sheet)}</small>`:''),esc(datefmt(f.at)),fmt(f.lines),`<button class="compact-action" data-download="/api/v2/source?sha=${esc(f.sha)}">Скачать</button>`]));}
function sumTruck(t){return [...t.pallets,...t.loose].reduce((s,p)=>s+p.qty,0);}
function summary(t){const map=new Map();for(const p of [...t.pallets,...t.loose]){const k=p.rowId;const item=map.get(k)||{...p,qty:0,places:0,partialCount:0};item.qty+=p.qty;item.places+=p.type==='loose'?0:1;item.partialCount+=p.partial?1:0;map.set(k,item);}return [...map.values()];}
function pallet(p,i,t,history){return `<button class="pallet ${p.type==='double'?'double':''}" data-pallet="${i}" data-truck="${t.id}" ${history?`data-shipment="${t.shipmentId}"`:''} title="${esc(p.name+' · '+p.qty+' шт. · заказ '+p.order)}">${i+1}<small>${p.qty} шт.${p.vertical?' ВП':''}</small></button>`;}
function diagram(t,history){let i=0,s='';for(let d=0;d<t.layout.doubles;d++){s+=`<div class="pallet-column double" data-width="1200">${pallet(t.pallets[i],i,t,history)}</div>`;i++;}for(let d=0;d<t.layout.singles;d+=2){s+=`<div class="pallet-column" data-width="1000">${pallet(t.pallets[i],i,t,history)}${pallet(t.pallets[i+1],i+1,t,history)}</div>`;i+=2;}return `<div class="diagram-scroll"><div class="diagram">${s}</div></div>`;}
function sizes(){document.querySelectorAll('[data-width]').forEach(n=>n.style.width=(Number(n.dataset.width)/13500*100)+'%');}
function layoutDiagram(l){
 const double='<span class="layout-pallet double">1</span>',single='<span class="layout-pallet">0,5</span>';
 const columns=Array.from({length:l.doubles},()=>`<div class="pallet-column double" data-width="1200">${double}</div>`).join('')+Array.from({length:l.singles/2},()=>`<div class="pallet-column" data-width="1000">${single}${single}</div>`).join('');
 return `<div class="diagram-scroll"><div class="diagram layout-diagram" role="img" aria-label="Схема ${l.id}: сдвоенных паллет ${l.doubles}, одиночных ${l.singles}"><div class="layout-columns" aria-hidden="true">${columns}</div></div></div>`;
}
function renderLayouts(){
 $('layoutGrid').innerHTML=layouts.map(l=>`<article id="layout-${l.id}" class="panel layout-card" tabindex="-1"><div class="layout-heading"><h2>Схема № ${l.id}</h2><span class="tag purple">${l.doubles} × 1 + ${l.singles} × 0,5</span></div>${layoutDiagram(l)}<div class="diagram-caption"><span>КАБИНА ← · вид сверху</span><span>${fmt(l.length)} / 13 500 мм</span></div><dl class="layout-metrics"><div><dt>Сдвоенных · чёрные «1»</dt><dd>${l.doubles}</dd></div><div><dt>Одиночных · красные «0,5»</dt><dd>${l.singles}</dd></div><div><dt>Остаток длины</dt><dd>${fmt(13500-l.length)} мм</dd></div></dl></article>`).join('');sizes();
}
async function loadLayouts(){
 if(layouts.length)return layouts;if(layoutsLoading)return layoutsLoading;
 $('layoutStatus').textContent='Загружаем 12 схем укладки…';$('retryLayouts').hidden=true;
 layoutsLoading=(async()=>{try{const data=await request('layouts');if(!Array.isArray(data)||data.length!==12)throw Error('Не удалось получить все 12 схем укладки.');layouts=data;renderLayouts();$('layoutStatus').textContent='Все 12 вариантов используются при автоматической сборке машин.';return layouts;}catch(e){$('layoutStatus').textContent='Не удалось загрузить схемы: '+e.message;$('retryLayouts').hidden=false;throw e;}finally{layoutsLoading=null;}})();return layoutsLoading;
}
async function openLayout(id){tab('layouts');await loadLayouts();layouts.forEach(l=>$('layout-'+l.id).classList.toggle('selected',l.id===id));const card=$('layout-'+id);if(card){card.scrollIntoView({behavior:'smooth',block:'center'});card.focus({preventScroll:true});}}
function truckHtml(t,history=false){return `<article class="panel truck ${history?'shipment':''}"><div class="truck-top"><div class="truck-name"><span class="truck-number">${String(t.id).padStart(2,'0')}</span><div><h3>Машина № ${t.id} <span class="tag">${history?'Отгружена':'Полная укладка'}</span></h3><p>${datefmt(history?t.actualDate:t.date)} · Рейс ${t.slot} · Схема ${t.layout.id}${t.secondReason?' · '+esc(t.secondReason):''}</p></div></div><button class="tag purple layout-link" data-layout="${t.layout.id}" aria-label="Показать схему укладки № ${t.layout.id}">Схема № ${t.layout.id} · ${t.layout.doubles} × 1 + ${t.layout.singles} × 0,5 ↗</button></div>${diagram(t,history)}<div class="diagram-caption"><span>КАБИНА ← · Нажмите паллету, чтобы открыть товар и заказ</span><span>${(t.layout.length/1000).toFixed(1)} / 13,5 м</span></div><details open><summary>Состав · ${summary(t).length} поз. · ${fmt(sumTruck(t))} шт.</summary><div class="table-wrap">${table(['Заказ','Счёт','Товар / артикул','Количество','Мест','Особенности'],summary(t).map(p=>[esc(p.order),esc(p.invoice),esc(p.name)+`<small class="row-note">${esc(p.sku||p.komusSku||'')}</small>`,`<strong>${fmt(p.qty)} шт.</strong>`,String(p.places),[p.vertical?'ВП':'',p.partialCount?'Неполных паллет: '+p.partialCount:'',p.type==='loose'?'Без места: '+esc(p.fixation):''].filter(Boolean).join(' · ')]))}</div></details>${history?`<p class="muted">Фактически отправлено ${datefmt(t.actualDate)}. В остатках заказов уже учтено.</p>`:`<div class="truck-actions"><span>Планирование не списывает товар. Отмечайте только после реальной отправки.</span><button class="mutation" data-dispatch="${t.id}">Отметить отгруженной</button></div>`}</article>`;}
function renderPlan(){const p=state.plan;$('clearPlan').hidden=!p||readOnly;$('statTrucks').textContent=p?p.trucks.length:'—';$('statRemaining').textContent=fmt(state.totals.remaining);$('statConfirmed').textContent=fmt(state.totals.confirmed);$('statShipped').textContent=fmt(state.totals.shipped);$('start').value=state.settings.start;$('strategy').value=state.settings.strategy;
 $('setupSteps').innerHTML=[['initial','1. Первоначальный файл',state.bootstrap||state.rows.length?'Журнал сохранён':'Загрузить один раз',!!state.bootstrap||!!state.rows.length],['catalog','2. Кратность Тула',state.catalog.length?state.catalog.length+' товаров · хранится постоянно':'Загрузить справочник',!!state.catalog.length],['orders','3. Новые заказы',state.orders.length+' заказов · добавляются к журналу',!!state.orders.length],['responses','4. Ответы Донцовой',state.totals.confirmed?fmt(state.totals.confirmed)+' шт. с готовностью':'Загрузить подтверждения',!!state.totals.confirmed]].map(([dest,title,note,done])=>`<button class="setup-card ${done?'done':'todo'}" data-tab="${dest}"><strong>${title}</strong><span>${note}</span></button>`).join('');
 $('reconcileBox').hidden=state.reconciled||!state.rows.length;$('exportPlan').disabled=!p||!p.wholePalletsOnly||p.readinessMode!=='position';$('printPlan').disabled=!p||!p.wholePalletsOnly||p.readinessMode!=='position';
 if(p&&(!p.wholePalletsOnly||p.readinessMode!=='position')){$('planResults').innerHTML='<div class="panel help"><h2>Пересоберите машины по дате готовности позиции</h2><p>Нажмите «Собрать машины». Загруженные заказы, готовность и история отправок сохранены. На позицию используется одна дата готовности. Количество берётся из заказа, отгрузка — целыми паллетами.</p></div>';return;}
 if(!p){$('planResults').innerHTML=`<div class="panel empty"><div class="empty-icon">▥</div><h2>${state.rows.length?'Журнал готов к сборке машин':'Начните с действующего файла отгрузок'}</h2><p>${state.rows.length?'Загрузите даты готовности и кратность, затем нажмите «Собрать машины».<br>Любые изменения источников требуют нового расчёта.':'Первоначальный файл сохраняется один раз.<br>Затем добавляйте только новые заказы и ответы Донцовой.'}</p><div class="actions"><button data-tab="${state.rows.length?'responses':'initial'}" class="primary">${state.rows.length?'Перейти к готовности':'Загрузить первоначальный файл'} →</button></div></div>`;return;}
 const issues=p.issues.length?`<div class="preview-errors"><strong>Требуют проверки: ${p.issues.length}</strong><br>${p.issues.map(x=>esc(x.name)+': '+esc(x.reason)).join('<br>')}</div>`:'';
 $('planResults').innerHTML=`<div class="result-title"><h2>Готовые машины · ${p.trucks.length}</h2><span class="muted">Сохранено ${datefmt(p.createdAt)} · ${(p.trucks.reduce((a,t)=>a+sumTruck(t),0)).toLocaleString('ru-RU')} шт. в плане</span></div>${p.trucks.map(t=>truckHtml(t)).join('')||'<div class="panel help"><h2>Полных машин пока нет</h2><p>Проверьте причины в остатке ниже.</p></div>'}<div class="panel"><div class="panel-heading"><h2>Остаток и причины переноса</h2><span class="tag amber">${p.leftovers.length} поз.</span></div>${issues}<div class="table-wrap">${table(['Заказ','Счёт','Товар','Количество','Причина'],p.leftovers.map(r=>[esc(r.order),esc(r.invoice),esc(r.name),fmt(r.qty),esc(r.reason)]),'Весь подтверждённый товар распределён.')}</div></div><p class="muted plan-notice">${esc(p.notice)}</p>`;
}
function renderOrders(){const old=$('orderSelect').value;$('orderSelect').innerHTML='<option value="">Все заказы</option>'+state.orders.map(o=>`<option value="${esc(o.order)}">${esc(o.order)} · остаток ${fmt(o.remaining)} шт.</option>`).join('');if(state.orders.some(o=>o.order===old))$('orderSelect').value=old;filterOrders();$('orderBadge').textContent=state.orders.length;}
function filterOrders(){if(!state)return;const selected=$('orderSelect').value,q=$('orderSearch').value.trim().toLowerCase();const rows=state.rows.filter(r=>(!selected||r.order===selected)&&[r.order,r.invoice,r.name,r.sku,r.komusSku].join(' ').toLowerCase().includes(q));
 $('downloadOrderReply').disabled=!selected||readOnly;
 $('ordersSummary').innerHTML=state.orders.filter(o=>!selected||o.order===selected).map(o=>`<div class="order-card"><strong>Заказ ${esc(o.order)}</strong><span>${o.lines} поз. · осталось ${fmt(o.remaining)} шт.</span><p>С датой готовности: ${fmt(o.confirmed)} шт.<br>Отгружено: ${fmt(o.shipped)} шт.<br>Срок: ${datefmt(o.due)}</p>${o.issues?`<span class="tag amber">Проверить: ${o.issues}</span>`:''}</div>`).join('');
 $('ordersTable').innerHTML=table(['Заказ','Счёт','Товар / артикулы','Заказано','Отгружено','Остаток','Дата готовности всей позиции','Отгрузить до','Действие'],rows.map(r=>[esc(r.order),esc(r.invoice),esc(r.name)+`<small class="row-note">Промет: ${esc(r.sku||'—')} · Комус: ${esc(r.komusSku||'—')}</small>`,fmt(r.qty),fmt(r.shipped),fmt(r.qty-r.shipped)+(r.reserved?`<small class="row-note">В готовой машине: ${fmt(r.reserved)} шт.</small>`:''),r.ready?datefmt(r.ready):`<span class="tag amber">${r.readinessNeedsReview?'Уточнить одну дату позиции':'Нет даты готовности'}</span>`,datefmt(r.due),orderRowActions(r)]),'Заказов пока нет. Загрузите первоначальный файл или новый заказ по шаблону.');if(readOnly)lock(false);
}
function orderRowActions(row){
 const id=esc(row.id);
 const edit=`<button class="compact-action mutation" data-edit-row="${id}">Проверить / исправить</button>`;
 if(readOnly)return edit;
 if(row.shipped===row.qty)return '<span class="tag">Отгружено</span>'+edit;
 const release=row.reserved? `<button class="compact-action mutation" data-release-row="${id}">Снять отметку</button>`:'';
 return `<div class="actions">
  <button class="compact-action mutation" data-ship-row="${id}">Отгружено</button>
  <button class="compact-action mutation" data-reserve-row="${id}">${row.reserved?'Изменить количество в машине':'В готовой машине'}</button>
  ${release}
  ${edit}
 </div>`;
}
function renderResponses(){const rows=state.rows.filter(r=>r.ready||r.replyAsOf||r.readinessNeedsReview);$('responsesTable').innerHTML=table(['Заказ','Счёт','Товар','Дата ответа','Дата готовности всей позиции'],rows.map(r=>[esc(r.order),esc(r.invoice),esc(r.name),r.replyAsOf?datefmt(r.replyAsOf):'Из первоначального файла',r.ready?datefmt(r.ready):r.readinessNeedsReview?'Нужна одна дата на всю позицию':'Дата готовности не указана']));$('responseFiles').innerHTML=fileTable(state.imports.filter(h=>h.kind==='response'));}
function catalogCapacity(item){
 if(item.needsReview)return '—';
 return String(item.type==='split'?item.capacity/2:item.capacity);
}
function renderCatalog(){
 const q=$('catalogSearch').value.toLowerCase(),moscow=state.moscowCatalog||[];
 const matches=r=>[r.name,r.sku,r.komusSku].join(' ').toLowerCase().includes(q);
 $('catalogMeta').textContent=`Тула: ${fmt(state.catalog.length)} поз.`+
  (state.catalogUpdated?` · ${datefmt(state.catalogUpdated)}`:'')+
  ` | Москва: ${fmt(moscow.length)} поз.`+
  (state.moscowCatalogUpdated?` · ${datefmt(state.moscowCatalogUpdated)}`:'');
 $('catalogTable').innerHTML='<div class="panel-heading"><h2>Тула</h2></div>'+table(
  ['Товар','Тип','Шт. на паллете или в партии','ВП','Проверка','Крепление для 0','Действие'],
  state.catalog.map((r,i)=>({...r,i})).filter(matches).map(r=>[esc(r.name),esc(types[r.type]),catalogCapacity(r),r.vertical?'ВП':'—',r.needsReview?`<span class="tag amber">Проверить: ${esc(r.original||'кратность')}</span>`:'—',esc(r.fixation||''),`<button class="compact-action mutation" data-edit-catalog="${r.i}">Исправить</button>`]));
 $('moscowCatalogTable').innerHTML=table(['Товар','Артикул Комуса','Артикул ПРОМЕТ','Кратность'],moscow.filter(matches).map(r=>
  [esc(r.name),esc(r.komusSku||'—'),esc(r.sku||'—'),esc(r.original||'—')]),
  moscow.length?'По запросу ничего не найдено.':'Загрузите справочник Москвы.');
 if(readOnly)lock(false);
}
function renderInitial(){const b=state.bootstrap;const started=!!b||!!state.rows.length;$('initialPanel').innerHTML=started?`<h2>Первоначальные данные уже сохранены</h2><span class="tag">Журнал ведётся на сервере</span><p>${esc(b?.filename||'Журнал начат с новых заказов')}${b?.at?' · '+datefmt(b.at):''}</p><p>Повторно загружать весь файл не нужно. Новые заказы добавляйте в «Заказы Комуса», готовность — в «Ответы Донцовой». Отправленные машины отмечайте в плане.</p>${b?.sha?`<button data-download="/api/v2/source?sha=${esc(b.sha)}">Скачать первоначальный файл</button>`:''}`:`<h2>Загрузите действующий файл отгрузок Комуса</h2><p>Выберите актуальный лист и укажите, содержатся ли в нём исходные количества или уже чистые остатки. После проверки файл и строки останутся на сервере.</p><button class="primary mutation" data-import="initial">Загрузить первоначальный файл</button>`;$('importFiles').innerHTML=fileTable(state.imports);}
function manualTruckHtml(entries){
 const groups=new Map();
 for(const entry of entries){
  const key=JSON.stringify([entry.rowId,entry.order,entry.invoice,entry.name]);
  const row=groups.get(key)||{...entry,qty:0};
  row.qty+=entry.qty;
  groups.set(key,row);
 }
 const total=entries.reduce((sum,entry)=>sum+entry.qty,0);
 const rows=[...groups.values()].map(entry=>[esc(entry.order),esc(entry.invoice),esc(entry.name),`<strong>${fmt(entry.qty)} шт.</strong>`]);
 return `<article class="panel truck shipment"><div class="truck-top"><div class="truck-name"><h3>Машина <span class="tag">Ручные отметки</span></h3></div></div>
  <details open><summary>Состав · ${groups.size} поз. · ${fmt(total)} шт.</summary><div class="table-wrap">${table(['Заказ','Счёт','Товар','Отгружено'],rows)}</div></details></article>`;
}
function renderHistory(){
 const days=new Map();
 for(const [kind,entries] of [['trucks',state.shipments],['manual',state.manualShipments||[]]]){
  for(const entry of entries){
   const day=days.get(entry.actualDate)||{trucks:[],manual:[]};
   day[kind].push(entry);
   days.set(entry.actualDate,day);
  }
 }
 $('shipments').innerHTML=[...days.keys()].sort().reverse().map(date=>{
  const {trucks,manual}=days.get(date);
  return `<div class="result-title"><h2>${datefmt(date)}</h2></div>`+
   trucks.sort((a,b)=>a.slot-b.slot).map(truck=>truckHtml(truck,true)).join('')+
   (manual.length?manualTruckHtml(manual):'');
 }).join('')||'<div class="panel help"><h2>В истории пока нет отгруженных машин</h2><p>Состав машины появится здесь после отметки об отправке.</p></div>';
}
function renderCalendar(){if(!state)return;const[y,m]=month.split('-').map(Number),first=new Date(y,m-1,1,12),offset=(first.getDay()+6)%7,days=new Date(y,m,0).getDate();$('monthTitle').textContent=new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric'}).format(first);let html=['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'].map(d=>`<div class="weekday">${d}</div>`).join('');for(let i=0;i<offset;i++)html+='<div class="calendar-day weekend"></div>';
 for(let n=1;n<=days;n++){const day=`${y}-${String(m).padStart(2,'0')}-${String(n).padStart(2,'0')}`,trucks=state.plan?.wholePalletsOnly&&state.plan.readinessMode==='position'?state.plan.trucks.filter(t=>t.date===day):[],sent=state.shipments.filter(t=>t.actualDate===day);html+=`<div class="calendar-day ${(offset+n-1)%7>4?'weekend':''} ${day===today()?'today':''}"><span class="date-num">${n}</span>${trucks.length?`<span class="tag purple">План: ${trucks.length} маш.</span>`:''}${sent.length?`<span class="tag">Отгружено: ${sent.length}</span>`:''}${trucks.map(t=>`<button class="calendar-event" data-calendar="${t.id}"><strong>Машина № ${t.id}</strong><small>${fmt(sumTruck(t))} шт. · ${summary(t).length} поз.</small><small>${summary(t).map(p=>p.order).join(', ')}</small></button>`).join('')}${sent.map(t=>`<button class="calendar-event" data-history-card="${t.shipmentId}"><strong>✓ Отгружена № ${t.id}</strong><small>${fmt(sumTruck(t))} шт.</small></button>`).join('')}</div>`;}
 for(let i=(offset+days)%7;i>0&&i<7;i++)html+='<div class="calendar-day weekend"></div>';$('calendarGrid').innerHTML=html;$('calendarCaption').textContent=state.plan?.wholePalletsOnly&&state.plan.readinessMode==='position'?'Нажмите машину, чтобы открыть состав и паллеты. Зелёным отмечены фактические отгрузки.':'План ещё не собран. После расчёта машины появятся на своих датах.';$('calendarDetails').innerHTML='';}
function render(){if(!state)return;$('saveState').textContent=readOnly?'Демонстрация · вымышленные данные':'Все изменения сохранены · версия '+revision;renderPlan();renderOrders();renderResponses();renderCatalog();renderInitial();renderHistory();renderCalendar();sizes();lock(busy);}
function showUpload(kind){if(readOnly)return banner('Для загрузки своих файлов войдите в приложение.');uploadKind=kind;$('uploadForm').reset();$('uploadYear').value=today().slice(0,4);$('uploadTitle').textContent=importNames[kind];$('uploadDescription').textContent={initial:'Одноразовый импорт действующего журнала. Заказы и подтверждённые даты сохранятся на сервере.',catalog:'Загрузите справочник Тулы. Из общей книги с блоком «МОСКВА» загрузятся также позиции Москвы.',moscow:'Загрузите общую книгу с блоком «МОСКВА» или файл с колонками «Наименование» и «Кратность».',order:'В одном файле можно загрузить несколько новых заказов. Номер заказа укажите в каждой строке. Они добавятся к общему журналу.',response:'Обновятся только подтверждения найденных позиций заказа. Заказы и сроки клиента сохранятся.'}[kind];$('initialModeBox').hidden=kind!=='initial';$('uploadSheetBox').hidden=true;$('uploadMessage').hidden=true;clearPreview('Выберите файл, заполните параметры и нажмите «Проверить файл».');$('uploadDialog').showModal();}
function clearPreview(message='Параметры изменены. Нажмите «Проверить файл».'){
 uploadGeneration++;uploadToken=null;$('applyImport').disabled=true;$('uploadPreview').innerHTML='';$('uploadStatus').textContent=message;
}
function uploadError(error){clearPreview('Сохранение недоступно. Исправьте причину и повторите проверку.');$('uploadMessage').hidden=false;$('uploadMessage').classList.add('error');$('uploadMessage').textContent=error.message;}
async function previewFile(){
 clearPreview('Проверяем файл…');const generation=uploadGeneration,file=$('uploadFile').files[0];
 if(!file)throw Error('Сначала выберите файл Excel.');
 if(file.size>15*1024*1024)throw Error('Файл должен быть не больше 15 МБ');
 if(uploadKind==='initial'&&!$('initialMode').value)throw Error('Выберите, какие количества содержатся в первоначальном файле: исходные количества или чистые остатки.');
 $('uploadMessage').hidden=true;$('uploadMessage').classList.remove('error');
 const params=new URLSearchParams({kind:uploadKind,filename:file.name,year:$('uploadYear').value});
 if(!$('uploadSheetBox').hidden&&$('uploadSheet').value)params.set('sheet',$('uploadSheet').value);
 if(uploadKind==='initial')params.set('mode',$('initialMode').value);
 let r=await request('v2/import-preview?'+params,{body:file,raw:true});
 if(generation!==uploadGeneration)return;
 if(r.sheets){
  const expected={order:'новый заказ',response:'ответ донцовой',initial:'отгрузка с узмк'}[uploadKind];
  const preferred=r.sheets.find(s=>s.name.trim().toLowerCase()===expected);
  const allowed=r.sheets.filter(s=>!/(отгруженные|архив)/i.test(s.name));
  const selected=preferred||(allowed.length===1?allowed[0]:null);
  $('uploadSheet').innerHTML='<option value="">Выберите актуальный лист…</option>'+r.sheets.map(s=>`<option value="${esc(s.name)}">${esc(s.name)} · ${s.rows} строк</option>`).join('');
  $('uploadSheet').value=selected?selected.name:'';$('uploadSheetBox').hidden=false;
  if(!selected){$('uploadMessage').hidden=false;$('uploadMessage').textContent='В книге несколько листов. Выберите актуальную таблицу с заказами и нажмите «Проверить файл».';$('uploadStatus').textContent='Сохранение недоступно: ещё не выбран и не проверен лист.';return;}
  params.set('sheet',selected.name);$('uploadStatus').textContent='Проверяем лист «'+selected.name+'»…';
  r=await request('v2/import-preview?'+params,{body:file,raw:true});
  if(generation!==uploadGeneration)return;
 }
 $('uploadMessage').hidden=false;
 if(r.duplicate){$('uploadMessage').textContent=r.message;clearPreview('Этот файл уже сохранён. Повторное применение не требуется.');return;}
 const errors=r.errors||[];
 if(!errors.length&&(!r.count||!r.token))throw Error('Не найдено заполненных строк для сохранения. Проверьте выбранный лист.');
 $('uploadMessage').classList.toggle('error',!!errors.length);
 $('uploadMessage').textContent=`Лист: ${params.get('sheet')||importNames[uploadKind]}. Проверено позиций: ${r.count}. Ошибок: ${errors.length}.\n`+(r.warnings||[]).join('\n');
 let grid='';if(uploadKind==='catalog')grid=table(['Товар','Тип','Шт. на паллете или в партии','Проверка'],r.catalog.map(x=>[esc(x.name),esc(types[x.type]),catalogCapacity(x),x.needsReview?'Нужно уточнить':'—']));
 else if(uploadKind==='moscow')grid=table(['Товар','Кратность'],r.moscowCatalog.map(x=>[esc(x.name),esc(x.original||'—')]));
 else if(uploadKind==='response')grid=table(['Заказ','Счёт','Товар','Дата готовности всей позиции','Дата ответа'],r.changes.map(x=>[esc(x.order),esc(x.invoice),esc(x.name),x.ready?datefmt(x.ready):'Снять прежнюю дату готовности',datefmt(x.asOf)]));
 else grid=table(['Заказ','Счёт','Товар','Количество','Отгружено','Срок'],r.rows.map(x=>[esc(x.order),esc(x.invoice),esc(x.name),fmt(x.qty),fmt(x.shipped),datefmt(x.due)]));
 $('uploadPreview').innerHTML=(errors.length?`<div class="preview-errors"><strong>Сохранение заблокировано: исправьте ${errors.length} ошибок в файле.</strong><br>${errors.map(e=>'Строка '+esc(e.line)+': '+esc(e.reason)).join('<br>')}</div>`:'')+grid;
 uploadToken=errors.length?null:r.token;$('applyImport').disabled=readOnly||!uploadToken;
 $('uploadStatus').textContent=errors.length?'Сохранение недоступно: в файле '+errors.length+' ошибок. Причины перечислены выше.':'Проверка завершена. Проверьте таблицу и нажмите «Применить и сохранить».';
}

function field(label,key,value,type='text',options={}){return `<label class="${options.wide?'wide':''}">${esc(label)}<input name="${key}" type="${type}" value="${esc(value??'')}" ${options.readonly?'readonly':''} ${type==='number'?'min="0" max="1000000" step="1"':''}></label>`;}
function editRow(id){const r=state.rows.find(x=>x.id===id);editing={kind:'row',id};$('editTitle').textContent='Проверка позиции заказа';const locked=state.shipments.some(t=>t.pallets.concat(t.loose).some(p=>p.rowId===id))||(state.manualShipments||[]).some(entry=>entry.rowId===id);$('editFields').innerHTML=field('Номер заказа','order',r.order)+field('Номер счёта','invoice',r.invoice)+field('Артикул Промет','sku',r.sku)+field('Артикул Комуса','komusSku',r.komusSku)+field('Отгрузить до','due',r.due,'date')+field('Наименование','name',r.name,'text',{wide:true})+field('Заказано, шт.','qty',r.qty,'number')+field('Фактически отгружено до начала учёта','shipped',r.shipped,'number',{readonly:locked})+`<p class="muted wide">Дата готовности всей позиции: ${r.ready?datefmt(r.ready):'не указана'}. Дата обновляется ответом Донцовой. При изменении количества готовность этой позиции сбросится: потребуется свежий ответ Донцовой. После правки нужна повторная сверка остатков.</p>`;$('editError').textContent='';$('editDialog').showModal();if(readOnly)lock(false);}
function editCatalog(index){const r=state.catalog[index];editing={kind:'catalog',index};$('editTitle').textContent='Кратность товара';$('editFields').innerHTML=field('Наименование','name',r.name,'text',{wide:true})+field('Артикул Комуса','komusSku',r.komusSku)+field('Артикул ПРОМЕТ','sku',r.sku)+`<label>Тип паллеты<select name="type">${Object.entries(types).map(([k,v])=>`<option value="${k}" ${r.type===k?'selected':''}>${v}</option>`).join('')}</select></label>`+field('Кратность, шт. (для красной 1 — на две паллеты)','capacity',r.capacity,'number')+`<label class="check-field"><input name="vertical" type="checkbox" ${r.vertical?'checked':''}> Вертикальное паллетирование (ВП)</label><label class="check-field"><input name="needsReview" type="checkbox" ${r.needsReview?'checked':''}> Требует проверки</label>`+field('Крепление / размещение для 0','fixation',r.fixation,'text',{wide:true})+`<p class="muted wide">Исходная запись: ${esc(r.original||'—')}. Для красной 1 поле хранит сумму на две паллеты 0,5. Запись N*2п означает N шт. на одной паллете. Минимальный заказ проверяет менеджер, обе паллеты могут ехать отдельно. Для груза с маркером 0 также нужна положительная кратность отгрузки; неполные паллеты не отправляются.</p>`;$('editError').textContent='';$('editDialog').showModal();if(readOnly)lock(false);}
async function boot(){if(!readOnly){const s=await request('session');if(!s.authenticated)return;}$('login').hidden=true;$('shell').hidden=false;await loadState();if(state.plan?.trucks[0])month=state.plan.trucks[0].date.slice(0,7);renderCalendar();if(readOnly){banner('Демонстрация · вымышленные данные. Для своего журнала войдите в приложение.');$('logout').textContent='Войти';$('logout').onclick=()=>location.href='/';}}
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await request('login',{body:{password:$('password').value}});$('password').value='';$('loginError').textContent='';await boot();}catch(e){$('loginError').textContent=e.message;}};
$('logout').onclick=async()=>{try{await request('logout',{body:{}});location.reload();}catch(e){banner(e.message,true);}};
$('refresh').onclick=()=>loadState().then(()=>banner('Журнал обновлён с сервера.')).catch(e=>banner(e.message,true));
$('theme').onclick=()=>{document.body.classList.toggle('dark');localStorage.setItem('komus-theme',document.body.classList.contains('dark')?'dark':'light');};if(localStorage.getItem('komus-theme')==='dark')document.body.classList.add('dark');
$('calculate').onclick=async()=>{try{await action('build',{start:$('start').value,strategy:$('strategy').value});if(state.plan?.trucks[0])month=state.plan.trucks[0].date.slice(0,7);renderCalendar();}catch(e){banner(e.message,true);}};
$('reconcile').onclick=()=>action('reconcile').catch(e=>banner(e.message,true));$('orderSearch').oninput=filterOrders;$('orderSelect').onchange=filterOrders;$('catalogSearch').oninput=renderCatalog;
$('downloadOrderReply').onclick=()=>{const order=$('orderSelect').value;if(order)download('/api/v2/templates/response?order='+encodeURIComponent(order));};
$('uploadForm').onsubmit=async e=>{e.preventDefault();if(busy)return;lock(true);try{await previewFile();}catch(e){uploadError(e);}finally{lock(false);}};
$('uploadFile').onchange=()=>{$('uploadSheetBox').hidden=true;$('uploadSheet').innerHTML='';$('uploadMessage').hidden=true;clearPreview('Файл выбран. Заполните параметры и нажмите «Проверить файл».');};const uploadChanged=()=>{$('uploadMessage').hidden=true;clearPreview();};$('uploadSheet').onchange=uploadChanged;$('initialMode').onchange=uploadChanged;$('uploadYear').onchange=uploadChanged;
$('applyImport').onclick=async()=>{if(!uploadToken)return;try{const result=await action('import-apply',{token:uploadToken});if(result){$('uploadDialog').close();uploadToken=null;}}catch(e){uploadError(e);}};
$('editForm').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{if(editing.kind==='row'){const changes=Object.fromEntries(f);changes.qty=Number(changes.qty);changes.shipped=Number(changes.shipped);await action('row',{id:editing.id,changes});}else{const old=state.catalog[editing.index],item={...old,...Object.fromEntries(f),capacity:Number(f.get('capacity')),vertical:f.has('vertical'),needsReview:f.has('needsReview')};await action('catalog-row',{index:editing.index,item});}$('editDialog').close();}catch(e){$('editError').textContent=e.message;}};
$('dispatchForm').onsubmit=async e=>{e.preventDefault();try{await action('dispatch',{planId:state.plan.id,truckId:dispatching,actualDate:$('actualDate').value});$('dispatchDialog').close();}catch(e){$('dispatchError').textContent=e.message;}};
function openRowTransition(id,operation){
 const row=state.rows.find(row=>row.id===id);
 if(!row||readOnly||busy)return;
 const remaining=row.qty-row.shipped;
 if(remaining<=0)return;
 const isShipment=operation==='ship';
 const isRelease=operation==='release';
 if(isRelease&&!row.reserved)return;
 rowTransition={id,operation,revision};
 $('rowTransitionTitle').textContent=isRelease?'Снять отметку «В готовой машине»':isShipment?'Отметить отгрузку':'Товар в готовой машине';
 $('rowTransitionDescription').textContent=`Заказ ${row.order} · ${row.name}. Остаток: ${fmt(remaining)} шт.`+
  (row.reserved?` В готовой машине: ${fmt(row.reserved)} шт.`:'');
 $('rowTransitionQtyLabel').textContent=isShipment?'Отправлено сейчас, шт.':'Всего в готовой машине, шт.';
 const quantity=$('rowTransitionQty');
 $('rowTransitionQuantityLabel').hidden=isRelease;
 quantity.required=!isRelease;
 quantity.min=isShipment?'1':'0';
 quantity.max=remaining;
 quantity.value=isRelease?0:row.reserved||remaining;
 $('rowTransitionDateLabel').hidden=!isShipment;
 $('rowTransitionDate').required=isShipment;
 $('rowTransitionDate').value=today();
 $('rowTransitionDate').max=today();
 $('rowTransitionHint').textContent=isRelease
  ? `В расчёт вернутся ${fmt(row.reserved)} шт. по этой позиции. Фактические отгрузки сохранятся.`
  : isShipment
  ? 'Укажите количество за эту отправку. Если товар был в готовой машине, приложение сначала спишет его оттуда.'
  : 'Это количество исключается из сборки новых машин. Введите 0, чтобы вернуть товар в расчёт.';
 if(state.plan)$('rowTransitionHint').textContent+=' После сохранения изменения соберите план машин заново.';
 $('saveRowTransition').textContent=isRelease?'Снять отметку':isShipment?'Подтвердить отгрузку':'Сохранить количество';
 $('rowTransitionError').textContent='';
 $('rowTransitionDialog').showModal();
}
$('rowTransitionForm').onsubmit=async event=>{
 event.preventDefault();
 if(!rowTransition||busy)return;
 if(rowTransition.revision!==revision){
  $('rowTransitionError').textContent='Журнал изменился. Закройте окно и заново откройте действие для этой позиции.';
  return;
 }
 const {id,operation}=rowTransition;
 const data=operation==='release'
  ? {id,operation:'reserve',quantity:0}
  : {id,operation,quantity:Number($('rowTransitionQty').value)};
 if(operation==='ship')data.actualDate=$('rowTransitionDate').value;
 try{
  if(await action('row-transition',data))$('rowTransitionDialog').close();
 }catch(error){$('rowTransitionError').textContent=error.message;}
};
$('clearPlan').onclick=()=>{
 if(!state?.plan||readOnly||busy)return;
 planToClear=state.plan.id;
 $('clearPlanError').textContent='';
 $('clearPlanDialog').showModal();
};
$('clearPlanForm').onsubmit=async event=>{
 event.preventDefault();
 if(busy)return;
 try{
  if(await action('clear-plan',{planId:planToClear}))$('clearPlanDialog').close();
 }catch(error){$('clearPlanError').textContent=error.message;}
};
function shiftMonth(delta){const[y,m]=month.split('-').map(Number),d=new Date(y,m-1+delta,1,12);month=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');renderCalendar();}$('prevMonth').onclick=()=>shiftMonth(-1);$('nextMonth').onclick=()=>shiftMonth(1);$('printCalendar').onclick=$('printPlan').onclick=()=>window.print();
$('printLayouts').onclick=()=>window.print();$('retryLayouts').onclick=()=>loadLayouts().catch(()=>{});
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||b.disabled)return;
 if(b.dataset.layout)openLayout(Number(b.dataset.layout)).catch(()=>{});
 if(b.dataset.tab)tab(b.dataset.tab);if(b.dataset.close)$(b.dataset.close).close();if(b.dataset.download)download(b.dataset.download);if(b.dataset.import)showUpload(b.dataset.import);if(b.dataset.editRow)editRow(b.dataset.editRow);if(b.dataset.editCatalog!==undefined)editCatalog(Number(b.dataset.editCatalog));
 if(b.dataset.shipRow)openRowTransition(b.dataset.shipRow,'ship');
 if(b.dataset.reserveRow)openRowTransition(b.dataset.reserveRow,'reserve');
 if(b.dataset.releaseRow)openRowTransition(b.dataset.releaseRow,'release');
 if(b.dataset.dispatch){dispatching=Number(b.dataset.dispatch);const t=state.plan.trucks.find(t=>t.id===dispatching);$('dispatchDescription').textContent=`Машина № ${t.id}: ${fmt(sumTruck(t))} шт., ${summary(t).length} позиций. Плановая дата ${datefmt(t.date)}.`;$('actualDate').value=today();$('actualDate').max=today();$('dispatchError').textContent='';$('dispatchDialog').showModal();}
 if(b.dataset.calendar||b.dataset.historyCard){const hist=!!b.dataset.historyCard,t=hist?state.shipments.find(t=>t.shipmentId===b.dataset.historyCard):state.plan.trucks.find(t=>t.id===Number(b.dataset.calendar));$('calendarDetails').innerHTML=truckHtml(t,hist);sizes();lock(busy);$('calendarDetails').scrollIntoView({behavior:'smooth',block:'start'});}
 if(b.dataset.pallet!==undefined){const t=b.dataset.shipment?state.shipments.find(t=>t.shipmentId===b.dataset.shipment):state.plan.trucks.find(t=>t.id===Number(b.dataset.truck));const p=t.pallets[Number(b.dataset.pallet)];$('palletDetail').innerHTML=`<span class="eyebrow">МАШИНА № ${t.id} · МЕСТО ${Number(b.dataset.pallet)+1}</span><h2>${esc(p.name)}</h2><p><strong>${fmt(p.qty)} шт.</strong>${p.vertical?' · ВП':''}</p><p>Заказ: ${esc(p.order)}<br>Счёт: ${esc(p.invoice)}<br>Артикул: ${esc(p.sku||p.komusSku||'—')}</p><p>Готовность: ${datefmt(p.ready)}<br>Отгрузить до: ${datefmt(p.due)}</p>${p.partial?'<p class="tag amber">Паллета заполнена частично, занимает целое место.</p>':''}`;$('palletDialog').showModal();}
});
boot().catch(e=>{$('loginError').textContent=e.message;banner(e.message,true);});
