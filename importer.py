"""Workbook readers retain provenance and never infer actual shipment from planned columns."""
from datetime import date, datetime
from io import BytesIO
import re, zipfile
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.styles.colors import COLOR_INDEX

def workbook(raw):
    if len(raw)>15*1024*1024: raise ValueError('Файл больше 15 МБ')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist())>80*1024*1024: raise ValueError('Слишком большой распакованный Excel')
        return load_workbook(BytesIO(raw),data_only=True)
    except (zipfile.BadZipFile,KeyError,OSError): raise ValueError('Нужен корректный файл .xlsx')

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

def catalog_import(raw):
    w=workbook(raw); s=w.active; result=[]
    for row in s.iter_rows(min_row=3):
        if len(row)<4 or not row[2].value: continue
        marker=str(row[0].value or '').replace(',','.').strip().rstrip('.')
        if marker not in ('0','0.5','1'):continue
        name=str(row[2].value).strip(); amount=str(row[3].value or '').strip()
        typ='loose' if marker=='0' else 'single' if marker=='0.5' else 'split' if red(row[0]) else 'double'
        try:cap=int(amount); review=cap<=0 and typ!='loose'
        except ValueError:cap=0;review=typ!='loose'
        result.append(dict(name=name,type=typ,capacity=cap,vertical=str(row[1].value or '').strip().upper()=='ВП',
                           original=amount,needsReview=review,fixation='',source=f'{s.title}!C{row[2].row}:D{row[2].row}'))
    if not result:raise ValueError('Не найден справочник Тула: ожидаются маркер A, ВП B, наименование C, кратность D')
    return {'catalog':result,'warnings':['Сложные кратности требуют ручного подтверждения. Для красной 1 введите количество на сдвоенный паллет; программа разделит его пополам.']}

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
    p=w.create_sheet('Расстановка');p.append(['Машина','Место','Заказ','Счёт','Наименование','Количество','Тип','ВП','Готовность','Отгрузить до','Крепление'])
    for t in plan['trucks']:
        l=t['layout'];s.append([t['id'],t['date'],t['slot'],l['id'],l['doubles'],l['singles'],l['length'],t['secondReason']])
        for i,r in enumerate(t['pallets']+t['loose'],1):p.append([t['id'],i,r['order'],r['invoice'],r['name'],r['qty'],r['type'],'ВП' if r['vertical'] else '',r['ready'],r['due'],r.get('fixation','')])
    r=w.create_sheet('Остаток');r.append(['Заказ','Счёт','Наименование','Количество','Причина'])
    for x in plan['leftovers']:r.append([x['order'],x['invoice'],x['name'],x['qty'],x['reason']])
    n=w.create_sheet('Примечания');n.append(['Условия расчёта']);n.append([plan['notice']]);n.append(['Горизонт, дней',plan['horizon']]);n.append(['Критерий полной машины: строго одна из 12 утверждённых укладок.'])
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
