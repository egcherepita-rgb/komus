import unittest,tempfile,hashlib,json
from unittest.mock import patch
from copy import deepcopy
from datetime import timedelta,date
from io import BytesIO
from openpyxl import Workbook,load_workbook
import workflow as wf
import ledger_store
from import_v2 import ORDER_HEADERS,REPLY_HEADERS,parse_order,parse_response,parse_initial,template_file

TODAY='2026-11-17'
def xlsx(headers,rows,title='Data'):
    w=Workbook();s=w.active;s.title=title;s.append(headers)
    for row in rows:s.append(row)
    b=BytesIO();w.save(b);return b.getvalue()
def base_row(id='r1',order='O1',qty=260,shipped=0):
    return {'id':id,'order':order,'position':'10','invoice':'I'+order,'sku':'S1','komusSku':'K1','name':'A','qty':qty,'shipped':shipped,'due':TODAY,'replyAsOf':'','lots':[]}
def state():
    s=wf.empty_state();s['rows']=[base_row(),base_row('r2','O2')];s['catalog']=[{'name':'A','type':'single','capacity':10,'vertical':False}];s['reconciled']=True;return s

def response(r,qty=260,part='1',ready=TODAY,asof=TODAY,baseline=None):
    return [r['id'],r['order'],r['position'],r['invoice'],r['komusSku'],r['sku'],r['name'],part,qty,ready,asof,r['shipped'] if baseline is None else baseline]
