"""Workbook readers retain provenance and never infer actual shipment from planned columns."""
from datetime import date, datetime
from io import BytesIO
import re, zipfile
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.styles.colors import COLOR_INDEX
from planner import (LIMITER_PLACEMENT, apply_two_pallet_notation, article_key, number,
                     pallet_capacity, pallet_limiter_model, resolve_catalog_product,
                     normalize, without_komus_prefix)

PALLET_HEADERS = ('Наименование товара', 'Количество')
PALLET_RESULT_COLUMNS = (
    ('line', 'Строка Excel'),
    ('order', '№ заказа Комуса'),
    ('name', 'Наименование товара'),
    ('komusSku', 'Артикул Комуса'),
    ('sku', 'Артикул ПРОМЕТ'),
    ('warehouse', 'Площадка'),
    ('quantity', 'Количество, шт.'),
    ('capacity', 'Шт. в паллете'),
    ('redFull', 'Красная 1'),
    ('blackFull', 'Чёрная 1'),
    ('redHalf', 'Красная 0,5'),
    ('remainder', 'Остаток, шт.'),
    ('note', 'Примечание'),
)

def column_title(value):
    return ' '.join(str(value or '').casefold().replace('ё', 'е').split()).rstrip(':')

def article_columns(titles):
    aliases = {'артикул комуса': 'komusSku', 'артикул комус': 'komusSku', 'артикул промет': 'sku'}
    return {aliases[title]: i for i, title in enumerate(titles) if title in aliases}

def moscow_model(value):
    name = without_komus_prefix(value).upper()
    match = re.search(r'\bKEY[ .-]?(\d+)(?![\w/-])', name)
    if match and re.match(r'(КЛЮЧНИЦА|ШКАФ ДЛЯ КЛЮЧЕЙ)\b', name):
        return 'KEY', match[1]
    if name.startswith('СЕЙФ '):
        name = name.translate(str.maketrans('АВСЕНКМОРТХ–—', 'ABCEHKMOPTX--'))
        match = re.search(r'\b(FRS|ASM|ASK|ASD|SH)[ .-]?\d+[A-Z]*(?:/\d+)?(?:[ .-]?(?:EL|KL|CL|TEL))?(?![\w/-])', name)
        if match:
            return 'SAFE', normalize(match[0])
    return None

def workbook(raw):
    if len(raw)>15*1024*1024: raise ValueError('Файл больше 15 МБ')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist())>80*1024*1024: raise ValueError('Слишком большой распакованный Excel')
        return load_workbook(BytesIO(raw),data_only=True)
    except (zipfile.BadZipFile,KeyError,OSError): raise ValueError('Нужен корректный файл .xlsx')

