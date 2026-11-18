"""Separate, strict Excel channels: initial ledger, new order, readiness response."""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from io import BytesIO
import re
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill,Alignment
from openpyxl.worksheet.table import Table,TableStyleInfo
from importer import workbook,parse_date,catalog_import
from planner import number
from workflow import canonical_order,clean,key,uid,validate_row,today

ORDER_HEADERS=['№ заказа Комуса','№ позиции заказа','Номер счета','Артикул Комуса','Артикул Промет','Наименование','Заказано, шт.','Отгрузить до','Комментарий']
REPLY_HEADERS=['ID строки','№ заказа Комуса','№ позиции заказа','Номер счета','Артикул Комуса','Артикул Промет','Наименование','№ части','Подтверждено, шт.','Дата подтвержденная','Дата ответа','Отгружено на дату запроса, шт.']

def header(v):return re.sub(r'\s+',' ',clean(v).lower().replace('ё','е').rstrip(':'))
def text(v):return str(int(v)) if isinstance(v,float) and v.is_integer() else clean(v)
def strict_date(v,year,label,required=True):
    result=parse_date(v,year)
    if required and not result:raise ValueError('Укажите корректную дату: '+label)
    if v not in (None,'') and not result:raise ValueError('Некорректная дата: '+label)
    return result

def sheet_records(raw,sheet,required,year):
    w=workbook(raw)
    if not sheet:return {'sheets':[{'name':s.title,'rows':s.max_row} for s in w]}
    if sheet not in w.sheetnames:raise ValueError('Лист не найден')
    if any(x in sheet.lower() for x in ('отгруженные','архив')):raise ValueError('Архив отгруженного не подходит для загрузки текущих заказов')
    s=w[sheet];columns=None;header_row=0
    for rr in s.iter_rows(min_row=1,max_row=min(25,s.max_row)):
        names=[header(c.value) for c in rr]
        if all(header(x) in names for x in required):columns={v:i for i,v in enumerate(names) if v};header_row=rr[0].row;break
    if columns is None:raise ValueError('Не найдены заголовки шаблона: '+', '.join(required))
    if s.max_row-header_row>10000:raise ValueError('В файле слишком много строк')
    records=[]
    for rr in s.iter_rows(min_row=header_row+1):
        # Only declared source headers, not user formulas/check columns, define a data row.
        values={h:rr[i].value if i<len(rr) else None for h,i in columns.items()}
        if not any(v not in (None,'') for v in values.values()):continue
        records.append((rr[0].row,values))
    return {'records':records,'sheet':s.title,'year':year,'columns':columns}

def parse_initial(raw,sheet,year,mode):
    if not sheet:return sheet_records(raw,None,[],year)
    if mode not in ('full','remaining'):raise ValueError('Выберите: исходные количества или чистые остатки')
    w=workbook(raw);s=w[sheet]
    if s.max_row>10000:raise ValueError('В файле слишком много строк')
    columns=None;hr=0
    for rr in s.iter_rows(min_row=1,max_row=min(25,s.max_row)):
        names=[header(c.value) for c in rr]
        if 'наименование' in names and any(x in names for x in ('кол.','кол','количество','заказано, шт.')):
            columns={v:i for i,v in enumerate(names) if v};hr=rr[0].row;break
    if columns is None:raise ValueError('Не найдена таблица продукции с заголовками «Наименование», «Кол.»')
    if any(x in sheet.lower() for x in ('отгруженные','архив')):raise ValueError('Выберите актуальный лист, а не архив')
    aliases={'order':['№ заказа комуса','№ заказа','заказ'],'position':['№ позиции заказа','позиция'],
       'invoice':['номер счета','счет'],'name':['наименование'],'qty':['кол.','кол','количество','заказано, шт.'],
       'shipped':['уже отгружено','отгружено','уже отгружено, шт.'],'ready':['дата подтвержденная','готовность'],
       'due':['отгрузить до'],'sku':['артикул промет'],'komusSku':['артикул комуса']}
    idx={k:next((columns[x] for x in al if x in columns),None) for k,al in aliases.items()}
    rows=[];errors=[];order='';invoice='';keys=set()
    for rr in s.iter_rows(min_row=hr+1):
        def v(k):i=idx[k];return rr[i].value if i is not None and i<len(rr) else None
        if v('order') is not None:
            incoming_order=canonical_order(text(v('order')))
            if incoming_order!=order:invoice=''
            order=incoming_order
        if v('invoice') is not None:invoice=text(v('invoice'))
        if not v('name'):continue
        try:
            q=number(v('qty'),'Количество'); shipped=0 if mode=='remaining' else number(v('shipped') or 0,'Отгружено')
            if q==0:continue
            position=text(v('position')) or 'И-'+str(rr[0].row)
            r={'id':uid(),'order':order,'position':position,'invoice':invoice,'name':text(v('name')),
               'sku':text(v('sku')),'komusSku':text(v('komusSku')),'qty':q,'shipped':shipped,
               'due':strict_date(v('due'),year,'Отгрузить до',False),'lots':[],
               'internalPosition':v('position') in (None,''),'source':f'{sheet}!{rr[0].row}','replyAsOf':''}
            ready=strict_date(v('ready'),year,'Готовность',False)
            if q<shipped:raise ValueError('Отгружено больше количества')
            if ready and q>shipped:r['lots']=[{'part':'1','qty':q-shipped,'ready':ready,'asOf':'','source':'initial'}]
            if not order:raise ValueError('Нет номера заказа: заполните его в исходном файле')
            if key(order,position) in keys:raise ValueError('Повтор номера позиции внутри заказа')
            keys.add(key(order,position));validate_row(r);rows.append(r)
        except ValueError as e:errors.append({'line':rr[0].row,'reason':str(e),'name':text(v('name'))})
    return {'rows':rows,'errors':errors,'count':len(rows),'warnings':[
        'Сохранится только выбранный актуальный лист. История старых рейсов не импортируется как новые заказы.',
        'Зелёная заливка и плановые колонки не считаются фактом. После загрузки проверьте остатки и подтвердите сверку.',
        'Если в исходнике нет номеров позиций, назначены постоянные внутренние номера И-...; они попадут в шаблон ответа.']}

