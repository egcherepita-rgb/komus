"""Deterministic pallet placement. No product/order mixing within a pallet."""
from bisect import bisect_right
from datetime import date, timedelta
from functools import lru_cache
import re

LAYOUTS = [dict(id=i+1, doubles=d, singles=s, length=d*1200+s//2*1000)
           for i,(d,s) in enumerate([(0,26),(1,24),(2,22),(3,18),(4,16),(5,14),(6,12),(7,10),(8,6),(9,4),(10,2),(11,0)])]

def normalize(value):
    text = str(value or '').upper().replace('Ё','Е')
    text = text.translate(str.maketrans('АВСЕНКМОРТХ','ABCEHKMOPTX'))
    return re.sub(r'[^A-ZА-Я0-9/]', '', text)

def without_komus_prefix(value):
    return re.sub(r'(?<![\w])[0ОO][МM][ХX]_\s*', '', str(value or ''), flags=re.I)

def product_name_key(value):
    return normalize(without_komus_prefix(value))

LIMITER_PLACEMENT = 'Сверху на одной из паллет машины'

def pallet_limiter_model(name):
    title = without_komus_prefix(name).upper().translate(str.maketrans('МС–—', 'MC--'))
    match = re.fullmatch(
        r'\s*ПЛАНКА\s*-?\s*(?:ОГРАНИЧИТЕЛЬ|ОГР\.?)\s+(?:ПРАКТИК\s+)?'
        r'MS\s*-?\s*(30|60)(?:\s*\(\s*20\s*ШТ\.?\s*(?:/\s*УПАК\.?)?\s*\))?\s*', title)
    return f'MS-{match[1]}' if match else ''

def article_key(value):
    value = str(value if value is not None else '').strip().upper()
    if re.fullmatch(r'\d+\.0+', value):
        return value.split('.')[0]
    return value

def article_candidates(product, candidates):
    """Prefer Komus, corroborate with Promet, and discard contradictory codes."""
    komus = article_key(product.get('komusSku'))
    promet = article_key(product.get('sku'))
    primary = [row for row in candidates if komus and article_key(row.get('komusSku')) == komus]
    compatible = []
    for row in primary or candidates:
        row_komus = article_key(row.get('komusSku'))
        row_promet = article_key(row.get('sku'))
        if komus and row_komus and komus != row_komus:
            continue
        if promet and row_promet and promet != row_promet:
            continue
        compatible.append(row)
    secondary = [row for row in compatible if promet and article_key(row.get('sku')) == promet]
    if primary:
        return secondary or compatible, True
    if secondary:
        return secondary, True
    return compatible, False

def different_product_model(left, right):
    """Compare whole model codes, including terminal letters, before comparing digits."""
    first_rack, second_rack = rack_signature(left or ''), rack_signature(right or '')
    if first_rack and second_rack:
        return first_rack != second_rack
    def signature(value):
        value = without_komus_prefix(value).upper().translate(str.maketrans('АВСЕНКМОРТХ–—−', 'ABCEHKMOPTX---'))
        models = re.findall(r'(?<![A-ZА-Я0-9])[A-Z]+(?:/[A-Z]+)?[ -]?\d+[A-Z]*(?:-\d+[A-Z]*)*', value)
        return [re.sub(r' ', '-', model) for model in models], re.findall(r'\d+', value)
    first, first_numbers = signature(left)
    second, second_numbers = signature(right)
    if first and second:
        if first[0] != second[0]:
            return True
    # Names without a model can still disagree on dimensions or other numeric details.
    return bool(first_numbers and second_numbers and first_numbers != second_numbers
                and not (first and second and (first_numbers == second_numbers[:len(first_numbers)]
                                               or second_numbers == first_numbers[:len(second_numbers)])))

@lru_cache(maxsize=8192)
def tula_model(name):
    """Read complete identifiers from the short cabinet, file cabinet and safe names used in Tula."""
    limiter = pallet_limiter_model(name)
    if limiter:
        return ('ПЛАНКА', limiter, '', (), (), '')
    title = without_komus_prefix(name).upper().strip()
    if re.fullmatch(r'(?:КОМУС\s+МЕБЕЛЬ\s+)?ML[-–—]24[-–—]60', title):
        title = 'ШКАФ ДЛЯ СУМОК ' + title
    category = re.match(r'(ШКАФ|КАРТОТЕКА|АНТРЕСОЛЬ|СЕЙФ)\b', title)
    if not category:
        return None
    latin = title.translate(str.maketrans('АВСЕНКМОРТХ–—−', 'ABCEHKMOPTX---'))
    code = r'(?<![A-ZА-Я0-9])(?:ML/LS|AFC|RUS|LS|ML|CB|AMH|AM|TM|SL|A|M)[ .-]?\d+[A-Z]*(?:[-/]\d+[A-Z]*)*(?![\w/-])'
    model = re.search(code, latin)
    if not model:
        return None
    # An alternative identifier must be explicitly written next to the primary model.
    alias = re.match(r'\s*\(\s*('+code+r')\s*\)', latin[model.end():])
    variants = (
        bool(re.search(r'КОМУС\s+МЕБЕЛЬ', title)),
        'УСИЛЕНН' in title,
        'СТАНДАРТ' in title,
        bool(re.search(r'\b(?:EL|ЭЛ)\b|ЭЛЕКТР', title)),
        tuple(sorted(set(re.findall(r'АНТРАЦИТ|СЕРЫЙ|БЕЛЫЙ|Ч[ЕЁ]РНЫЙ|RAL\s*\d+', title)))),
    )
    size = re.search(r'(?<!\d)(\d{2,4})\s*X\s*(\d{2,4})\s*X\s*(\d{2,4})(?!\d)', latin)
    sections = re.search(r'(\d+)\s*[- ]?\s*(?:СЕКЦ|ОТДЕЛЕН|ДВЕР)', title)
    kind, model_code = category[1], normalize(model[0])
    # These two catalogue names identify the same products in the Komus order.
    if model_code == 'ML2460' and kind == 'ШКАФ':
        variants = (False, False, *variants[2:])
    if model_code == 'M08' and kind == 'АНТРЕСОЛЬ':
        kind = 'ШКАФ'
    return (kind, model_code, normalize(alias[1]) if alias else '', variants,
            size.groups() if size else (), sections[1] if sections else '')

def same_tula_model(left, right):
    if not left or not right:
        return False
    category, model, alias, variants, size, sections = left
    other_category, other_model, other_alias, other_variants, other_size, other_sections = right
    same_model = (model == other_model or (not other_alias and alias == other_model)
                  or (not alias and model == other_alias))
    return (category == other_category and same_model and variants == other_variants
            and (not size or not other_size or size == other_size)
            and (not sections or not other_sections or sections == other_sections))

# Tula uses height/width x depth/shelves. Orders also use three dimensions
# separated by × and a separate shelf count, often in another dimension order.
RACK_SIZE = re.compile(
    r'(?<!\w)(\d{2,4})\s*(KD|КД)?\s*(?:[X×*/\\])\s*'
    r'(\d{2,4})\s*(?:[X×*/\\])\s*(\d{2,4})(?!\w)'
)
RACK_SERIES = re.compile(r'\b(COBALT\s+SHELF\s+MS|MS(?:\s+(?:STRONG|STR|PRO|HARD|STANDART|STANDARD|CТАНДАРТ|U))?|SBL|SBE|SB|ES)\b')
RACK_SHELVES = re.compile(r'\b(\d+)\s*ПОЛ(?:ОК|К[А-Я]*)\b')

@lru_cache(maxsize=8192)
def rack_signature(name):
    original = without_komus_prefix(name).upper().strip()
    category = re.match(r'СТЕЛЛАЖ\b', original)
    if not category:
        return None
    original = original[category.end():]
    title = original.translate(str.maketrans('МСХ', 'MCX'))
    series = RACK_SERIES.search(title)
    sizes = list(RACK_SIZE.finditer(title))
    if not series or len(sizes) != 1:
        return None
    size = sizes[0]
    unit = re.match(r'\s*(MM|CM)\b', title[size.end():])
    tail_start = size.end() + (unit.end() if unit else 0)
    compact_shelves = re.match(r'\s*[/\\]\s*(\d+)\b(?:\s+ПОЛ(?:ОК|К[А-Я]*)\b)?', original[tail_start:])
    written_counts = {int(m[1]) for m in RACK_SHELVES.finditer(original)}
    if compact_shelves:
        written_counts.add(int(compact_shelves[1]))
    if len(written_counts) != 1:
        return None
    dimensions = tuple(sorted(int(size[i]) for i in (1, 3, 4)))
    in_cm = unit[1] == 'CM' if unit else dimensions[-1] <= 300
    dimensions_mm = tuple(n * 10 for n in dimensions) if in_cm else dimensions
    family = ' '.join(series[1].split())
    family = {'MS STR': 'MS STRONG', 'MS STANDART': 'MS', 'MS STANDARD': 'MS', 'MS CТАНДАРТ': 'MS'}.get(family, family)
    tail_end = tail_start + (compact_shelves.end() if compact_shelves else 0)
    if series.end() > size.start():
        return None
    details = original[:series.start()] + ' ' + original[series.end():size.start()] + ' ' + original[tail_end:]
    details = RACK_SHELVES.sub(' ', details)
    kd = bool(size[2] or re.search(r'\b(?:KD|КД)\b', details))
    if family == 'COBALT SHELF MS' and kd:
        # Komus uses the actual height; Tula labels these two KD models nominally.
        nominal_height = {1750: 1800, 1950: 2000}.get(dimensions_mm[-1])
        if nominal_height:
            dimensions_mm = (*dimensions_mm[:-1], nominal_height)
    details = re.sub(r'\b(?:KD|КД)\b', ' ', details)
    # Preserve catalogue variants such as medical/СТМ or galvanized finish.
    details = re.sub(r'\b(?:МЕТАЛЛИЧЕСКИЙ|ПРАКТИК|КОМУС)\b', ' ', details)
    qualifiers = normalize(details)
    return family, dimensions_mm, next(iter(written_counts)), kd, qualifiers

def resolve_catalog_product(product, catalog, label='Тула', extra_model=None):
    candidates, matched_article = article_candidates(product, catalog)
    if matched_article:
        if len(candidates) == 1:
            return candidates[0]
        reason = 'Артикулы расходятся' if not candidates else 'Артикул соответствует нескольким строкам'
        raise ValueError(f'{reason} в справочнике {label}. Проверьте артикулы Комуса и ПРОМЕТ.')
    names = (product.get('prometName'), product.get('name'))
    for name in names:
        key = product_name_key(name)
        matches = [row for row in candidates if key and product_name_key(row.get('name')) == key]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f'В справочнике {label} несколько строк с таким названием. Укажите артикул.')
    for name in names:
        rack = rack_signature(name or '')
        if not rack:
            continue
        matches = [row for row in candidates if rack == rack_signature(row.get('name') or '')]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f'В справочнике {label} несколько стеллажей с такой серией, размерами и числом полок. Укажите артикул.')
    for name in names:
        model = tula_model(name or '')
        if not model:
            continue
        matches = [row for row in candidates if same_tula_model(model, tula_model(row.get('name') or ''))]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f'Нет однозначного совпадения в справочнике {label}. Проверьте артикулы или полную модель товара.')
    if extra_model:
        model = extra_model(product.get('name') or '')
        matches = [row for row in candidates if model and model == extra_model(row['name'])]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f'Нет однозначного совпадения в справочнике {label}. Проверьте артикулы или полную модель товара.')

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