def pallet_template():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Паллеты'
    sheet.append(PALLET_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(color='FFFFFF', bold=True)
        cell.fill = PatternFill('solid', fgColor='005BA8')
    sheet.column_dimensions['A'].width = 68
    sheet.column_dimensions['B'].width = 18
    sheet.freeze_panes = 'A2'
    output = BytesIO()
    book.save(output)
    return output.getvalue()

def calculate_pallets(raw, catalog, moscow=()):
    if not catalog and not moscow:
        raise ValueError('Сначала загрузите справочник во вкладке «Кратность Тула и Москва».')
    products = [dict(item, warehouse='Тула') for item in catalog]
    products.extend(dict(item, warehouse='МСК') for item in moscow)
    book = workbook(raw)
    for sheet in book:
        for line, cells in enumerate(sheet.iter_rows(max_row=min(25, sheet.max_row), values_only=True), 1):
            titles = [column_title(value) for value in cells]
            name_col = next((i for i, title in enumerate(titles)
                             if title in ('наименование товара', 'наименование')), None)
            qty_col = next((i for i, title in enumerate(titles)
                            if title in ('количество', 'количество, шт.', 'кол.', 'кол-во', 'кол-во, шт.', 'заказано, шт.')), None)
            if name_col is not None and qty_col is not None:
                order_col = titles.index('№ заказа комуса') if '№ заказа комуса' in titles else None
                return calculate_pallet_rows(sheet, line, name_col, qty_col, products, article_columns(titles), order_col)
    raise ValueError('Загрузите шаблон заказа Комуса или Excel с колонками «Наименование товара» и «Количество».')

def calculate_pallet_rows(sheet, header_line, name_col, qty_col, catalog, articles, order_col):
    if sheet.max_row - header_line > 5000:
        raise ValueError('В одном расчёте допускается до 5 000 строк.')
    rows = []
    has_moscow = any(item['warehouse'] == 'МСК' for item in catalog)
    for line, cells in enumerate(sheet.iter_rows(min_row=header_line + 1, values_only=True), header_line + 1):
        name = str(cells[name_col] or '').strip()
        quantity = cells[qty_col]
        product = {field: article_key(cells[col]) for field, col in articles.items()}
        if not name and quantity in (None, '') and not any(product.values()):
            continue
        order = article_key(cells[order_col]) if order_col is not None else ''
        result = {'line': line, 'name': name, 'order': order, 'quantity': str(quantity) if quantity is not None else ''}
        result.update(product)
        rows.append(result)
        try:
            if not name and not any(product.values()):
                raise ValueError('Укажите наименование товара.')
            result['quantity'] = number(quantity, 'Количество', allow_zero=False)
            product['name'] = name
            item = dict(resolve_catalog_product(product, catalog, 'Тула и Москва' if has_moscow else 'Тула',
                                                moscow_model if has_moscow else None))
            result.update(warehouse=item['warehouse'], name=name or item['name'])
            for field in ('komusSku', 'sku'):
                result[field] = product.get(field) or article_key(item.get(field))
            if item['warehouse'] == 'МСК':
                result['note'] = 'Позиция Москвы'
                continue
            apply_two_pallet_notation(item)
            if item.get('needsReview'):
                raise ValueError('Проверьте кратность товара в справочнике Тула.')
            kind = item.get('type')
            if kind == 'loose':
                result.update(redFull=0, blackFull=0, redHalf=0, remainder=0, note='Без отдельной паллеты')
                continue
            if kind not in ('split', 'double', 'single'):
                raise ValueError('Проверьте тип паллеты в справочнике Тула.')
            capacity = pallet_capacity(item)
            pallets, remainder = divmod(result['quantity'], capacity)
            red_full, red_half = divmod(pallets, 2) if kind == 'split' else (0, 0)
            result.update(capacity=capacity, redFull=red_full, blackFull=pallets if kind == 'double' else 0,
                          redHalf=pallets if kind == 'single' else red_half, remainder=remainder)
        except ValueError as error:
            result['error'] = str(error)
    if not rows:
        raise ValueError('В файле нет заполненных строк.')
    return {'sheet': sheet.title, 'columns': PALLET_RESULT_COLUMNS, 'rows': rows}

def export_pallets(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Для выгрузки нужно от 1 до 5 000 строк расчёта.')
    book = Workbook()
    sheet = book.active
    sheet.title = 'Паллеты'
    sheet.append([title for _, title in PALLET_RESULT_COLUMNS])
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Неверный формат строки расчёта.')
        values = [row.get(key) for key, _ in PALLET_RESULT_COLUMNS]
        values[-1] = row.get('error') or values[-1]
        if any(value is not None and type(value) not in (str, int) for value in values):
            raise ValueError('Неверный формат значения в строке расчёта.')
        sheet.append(values)
    return finish_export(book)

def red(cell):
    c=cell.font.color
    if c is None: return False
    v=c.rgb if c.type=='rgb' else COLOR_INDEX[c.indexed] if c.type=='indexed' and c.indexed<len(COLOR_INDEX) else ''
    return bool(v and str(v)[-6:].upper() in ('FF0000','F00000'))

def parse_date(value,year):
    if isinstance(value,(datetime,date)): return value.date().isoformat() if isinstance(value,datetime) else value.isoformat()
    v=str(value or '').strip()
    if not v: return ''
    try: return date.fromisoformat(v[:10]).isoformat()
    except ValueError: pass
    m=re.fullmatch(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\.?',v)
    if m:
        y=int(m[3]) if m[3] else year
        if y<100:y+=2000
        try:return date(y,int(m[2]),int(m[1])).isoformat()
        except ValueError: pass
    return ''

def read_moscow_catalog(book, standalone=False):
    for sheet in book:
        for line, values in enumerate(sheet.iter_rows(max_row=min(25, sheet.max_row), values_only=True), 1):
            titles = [column_title(value) for value in values]
            region = next((i for i, title in enumerate(titles) if title in ('москва', 'мск')), None)
            if region is not None and region + 1 < len(values):
                headings = next(sheet.iter_rows(min_row=line + 1, max_row=line + 1, values_only=True))
                titles = [column_title(value) if i >= region else '' for i, value in enumerate(headings)]
                name_col, qty_col = region, region + 1
                line += 1
            elif standalone:
                name_col = next((i for i, title in enumerate(titles)
                                 if title in ('наименование товара', 'наименование', 'наименование промет')), None)
                qty_col = next((i for i, title in enumerate(titles)
                                if title in ('кратность', 'крат-ность', 'кратность, шт.')), None)
                if name_col is None or qty_col is None:
                    continue
            else:
                continue
            if sheet.max_row - line > 5000:
                raise ValueError('В справочнике Москвы допускается до 5 000 строк.')
            articles = article_columns(titles)
            rows = []
            for cells in sheet.iter_rows(min_row=line + 1, values_only=True):
                name = str(cells[name_col] or '').strip()
                if not name:
                    continue
                amount = cells[qty_col]
                row = {'name': name, 'original': str(amount).strip() if amount is not None else ''}
                row.update({field: article_key(cells[col]) for field, col in articles.items()
                            if article_key(cells[col])})
                rows.append(row)
            return rows
    return []


def moscow_catalog_import(raw):
    catalog = read_moscow_catalog(workbook(raw), standalone=True)
    if not catalog:
        raise ValueError('Не найден справочник Москвы. Загрузите книгу с блоком «МОСКВА» или колонками «Наименование» и «Кратность».')
    return {'moscowCatalog': catalog, 'count': len(catalog), 'warnings': [], 'errors': []}


def catalog_import(raw):
    w=workbook(raw); s=w.active; result=[]
    tula_articles={}
    for cells in s.iter_rows(max_row=2):
        for cell in cells:
            title=' '.join(str(cell.value or '').lower().split()).rstrip(':').strip()
            if title=='артикул комуса' or title=='артикул комус':
                tula_articles.setdefault('komusSku',cell.column-1)
            elif title=='артикул промет':
                tula_articles.setdefault('sku',cell.column-1)
    for row in s.iter_rows(min_row=3):
        if len(row)<4 or not row[2].value: continue
        marker_value = row[0].value
        marker = str(marker_value if marker_value is not None else '').replace(',','.').strip().rstrip('.')
        if marker == '0.0':
            marker = '0'
        if marker not in ('0','0.5','1'):continue
        name=str(row[2].value).strip(); amount=str(row[3].value or '').strip()
        typ='loose' if marker=='0' else 'single' if marker=='0.5' else 'split' if red(row[0]) else 'double'
        try:cap=int(amount); review=cap<=0
        except ValueError:cap=0;review=True
        item=dict(name=name,type=typ,capacity=cap,vertical=str(row[1].value or '').strip().upper()=='ВП',
                  original=amount,needsReview=review,
                  fixation=LIMITER_PLACEMENT if typ=='loose' and pallet_limiter_model(name) else '',
                  source=f'{s.title}!C{row[2].row}:D{row[2].row}')
        for field,column in tula_articles.items():
            article=article_key(row[column].value)
            if article:item[field]=article
        apply_two_pallet_notation(item)
        result.append(item)
    if not result:raise ValueError('Не найден справочник Тула: ожидаются маркер A, ВП B, наименование C, кратность D')
    parsed = {'catalog':result,'warnings':['Для красной 1 запись N*2п означает N шт. на одной паллете 0,5. Две паллеты — минимум при заказе; по машинам они распределяются отдельно. Остальные сложные записи требуют проверки.']}
    moscow = read_moscow_catalog(w)
    if moscow:
        parsed['moscowCatalog'] = moscow
        parsed['warnings'].append(f'В блоке «Москва» найдено позиций: {len(moscow)}.')
    return parsed

def rows_import(raw,sheet=None,year=None):
    w=workbook(raw)
    if not sheet:return {'sheets':[{'name':s.title,'rows':s.max_row} for s in w]}
    if sheet not in w.sheetnames:raise ValueError('Лист не найден')
    if any(t in sheet.lower() for t in ('отгруженные','архив')):raise ValueError('Выбран архив отгруженных заказов. Выберите актуальный лист остатков.')
    year=int(year or date.today().year)
    if not 2000<=year<=2100:raise ValueError('Год должен быть от 2000 до 2100')
    s=w[sheet]; headers=None; headerrow=0
    for rr in s.iter_rows(min_row=1,max_row=min(25,s.max_row)):
        values=[str(c.value or '').strip().lower().rstrip(':').replace('ё','е') for c in rr]
        if 'наименование' in values and any(v in ('кол.','кол','количество') for v in values):
            headers=values;headerrow=rr[0].row;break
    if not headers:raise ValueError('Не найдена строка заголовков «Наименование», «Кол.»')
    def col(*names):return next((i for i,v in enumerate(headers) if v in names),None)
    mapping={k:col(*v) for k,v in dict(name=('наименование',),qty=('кол.','кол','количество'),
        order=('№ заказа','заказ'),invoice=('номер счета','счет'),due=('отгрузить до',),
        ready=('дата подтвержденная','готовность'),shipped=('отгружено','уже отгружено')).items()}
    rows=[];warnings=[];order='';invoice=''
    for rr in s.iter_rows(min_row=headerrow+1):
        def val(k):
            i=mapping[k];return rr[i].value if i is not None and i<len(rr) else None
        if val('order') is not None:order=str(val('order')).strip()
        if val('invoice') is not None:invoice=str(val('invoice')).strip()
        if not val('name'):continue
        qty=val('qty')
        if not isinstance(qty,(int,float)) or isinstance(qty,bool):
            warnings.append(f'Строка {rr[0].row}: количество не является числом; строка не загружена');continue
        if qty<=0:continue
        rows.append(dict(id=str(rr[0].row),name=str(val('name')).strip(),qty=qty,shipped=val('shipped') or 0,
             order=order,invoice=invoice,ready=parse_date(val('ready'),year),due=parse_date(val('due'),year),source=f'{sheet}!{rr[0].row}'))
    warnings += ['Плановые столбцы «Отгрузка …», зелёная заливка и архив не вычитаются автоматически. Сверьте фактический остаток; укажите «Уже отгружено» либо загрузите чистые остатки.','Пустые даты не наследуются от соседних строк и требуют заполнения.']
    return {'rows':rows,'warnings':warnings,'reconciled':False}

def export_plan(plan):
    w=Workbook();s=w.active;s.title='Машины'
    s.append(['Машина','Дата','Рейс за день','Укладка','Сдвоенные','Одиночные','Длина мм','Основание 2 рейса'])
    p=w.create_sheet('Расстановка');p.append(['Машина','Место','Заказ','Счёт','Наименование','Количество','Тип','ВП','Готовность','Отгрузить до','Крепление','Артикул Промет','Артикул Комуса'])
    for t in plan['trucks']:
        l=t['layout'];s.append([t['id'],t['date'],t['slot'],l['id'],l['doubles'],l['singles'],l['length'],t['secondReason']])
        for i,r in enumerate(t['pallets']+t['loose'],1):
            place = 'Без отдельного места' if r['type']=='loose' else i
            p.append([t['id'],place,r['order'],r['invoice'],r['name'],r['qty'],r['type'],'ВП' if r['vertical'] else '',r['ready'],r['due'],r.get('fixation',''),r.get('sku',''),r.get('komusSku','')])
    r=w.create_sheet('Остаток');r.append(['Заказ','Счёт','Наименование','Количество','Причина'])
    for x in plan['leftovers']:r.append([x['order'],x['invoice'],x['name'],x['qty'],x['reason']])
    n=w.create_sheet('Примечания');n.append(['Условия расчёта']);n.append([plan['notice']]);n.append(['Горизонт, дней',plan['horizon']]);n.append(['Критерий полной машины: строго одна из 12 утверждённых укладок.'])
    return finish_export(w)

def finish_export(w):
    for sh in w:
        sh.freeze_panes='A2';sh.auto_filter.ref=sh.dimensions
        for c in sh[1]:c.font=Font(color='FFFFFF',bold=True);c.fill=PatternFill('solid',fgColor='005BA8')
        for column in sh.columns:
            sh.column_dimensions[column[0].column_letter].width=min(65,max(14,max(len(str(c.value or '')) for c in column)+2))
            for c in column:
                # Excel formula injection: all external strings remain literal text.
                if isinstance(c.value,str) and c.value.startswith(('=','+','-','@')):c.data_type='s'
                c.alignment=Alignment(vertical='top',wrap_text=True)
    b=BytesIO();w.save(b);return b.getvalue()