def parse_order(raw,sheet,year):
    result=sheet_records(raw,sheet,ORDER_HEADERS,year)
    if 'sheets' in result:return result
    rows=[];errors=[];seen=set();orders=set()
    for line,v in result['records']:
        g=lambda h:v.get(header(h))
        # Ignore a preformatted blank template row.
        if not any(g(x) not in (None,'') for x in ORDER_HEADERS):continue
        try:
            o=canonical_order(text(g('№ заказа Комуса')));pos=text(g('№ позиции заказа'))
            r={'id':uid(),'order':o,'position':pos,'invoice':text(g('Номер счета')),'komusSku':text(g('Артикул Комуса')),
               'sku':text(g('Артикул Промет')),'name':text(g('Наименование')),'qty':number(g('Заказано, шт.'),'Заказано',False),
               'shipped':0,'due':strict_date(g('Отгрузить до'),year,'Отгрузить до'),'lots':[],'replyAsOf':'',
               'source':f'{sheet}!{line}','comment':text(g('Комментарий'))}
            validate_row(r);k=key(o,pos)
            if k in seen:raise ValueError('Повтор номера позиции в заказе')
            seen.add(k);orders.add(o);rows.append(r)
        except ValueError as e:errors.append({'line':line,'name':text(g('Наименование')),'reason':str(e)})
    if len(orders)>1:errors.append({'line':'','reason':'В одном шаблоне должен быть один новый заказ Комуса'})
    return {'rows':rows,'errors':errors,'count':len(rows),'warnings':['Заказ добавится к общему журналу. Готовность по нему появится после отдельного ответа Донцовой.']}

