import unittest, tempfile, threading, json, urllib.request, urllib.error, http.cookiejar
from pathlib import Path
import server

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.DATA=Path(cls.temp.name);server.PASSWORD='unittest-password';server.db().close()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start();cls.url='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
    def client(self):return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(self,c,path,data=None,header=True):
        h={'X-Komus-Client':'web'} if header else {}
        if data is not None:h['Content-Type']='application/json'
        r=urllib.request.Request(self.url+path,data=None if data is None else json.dumps(data).encode(),headers=h)
        return c.open(r)
    def test_authenticated_save_conflict_plan_export_logout(self):
        c=self.client()
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/project')
        self.assertEqual(e.exception.code,401)
        self.request(c,'/api/login',{'password':server.PASSWORD})
        project=server.demo();res=json.load(self.request(c,'/api/project'));rev=res['revision']
        r=json.load(self.request(c,'/api/project',{'project':project,'revision':rev}));self.assertEqual(r['revision'],rev+1)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/project',{'project':project,'revision':rev})
        self.assertEqual(e.exception.code,409)
        self.assertTrue(json.load(self.request(c,'/api/plan',project))['trucks'])
        x=self.request(c,'/api/export',project);self.assertTrue(x.read().startswith(b'PK'))
        self.request(c,'/api/logout',{})
        self.assertFalse(json.load(self.request(c,'/api/session'))['authenticated'])
    def test_csrf_and_no_private_files(self):
        c=self.client()
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,'/api/login',{'password':server.PASSWORD},False)
        self.assertEqual(e.exception.code,403)
        for path in ('/.env','/data/komus.db','/../server.py'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(c,path)
            self.assertEqual(e.exception.code,404)
    def test_public_demo_has_only_synthetic_data(self):
        c=self.client();d=json.load(self.request(c,'/api/demo'))
        self.assertTrue(all(r['order'].startswith('ДЕМО') for r in d['rows']))
        self.assertTrue(json.load(self.request(c,'/api/demo-plan'))['trucks'])
if __name__=='__main__':unittest.main()