def parsed_reply(s,rows):return parse_response(xlsx(REPLY_HEADERS,rows),'Data',2026,s)
def meta(kind,seed='1'):return {'sha':hashlib.sha256((kind+seed).encode()).hexdigest(),'filename':'file.xlsx','kind':kind,'at':wf.now()}

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        for target in ('workflow.today','import_v2.today'):
            p=patch(target,return_value=TODAY);p.start();self.addCleanup(p.stop)
    def test_same_product_in_two_orders_updates_only_target(self):
        s=state();p=parsed_reply(s,[response(s['rows'][0],100)])
        self.assertFalse(p['errors']);wf.apply_import(s,'response',p,meta('response'))
        self.assertEqual(sum(l['qty'] for l in s['rows'][0]['lots']),100);self.assertEqual(s['rows'][1]['lots'],[])
        self.assertEqual([r['qty'] for r in s['rows']],[260,260])
    def test_reply_wrong_order_or_article_or_invoice_does_not_match(self):
        for idx,value in [(1,'O2'),(3,'wrong invoice'),(4,'wrong SKU'),(5,'wrong SKU'),(6,'wrong name')]:
            s=state();row=response(s['rows'][0]);row[idx]=value
            self.assertTrue(parsed_reply(s,[row])['errors'])
    def test_reply_matches_order_position_if_id_missing(self):
        s=state();r=response(s['rows'][1]);r[0]='';p=parsed_reply(s,[r]);self.assertEqual(p['changes'][0]['rowId'],'r2')
    def test_partial_readiness_replaces_not_adds_and_keeps_unconfirmed(self):
        s=state();r=s['rows'][0]
        p=parsed_reply(s,[response(r,100,'1'),response(r,50,'2',ready=(date.fromisoformat(TODAY)+timedelta(days=3)).isoformat())]);self.assertFalse(p['errors'])
        wf.apply_import(s,'response',p,meta('response'));wf.make_plan(s)
        self.assertEqual(sum(l['qty'] for l in r['lots']),150)
        self.assertTrue(any(x['rowId']=='r1' and x['qty']==110 and 'Донцовой' in x['reason'] for x in s['plan']['leftovers']))
        second=parsed_reply(s,[response(r,70)]);wf.apply_import(s,'response',second,meta('response','2'));self.assertEqual(sum(l['qty'] for l in r['lots']),70)
    def test_repeat_file_noop_after_dispatch_and_older_answer_rejected(self):
        s=state();s['rows']=[s['rows'][0]];p=parsed_reply(s,[response(s['rows'][0])]);m=meta('response');wf.apply_import(s,'response',p,m);wf.make_plan(s)
        t=s['plan']['trucks'][0];wf.dispatch(s,s['plan']['id'],t['id'],t['date'])
        self.assertEqual(s['rows'][0]['shipped'],260);self.assertTrue(wf.apply_import(s,'response',p,m)['duplicate']);self.assertEqual(s['rows'][0]['lots'],[])
    def test_overconfirmation_duplicate_parts_blank_date_and_zero(self):
        s=state();r=s['rows'][0]
        for records in [[response(r,261)],[response(r,10,'1'),response(r,10,'1')],[response(r,10,ready='')],[response(r,10,baseline=1)]]:
            self.assertTrue(parsed_reply(s,records)['errors'])
        p=parsed_reply(s,[response(r,0,ready='')]);self.assertFalse(p['errors']);self.assertEqual(p['changes'][0]['lots'],[])
        v=response(r);v[8]=None;self.assertTrue(parsed_reply(s,[v])['errors'])
    def test_stale_template_after_shipment(self):
        s=state();s['rows'][0]['shipped']=10;p=parsed_reply(s,[response(s['rows'][0],100,baseline=0)]);self.assertTrue(p['errors'])
    def test_prepopulated_reply_template_no_answer_is_no_update(self):
        s=state();p=parse_response(template_file('response',s['rows']),'Ответ Донцовой',2026,s)
        self.assertEqual(p['count'],0);self.assertFalse(p['errors'])
    def test_reply_template_hides_auto_fields_and_has_no_comment_column(self):
        s=state();w=load_workbook(BytesIO(template_file('response',s['rows'])),data_only=True);sheet=w['Ответ Донцовой']
        self.assertTrue(sheet.column_dimensions['A'].hidden);self.assertTrue(sheet.column_dimensions['L'].hidden)
        self.assertEqual(sheet['A5'].value,'r1');self.assertEqual(sheet['L5'].value,0);self.assertEqual(sheet['H5'].value,'1')
        self.assertNotIn('Комментарий Донцовой',[c.value for c in sheet[4]])
        blank=load_workbook(BytesIO(template_file('response')));self.assertEqual(blank.active['L5'].value,0)
    def test_reply_zero_or_default_part_needs_no_comment(self):
        s=state();r=response(s['rows'][0],0,ready='');r[7]='';p=parsed_reply(s,[r])
        self.assertFalse(p['errors']);self.assertEqual(p['changes'][0]['lots'],[])
        r=response(s['rows'][0],20);r[7]='';p=parsed_reply(s,[r]);self.assertFalse(p['errors']);self.assertEqual(p['changes'][0]['lots'][0]['part'],'1')
    def test_older_reply_template_with_comment_is_still_accepted(self):
        s=state();r=response(s['rows'][0],20);r.insert(11,'Legacy note')
        headers=REPLY_HEADERS[:11]+['Комментарий Донцовой']+REPLY_HEADERS[11:]
        p=parse_response(xlsx(headers,[r]),'Data',2026,s);self.assertFalse(p['errors']);self.assertEqual(p['changes'][0]['lots'][0]['qty'],20)
    def test_order_template_append_and_duplicate_order_protection(self):
        raw=xlsx(ORDER_HEADERS,[['O3','10','I3','K1','S1','A',25,TODAY,'']]);parsed=parse_order(raw,'Data',2026)
        s=state();wf.apply_import(s,'order',parsed,meta('order'));self.assertEqual(len(s['rows']),3);self.assertEqual(s['rows'][2]['lots'],[]);self.assertEqual(len(s['catalog']),1)
        self.assertTrue(wf.apply_import(s,'order',parsed,meta('order'))['duplicate'])
        with self.assertRaises(ValueError):wf.apply_import(s,'order',parsed,meta('order','another-file'))
    def test_unknown_response_does_not_add_new_order(self):
        s=state();r=base_row('not-present','O99');p=parsed_reply(s,[response(r)]);self.assertTrue(p['errors']);before=deepcopy(s)
        with self.assertRaises(ValueError):wf.apply_import(s,'response',p,meta('response'))
        self.assertEqual(s,before)
    def test_initial_one_time_missing_invoice_not_carried_between_orders(self):
        raw=xlsx(['№ заказа:','Номер счета','Наименование','Кол.','Дата подтвержденная:','Отгрузить до:'],[['Заказ O1','I1','A',260,TODAY,TODAY],['Заказ O2',None,'A',10,None,TODAY]])
        p=parse_initial(raw,'Data',2026,'remaining');self.assertFalse(p['errors']);self.assertEqual(p['rows'][1]['invoice'],'')
        s=wf.empty_state();wf.apply_import(s,'initial',p,meta('initial'));self.assertFalse(s['reconciled']);self.assertTrue(s['bootstrap'])
        with self.assertRaises(ValueError):wf.apply_import(s,'initial',p,meta('initial','newfile'))
    def test_invalid_initial_quantity_blocks_whole_import(self):
        raw=xlsx(['№ заказа:','Наименование','Кол.'],[['O1','A','bad']]);p=parse_initial(raw,'Data',2026,'remaining');self.assertTrue(p['errors'])
    def test_dispatch_conserves_orders_lots_and_duplicate_does_not_double_count(self):
        s=state();s['rows']=[base_row(qty=520)];wf.apply_import(s,'response',parsed_reply(s,[response(s['rows'][0],520)]),meta('response'));wf.make_plan(s)
        old_id=s['plan']['id'];t=s['plan']['trucks'][0];wf.dispatch(s,old_id,t['id'],t['date'])
        self.assertEqual(s['rows'][0]['shipped'],260);self.assertEqual(s['rows'][0]['lots'][0]['qty'],260);self.assertEqual(len(s['shipments']),1)
        self.assertTrue(wf.dispatch(s,old_id,t['id'],t['date'])['duplicate']);self.assertEqual(s['rows'][0]['shipped'],260)
        self.assertTrue(all(t['date']>s['shipments'][0]['actualDate'] for t in s['plan']['trucks']))
    def test_future_or_not_ready_dispatch_blocked(self):
        s=state();r=s['rows'][0];r['lots']=[{'part':'1','qty':260,'ready':TODAY}];wf.make_plan(s)
        with self.assertRaises(ValueError):wf.dispatch(s,s['plan']['id'],1,(date.fromisoformat(TODAY)+timedelta(days=100)).isoformat())
        with self.assertRaises(ValueError):wf.dispatch(s,s['plan']['id'],1,(date.fromisoformat(TODAY)-timedelta(days=1)).isoformat())
    def test_two_whole_pallets_of_one_order_can_ship_on_different_days(self):
        s=state();a=base_row(qty=40);b=base_row('r2','O2',qty=500);b['name']='B'
        tomorrow=(date.fromisoformat(TODAY)+timedelta(days=1)).isoformat()
        a['lots']=[{'part':'1','qty':20,'ready':TODAY},{'part':'2','qty':20,'ready':tomorrow}];b['lots']=[{'part':'1','qty':500,'ready':TODAY}]
        s['rows']=[a,b];s['catalog']=[{'name':'A','type':'single','capacity':20},{'name':'B','type':'single','capacity':10}];wf.make_plan(s)
        self.assertEqual([t['date'] for t in s['plan']['trucks']],[TODAY,tomorrow])
        for t in s['plan']['trucks']:self.assertEqual(sum(p['qty'] for p in t['pallets'] if p['rowId']=='r1'),20)
        t=s['plan']['trucks'][0];wf.dispatch(s,s['plan']['id'],t['id'],TODAY);self.assertEqual(a['shipped'],20)
        with patch('workflow.today',return_value=tomorrow):
            t=s['plan']['trucks'][0];wf.dispatch(s,s['plan']['id'],t['id'],tomorrow)
        self.assertEqual(a['shipped'],40);self.assertEqual(a['lots'],[]);self.assertEqual(len(s['shipments']),2)
    def test_readiness_fragments_combine_only_after_all_pieces_are_ready(self):
        s=state();a=base_row(qty=20);b=base_row('r2','O2',qty=250);b['name']='B'
        tomorrow=(date.fromisoformat(TODAY)+timedelta(days=1)).isoformat()
        a['lots']=[{'part':'1','qty':7,'ready':TODAY},{'part':'2','qty':13,'ready':tomorrow}];b['lots']=[{'part':'1','qty':250,'ready':TODAY}]
        s['rows']=[a,b];s['catalog']=[{'name':'A','type':'single','capacity':20},{'name':'B','type':'single','capacity':10}];wf.make_plan(s)
        t=s['plan']['trucks'][0];self.assertEqual(t['date'],tomorrow)
        pallet=next(p for p in t['pallets'] if p['rowId']=='r1');self.assertEqual(pallet['qty'],20)
        self.assertEqual(pallet['lotAllocations'],[{'lotId':'1','qty':7},{'lotId':'2','qty':13}])
        with patch('workflow.today',return_value=tomorrow):wf.dispatch(s,s['plan']['id'],t['id'],tomorrow)
        self.assertEqual(a['shipped'],20);self.assertEqual(a['lots'],[])
    def test_old_or_partial_cached_plan_cannot_be_dispatched(self):
        s=state();s['rows'][0]['lots']=[{'part':'1','qty':260,'ready':TODAY}];wf.make_plan(s)
        s['plan']['wholePalletsOnly']=False;before=deepcopy(s)
        with self.assertRaisesRegex(ValueError,'Пересоберите'):wf.dispatch(s,s['plan']['id'],1,TODAY)
        self.assertEqual(s,before)
        s['plan']['wholePalletsOnly']=True;s['plan']['trucks'][0]['pallets'][0]['qty']=1;before=deepcopy(s)
        with self.assertRaisesRegex(ValueError,'целыми паллетами'):wf.dispatch(s,s['plan']['id'],1,TODAY)
        self.assertEqual(s,before)
    def test_legacy_migration_preserves_quantities_dates_and_catalog(self):
        old={'rows':[dict(base_row(),ready=TODAY)],'catalog':state()['catalog'],'reconciled':True,'start':TODAY,'strategy':'urgent'}
        s=wf.upgrade(old);self.assertEqual(s['schema'],2);self.assertEqual(s['rows'][0]['qty'],260);self.assertEqual(s['rows'][0]['lots'][0]['qty'],260);self.assertEqual(len(s['catalog']),1)
    def test_sql_atomicity_conflict_restart_and_raw_file_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            rev,s=ledger_store.read(directory);raw=xlsx(ORDER_HEADERS,[['O1','10','','','','A',260,TODAY,'']]);p=parse_order(raw,'Data',2026);m=meta('order')
            rev,s,_=ledger_store.mutate(directory,rev,lambda s:wf.apply_import(s,'order',p,m),upload=(m,raw))
            self.assertEqual(ledger_store.read(directory)[1]['rows'][0]['order'],'O1');self.assertEqual(ledger_store.source(directory,m['sha'])[1],raw)
            with self.assertRaises(ledger_store.Conflict):ledger_store.mutate(directory,rev-1,lambda s:s.update(rows=[]))
            def fail(s):s['rows']=[];raise ValueError('rollback')
            with self.assertRaises(ValueError):ledger_store.mutate(directory,rev,fail)
            self.assertEqual(len(ledger_store.read(directory)[1]['rows']),1)
    def test_catalog_persists_when_order_added_and_new_reply_invalidates_only_plan(self):
        s=state();before=deepcopy(s['catalog']);wf.apply_import(s,'response',parsed_reply(s,[response(s['rows'][0])]),meta('response'));wf.make_plan(s)
        wf.apply_import(s,'response',parsed_reply(s,[response(s['rows'][1])]),meta('response','2'))
        self.assertEqual(s['catalog'],before);self.assertIsNone(s['plan']);self.assertEqual(len(s['rows']),2)
    def test_manual_quantity_correction_clears_stale_readiness(self):
        s=state();s['rows'][0]['lots']=[{'part':'1','qty':260,'ready':TODAY}]
        wf.update_row(s,'r1',{'shipped':100})
        self.assertEqual(s['rows'][0]['shipped'],100);self.assertEqual(s['rows'][0]['lots'],[]);self.assertFalse(s['reconciled'])
    def test_rebuild_does_not_plan_in_the_past(self):
        s=state();s['rows'][0]['lots']=[{'part':'1','qty':260,'ready':'2026-11-02'}];s['settings']['start']='2026-11-02'
        wf.make_plan(s);self.assertEqual(s['plan']['trucks'][0]['date'],TODAY)
    def test_sql_migration_keeps_legacy_snapshot_and_stable_ids(self):
        old={'rows':[dict(base_row(),ready=TODAY)],'catalog':state()['catalog'],'reconciled':True,'start':TODAY}
        with tempfile.TemporaryDirectory() as directory:
            c=ledger_store.connect(directory);raw=json.dumps(old,ensure_ascii=False)
            c.execute('INSERT INTO project VALUES(1,7,?)',(raw,));c.commit();c.close()
            rev,s=ledger_store.read(directory);rev2,s2=ledger_store.read(directory)
            self.assertEqual((rev,rev2),(8,8));self.assertEqual(s['rows'][0]['id'],s2['rows'][0]['id']);self.assertEqual(s['catalog'],old['catalog'])
            c=ledger_store.connect(directory);self.assertEqual(c.execute('SELECT body FROM state_history WHERE revision=7').fetchone()[0],raw);c.close()
