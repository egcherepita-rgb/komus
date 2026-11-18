"""Persistent order ledger and readiness lots. All mutations run in one DB transaction."""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo
import hashlib, json, uuid
from planner import build_plan, number, resolve_catalog_product, workdays, pallet_capacity


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
    if number(r.get('reserved',0),'В готовой машине')>qty-shipped:
        raise ValueError('Количество в готовой машине превышает неотгруженный остаток')
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

def position_ready(r):
    """A date is usable only when it covers the whole unshipped order position."""
    try:
        q,s=validate_row(r);lots=r.get('lots',[])
        if q==s or not lots or sum(l['qty'] for l in lots)!=q-s:return ''
        dates={l['ready'] for l in lots}
        return next(iter(dates)) if len(dates)==1 else ''
    except ValueError:return ''

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
            q,s=validate_row(r);left=q-s; confirmed=left if position_ready(r) else 0
            entry['qty']+=q;entry['shipped']+=s;entry['remaining']+=left;entry['confirmed']+=confirmed
        except ValueError:entry['issues']+=1
        if r.get('due'):entry['due']=min(entry['due'] or r['due'],r['due'])
    return list(result.values())

def public_state(state):
    s=deepcopy(state);s['orders']=list_orders(s)
    for r in s['rows']:
        r['ready']=position_ready(r);r['readinessNeedsReview']=bool(r.get('lots')) and not r['ready']
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
            ready=change['ready']
            if ready:
                try:date.fromisoformat(ready)
                except (TypeError,ValueError):raise ValueError('Проверьте дату готовности позиции')
            q,s=validate_row(r)
            lots=[{'part':'1','qty':q-s,'ready':ready,'asOf':change['asOf']}] if ready and q>s else []
            candidate=dict(r,lots=lots);validate_row(candidate)
            r['lots']=lots;r['replyAsOf']=change['asOf']
            for field in ('sku','komusSku'):
                if not r.get(field) and change.get(field):
                    r[field]=change[field]
            if change.get('prometName'):
                r['prometName']=change['prometName']
    else:raise ValueError('Неизвестный тип загрузки')
    if len(state['rows'])>5000:raise ValueError('В журнале не может быть более 5000 позиций')
    state['imports'].append(dict(meta,kind=kind,lines=parsed.get('count',len(parsed.get('rows',parsed.get('catalog',[]))))))
    state['plan']=None
    return {'duplicate':False,'message':'Данные загружены и сохранены на сервере.'}

def update_row(state,rid,changes):
    r=next((r for r in state['rows'] if r['id']==rid),None)
    if r is None:raise ValueError('Позиция не найдена')
    allowed={'order','position','invoice','sku','komusSku','name','due','qty','shipped'}
    if set(changes)-allowed:raise ValueError('Неизвестное поле позиции')
    candidate=deepcopy(r);candidate.update(changes);candidate['order']=canonical_order(candidate.get('order'))
    # Shipment history is authoritative after the first recorded dispatch.
    has_history=any(p['rowId']==rid for t in state['shipments'] for p in t['pallets']+t['loose'])
    has_history=has_history or any(entry['rowId']==rid for entry in state.get('manualShipments',[]))
    if has_history:
        if candidate['shipped']!=r['shipped']:raise ValueError('Количество отправленного учтено в истории отгрузок. Для новой отправки нажмите «Отгружено».')
        if any(candidate.get(k)!=r.get(k) for k in ('order','position','name','invoice','sku','komusSku')):raise ValueError('Реквизиты позиции с фактической отгрузкой связаны с историей отгрузок.')
    if 'lots' not in changes and any(candidate.get(k)!=r.get(k) for k in ('qty','shipped')):
        candidate['lots']=[];candidate['replyAsOf']='';candidate['replyComment']='Количество исправлено вручную: требуется новое подтверждение остатка.'
    if any(candidate.get(k)!=r.get(k) for k in ('name','sku','komusSku')):
        candidate['prometName']=''
    validate_row(candidate)
    if any(x['id']!=rid and key(x.get('order'),x.get('position'))==key(candidate['order'],candidate['position']) for x in state['rows']):raise ValueError('Такая позиция этого заказа уже существует')
    r.update(candidate);state['reconciled']=False;state['plan']=None