def parse_response(raw,sheet,year,state):
    result=sheet_records(raw,sheet,REPLY_HEADERS,year)
    if 'sheets' in result:return result
    byid={r['id']:r for r in state['rows']};bykey=defaultdict(list)
    for r in state['rows']:bykey[key(r.get('order'),r.get('position'))].append(r)
    groups=defaultdict(list);errors=[]
    for line,v in result['records']:
        g=lambda h:v.get(header(h))
        # Populated copied identifiers with no answer are intentionally left untouched.
        if all(g(x) in (None,'') for x in ['Подтверждено, шт.','Дата подтвержденная','Дата ответа']):continue
        try:
            oid=canonical_order(text(g('№ заказа Комуса')));pos=text(g('№ позиции заказа'));rid=text(g('ID строки'))
            if not oid or not pos:raise ValueError('Нет номера заказа или позиции')
            if rid:r=byid.get(rid)
            else:
                candidates=bykey[key(oid,pos)];r=candidates[0] if len(candidates)==1 else None
            if not r:raise ValueError('Нет однозначной позиции исходного заказа')
            if key(oid,pos)!=key(r['order'],r['position']):raise ValueError('ID относится к другому заказу или позиции')
            for field,label in [('invoice','Номер счета'),('komusSku','Артикул Комуса'),('sku','Артикул Промет'),('name','Наименование')]:
                expected=clean(r.get(field));incoming=text(g(label))
                if expected and incoming!=expected:raise ValueError('Не совпадает '+label)
            baseline=number(g('Отгружено на дату запроса, шт.'),'Отгружено на дату запроса')
            if baseline!=r['shipped']:raise ValueError('После формирования запроса была отгрузка. Скачайте новый шаблон ответа по заказу.')
            part=text(g('№ части')) or '1'
            qty=number(g('Подтверждено, шт.'),'Подтверждено')
            ready=strict_date(g('Дата подтвержденная'),year,'Готовность',qty>0)
            asof=strict_date(g('Дата ответа'),year,'Дата ответа')
            if asof>today():raise ValueError('Дата ответа не может быть в будущем')
            if asof<r.get('replyAsOf',''):raise ValueError('Ответ старее уже сохранённого')
            if qty==0 and ready:raise ValueError('При нуле оставьте дату готовности пустой')
            groups[r['id']].append({'line':line,'part':part,'qty':qty,'ready':ready,'asOf':asof,'baseline':baseline})
        except ValueError as e:errors.append({'line':line,'name':text(g('Наименование')),'reason':str(e)})
    changes=[]
    for rid,parts in groups.items():
        r=byid[rid]
        if len({p['part'] for p in parts})!=len(parts):errors.append({'line':parts[0]['line'],'reason':'Повтор номера части по одной позиции заказа'});continue
        if len({p['asOf'] for p in parts})!=1:errors.append({'line':parts[0]['line'],'reason':'Разные даты ответа для одной позиции. Оставьте только актуальную разбивку.'});continue
        lots=[{k:p[k] for k in ('part','qty','ready','asOf')} for p in parts if p['qty']>0]
        try:validate_row(dict(r,lots=lots))
        except ValueError as e:errors.append({'line':parts[0]['line'],'reason':str(e)});continue
        changes.append({'rowId':rid,'order':r['order'],'position':r['position'],'name':r['name'],'lots':lots,
             'asOf':parts[0]['asOf'],'shippedAtRequest':parts[0]['baseline']})
    return {'changes':changes,'errors':errors,'count':len(changes),'warnings':[
        'По найденным позициям готовность заменится актуальной разбивкой. Заказанное количество и срок клиента сохранятся.',
        'Позиции без заполненного ответа не изменятся. Неподтверждённый остаток не включается в машины.',
        'Количество указывается в штуках товара. В машины попадут только целые паллеты по справочнику кратности.']}

