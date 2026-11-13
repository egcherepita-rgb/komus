import unittest, random
from datetime import date
from planner import build_plan,LAYOUTS,workdays

START='2026-11-16'
def row(i,name,qty,ready=START,due='2026-11-18',shipped=0):
    return dict(id=str(i),name=name,qty=qty,ready=ready,due=due,shipped=shipped,order='O'+str(i),invoice='I'+str(i))
def cat(name,typ='single',cap=1,**kw):return dict(name=name,type=typ,capacity=cap,**kw)
def run(rows,catalog,**kw):return build_plan(rows,catalog,kw.pop('start',START),reconciled=True,**kw)

class PlannerTests(unittest.TestCase):
    def test_all_twelve_layouts(self):
        for l in LAYOUTS:
            with self.subTest(layout=l['id']):
                p=run([row(1,'A',l['doubles']),row(2,'B',l['singles'])],[cat('A','double'),cat('B')])
                self.assertEqual(len(p['trucks']),1);self.assertEqual(p['trucks'][0]['layout'],l)
                self.assertFalse(p['leftovers']);self.assertLessEqual(l['length'],13500)
    def test_underload_waits(self):
        p=run([row(1,'A',25)],[cat('A')]);self.assertFalse(p['trucks']);self.assertEqual(p['leftovers'][0]['qty'],25)
    def test_not_before_ready_or_weekend(self):
        p=run([row(1,'A',26,ready='2026-11-21')],[cat('A')])
        self.assertEqual(p['trucks'][0]['date'],'2026-11-23')
    def test_black_double_indivisible(self):
        p=run([row(1,'A',10),row(2,'B',1)],[cat('A','double'),cat('B')]);self.assertFalse(p['trucks'])
    def test_red_double_split_quantity(self):
        p=run([row(1,'A',260)],[cat('A','split',20)])
        self.assertEqual(len(p['trucks'][0]['pallets']),26);self.assertEqual(sum(x['qty'] for x in p['trucks'][0]['pallets']),260)
    def test_partial_pallet_keeps_footprint(self):
        p=run([row(1,'A',251)],[cat('A','single',10)])
        self.assertEqual(len(p['trucks']),1);self.assertTrue(p['trucks'][0]['pallets'][-1]['partial'])
    def test_subtract_shipped(self):
        p=run([row(1,'A',30,shipped=4)],[cat('A')]);self.assertEqual(len(p['trucks']),1)
    def test_second_truck_threshold(self):
        a=run([row(1,'A',52,due='2026-11-16')],[cat('A')])
        self.assertEqual([t['date'] for t in a['trucks']],['2026-11-16','2026-11-17'])
        b=run([row(1,'A',52,due='2026-11-10')],[cat('A')])
        self.assertEqual([t['date'] for t in b['trucks']],['2026-11-16','2026-11-16']);self.assertEqual(b['trucks'][1]['slot'],2)
        self.assertEqual(workdays(date(2026,11,11),date(2026,11,16)),3)
    def test_ambiguous_catalog_and_dates_block(self):
        p=run([row(1,'A',26),row(2,'B',26,ready='')],[cat('A'),cat('A'),cat('B')]);self.assertEqual(len(p['issues']),2)
    def test_complex_and_odd_capacity_block(self):
        p=run([row(1,'A',26),row(2,'B',26)],[cat('A','split',3),cat('B',needsReview=True)])
        self.assertEqual(len(p['issues']),2);self.assertFalse(p['trucks'])
    def test_loose_requires_fixation_and_full_truck(self):
        p=run([row(1,'A',26),row(2,'B',50)],[cat('A'),cat('B','loose',0,fixation='В закреплённом коробе')])
        self.assertEqual(p['trucks'][0]['loose'][0]['qty'],50)
        p=run([row(1,'B',50)],[cat('B','loose',0,fixation='Короб')]);self.assertFalse(p['trucks']);self.assertEqual(p['leftovers'][0]['qty'],50)
    def test_no_mixing_orders_within_pallet(self):
        p=run([row(i,'A',1) for i in range(26)],[cat('A','single',10)])
        self.assertEqual(len(p['trucks'][0]['pallets']),26)
        self.assertEqual(len({x['order'] for x in p['trucks'][0]['pallets']}),26)
    def test_reconciliation_required(self):
        with self.assertRaises(ValueError):build_plan([],[],START)
    def test_negative_fractional_quantities(self):
        for qty in (-1,1.5,'abc',True):
            self.assertEqual(len(run([row(1,'A',qty)],[cat('A')])['issues']),1)
    def test_conservation_randomized_all_strategies(self):
        rng=random.Random(2026)
        for strategy in ('urgent','compact','balanced'):
            rows=[row(i,'A' if i%2 else 'B',rng.randint(1,150),ready='2026-11-'+str(rng.randint(16,25))) for i in range(15)]
            cats=[cat('A','single',6),cat('B','double',8,vertical=True)]
            p=run(rows,cats,strategy=strategy);sent={r['id']:0 for r in rows}
            for t in p['trucks']:
                self.assertIn(t['layout'],LAYOUTS)
                self.assertEqual(sum(x['type']=='double' for x in t['pallets']),t['layout']['doubles'])
                self.assertEqual(sum(x['type']=='single' for x in t['pallets']),t['layout']['singles'])
                for x in t['pallets']:
                    self.assertGreaterEqual(t['date'],x['ready']);sent[x['rowId']]+=x['qty']
                    if x['name']=='B':self.assertTrue(x['vertical'])
            for x in p['leftovers']:sent[x['rowId']]+=x['qty']
            self.assertEqual(sent,{r['id']:r['qty'] for r in rows})
if __name__=='__main__':unittest.main()