def pallet_capacity(c):
    cap=number(c.get('capacity'), 'Кратность отгрузки',False)
    if c.get('type')=='split':
        if cap%2:raise ValueError('Красная 1: кратность должна делиться на две одиночные паллеты')
        cap//=2
    return cap

def apply_two_pallet_notation(item):
    """A red 1 entry N*2п gives N pieces per single pallet and a two-pallet order minimum."""
    if item.get('type') != 'split':
        return
    match = re.fullmatch(r'\s*([1-9]\d{0,5})\s*[*×xх]\s*2\s*п\.?\s*',
                         item.get('original') or '', flags=re.I)
    if not match:
        return
    pieces = int(match[1])
    # Correct the obsolete value in both the saved catalogue and old Excel uploads.
    model = tula_model(item.get('name') or '')
    if pieces == 20 and model and model[:2] == ('ШКАФ','ML2460'):
        item.update(original='18*2п',capacity=36,needsReview=False)
    elif item.get('needsReview') and pieces <= 500000:
        item.update(capacity=pieces*2,needsReview=False)

def whole_pallets(r,left,cap):
    """Combine readiness only within this order position; each chunk is one whole pallet."""
    source_lots=r.get('lots')
    if source_lots is None:
        for _ in range(left//cap):yield {'qty':cap,'ready':r['ready']}
        return
    sources=[];parts=set()
    for lot in source_lots:
        part=str(lot.get('part','')).strip()
        if not part or part in parts:raise ValueError('Проверьте номера частей готовности')
        parts.add(part)
        try:ready=date.fromisoformat(lot['ready']).isoformat()
        except (KeyError,TypeError,ValueError):raise ValueError('Проверьте даты частей готовности')
        sources.append({'lotId':part,'qty':number(lot.get('qty'),'Подтверждено',False),'ready':ready})
    if sum(s['qty'] for s in sources)!=left:raise ValueError('Количество частей не совпадает с подтверждённым остатком')
    sources.sort(key=lambda s:(s['ready'],s['lotId']));index=0
    for _ in range(left//cap):
        needed=cap;allocations=[];ready=''
        while needed:
            source=sources[index];take=min(needed,source['qty'])
            allocations.append({'lotId':source['lotId'],'qty':take});ready=max(ready,source['ready'])
            needed-=take;source['qty']-=take
            if not source['qty']:index+=1
        yield {'qty':cap,'ready':ready,'lotAllocations':allocations}

def allocate_trucks(pallets, loose, start, horizon, daily_used, strategy, schedule=None):
    trucks=[]
    def priority(p): return (p['due'],p['ready'],p['rowId'])
    for offset in range(horizon):
        day=start+timedelta(days=offset)
        if day.weekday()>4: continue
        if schedule is not None and day.isoformat() not in schedule: continue
        daily_layouts = LAYOUTS if schedule is None else [schedule[day.isoformat()]]
        for slot in range(2 if schedule is None else 1):
            if slot < (daily_used or {}).get(day.isoformat(),0): continue
            available=sorted((p for p in pallets if p['ready']<=day.isoformat()),key=priority)
            if slot==1 and not any(workdays(date.fromisoformat(p['due']),day)>3 for p in available): break
            doubles=[p for p in available if p['type']=='double']; singles=[p for p in available if p['type']=='single']
            candidates=[]
            ranks={id(p):n for n,p in enumerate(available)}
            for layout in daily_layouts:
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
            trucks.append({'id':len(trucks)+1,'date':day.isoformat(),'slot':slot+1,'layout':layout,
                           'pallets':selected,'loose':attached,'late':sum(p['due']<day.isoformat() for p in selected),
                           'secondReason':'Просрочка доступного груза более 3 рабочих дней' if slot else ''})
    return trucks

def fuller_schedule(pallets, start, horizon, daily_used, current_trucks):
    """Look ahead on small batches where changing the mix may save another full truck."""
    if not pallets or len(pallets) > 250:
        return None
    arrivals = {'double': [], 'single': []}
    for pallet in pallets:
        arrivals[pallet['type']].append(pallet['ready'])
    doubles = sorted(arrivals['double'])
    singles = sorted(arrivals['single'])
    if (2*len(doubles)+len(singles))//min(2*x['doubles']+x['singles'] for x in LAYOUTS) <= len(current_trucks):
        return None

    days = []
    for offset in range(horizon):
        day = start+timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        iso_day = day.isoformat()
        if not (daily_used or {}).get(iso_day):
            days.append((iso_day, bisect_right(doubles,iso_day), bisect_right(singles,iso_day)))

    @lru_cache(maxsize=None)
    def best(index, used_double, used_single):
        if index == len(days):
            return 0, 0
        _, available_double, available_single = days[index]
        score = best(index+1,used_double,used_single)
        for layout in LAYOUTS:
            next_double = used_double+layout['doubles']
            next_single = used_single+layout['singles']
            if next_double > available_double or next_single > available_single:
                continue
            count, loaded = best(index+1,next_double,next_single)
            score = max(score,(count+1,loaded+layout['doubles']+layout['singles']))
        return score

    if best(0,0,0)[0] <= len(current_trucks):
        return None
    schedule = {}
    preferred = {truck['date']:truck['layout']['id'] for truck in current_trucks if truck['slot'] == 1}
    used_double = used_single = 0
    for index,(day, available_double, available_single) in enumerate(days):
        score = best(index,used_double,used_single)
        if not score[0]:
            break
        # Keep the original strategy's choice when it still permits the better plan.
        layouts = sorted(LAYOUTS,key=lambda layout: layout['id'] != preferred.get(day))
        for layout in layouts:
            next_double = used_double+layout['doubles']
            next_single = used_single+layout['singles']
            if next_double > available_double or next_single > available_single:
                continue
            count, loaded = best(index+1,next_double,next_single)
            if (count+1,loaded+layout['doubles']+layout['singles']) == score:
                schedule[day] = layout
                used_double, used_single = next_double,next_single
                break
    return schedule

def build_plan(rows, catalog, start, strategy='urgent', reconciled=False, horizon=120, daily_used=None):
    if not reconciled: raise ValueError('Сначала подтвердите сверку остатков с уже отгруженными рейсами.')
    if strategy not in ('urgent','compact','balanced'): raise ValueError('Неизвестный вариант расчёта')
    start=date.fromisoformat(start)
    if len(rows)>5000: raise ValueError('Не более 5000 строк в проекте')
    pallets=[]; loose=[]; issues=[]; remaining={}; rowmap={}; reasons={};tails={};capacities={}
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
            c=resolve_catalog_product(r,catalog); typ=c.get('type')
            if typ not in ('single','double','split','loose'): raise ValueError('Укажите тип паллетирования')
            if c.get('needsReview'): raise ValueError('Подтвердите толкование кратности в справочнике')
            common={'rowId':rid,'name':r['name'],'order':r.get('order',''),'invoice':r.get('invoice',''),
                    'ready':ready.isoformat(),'due':due.isoformat(),'vertical':bool(c.get('vertical'))}
            placement = c.get('fixation')
            if typ == 'loose' and not placement:
                if not pallet_limiter_model(c.get('name')):
                    raise ValueError('Для груза без места укажите способ размещения / крепления')
                placement = LIMITER_PLACEMENT
            cap=pallet_capacity(c)
            if len(pallets)+len(loose)+left//cap>20000:raise ValueError('Расчёт превышает 20 000 паллет; разделите проект')
            chunks=list(whole_pallets(r,left,cap));tails[rid]=left%cap;capacities[rid]=cap
            for chunk in chunks:
                item=dict(common,**{'capacity':cap,'partial':False,'type':'double' if typ=='double' else 'single'})
                item.update(chunk)
                if typ=='loose':item.update(type='loose',fixation=placement);loose.append(item)
                else:pallets.append(item)
        except ValueError as e:
            issues.append({'rowId':rid,'name':r.get('name',''),'reason':str(e)})
            reasons[rid]=str(e)
    trucks=allocate_trucks(pallets,loose,start,horizon,daily_used,strategy)
    schedule=fuller_schedule(pallets,start,horizon,daily_used,trucks)
    if schedule:
        alternative=allocate_trucks(pallets,loose,start,horizon,daily_used,strategy,schedule)
        if (len(alternative)>len(trucks)
                and sum(len(t['pallets']) for t in alternative)>=sum(len(t['pallets']) for t in trucks)):
            trucks=alternative
    for truck in trucks:
        for pallet in truck['pallets']+truck['loose']:
            remaining[pallet['rowId']]-=pallet['qty']
    leftovers=[]
    for rid,left in remaining.items():
        if left:
            r=rowmap[rid]
            tail=tails.get(rid,0)
            if tail:
                cap=capacities[rid]
                leftovers.append(dict(rowId=rid,name=r.get('name',''),qty=tail,order=r.get('order',''),invoice=r.get('invoice',''),
                    reason=f'Остаток не кратен паллете: {tail} шт. при кратности {cap} шт. Не хватает {cap-tail} шт. до целой паллеты; неполные паллеты не отгружаются.'))
                left-=tail
            if not left:continue
            reason=reasons.get(rid)
            if not reason:
                if r.get('ready','')>(start+timedelta(days=horizon-1)).isoformat(): reason='Готовность за горизонтом расчёта'
                else: reason='Не хватает совместимых паллет для полной машины по одной из 12 схем; нужен перенос / догруз'
            leftovers.append(dict(rowId=rid,name=r.get('name',''),qty=left,order=r.get('order',''),invoice=r.get('invoice',''),reason=reason))
    return {'trucks':trucks,'leftovers':leftovers,'issues':issues,'strategy':strategy,'start':start.isoformat(),
            'horizon':horizon,'wholePalletsOnly':True,'notice':'Отгрузка только целыми паллетами. Заказ можно разделять между датами на границах паллет. План расстановки по габаритам: вес, высота, устойчивость и крепление проверяются перед отгрузкой. Рабочие дни: пн–пт, без календаря праздников.'}
