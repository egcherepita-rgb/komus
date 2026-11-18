import unittest,tempfile,threading,json,urllib.request,urllib.error,http.cookiejar
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
