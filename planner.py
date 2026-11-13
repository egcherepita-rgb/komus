"""Deterministic pallet placement. No product/order mixing within a pallet."""
from datetime import date, timedelta
from math import ceil
import re

LAYOUTS = [dict(id=i+1, doubles=d, singles=s, length=d*1200+s//2*1000)
           for i,(d,s) in enumerate([(0,26),(1,24),(2,22),(3,18),(4,16),(5,14),(6,12),(7,10),(8,6),(9,4),(10,2),(11,0)])]

def normalize(value):
    text = str(value or '').upper().replace('Ё','Е')
    text = text.translate(str.maketrans('АВСЕНКМОРТХ','ABCEHKMOPTX'))
    return re.sub(r'[^A-ZА-Я0-9/]', '', text)

def workdays(a,b):
    """Weekdays after a through b, without a public-holiday calendar."""
    return sum((a+timedelta(days=n)).weekday()<5 for n in range(1,max(0,(b-a).days)+1))

def number(value, label, allow_zero=True):
    if isinstance(value,bool): raise ValueError(label+': требуется целое число')
    try:
        n=float(str(value).replace(',','.'))
        if not n.is_integer() or n<0 or (not allow_zero and n==0) or n>1000000: raise ValueError()
        return int(n)
    except (ValueError,TypeError,OverflowError): raise ValueError(label+': требуется целое число от '+('0' if allow_zero else '1')+' до 1 000 000')

def build_plan(rows, catalog, start, strategy='urgent', reconciled=False, horizon=120):
    if not reconciled: raise ValueError('Сначала подтвердите сверку остатков с уже отгруженными рейсами.')
    if strategy not in ('urgent','compact','balanced'): raise ValueError('Неизвестный вариант расчёта')
    start=date.fromisoformat(start)
    if len(rows)>5000: raise ValueError('Не более 5000 строк в проекте')
    index={}
    for c in catalog: index.setdefault(normalize(c.get('name')),[]).append(c)
    pallets=[]; loose=[]; issues=[]; remaining={}; rowmap={}; reasons={}
    for pos, source in enumerate(rows):
        r=dict(source); rid=str(r.get('id',pos))
        if rid in rowmap: raise ValueError('Повторяется идентификатор строки '+rid)
        r['id']=rid; rowmap[rid]=r
        try:
            qty=number(r.get('qty'), 'Количество'); shipped=number(r.get('shipped',0),'Уже отгружено')
            if shipped>qty: raise ValueError('Отгружено больше заказанного')
            left=qty-shipped; remaining[rid]=left
            if not left: continue
            if not str(r.get('name','')).strip(): raise ValueError('Не указано наименование')
            try: ready=date.fromisoformat(r.get('ready','')); due=date.fromisoformat(r.get('due',''))
            except (TypeError,ValueError): raise ValueError('Укажите готовность и срок «Отгрузить до»')
            matches=index.get(normalize(r['name']),[])
            if len(matches)!=1: raise ValueError('Нет однозначного совпадения в справочнике Тула')
            c=matches[0]; typ=c.get('type')
            if typ not in ('single','double','split','loose'): raise ValueError('Укажите тип паллетирования')
            if c.get('needsReview'): raise ValueError('Подтвердите толкование кратности в справочнике')
            common={'rowId':rid,'name':r['name'],'order':r.get('order',''),'invoice':r.get('invoice',''),
                    'ready':ready.isoformat(),'due':due.isoformat(),'vertical':bool(c.get('vertical'))}
            if typ=='loose':
                if not c.get('fixation'): raise ValueError('Для груза без места укажите способ размещения / крепления')
                loose.append(dict(common,qty=left,type='loose',fixation=c['fixation'])); continue
            cap=number(c.get('capacity'), 'Кратность',False)
            if typ=='split':
                if cap%2: raise ValueError('Красная 1: кратность должна делиться на две одиночные паллеты')
                cap//=2
            if len(pallets)+ceil(left/cap)>20000: raise ValueError('Расчёт превышает 20 000 паллет; разделите проект')
            for n in range(ceil(left/cap)):
                amount=min(cap,left-n*cap)
                pallets.append(dict(common,qty=amount,capacity=cap,partial=amount<cap,
                                    type='double' if typ=='double' else 'single'))
        except ValueError as e:
            issues.append({'rowId':rid,'name':r.get('name',''),'reason':str(e)})
            reasons[rid]=str(e)
    trucks=[]
    def priority(p): return (p['due'],p['ready'],p['rowId'])
    for offset in range(horizon):
        day=start+timedelta(days=offset)
        if day.weekday()>4: continue
        for slot in range(2):
            available=sorted((p for p in pallets if p['ready']<=day.isoformat()),key=priority)
            if slot==1 and not any(workdays(date.fromisoformat(p['due']),day)>3 for p in available): break
            doubles=[p for p in available if p['type']=='double']; singles=[p for p in available if p['type']=='single']
            candidates=[]
            ranks={id(p):n for n,p in enumerate(available)}
            for layout in LAYOUTS:
                d,s=layout['doubles'],layout['singles']
                if len(doubles)<d or len(singles)<s: continue
                selected=doubles[:d]+singles[:s]
                urgent=sum(max(0,(day-date.fromisoformat(p['due'])).days)+1 for p in selected)
                # Rank earliest deadlines first, then carried pallet count / length.
                deadline_score=tuple(-ranks[id(p)] for p in sorted(selected,key=priority))
                if strategy=='compact': score=(len(selected),layout['length'],deadline_score)
                elif strategy=='balanced': score=(sum(p['due']<=day.isoformat() for p in selected),len(selected),urgent)
                else: score=(deadline_score,len(selected),layout['length'])
                candidates.append((score,layout,selected))
            if not candidates: break
            _,layout,selected=max(candidates,key=lambda c:c[0])
            chosen={id(p) for p in selected}; pallets=[p for p in pallets if id(p) not in chosen]
            attached=[p for p in loose if p['ready']<=day.isoformat()]
            loose=[p for p in loose if p not in attached]
            for p in selected+attached: remaining[p['rowId']]-=p['qty']
            trucks.append({'id':len(trucks)+1,'date':day.isoformat(),'slot':slot+1,'layout':layout,
                           'pallets':selected,'loose':attached,'late':sum(p['due']<day.isoformat() for p in selected),
                           'secondReason':'Просрочка доступного груза более 3 рабочих дней' if slot else ''})
    leftovers=[]
    for rid,left in remaining.items():
        if left:
            r=rowmap[rid]
            reason=reasons.get(rid)
            if not reason:
                if r.get('ready','')>(start+timedelta(days=horizon-1)).isoformat(): reason='Готовность за горизонтом расчёта'
                else: reason='Не хватает совместимых паллет для полной машины по одной из 12 схем; нужен перенос / догруз'
            leftovers.append(dict(rowId=rid,name=r.get('name',''),qty=left,order=r.get('order',''),invoice=r.get('invoice',''),reason=reason))
    return {'trucks':trucks,'leftovers':leftovers,'issues':issues,'strategy':strategy,'start':start.isoformat(),
            'horizon':horizon,'notice':'План расстановки по габаритам. Вес, высота, устойчивость и крепление проверяются перед отгрузкой. Рабочие дни: пн–пт, без календаря праздников.'}
