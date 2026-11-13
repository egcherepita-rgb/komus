import unittest
from io import BytesIO
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font
from importer import rows_import,catalog_import,parse_date,export_plan
from planner import build_plan

def bytes_for(w):
    b=BytesIO();w.save(b);return b.getvalue()
class ImportTests(unittest.TestCase):
    def test_tula_colors_and_complex(self):
        w=Workbook();s=w.active;s.append(['','','ТУЛА','','','МОСКВА']);s.append(['','ВП','Наименование','Кратность']);
        for r in [('0.5.','','A',12),('1','ВП','B','22*2п'),('1','','C',30)]:s.append(r)
        s['A3'].font=Font(color='FFFF0000');s['A4'].font=Font(color='FFFF0000')
        c=catalog_import(bytes_for(w))['catalog'];self.assertEqual([x['type'] for x in c],['single','split','double']);self.assertTrue(c[1]['needsReview']);self.assertTrue(c[1]['vertical'])
    def test_rows_only_selected_sheet_no_planned_deduction(self):
        w=Workbook();s=w.active;s.title='отгрузка с УЗМК';s.append(['Комментарий']);s.append(['№ заказа:','Номер счета','Наименование','Кол.','Отгрузить до:','Дата подтвержденная:','Отгрузка 16.11.']);s.append(['O1','I1','A',26,'18.11.','16.11.',13]);s.append([None,None,'B',5,None,None,None]);w.create_sheet('отгруженные целиком')
        raw=bytes_for(w);self.assertEqual(len(rows_import(raw)['sheets']),2)
        r=rows_import(raw,s.title,2026)['rows'];self.assertEqual(len(r),2);self.assertEqual(r[0]['shipped'],0);self.assertEqual(r[1]['order'],'O1');self.assertEqual(r[1]['ready'],'')
        with self.assertRaises(ValueError):rows_import(raw,'отгруженные целиком',2026)
    def test_dates_and_export_formula_injection(self):
        self.assertEqual(parse_date('18.11.',2026),'2026-11-18');self.assertEqual(parse_date('31.02.',2026),'')
        p=build_plan([dict(id='1',name='=1+1',qty=26,ready='2026-11-16',due='2026-11-18',order='@x',invoice='I')],[dict(name='=1+1',type='single',capacity=1)],'2026-11-16',reconciled=True)
        w=load_workbook(BytesIO(export_plan(p)));self.assertEqual(w['Расстановка']['E2'].data_type,'s');self.assertEqual(w['Расстановка']['E2'].value,'=1+1')
if __name__=='__main__':unittest.main()