def transition_row(state, row_id, operation, quantity, actual_date=None):
    row=next((row for row in state['rows'] if row['id']==row_id),None)
    if row is None:raise ValueError('Позиция не найдена')
    ordered,shipped=validate_row(row)
    remaining=ordered-shipped
    reserved=row.get('reserved',0)
    if operation not in ('reserve','ship'):raise ValueError('Неизвестное действие с позицией')
    quantity=number(quantity,'Количество',allow_zero=operation=='reserve')
    if quantity>remaining:raise ValueError('Количество превышает неотгруженный остаток')
    if operation=='reserve':
        if quantity==reserved:return {'message':'Количество в готовой машине уже сохранено.'}
        row['reserved']=quantity
        message='Количество в готовой машине сохранено.' if quantity else 'Товар снова доступен для планирования.'
    else:
        try:actual=date.fromisoformat(actual_date)
        except (TypeError,ValueError):raise ValueError('Укажите фактическую дату отгрузки')
        if actual.isoformat()>today():raise ValueError('Фактическая дата отгрузки не может быть в будущем')
        # A confirmed date for the whole balance stays valid for the remaining pieces.
        lots=limit_lots(row['lots'],remaining-quantity) if position_ready(row) else []
        readiness_cleared=bool(row['lots']) and not lots and remaining>quantity
        row.update(shipped=shipped+quantity,reserved=max(0,reserved-quantity),lots=lots)
        state.setdefault('manualShipments',[]).append({
            'rowId':row_id,'order':row['order'],'invoice':row.get('invoice',''),'name':row['name'],
            'qty':quantity,'actualDate':actual.isoformat(),'markedAt':now()
        })
        message='Позиция отмечена отгруженной. Остаток уменьшен.' if quantity==remaining else 'Частичная отгрузка сохранена. Остаток уменьшен.'
        if readiness_cleared:message+=' Для остатка нужна одна подтверждённая дата готовности.'
    if state['plan']:
        state['plan']=None
        message+=' Текущий план сброшен. Соберите машины заново.'
    return {'message':message}

def limit_lots(lots, quantity):
    result=[]
    for lot in lots:
        if not quantity:break
        take=min(quantity,lot['qty'])
        result.append(dict(lot,qty=take))
        quantity-=take
    return result

def clear_plan(state, plan_id):
    plan=state.get('plan')
    if not plan or plan.get('id')!=plan_id:raise ValueError('План изменился. Обновите страницу.')
    state['plan']=None
    return {'message':'План машин сброшен. Товар доступен для новой сборки по текущим остаткам.'}

def make_plan(state):
    if not state['reconciled']:raise ValueError('Подтвердите сверку первоначальных остатков перед сборкой машин.')
    if not state['catalog']:raise ValueError('Сначала загрузите справочник кратности Тула.')
    units=[];mapping={};extra=[];issues=[]
    for r in state['rows']:
        try:
            q,s=validate_row(r);reserved=r.get('reserved',0);left=q-s-reserved
            if reserved:
                extra.append({'rowId':r['id'],'order':r['order'],'position':r['position'],'invoice':r.get('invoice',''),
                              'name':r['name'],'qty':reserved,'reason':'В готовой машине, собранной вручную'})
            if left==0:continue
            if not r.get('due'):raise ValueError('Не указан срок «Отгрузить до»')
            if not r.get('order'):raise ValueError('Не указан номер заказа')
            confirmed=sum(l['qty'] for l in r['lots'])
            ready=position_ready(r)
            if confirmed and not ready:raise ValueError('Нужна одна дата готовности на весь остаток позиции. Загрузите новый ответ Донцовой.')
            if ready:
                unit=r['id'];mapping[unit]=r
                units.append({'id':unit,'name':r['name'],'order':r['order'],'invoice':r.get('invoice',''),
                              'sku':r.get('sku'),'komusSku':r.get('komusSku'),'prometName':r.get('prometName'),
                              'qty':left,'shipped':0,'ready':ready,'due':r['due'],'lots':limit_lots(r['lots'],left)})
            else:extra.append({'rowId':r['id'],'order':r['order'],'position':r['position'],'invoice':r.get('invoice',''),
                 'name':r['name'],'qty':left,'reason':'Нет даты готовности позиции от Донцовой'})
        except ValueError as e:
            issues.append({'rowId':r['id'],'name':r.get('name',''),'reason':str(e)})
            try:left=max(0,number(r.get('qty'),'Кол.')-number(r.get('shipped',0),'Отгружено')-number(r.get('reserved',0),'В готовой машине'))
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
    p['leftovers'].extend(extra);p['issues'].extend(issues);p['id']=uid();p['createdAt']=now();p['readinessMode']='position'
    state['plan']=p;return p

def dispatch(state,plan_id,truck_id,actual_date):
    existing=next((s for s in state['shipments'] if s['planId']==plan_id and s['id']==truck_id),None)
    if existing:return {'duplicate':True,'message':'Эта машина уже отмечена отгруженной.'}
    p=state.get('plan')
    if not p or p['id']!=plan_id:raise ValueError('План изменился. Обновите страницу.')
    t=next((x for x in p['trucks'] if x['id']==truck_id),None)
    if not t:raise ValueError('Машина не найдена в текущем плане')
    if not p.get('wholePalletsOnly') or p.get('readinessMode')!='position':raise ValueError('Пересоберите машины: нужна одна дата готовности позиции и отгрузка целыми паллетами.')
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
        r=index[item['rowId']]
        if not position_ready(r):raise ValueError('Нет одной даты готовности на весь остаток позиции. Обновите ответ Донцовой.')
        cap=pallet_capacity(resolve_catalog_product(r,state['catalog']))
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