def template_file(kind,rows=None):
    w=Workbook();s=w.active;s.title='Новый заказ' if kind=='order' else 'Ответ Донцовой'
    headers=ORDER_HEADERS if kind=='order' else REPLY_HEADERS
    if kind=='order':
        s.append(['ПРОМЕТ. Новый заказ Комуса'])
        s.append(['Один файл — один новый заказ. Заполните все позиции. Количество — полный объём заказа.'])
        s.append(['Даты: ДД.ММ.ГГГГ. Заказы с одинаковым товаром учитываются отдельно.'])
    else:
        s.append(['','ПРОМЕТ. Подтверждение готовности по заказу'])
        s.append(['','Реквизиты заполнит приложение. Донцова заполняет готовое количество товара в штуках и даты.'])
        s.append(['','Одна дата готовности — часть 1. Для разных дат скопируйте строку целиком и укажите части 1, 2… Отгрузка только целыми паллетами.'])
    s.append(headers)
    if kind=='response' and rows:
        for r in rows:
            if r['qty']<=r['shipped']:continue
            s.append([r['id'],r['order'],r['position'],r.get('invoice',''),r.get('komusSku',''),r.get('sku',''),r['name'],'1',None,None,None,r['shipped']])
    while s.max_row<24:s.append([None]*len(headers))
    if kind=='response':
        for row in range(5,s.max_row+1):
            if s.cell(row,8).value is None:s.cell(row,8,'1')
            if s.cell(row,12).value is None:s.cell(row,12,0)
        s.column_dimensions['A'].hidden=True;s.column_dimensions['L'].hidden=True
    first=1 if kind=='order' else 2;last=len(headers) if kind=='order' else 11
    for row in (1,2,3):s.merge_cells(start_row=row,start_column=first,end_row=row,end_column=last)
    s.freeze_panes='D5';s.auto_filter.ref=f'A4:{s.cell(4,len(headers)).column_letter}{s.max_row}'
    for row in s:
        for c in row:
            c.font=Font(name='Arial',size=10,color='203146');c.alignment=Alignment(vertical='center',wrap_text=True)
            if isinstance(c.value,str) and c.value.startswith(('=','+','-','@')):c.data_type='s'
            if c.row>=5:c.fill=PatternFill('solid',fgColor='FFF6DA' if kind=='order' or 8<=c.column<=11 else 'EFF5FB')
            if kind=='response' and c.row>=5 and c.column in (10,11):c.number_format='dd.mm.yyyy'
            if kind=='order' and c.row>=5 and c.column==8:c.number_format='dd.mm.yyyy'
            if c.row>=5 and c.column<=(6 if kind=='order' else 7):c.number_format='@'
    for c in s[4]:c.font=Font(name='Arial',color='FFFFFF',bold=True,size=10);c.fill=PatternFill('solid',fgColor='005BA8')
    s.row_dimensions[4].height=44;s.row_dimensions[1].height=28;s.row_dimensions[2].height=32;s.row_dimensions[3].height=36
    s.cell(1,first).font=Font(name='Arial',size=15,bold=True,color='005BA8')
    for i,h in enumerate(headers,1):s.column_dimensions[s.cell(4,i).column_letter].width=40 if 'Наименование' in h or 'Комментарий' in h else 25 if 'ID' in h else 21
    if kind=='response':
        guide=w.create_sheet('Как заполнить')
        explanations=[
            ('Заполнение','Скачайте запрос из выбранного заказа в приложении: реквизиты будут заполнены автоматически. Жёлтые поля — ответ Донцовой.'),
            ('Подтверждено, шт.','Количество готового ещё неотгруженного товара в штуках, а не число паллет. Пример: на паллете 20 шт., две готовые паллеты — введите 40.'),
            ('№ части','Разбивка одной позиции по датам готовности. Если всё готово в одну дату, оставьте 1. Если 20 шт. готовы 19 ноября, ещё 20 — 20 ноября, скопируйте строку целиком и укажите части 1 и 2.'),
            ('Отгрузка по дням','Если обе паллеты готовы одновременно, достаточно одной строки с общим количеством. Программа может распределить целые паллеты этого заказа по разным машинам и датам.'),
            ('Целые паллеты','Отгрузка только кратно паллете из справочника. Остаток, которого не хватает до целой паллеты, останется с объяснением причины. Части готовности одной позиции могут объединяться после готовности всех нужных штук.'),
            ('Даты','«Дата подтвержденная» — когда товар готов на заводе. «Дата ответа» — когда подтверждена эта информация; одна дата ответа для всех частей позиции.'),
            ('Обновление','По включённой позиции укажите всю актуальную разбивку неотгруженного остатка. Она заменит предыдущую готовность. Пустое количество — нет ответа; 0 — готовности нет, дату готовности оставьте пустой.'),
            ('Служебные поля','ID строки и «Отгружено на дату запроса, шт.» заполняет приложение. Они скрыты, вручную их вводить или менять не нужно.'),
            ('Защита от старого ответа','Если после скачивания запроса была отгрузка этой позиции, скачайте свежий запрос: прежний будет отклонён, чтобы не вернуть отправленный товар в остатки.'),
            ('Загрузка','В приложении: «Ответы Донцовой» → «Загрузить ответ Донцовой». Выберите рабочий лист, проверьте и примените.'),
        ]
        guide.append(['Поле / действие','Пояснение'])
        for title,description in explanations:guide.append([title,description])
        guide.column_dimensions['A'].width=28;guide.column_dimensions['B'].width=100
        for row in guide:
            guide.row_dimensions[row[0].row].height=58
            for c in row:c.font=Font(name='Arial',size=11,color='203146');c.alignment=Alignment(wrap_text=True,vertical='center')
        for c in guide[1]:c.fill=PatternFill('solid',fgColor='005BA8');c.font=Font(name='Arial',size=11,bold=True,color='FFFFFF')
    b=BytesIO();w.save(b);return b.getvalue()
