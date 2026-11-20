import hashlib,unittest,tempfile,threading,json,urllib.request,urllib.error,http.cookiejar
from pathlib import Path
from io import BytesIO
from openpyxl import load_workbook
from openpyxl import Workbook
from unittest.mock import patch
import server,workflow as wf,ledger_store
from import_v2 import template_file

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.DATA=Path(cls.temp.name);server.PASSWORD='unittest-password';server.db().close()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start();cls.url='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
    def setUp(self):
        for target in ('workflow.today','import_v2.today','server.today'):
            p=patch(target,return_value='2026-11-17');p.start();self.addCleanup(p.stop)
        c=server.db()
        for table in ('project','state_history','uploads'):c.execute('DELETE FROM '+table)
        c.commit();c.close();server.PREVIEWS.clear()
    def client(self,login=True):
        c=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if login:self.request(c,'/api/login',{'password':server.PASSWORD})
        return c
    def request(self,c,path,data=None,headers=True,raw=False,revision=None):
        h={'X-Komus-Client':'web'} if headers else {}
        if revision is not None:h['X-Komus-Revision']=str(revision)
        if data is not None and not raw:h['Content-Type']='application/json'
        r=urllib.request.Request(self.url+path,data=data if raw else None if data is None else json.dumps(data).encode(),headers=h)
        return c.open(r)
    def test_order_preview_apply_restart_download_and_duplicate(self):
        c=self.client();initial=json.load(self.request(c,'/api/v2/state'));rev=initial['revision']
        raw=template_file('order');w=load_workbook(BytesIO(raw));s=w.active;s.append(['WEB-1','','','','A',260,wf.today(),'']);b=BytesIO();w.save(b);raw=b.getvalue()
        p=json.load(self.request(c,'/api/v2/import-preview?kind=order&sheet='+urllib.parse.quote('Новый заказ')+'&filename=new.xlsx',raw,raw=True,revision=rev))
        self.assertFalse(p['errors']);self.assertEqual(len(json.load(self.request(c,'/api/v2/state'))['state']['rows']),0)
        applied=json.load(self.request(c,'/api/v2/import-apply',{'revision':rev,'token':p['token']}));self.assertEqual(len(applied['state']['rows']),1)
        self.assertTrue(json.load(self.request(c,'/api/v2/import-preview?kind=order',raw,raw=True))['duplicate'])
        export=self.request(c,'/api/v2/templates/response?order=WEB-1').read();rw=load_workbook(BytesIO(export),data_only=True);self.assertEqual(rw.active['B5'].value,'WEB-1')
        source=self.request(c,'/api/v2/source?sha='+applied['state']['imports'][0]['sha']).read();self.assertEqual(source,raw)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/v2/reconcile',{'revision':rev})
        self.assertEqual(e.exception.code,409)
        self.request(c,'/api/logout',{});self.assertFalse(json.load(self.request(c,'/api/session'))['authenticated'])
    def test_csrf_and_private_routes(self):
        c=self.client(False)
        for path in ('/api/v2/state','/api/v2/templates/response','/api/v2/source?sha=x'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,path)
            self.assertEqual(e.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/login',{'password':server.PASSWORD},False)
        self.assertEqual(e.exception.code,403)
        for path in ('/.env','/data/komus.db','/../server.py'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,path)
            self.assertEqual(e.exception.code,404)
    def test_pallet_template_and_calculation_use_catalog_without_changing_ledger(self):
        client = self.client()
        state = server.demo()
        state['catalog'] += [
            {'name':'Шкаф ML-11-50D','type':'split','capacity':0,'original':'22*2п','needsReview':True},
            {'name':'Планка-ограничитель MS-60','type':'loose','capacity':1},
        ]
        ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        before = json.load(self.request(client,'/api/v2/state'))
        template = self.request(client,'/api/v2/templates/pallets').read()
        book = load_workbook(BytesIO(template))
        self.assertEqual(tuple(c.value for c in book.active[1]),('Наименование товара','Количество'))
        for row in (
            ['Шкаф ML-11-50D',67],
            ['Шкаф ML-11-50D',22],
            ['Демонстрационный шкаф',41],
            ['Демонстрационный сейф',31],
            ['Планка-ограничитель MS-60',3],
            ['Нет в справочнике',1],
            ['Демонстрационный сейф','ошибка'],
        ):
            book.active.append(row)
        raw = BytesIO()
        book.save(raw)
        result = json.load(self.request(client,'/api/v2/pallets',raw.getvalue(),raw=True))
        self.assertEqual(result['sheet'],'Паллеты')
        keys=('redFull','blackFull','redHalf','remainder')
        self.assertEqual([tuple(r[k] for k in keys) for r in result['rows'][:5]],
                         [(1,0,1,1),(0,0,1,0),(0,2,0,1),(0,0,3,1),(0,0,0,0)])
        self.assertEqual(result['rows'][4]['note'],'Без отдельной паллеты')
        self.assertIn('Нет однозначного совпадения',result['rows'][5]['error'])
        self.assertIn('целое число',result['rows'][6]['error'])
        self.assertEqual(json.load(self.request(client,'/api/v2/state')),before)
    def test_guide_templates_can_be_loaded_in_their_corresponding_forms(self):
        client = self.client()
        for template, kind, header_row, position in (
            ('initial','initial',1,['45001','1001','00001','P-1','Шкаф',24,0,'2026-11-19','2026-11-18']),
            ('tula','catalog',2,[1,'ВП','Шкаф',24,'00001','P-1']),
            ('moscow','moscow',1,['Сейф',10,'00002','P-2']),
            ('moscow-articles','moscow-articles',1,['Сейф','P-2','00002']),
        ):
            with self.subTest(template=template):
                book = load_workbook(BytesIO(self.request(client,f'/api/v2/templates/{template}').read()))
                for column, value in enumerate(position,1):
                    book.active.cell(header_row + 1,column,value)
                raw = BytesIO()
                book.save(raw)
                sheet = urllib.parse.quote(book.active.title)
                preview = json.load(self.request(client,f'/api/v2/import-preview?kind={kind}&sheet={sheet}&mode=full',
                                                 raw.getvalue(),raw=True))
                self.assertEqual(preview['count'],1)
                self.assertFalse(preview['errors'])
        ledger_store.mutate(server.DATA,0,lambda current:current.update(server.demo()))
        blank = load_workbook(BytesIO(self.request(client,'/api/v2/templates/response?blank=1').read()))
        self.assertIsNone(blank.active['B5'].value)
        populated = load_workbook(BytesIO(self.request(client,'/api/v2/templates/response').read()))
        self.assertEqual(populated.active['B5'].value,'ДЕМО-001')
    def test_pallet_articles_are_recovered_from_the_matching_order_and_exported(self):
        client = self.client()
        state = server.demo()
        for row, order, invoice, komus_sku, sku in (
            (state['rows'][0],'45001','1001','000712','A-1'),
            (state['rows'][2],'45002','1002','000713','A-2'),
        ):
            row.update(order=order,invoice=invoice,komusSku=komus_sku,sku=sku)
        ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        book = Workbook()
        book.active.append(['№ заказа Комуса','Номер счёта','Наименование','Количество','Артикул поставщика'])
        book.active.append(['45001','1001','Демонстрационный сейф',20,None])
        book.active.append([None,None,'Демонстрационный сейф',20,None])
        book.active.append(['45002','1002','Демонстрационный сейф',20,'A-2'])
        raw = BytesIO()
        book.save(raw)
        checked = json.load(self.request(client,'/api/v2/pallets',raw.getvalue(),raw=True))
        result = self.request(client,'/api/v2/pallets/export',{'rows':checked['rows']}).read()
        sheet = load_workbook(BytesIO(result), data_only=True).active
        headers = {cell.value:cell.column for cell in sheet[1]}
        for line, expected in enumerate([('000712','A-1'),(None,None),('000713','A-2')],2):
            self.assertEqual(tuple(sheet.cell(line,headers[name]).value
                                   for name in ('Артикул Комуса','Артикул ПРОМЕТ')),expected)
            self.assertEqual(sheet.cell(line,headers['Красная 0,5']).value,2)
    def test_moscow_catalog_import_and_order_template_pallet_check(self):
        client = self.client()
        state = server.demo()
        ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        before = json.load(self.request(client,'/api/v2/state'))
        book = Workbook()
        sheet = book.active
        sheet.append([None,None,'ТУЛА',None,None,'МОСКВА'])
        sheet.append([None,'ВП','Наименование Промет','Крат-ность',None,'Наименование Промет','Крат-ность','Артикул Комуса'])
        sheet.append([1,None,'Демонстрационный шкаф',20,None,'Ключница KEY-40',20,1234])
        raw = BytesIO()
        book.save(raw)
        preview = json.load(self.request(client,'/api/v2/import-preview?kind=moscow',raw.getvalue(),raw=True))
        applied = json.load(self.request(client,'/api/v2/import-apply',{'token':preview['token'],'revision':before['revision']}))
        self.assertEqual(applied['state']['plan'],before['state']['plan'])
        self.assertEqual(applied['state']['catalog'],before['state']['catalog'])
        self.assertEqual(applied['state']['moscowCatalog'],[{'name':'Ключница KEY-40','original':'20','komusSku':'1234'}])

        book = load_workbook(BytesIO(template_file('order')))
        book.active.append(['К-1','С-1',1234,'000045','Название из заказа',20,None,None])
        book.active.append(['К-1','С-1',None,None,'Шкаф для ключей AIKO KEY-40 на 40 ключей',20,None,None])
        book.active.append(['К-1','С-1',None,None,'Демонстрационный шкаф',40,None,None])
        raw = BytesIO()
        book.save(raw)
        checked = json.load(self.request(client,'/api/v2/pallets',raw.getvalue(),raw=True))
        first, second, third = checked['rows']
        self.assertEqual([r['warehouse'] for r in (first,second,third)],['МСК','МСК','Тула'])
        self.assertEqual(first['order'],'К-1')
        self.assertEqual(first['sku'],'000045')
        self.assertEqual(second['komusSku'],'1234')
        self.assertNotIn('blackFull',first)
        self.assertEqual(third['blackFull'],2)
        self.assertEqual(json.load(self.request(client,'/api/v2/state')), {
            'revision':applied['revision'],'state':applied['state'],'result':None})
        # Export every visible row, including a row requiring review.
        checked['rows'].append({'line':8,'name':'=1+1','komusSku':'00007','quantity':1,'error':'Нет в справочнике'})
        response = self.request(client,'/api/v2/pallets/export',{'rows':checked['rows']})
        self.assertIn('komus-pallets.xlsx',response.headers['Content-Disposition'])
        sheet = load_workbook(BytesIO(response.read()),data_only=False).active
        headers = {cell.value:cell.column for cell in sheet[1]}
        self.assertEqual(sheet.max_row,5)
        self.assertEqual(sheet.cell(2,headers['Артикул ПРОМЕТ']).value,'000045')
        self.assertEqual(sheet.cell(3,headers['Артикул Комуса']).value,'1234')
        self.assertEqual(sheet.cell(3,headers['Площадка']).value,'МСК')
        self.assertIsNone(sheet.cell(3,headers['Чёрная 1']).value)
        self.assertEqual(sheet.cell(4,headers['Площадка']).value,'Тула')
        self.assertEqual(sheet.cell(4,headers['Чёрная 1']).value,2)
        self.assertEqual(sheet.cell(4,headers['Красная 1']).value,0)
        self.assertEqual(sheet.cell(5,headers['Наименование товара']).data_type,'s')
        self.assertEqual(sheet.cell(5,headers['Примечание']).value,'Нет в справочнике')
        self.assertEqual(ledger_store.read(server.DATA)[0],applied['revision'])
    def test_moscow_article_file_enriches_catalog_and_reaches_pallet_export(self):
        client = self.client()
        state = server.demo()
        state['moscowCatalog'] = [{'name':'Ключница KEY-40','original':'20'},
                                  {'name':'Ключница KEY-60','original':'10'}]
        rev, _, _ = ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        book = Workbook()
        book.active.append(['Наименование','Артикул ПРОМЕТ','Артикул Комуса'])
        book.active.append(['Шкаф для ключей AIKO KEY-40','P-40','00040'])
        raw = BytesIO()
        book.save(raw)
        preview = json.load(self.request(client,'/api/v2/import-preview?kind=moscow-articles',raw.getvalue(),raw=True))
        self.assertFalse(preview['errors'])
        applied = json.load(self.request(client,'/api/v2/import-apply',{'token':preview['token'],'revision':rev}))
        self.assertEqual(applied['state']['plan'],state['plan'])
        self.assertEqual(applied['state']['moscowCatalog'],[
            {'name':'Ключница KEY-40','original':'20','sku':'P-40','komusSku':'00040'},
            {'name':'Ключница KEY-60','original':'10'}])

        book = Workbook()
        book.active.append(['Наименование','Количество'])
        book.active.append(['Ключница KEY-40',10])
        raw = BytesIO()
        book.save(raw)
        calculated = json.load(self.request(client,'/api/v2/pallets',raw.getvalue(),raw=True))
        exported = self.request(client,'/api/v2/pallets/export',{'rows':calculated['rows']}).read()
        sheet = load_workbook(BytesIO(exported),data_only=True).active
        self.assertEqual([sheet.cell(2,col).value for col in (4,5,6)],['00040','P-40','МСК'])
    def test_bulk_request_contains_all_open_orders_and_only_open_rows(self):
        c=self.client();state=wf.empty_state()
        def row(rid,order,shipped=0):
            return {'id':rid,'order':order,'position':rid,'invoice':'I'+order,'name':'A','sku':'S1','komusSku':'','qty':20,'shipped':shipped,'due':'2026-11-17','lots':[]}
        state['rows']=[row('r1','O1'),row('r2','O2',10),row('r3','O3',20)]
        ledger_store.mutate(server.DATA,0,lambda s:s.update(state))
        w=load_workbook(BytesIO(self.request(c,'/api/v2/templates/response').read()),data_only=True)
        values=[r for r in w.active.iter_rows(min_row=5,values_only=True) if r[1]]
        self.assertEqual([(r[1],r[6]) for r in values],[('O1',20),('O2',20)])
        filtered=load_workbook(BytesIO(self.request(c,'/api/v2/templates/response?order=O2').read()),data_only=True)
        self.assertEqual(filtered.active['B5'].value,'O2');self.assertIsNone(filtered.active['B6'].value)
    def test_public_demo_and_legacy_overwrite_blocked(self):
        c=self.client(False);d=json.load(self.request(c,'/api/v2/demo'))['state'];self.assertTrue(d['plan']['trucks']);self.assertTrue(all(r['order'].startswith('ДЕМО') for r in d['rows']))
        c=self.client()
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/project',{'project':{'rows':[]},'revision':0})
        self.assertEqual(e.exception.code,410)
    def test_old_cached_plan_cannot_be_exported(self):
        c=self.client();old=server.demo();old['plan'].pop('wholePalletsOnly',None)
        ledger_store.mutate(server.DATA,0,lambda s:s.update(old))
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/v2/export')
        self.assertEqual(e.exception.code,400)
    def test_build_updates_saved_two_pallet_entries_and_splits_order_between_days(self):
        client=self.client()
        initial=wf.empty_state()
        initial['reconciled']=True
        rack='Стеллаж Cobalt Shelf MS 180KD/100x50/4'
        cabinet='Шкаф для сумок усиленный Комус Мебель ML-24-60'
        initial['catalog']=[
            {'name':rack,'type':'split','original':'25*2п','needsReview':True,'capacity':0},
            {'name':cabinet,'type':'split','original':'20*2п','needsReview':True,'capacity':0},
            {'name':'Другой товар','type':'single','original':'1','capacity':1},
        ]
        def ready_row(rid,name,qty,ready,due):
            return {'id':rid,'order':rid,'position':'1','name':name,'qty':qty,
                    'shipped':0,'due':due,'lots':[{'part':'1','qty':qty,'ready':ready}]}
        first,second='2026-11-17','2026-11-18'
        initial['rows']=[
            ready_row('rack',rack,50,first,second),
            ready_row('fill1','Другой товар',25,first,first),
            ready_row('fill2','Другой товар',25,second,first),
        ]
        rev,_,_=ledger_store.mutate(server.DATA,0,lambda s:s.update(initial))
        result=json.load(self.request(client,'/api/v2/build',{'revision':rev,'start':first}))
        state=result['state']
        self.assertEqual(state['catalog'][0]['capacity'],50)
        self.assertEqual((state['catalog'][1]['original'],state['catalog'][1]['capacity']),('18*2п',36))
        trucks=state['plan']['trucks']
        self.assertEqual([t['date'] for t in trucks],[first,second])
        for truck in trucks:
            self.assertEqual([(p['type'],p['qty']) for p in truck['pallets'] if p['rowId']=='rack'],[('single',25)])
    def test_build_recovers_previously_skipped_limiters_and_exports_without_places(self):
        client=self.client()
        initial=server.demo()
        initial['catalog'][0]['capacity']=20  # A previous catalogue edit must survive.
        initial['rows'].append(dict(initial['rows'][0],id='rail',position='40',
            name='Планка-ограничитель Практик MS-30 (20шт/упак)',qty=10,
            lots=[{'part':'1','qty':10,'ready':wf.today()}]))

        book=Workbook()
        book.active.append(['','','ТУЛА'])
        book.active.append(['','ВП','Наименование','Кратность'])
        book.active.append(['0.5','','Демонстрационный сейф',10])
        book.active.append([0,'','Планка огр. MS-30 (20шт)',5])
        book.active.append([0,'','Планка огр. MS-60 (20шт)',5])
        buf=BytesIO()
        book.save(buf)
        raw=buf.getvalue()
        meta={'kind':'catalog','sha':hashlib.sha256(raw).hexdigest(),'filename':'catalog.xlsx','at':wf.now()}
        initial['imports'].append(meta)
        rev,_,_=ledger_store.mutate(server.DATA,0,lambda s:s.update(initial),upload=(meta,raw))

        result=json.load(self.request(client,'/api/v2/build',{'revision':rev}))
        result=json.load(self.request(client,'/api/v2/build',{'revision':result['revision']}))
        state=result['state']
        self.assertEqual(len(state['catalog']),4)
        self.assertEqual(state['catalog'][0]['capacity'],20)
        truck=state['plan']['trucks'][0]
        self.assertEqual(len(truck['pallets']),26)
        self.assertEqual(sum(item['qty'] for item in truck['loose']),10)
        self.assertTrue(all(item['fixation']=='Сверху на одной из паллет машины' for item in truck['loose']))

        exported=load_workbook(BytesIO(self.request(client,'/api/v2/export').read()),data_only=True)
        places=[row[1] for row in exported['Расстановка'].values if row[6]=='loose']
        self.assertEqual(places,['Без отдельного места']*2)
        result=json.load(self.request(client,'/api/v2/dispatch',{
            'revision':result['revision'],'planId':state['plan']['id'],'truckId':truck['id'],'actualDate':wf.today()}))
        self.assertEqual(next(row['shipped'] for row in result['state']['rows'] if row['id']=='rail'),10)
    def test_transition_and_reset_persist_without_clearing_recorded_shipments(self):
        c=self.client()
        initial=server.demo()
        initial['rows'][0]['qty']=initial['rows'][0]['lots'][0]['qty']=1000
        rev,_,_=ledger_store.mutate(server.DATA,0,lambda s:s.update(initial))
        payload={'id':'demo1','operation':'reserve','quantity':120,'revision':rev}
        reserved=json.load(self.request(c,'/api/v2/row-transition',payload))
        self.assertIsNone(reserved['state']['plan'])
        payload.update(operation='ship',quantity=20,actualDate='2026-11-17',revision=reserved['revision'])
        shipped=json.load(self.request(c,'/api/v2/row-transition',payload))
        with self.assertRaises(urllib.error.HTTPError) as ex:
            self.request(c,'/api/v2/row-transition',payload)
        self.assertEqual(ex.exception.code,409)
        built=json.load(self.request(c,'/api/v2/build',{'revision':shipped['revision']}))
        plan=built['state']['plan']
        truck=plan['trucks'][0]
        sent=json.load(self.request(c,'/api/v2/dispatch',{
            'revision':built['revision'],'planId':plan['id'],'truckId':truck['id'],'actualDate':'2026-11-17'}))
        with self.assertRaises(urllib.error.HTTPError) as ex:
            self.request(c,'/api/v2/clear-plan',{'revision':sent['revision'],'planId':plan['id']})
        self.assertEqual(ex.exception.code,400)
        json.load(self.request(c,'/api/v2/clear-plan',{'revision':sent['revision'],'planId':sent['state']['plan']['id']}))
        after=json.load(self.request(c,'/api/v2/state'))['state']
        self.assertIsNone(after['plan'])
        for key in ('rows','catalog','shipments','manualShipments','reconciled'):
            self.assertEqual(after[key],sent['state'][key])
    def test_catalog_site_move_controls_whether_item_is_included_in_the_plan(self):
        client = self.client()
        state = server.demo()
        state['moscowCatalog']=[{'name':'Московская позиция','original':'10','sku':'001-P'}]
        state['rows'][0].update(name='Московская позиция',sku='001-P')
        rev, _, _ = ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        move={'source':'moscow','destination':'tula','index':0,'revision':rev,
              'item':{'name':'Московская позиция','sku':'001-P','capacity':10}}
        with self.assertRaises(urllib.error.HTTPError) as ex:
            self.request(client,'/api/v2/catalog-row',move)
        self.assertEqual(ex.exception.code,400)
        move['item']['type']='single'
        moved=json.load(self.request(client,'/api/v2/catalog-row',move))
        self.assertFalse(moved['state']['moscowCatalog'])
        built=json.load(self.request(client,'/api/v2/build',{'revision':moved['revision']}))
        self.assertTrue(any(p['rowId']=='demo1' for t in built['state']['plan']['trucks'] for p in t['pallets']))
        index=next(i for i,row in enumerate(built['state']['catalog']) if row.get('sku')=='001-P')
        restored=json.load(self.request(client,'/api/v2/catalog-row',dict(move,index=index,source='tula',destination='moscow',
                                                                          revision=built['revision'])))
        self.assertEqual(restored['state']['moscowCatalog'][0]['sku'],'001-P')
        rebuilt=json.load(self.request(client,'/api/v2/build',{'revision':restored['revision']}))
        self.assertFalse(any(p['rowId']=='demo1' for t in rebuilt['state']['plan']['trucks'] for p in t['pallets']))

    def test_leftover_actions_change_the_order_history_and_refresh_the_plan(self):
        client=self.client()
        state=server.demo()
        state['rows'][2].update(sku='P-11',komusSku='K-22')
        rev,_,_=ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        data={'id':'demo3','operation':'reserve','quantity':20,'revision':rev,'fromPlan':True}
        reserved=json.load(self.request(client,'/api/v2/row-transition',data))
        self.assertEqual(reserved['state']['rows'][2]['reserved'],20)
        self.assertTrue(any(r['rowId']=='demo3' and r['reason'].startswith('В готовой машине') for r in reserved['state']['plan']['leftovers']))
        data.update(operation='ship',quantity=20,actualDate='2026-11-17',revision=reserved['revision'])
        shipped=json.load(self.request(client,'/api/v2/row-transition',data))['state']
        self.assertEqual(shipped['rows'][2]['shipped'],20)
        self.assertEqual(shipped['rows'][2]['reserved'],0)
        self.assertEqual(shipped['manualShipments'][0]['sku'],'P-11')
        self.assertEqual(sum(r['qty'] for r in shipped['plan']['leftovers'] if r['rowId']=='demo3'),40)

    def test_urgency_and_manual_exchange_are_saved_and_dispatch_preserves_other_trucks(self):
        client=self.client()
        state=server.demo()
        first=state['rows'][0]
        first.update(qty=260,lots=[{'part':'1','qty':260,'ready':'2026-11-17'}])
        second=dict(first,id='priority',order='45002',due='2026-11-25')
        state['rows']=[first,second]
        wf.make_plan(state)
        rev,_,_=ledger_store.mutate(server.DATA,0,lambda current:current.update(state))
        marked=json.load(self.request(client,'/api/v2/row-urgency',{'id':'priority','urgent':True,'revision':rev}))
        self.assertTrue(marked['state']['rows'][1]['urgent'])
        self.assertEqual(marked['state']['plan']['id'],state['plan']['id'])
        built=json.load(self.request(client,'/api/v2/build',{'revision':marked['revision'],'strategy':'compact'}))
        plan=built['state']['plan']
        a,b=plan['trucks']
        self.assertEqual(a['pallets'][0]['rowId'],'priority')
        self.assertEqual(b['pallets'][0]['rowId'],'demo1')
        b['pallets'][0]['ready']=b['date']
        with self.assertRaisesRegex(ValueError,'не будет готова'):
            wf.exchange_pallets(built['state'],plan['id'],a['id'],0,b['id'],0)
        b['pallets'][0]['ready']=a['date']
        b['pallets'][0]['type']='double'
        with self.assertRaisesRegex(ValueError,'одного типа'):
            wf.exchange_pallets(built['state'],plan['id'],a['id'],0,b['id'],0)
        data={'revision':built['revision'],'planId':plan['id'],'sourceId':a['id'],'sourceIndex':0,
              'targetId':b['id'],'targetIndex':0}
        exchanged=json.load(self.request(client,'/api/v2/pallet-exchange',data))
        a2,b2=exchanged['state']['plan']['trucks']
        self.assertEqual((a2['layout'],b2['layout']),(a['layout'],b['layout']))
        self.assertEqual((a2['pallets'][0]['rowId'],b2['pallets'][0]['rowId']),('demo1','priority'))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(client,'/api/v2/pallet-exchange',data)
        self.assertEqual(error.exception.code,409)
        sent=json.load(self.request(client,'/api/v2/dispatch',{'revision':exchanged['revision'],'planId':plan['id'],
                       'truckId':a['id'],'actualDate':a['date']}))
        self.assertEqual(sent['state']['plan']['trucks'],[b2])

    def test_initial_yellow_import_uses_order_fields_and_ignores_schedule_header(self):
        client=self.client()
        book=Workbook()
        sheet=book.active
        sheet.append([None,None,None,'15:30'])
        sheet.append(['№ заказа','Наименование','Количество','Рейс'])
        sheet.append(['45001','Стеллаж',24,None])
        sheet.append([None,'Шкаф',10,None])
        sheet.append(['45002','Сейф',1,None])
        from openpyxl.styles import PatternFill
        for key in ('D1','A3','D5'):
            sheet[key].fill=PatternFill('solid',fgColor='FFFF00')
        raw=BytesIO()
        book.save(raw)
        result=json.load(self.request(client,'/api/v2/import-preview?kind=initial&sheet=Sheet&mode=full',
                                      raw.getvalue(),raw=True))
        self.assertFalse(result['errors'])
        self.assertEqual([bool(row.get('urgent')) for row in result['rows']],[True,True,False])

    def test_readiness_build_dispatch_and_persistent_remaining_via_http(self):
        c=self.client();rev=0
        def upload(raw,kind,sheet):
            nonlocal rev
            q='/api/v2/import-preview?kind='+kind+'&sheet='+urllib.parse.quote(sheet)+'&filename=test.xlsx'
            preview=json.load(self.request(c,q,raw,raw=True,revision=rev));self.assertFalse(preview['errors'])
            saved=json.load(self.request(c,'/api/v2/import-apply',{'revision':rev,'token':preview['token']}));rev=saved['revision'];return saved
        w=load_workbook(BytesIO(template_file('order')));w.active.append(['E2E','','','','A',440,'2026-11-17','']);b=BytesIO();w.save(b)
        upload(b.getvalue(),'order','Новый заказ')
        w=Workbook();w.active.append(['','','ТУЛА','','','МОСКВА']);w.active.append(['','ВП','Наименование','Кратность']);w.active.append(['1','','A',20]);b=BytesIO();w.save(b)
        upload(b.getvalue(),'catalog',w.active.title)
        w=load_workbook(BytesIO(self.request(c,'/api/v2/templates/response?order=E2E').read()));s=w.active
        for cell,value in {'H5':'2026-11-17','I5':'2026-11-17'}.items():s[cell]=value
        b=BytesIO();w.save(b);reply=b.getvalue();upload(reply,'response','Ответ Донцовой')
        saved=json.load(self.request(c,'/api/v2/reconcile',{'revision':rev}));rev=saved['revision']
        saved=json.load(self.request(c,'/api/v2/build',{'revision':rev,'start':'2026-11-17'}));rev=saved['revision'];plan=saved['state']['plan']
        self.assertEqual(sum(p['qty'] for t in plan['trucks'] for p in t['pallets']),440)
        truck=plan['trucks'][0];shipped=sum(p['qty'] for p in truck['pallets'])
        saved=json.load(self.request(c,'/api/v2/dispatch',{'revision':rev,'planId':plan['id'],'truckId':truck['id'],'actualDate':'2026-11-17'}))
        stored=ledger_store.read(server.DATA)[1];self.assertEqual(stored['rows'][0]['shipped'],shipped)
        self.assertEqual(sum(p['qty'] for t in stored['plan']['trucks'] for p in t['pallets'])+shipped,440);self.assertEqual(len(stored['shipments']),1)
        self.assertTrue(json.load(self.request(c,'/api/v2/import-preview?kind=response',reply,raw=True))['duplicate'])
        w.active['H5']='2026-11-18';b=BytesIO();w.save(b)
        stale=json.load(self.request(c,'/api/v2/import-preview?kind=response&sheet='+urllib.parse.quote('Ответ Донцовой'),b.getvalue(),raw=True,revision=saved['revision']))
        self.assertFalse(stale['errors'])
        updated=json.load(self.request(c,'/api/v2/import-apply',{'revision':saved['revision'],'token':stale['token']}))
        self.assertEqual(updated['state']['rows'][0]['shipped'],shipped)
        self.assertEqual(sum(l['qty'] for l in updated['state']['rows'][0]['lots']),440-shipped)
