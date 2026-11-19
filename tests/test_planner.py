import unittest, random
from datetime import date
from planner import build_plan,LAYOUTS,resolve_catalog_product,workdays

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
    def test_lookahead_keeps_four_full_trucks_possible_with_mixed_pallets(self):
        # Readiness distribution from a batch where greedy daily allocation fills only three trucks.
        deliveries=[('2026-11-16',0,11),('2026-11-17',0,2),('2026-11-18',2,27),
                    ('2026-11-21',0,3),('2026-11-22',1,0),('2026-11-23',1,4),
                    ('2026-11-24',1,15),('2026-11-25',1,12),('2026-11-26',0,15)]
        rows=[]
        for index,(day,doubles,singles) in enumerate(deliveries):
            if doubles:rows.append(row(index*2,'D',doubles,ready=day,due='2026-11-25'))
            if singles:rows.append(row(index*2+1,'S',singles,ready=day,due='2026-11-25'))
        plan=run(rows,[cat('D','double'),cat('S')],start='2026-11-23')
        trucks=plan['trucks']
        self.assertEqual([t['date'] for t in trucks],['2026-11-23','2026-11-24','2026-11-25','2026-11-26'])
        self.assertEqual(sum(len(t['pallets']) for t in trucks),94)
        self.assertEqual(sum(x['qty'] for x in plan['leftovers']),1)
        for truck in trucks:
            self.assertIn(truck['layout'],LAYOUTS)
            self.assertTrue(all(p['ready']<=truck['date'] for p in truck['pallets']))
    def test_not_before_ready_or_weekend(self):
        p=run([row(1,'A',26,ready='2026-11-21')],[cat('A')])
        self.assertEqual(p['trucks'][0]['date'],'2026-11-23')
    def test_black_double_indivisible(self):
        p=run([row(1,'A',10),row(2,'B',1)],[cat('A','double'),cat('B')]);self.assertFalse(p['trucks'])
    def test_red_double_split_quantity(self):
        p=run([row(1,'A',260)],[cat('A','split',20)])
        self.assertEqual(len(p['trucks'][0]['pallets']),26);self.assertEqual(sum(x['qty'] for x in p['trucks'][0]['pallets']),260)
    def test_partial_pallet_is_left_out(self):
        p=run([row(1,'A',251)],[cat('A','single',10)])
        self.assertFalse(p['trucks']);self.assertEqual(sum(x['qty'] for x in p['leftovers']),251);self.assertTrue(any(x['qty']==1 and 'не кратен' in x['reason'] for x in p['leftovers']))
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
    def test_catalog_without_articles_removes_prefix_but_preserves_model_suffix(self):
        ordered=row(1,'Шкаф для раздевалок усиленный 0МХ_ML-11-50U (LS-11-50)',572)
        correct=cat('Шкаф для раздевалок усиленный ML-11-50U (LS-11-50)',cap=22)
        other=cat('Шкаф для раздевалок усиленный ML-11-50 (LS-11-50)',cap=11)
        plan=run([ordered],[correct,other])
        self.assertEqual({p['capacity'] for p in plan['trucks'][0]['pallets']},{22})
        self.assertEqual(len(run([ordered],[other])['issues']),1)
    def test_catalog_articles_override_name_and_block_conflicting_promet_article(self):
        ordered=dict(row(1,'Название Комуса',572),komusSku='1436687',sku='S23099404602')
        correct=cat('Название ПРОМЕТ',cap=22,komusSku='1436687',sku='S23099404602')
        misleading=cat('Название Комуса',cap=11)
        plan=run([ordered],[correct,misleading])
        self.assertEqual({p['capacity'] for p in plan['trucks'][0]['pallets']},{22})
        correct['sku']='S23099404603'
        self.assertIn('Артикулы расходятся',run([ordered],[correct,misleading])['issues'][0]['reason'])
    def test_tula_short_name_uses_only_unique_complete_model_and_product_variant(self):
        catalog=[
            cat('Шкаф для раздевалок Стандарт LS-21-600',cap=20),
            cat('Шкаф для раздевалок Стандарт LS-21-60D',cap=8),
            cat('Шкаф CB-12',cap=14),
            cat('Шкаф Комус Мебель CB-12',cap=28),
            cat('Сейф Cobalt RUS-17',cap=40),
            cat('Сейф Cobalt RUS-17 EL',cap=80),
        ]
        orders=[
            row(1,'Шкаф для одежды металлический 0MX_Стандарт LS-21-60 (LS-21-600)',10*20),
            row(2,'Шкаф для бумаг 0MX_CB-12 850х400х1860',6*14),
            row(3,'Сейф COBALT RUS-17 EL мебельный, электр.замок',10*80),
        ]
        plan=run(orders,catalog)
        self.assertEqual(len(plan['trucks']),1)
        self.assertEqual({p['rowId']:p['capacity'] for p in plan['trucks'][0]['pallets']},
                         {'1':20,'2':14,'3':80})
        self.assertEqual(len(run([orders[2]],catalog[:-1])['issues']),1)
        duplicate=cat('Шкаф CB-12 стальной',cap=14)
        self.assertEqual(len(run([orders[1]],catalog+[duplicate])['issues']),1)
    def test_confirmed_tula_aliases_keep_their_own_catalogue_entry(self):
        bag_cabinet=cat('Шкаф для сумок усиленный Комус Мебель ML-24-60','split',40)
        m08=cat('Шкаф M-08',cap=12)
        m18=cat('Шкаф M-18',cap=24)
        catalog=[bag_cabinet,m08,m18]
        for name in ('ML-24-60','Комус Мебель ML-24-60','Шкаф для сумок ML-24-60',
                     'Шкаф для сумок Комус Мебель ML-24-60'):
            self.assertIs(resolve_catalog_product({'name':name},catalog),bag_cabinet)
        name='Антресоль к шкафу для бумаг 0МХ_ПРАКТИК М-08 к шкафу М-18, 915х370х832'
        self.assertIs(resolve_catalog_product({'name':name},catalog),m08)
        self.assertTrue(run([row(1,'Антресоль M-18',24)],catalog)['issues'])
    def test_complex_and_odd_capacity_block(self):
        p=run([row(1,'A',26),row(2,'B',26)],[cat('A','split',3),cat('B',needsReview=True)])
        self.assertEqual(len(p['issues']),2);self.assertFalse(p['trucks'])
    def test_rack_matching_converts_units_reorders_dimensions_and_keeps_shelf_count(self):
        catalog=[
            cat('Стеллаж MS 185/100х50/4',cap=20),
            cat('Стеллаж MS 185/100х50/5',cap=15),
            cat('Стеллаж MS Str 185/100х50/4 Комус',cap=10),
            cat('Стеллаж медицинский СТМ MS 185/100х50/4',cap=9),
        ]
        orders=[
            row(1,'Стеллаж металлический ПРАКТИК MS 4 полки 1000×500×1850',260),
            row(2,r'Стеллаж MS 185х100х50\5',195),
        ]
        plan=run(orders,catalog)
        self.assertEqual(len(plan['trucks']),1)
        self.assertEqual({p['rowId']:p['capacity'] for p in plan['trucks'][0]['pallets']},
                         {'1':20,'2':15})
        duplicate=cat('Стеллаж MS 100×50×185 см 4 полки',cap=20)
        self.assertIn('несколько стеллажей',run([orders[0]],catalog+[duplicate])['issues'][0]['reason'])
        self.assertTrue(run([row(3,'Стеллаж MS 1000×500×1850',520)],catalog)['issues'])
    def test_rack_matching_preserves_strong_and_kd_variants(self):
        catalog=[
            cat('Стеллаж MS Str 200/100х50/6 Комус',cap=20),
            cat('Стеллаж SBL 180KD/90x40/5',cap=38),
        ]
        orders=[
            row(1,'Стеллаж металлический MS Strong 200/100x50/6 полок',260),
            row(2,'Стеллаж металлический SBL 180KD/90x40/ 5 полок',494),
        ]
        plan=run(orders,catalog)
        self.assertEqual(len(plan['trucks']),1)
        self.assertFalse(plan['issues'])
        self.assertTrue(run([row(2,'Стеллаж SBL 180/90x40/5',494)],catalog)['issues'])
    def test_cobalt_kd_uses_confirmed_nominal_heights_but_keeps_other_dimensions(self):
        catalog=[
            cat('Стеллаж Cobalt Shelf MS 180KD/100x30/4','split',60),
            cat('Стеллаж Cobalt Shelf MS 200KD/100x50/5','split',50),
        ]
        source='Стеллаж металлический Cobalt Shelf MS KD 1000x300х1750/4 полки'
        alternate='Стеллаж металлический Cobalt Shelf MS KD 1000x500х1950/5 полок'
        self.assertIs(resolve_catalog_product({'name':source},catalog),catalog[0])
        self.assertIs(resolve_catalog_product({'name':alternate},catalog),catalog[1])
        for changed in (source.replace('1750','1760'),source.replace('300','400'),
                        source.replace('Cobalt Shelf ',''),source.replace(' KD','')):
            with self.assertRaises(ValueError):
                resolve_catalog_product({'name':changed},catalog)
    def test_loose_requires_fixation_and_full_truck(self):
        p=run([row(1,'A',26),row(2,'B',50)],[cat('A'),cat('B','loose',10,fixation='В закреплённом коробе')])
        self.assertEqual(sum(x['qty'] for x in p['trucks'][0]['loose']),50)
        p=run([row(1,'B',50)],[cat('B','loose',10,fixation='Короб')]);self.assertFalse(p['trucks']);self.assertEqual(p['leftovers'][0]['qty'],50)
    def test_no_mixing_orders_within_pallet(self):
        p=run([row(i,'A',10) for i in range(26)],[cat('A','single',10)])
        self.assertEqual(len(p['trucks'][0]['pallets']),26)
        self.assertEqual(len({x['order'] for x in p['trucks'][0]['pallets']}),26)
    def test_small_remainders_of_different_orders_do_not_form_a_pallet(self):
        p=run([row(1,'A',10),row(2,'A',10),row(3,'B',250)],[cat('A','single',20),cat('B','single',10)])
        self.assertFalse(p['trucks']);self.assertEqual(sum(x['qty'] for x in p['leftovers']),270)
    def test_remainder_does_not_prevent_other_whole_pallets_from_shipping(self):
        p=run([row(1,'A',251),row(2,'B',10)],[cat('A','single',10),cat('B','single',10)])
        self.assertEqual(len(p['trucks']),1);self.assertTrue(all(x['qty']==10 for x in p['trucks'][0]['pallets']))
        self.assertEqual(p['leftovers'][0]['qty'],1);self.assertIn('не кратен',p['leftovers'][0]['reason'])
    def test_red_split_uses_whole_single_pallets(self):
        p=run([row(1,'A',521)],[cat('A','split',40)])
        self.assertEqual(len(p['trucks']),1);self.assertTrue(all(x['qty']==20 for x in p['trucks'][0]['pallets']))
        self.assertEqual(p['leftovers'][0]['qty'],1)
    def test_loose_still_requires_confirmed_shipping_multiple(self):
        p=run([row(1,'A',26),row(2,'B',11)],[cat('A'),cat('B','loose',5,fixation='Закреплено')])
        self.assertEqual(sum(x['qty'] for x in p['trucks'][0]['loose']),10)
        self.assertEqual(p['leftovers'][0]['qty'],1)
        p=run([row(1,'A',26),row(2,'B',10)],[cat('A'),cat('B','loose',0,fixation='Закреплено')])
        self.assertFalse(p['trucks'][0]['loose']);self.assertTrue(p['issues'])
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
                    self.assertEqual(x['qty'],x['capacity']);self.assertFalse(x['partial'])
                    self.assertGreaterEqual(t['date'],x['ready']);sent[x['rowId']]+=x['qty']
                    if x['name']=='B':self.assertTrue(x['vertical'])
            for x in p['leftovers']:sent[x['rowId']]+=x['qty']
            self.assertEqual(sent,{r['id']:r['qty'] for r in rows})
if __name__=='__main__':unittest.main()
