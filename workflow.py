"""Persistent order ledger and readiness lots. All mutations run in one DB transaction."""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo
import hashlib, json, uuid
from planner import build_plan, number, normalize, workdays, pallet_capacity


def today(): return datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
def now(): return datetime.now(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds')
def uid(): return uuid.uuid4().hex
def clean(v): return str(v or '').strip()
def canonical_order(v):
    import re
    return re.sub(r'^заказ\s*(?:№\s*)?', '',clean(v),flags=re.I).strip()
def key(order,position): return canonical_order(order)+'/'+clean(position)

def empty_state():
    return {'schema':2,'rows':[],'catalog':[],'bootstrap':None,'catalogUpdated':None,
            'imports':[],'shipments':[],'plan':None,'reconciled':False,
            'settings':{'start':today(),'strategy':'urgent'}}

def validate_row(r):
    if not canonical_order(r.get('order')): raise ValueError('Укажите номер заказа Комуса')
    if not clean(r.get('position')): raise ValueError('Укажите постоянный номер позиции заказа')
    if not clean(r.get('name')): raise ValueError('Укажите наименование')
    qty=number(r.get('qty'),'Заказано',False); shipped=number(r.get('shipped',0),'Уже отгружено')
    if shipped>qty: raise ValueError('Отгружено больше заказанного')
    if r.get('due'):
        try:date.fromisoformat(r['due'])
        except (ValueError,TypeError):raise ValueError('Некорректная дата «Отгрузить до»')
    lots=r.get('lots',[]); parts=set();total=0
    for lot in lots:
        part=clean(lot.get('part'))
        if not part or part in parts: raise ValueError('Повторяется номер части готовности')
        parts.add(part); total+=number(lot.get('qty'),'Подтверждено',False)
        try:date.fromisoformat(lot['ready'])
        except (KeyError,TypeError,ValueError):raise ValueError('Укажите дату готовности каждой части')
    if total>qty-shipped:raise ValueError('Подтверждено больше неотгруженного остатка')
    return qty,shipped

def upgrade(project):
    if not project:return empty_state()
    if project.get('schema')==2:return deepcopy(project)
    state=empty_state(); used=set()
    for i,old in enumerate(project.get('rows',[]),1):
        r=deepcopy(old);r['id']=uid();r['order']=canonical_order(r.get('order'));r['position']='И-'+str(i)
        r['internalPosition']=True;r['sku']='';r['komusSku']='';r['lots']=[];r['replyAsOf']=''
        try:
            q=number(r.get('qty'),'Количество');s=number(r.get('shipped',0),'Отгружено')
            if q>=s and q>s and r.get('ready'):
                date.fromisoformat(r['ready']);r['lots']=[{'part':'1','qty':q-s,'ready':r['ready'],'asOf':'','source':'v1'}]
        except (ValueError,TypeError):pass
        r.pop('ready',None);state['rows'].append(r)
    state['catalog']=deepcopy(project.get('catalog',[]));state['reconciled']=bool(project.get('reconciled'))
    state['settings']={'start':project.get('start') or today(),'strategy':project.get('strategy','urgent')}
    if state['rows']:state['bootstrap']={'filename':'Сохранённый проект версии 1','at':now(),'legacy':True}
    state['catalogUpdated']=now() if state['catalog'] else None
    return state

def list_orders(state):
    result={}
    for r in state['rows']:
        o=r.get('order','') or 'Без номера — требуется проверка'
        entry=result.setdefault(o,{'order':o,'lines':0,'qty':0,'shipped':0,'remaining':0,'confirmed':0,'due':'','issues':0})
        entry['lines']+=1
        try:
            q,s=validate_row(r);left=q-s; confirmed=sum(x['qty'] for x in r['lots'])
            entry['qty']+=q;entry['shipped']+=s;entry['remaining']+=left;entry['confirmed']+=confirmed
        except ValueError:entry['issues']+=1
        if r.get('due'):entry['due']=min(entry['due'] or r['due'],r['due'])
    return list(result.values())

def public_state(state):
    s=deepcopy(state);s['orders']=list_orders(s)
    s['totals']={k:sum(o[k] for o in s['orders']) for k in ('qty','shipped','remaining','confirmed')}
    return s

def apply_import(state,kind,parsed,meta):
    """Atomically apply an already reviewed file. Never replace the order book with a reply."""
    if any(h['sha']==meta['sha'] and h['kind']==kind for h in state['imports']):
        return {'duplicate':True,'message':'Этот файл уже обработан. Количество не добавлялось.'}
    if parsed.get('errors'):raise ValueError('В файле есть ошибки. Исправьте их перед загрузкой.')
    if kind=='initial':
        if state['bootstrap'] or state['rows']:raise ValueError('Первоначальный файл уже загружен. Добавляйте новые заказы отдельной кнопкой.')
        state['rows']=deepcopy(parsed['rows']);state['bootstrap']=deepcopy(meta);state['reconciled']=False
    elif kind=='order':
        incoming=parsed['rows'];existing={canonical_order(r.get('order')) for r in state['rows']}
        if any(canonical_order(r['order']) in existing for r in incoming):raise ValueError('Этот номер заказа уже есть в журнале. Повторный заказ не добавлен.')
        seen=set()
        for r in incoming:
            validate_row(r);k=key(r['order'],r['position'])
            if k in seen:raise ValueError('Повтор позиции внутри заказа')
            seen.add(k)
        state['rows'].extend(deepcopy(incoming))
    elif kind=='catalog':
        state['catalog']=deepcopy(parsed['catalog']);state['catalogUpdated']=now()
    elif kind=='response':
        index={r['id']:r for r in state['rows']}; changes=parsed['changes']
        for change in changes:
            r=index.get(change['rowId'])
            if not r:raise ValueError('Позиция заказа больше не существует. Обновите предварительный просмотр.')
            if r['shipped']!=change['shippedAtRequest']:raise ValueError('После подготовки ответа прошла отгрузка. Скачайте свежий шаблон ответа.')
            if r.get('replyAsOf','')>change['asOf']:raise ValueError('Ответ старее уже сохранённого подтверждения')
            candidate=dict(r,lots=change['lots']);validate_row(candidate)
            r['lots']=deepcopy(change['lots']);r['replyAsOf']=change['asOf'];r['replyComment']=change.get('comment','')
    else:raise ValueError('Неизвестный тип загрузки')
    if len(state['rows'])>5000:raise ValueError('В журнале не может быть более 5000 позиций')
    state['imports'].append(dict(meta,kind=kind,lines=parsed.get('count',len(parsed.get('rows',parsed.get('catalog',[]))))))
    state['plan']=None
    return {'duplicate':False,'message':'Данные загружены и сохранены на сервере.'}

def update_row(state,rid,changes):
    r=next((r for r in state['rows'] if r['id']==rid),None)
    if r is None:raise ValueError('Позиция не найдена')
    allowed={'order','position','invoice','sku','komusSku','name','due','qty','shipped','lots'}
    if set(changes)-allowed:raise ValueError('Неизвестное поле позиции')
    candidate=deepcopy(r);candidate.update(changes);candidate['order']=canonical_order(candidate.get('order'))
    # Shipment history is authoritative after the first recorded dispatch.
    has_history=any(any(p['rowId']==rid for p in t['pallets']+t['loose']) for t in state['shipments'])
    if has_history:
        if candidate['shipped']!=r['shipped']:raise ValueError('Количество отправленного ведётся по журналу машин и не редактируется вручную.')
        if any(candidate.get(k)!=r.get(k) for k in ('order','position','name','invoice','sku','komusSku')):raise ValueError('Реквизиты позиции с фактической отгрузкой изменять нельзя: они связаны с историей машины.')
    if 'lots' not in changes and any(candidate.get(k)!=r.get(k) for k in ('qty','shipped')):
        candidate['lots']=[];candidate['replyAsOf']='';candidate['replyComment']='Количество исправлено вручную: требуется новое подтверждение остатка.'
    validate_row(candidate)
    if any(x['id']!=rid and key(x.get('order'),x.get('position'))==key(candidate['order'],candidate['position']) for x in state['rows']):raise ValueError('Такая позиция этого заказа уже существует')
    r.update(candidate);state['reconciled']=False;state['plan']=None

def make_plan(state):
    if not state['reconciled']:raise ValueError('Подтвердите сверку первоначальных остатков перед сборкой машин.')
    if not state['catalog']:raise ValueError('Сначала загрузите справочник кратности Тула.')
    units=[];mapping={};extra=[];issues=[]
    for r in state['rows']:
        try:
            q,s=validate_row(r);left=q-s
            if left==0:continue
            if not r.get('due'):raise ValueError('Не указан срок «Отгрузить до»')
            if not r.get('order'):raise ValueError('Не указан номер заказа')
            confirmed=sum(l['qty'] for l in r['lots'])
            if confirmed:
                unit=r['id'];mapping[unit]=r
                units.append({'id':unit,'name':r['name'],'order':r['order'],'invoice':r.get('invoice',''),
                              'qty':confirmed,'shipped':0,'ready':min(l['ready'] for l in r['lots']),'due':r['due'],'lots':deepcopy(r['lots'])})
            if left>confirmed:extra.append({'rowId':r['id'],'order':r['order'],'position':r['position'],'invoice':r.get('invoice',''),
                 'name':r['name'],'qty':left-confirmed,'reason':'Нет подтверждённой готовности от Донцовой на это количество'})
        except ValueError as e:
            issues.append({'rowId':r['id'],'name':r.get('name',''),'reason':str(e)})
            try:left=max(0,number(r.get('qty'),'Кол.')-number(r.get('shipped',0),'Отгружено'))
            except ValueError:left=0
            if left:extra.append({'rowId':r['id'],'order':r.get('order',''),'position':r.get('position',''),'invoice':r.get('invoice',''),
                                  'name':r.get('name',''),'qty':left,'reason':str(e)})
    used=defaultdict(int)
    for shipment in state['shipments']:used[shipment['actualDate']]+=1
    start=max(today(),state['settings']['start'])
    p=build_plan(units,state['catalog'],start,state['settings']['strategy'],True,daily_used=used)
    for t in p['trucks']:
        for item in t['pallets']+t['loose']:
            r=mapping[item['rowId']];parts=[a['lotId'] for a in item['lotAllocations']]
            item.update(rowId=r['id'],lotId=', '.join(parts),position=r['position'],sku=r.get('sku',''),komusSku=r.get('komusSku',''))
    for item in p['leftovers']:
        r=mapping[item['rowId']];item.update(rowId=r['id'],position=r['position'])
    for item in p['issues']:
        r=mapping[item['rowId']];item['rowId']=r['id']
    p['leftovers'].extend(extra);p['issues'].extend(issues);p['id']=uid();p['createdAt']=now()
    state['plan']=p;return p

def dispatch(state,plan_id,truck_id,actual_date):
    existing=next((s for s in state['shipments'] if s['planId']==plan_id and s['id']==truck_id),None)
    if existing:return {'duplicate':True,'message':'Эта машина уже отмечена отгруженной.'}
    p=state.get('plan')
    if not p or p['id']!=plan_id:raise ValueError('План изменился. Обновите страницу.')
    t=next((x for x in p['trucks'] if x['id']==truck_id),None)
    if not t:raise ValueError('Машина не найдена в текущем плане')
    if not p.get('wholePalletsOnly'):raise ValueError('Пересоберите машины: этот план создан до включения правила целых паллет.')
    try:actual=date.fromisoformat(actual_date)
    except (TypeError,ValueError):raise ValueError('Укажите фактическую дату отгрузки')
    if actual.isoformat()>today():raise ValueError('Нельзя отметить будущую дату фактической отгрузки')
    if any(x['ready']>actual_date for x in t['pallets']+t['loose']):raise ValueError('Нельзя отгрузить раньше подтверждённой готовности')
    day_count=sum(s['actualDate']==actual_date for s in state['shipments'])
    if day_count>=2:raise ValueError('На эту дату уже учтены две машины')
    if day_count==1 and not any(workdays(date.fromisoformat(x['due']),actual)>3 for x in t['pallets']):raise ValueError('Вторая машина допускается только при просрочке более 3 рабочих дней')
    allocations=defaultdict(int)
    index={r['id']:r for r in state['rows']}
    for item in t['pallets']+t['loose']:
        r=index[item['rowId']];matches=[c for c in state['catalog'] if normalize(c.get('name'))==normalize(r['name'])]
        if len(matches)!=1:raise ValueError('Проверьте кратность позиции и пересоберите машины.')
        cap=pallet_capacity(matches[0])
        if item.get('partial') or item.get('capacity')!=cap or item['qty']!=cap:raise ValueError('Отгрузка допускается только целыми паллетами. Пересоберите машины.')
        sources=item.get('lotAllocations',[])
        if not sources or sum(number(a.get('qty'),'Количество части',False) for a in sources)!=item['qty']:raise ValueError('Не совпадает состав готовности паллеты. Пересоберите машины.')
        for source in sources:allocations[(item['rowId'],source['lotId'])]+=source['qty']
    for (rid,lid),qty in allocations.items():
        r=index[rid];lot=next((l for l in r['lots'] if l['part']==lid),None)
        if not lot or lot['qty']<qty:raise ValueError('Остаток подтверждения изменился. Пересоберите машины.')
        if lot['ready']>actual_date:raise ValueError('Нельзя отгрузить раньше готовности всех частей паллеты')
    for (rid,lid),qty in allocations.items():
        r=index[rid];lot=next(l for l in r['lots'] if l['part']==lid)
        lot['qty']-=qty;r['shipped']+=qty;r['lots']=[l for l in r['lots'] if l['qty']>0]
        validate_row(r)
    sent=deepcopy(t);sent.update(planId=plan_id,actualDate=actual_date,markedAt=now(),shipmentId=uid(),plannedSlot=t['slot'],slot=day_count+1)
    state['shipments'].append(sent);make_plan(state)
    return {'duplicate':False,'message':'Машина отгружена. Остатки списаны, план по оставшемуся товару пересобран.'}
