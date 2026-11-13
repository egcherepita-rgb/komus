from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import hashlib, hmac, json, mimetypes, os, secrets, sqlite3, time, threading
from planner import build_plan, LAYOUTS
from importer import rows_import, catalog_import, export_plan

ROOT=Path(__file__).resolve().parent
DATA=Path(os.getenv('DATA_DIR',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True)
PASSWORD=os.getenv('APP_PASSWORD','')
SESSIONS={};ATTEMPTS={};LOCK=threading.Lock()

def db():
    c=sqlite3.connect(DATA/'komus.db',timeout=15)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS project (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL)')
    return c

def demo():
    return {'rows':[
        {'id':'1','order':'ДЕМО-001','invoice':'Пример','name':'Демонстрационный сейф','qty':520,'shipped':0,'ready':'2026-11-16','due':'2026-11-18'},
        {'id':'2','order':'ДЕМО-002','invoice':'Пример','name':'Демонстрационный шкаф','qty':220,'shipped':0,'ready':'2026-11-16','due':'2026-11-19'},
        {'id':'3','order':'ДЕМО-003','invoice':'Пример','name':'Демонстрационный стеллаж','qty':100,'shipped':0,'ready':'2026-11-17','due':'2026-11-20'}],
        'catalog':[{'name':'Демонстрационный сейф','type':'single','capacity':10,'vertical':False},
                   {'name':'Демонстрационный шкаф','type':'double','capacity':20,'vertical':True},
                   {'name':'Демонстрационный стеллаж','type':'single','capacity':10,'vertical':False}],
        'start':'2026-11-16','reconciled':True,'strategy':'urgent','label':'Демонстрация — вымышленные данные'}

class Handler(BaseHTTPRequestHandler):
    server_version='Komus'
    def log_message(self,fmt,*args):
        # Do not log request bodies or passwords.
        print('%s %s'%(self.client_address[0],fmt%args))
    def send(self,status,body,mime='application/json; charset=utf-8',headers=None):
        raw=json.dumps(body,ensure_ascii=False).encode() if mime.startswith('application/json') else body
        self.send_response(status)
        self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def auth(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''));tok=cookie.get('komus_session')
            key=hashlib.sha256(tok.value.encode()).hexdigest() if tok else ''
            with LOCK:
                now=time.time()
                for k in list(SESSIONS):
                    if SESSIONS[k]<now:del SESSIONS[k]
                return SESSIONS.get(key,0)>now
        except Exception:return False
    def body(self,limit=22*1024*1024):
        try:n=int(self.headers.get('Content-Length','0'))
        except ValueError:raise ValueError('Неверный размер запроса')
        if n<0 or n>limit:raise ValueError('Запрос слишком большой')
        return self.rfile.read(n)
    def do_GET(self):
        try:
            path=urlparse(self.path).path
            if path=='/health':return self.send(200,{'ok':True})
            if path=='/api/session':return self.send(200,{'authenticated':self.auth()})
            if path=='/api/demo':return self.send(200,demo())
            if path=='/api/layouts':return self.send(200,LAYOUTS)
            if path=='/api/demo-plan':
                d=demo();return self.send(200,build_plan(d['rows'],d['catalog'],d['start'],reconciled=True))
            if path.startswith('/api/'):
                if not self.auth():return self.send(401,{'error':'Войдите в приложение'})
                if path=='/api/project':
                    with db() as c:r=c.execute('SELECT revision,body FROM project WHERE id=1').fetchone()
                    return self.send(200,{'revision':r[0] if r else 0,'project':json.loads(r[1]) if r else None})
                if path=='/api/layouts':return self.send(200,LAYOUTS)
                if path=='/api/demo':return self.send(200,demo())
                return self.send(404,{'error':'Не найдено'})
            allowed={'/':'index.html','/demo':'index.html','/app.js':'app.js','/style.css':'style.css','/assets/promet.png':'assets/promet.png','/assets/layouts.svg':'assets/layouts.svg'}
            if path not in allowed:return self.send(404,{'error':'Не найдено'})
            f=ROOT/'public'/allowed[path]
            return self.send(200,f.read_bytes(),mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
        except Exception:self.send(500,{'error':'Не удалось выполнить запрос'})
    def do_POST(self):
        try:
            path=urlparse(self.path).path;query=parse_qs(urlparse(self.path).query)
            # Require a custom same-origin header, including for login. Cross-site forms cannot provide it.
            if self.headers.get('X-Komus-Client')!='web':return self.send(403,{'error':'Недопустимый источник запроса'})
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):return self.send(403,{'error':'Недопустимый источник запроса'})
            if path=='/api/login':
                ip=self.client_address[0]
                with LOCK:
                    now=time.time();recent=[t for t in ATTEMPTS.get(ip,[]) if now-t<300]
                    ATTEMPTS[ip]=recent
                    if len(recent)>=10:return self.send(429,{'error':'Слишком много попыток. Повторите через 5 минут.'})
                data=json.loads(self.body(4096));attempt=str(data.get('password',''))
                if not hmac.compare_digest(hashlib.sha256(attempt.encode()).digest(),hashlib.sha256(PASSWORD.encode()).digest()):
                    with LOCK:ATTEMPTS[ip].append(time.time())
                    return self.send(401,{'error':'Неверный пароль'})
                token=secrets.token_urlsafe(40)
                with LOCK:SESSIONS[hashlib.sha256(token.encode()).hexdigest()]=time.time()+43200;ATTEMPTS.pop(ip,None)
                secure='; Secure' if os.getenv('COOKIE_SECURE','0')=='1' else ''
                return self.send(200,{'ok':True},headers={'Set-Cookie':'komus_session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200'+secure})
            if not self.auth():return self.send(401,{'error':'Войдите в приложение'})
            if path=='/api/logout':
                ck=SimpleCookie(self.headers.get('Cookie',''));tok=ck.get('komus_session')
                if tok:
                    with LOCK:SESSIONS.pop(hashlib.sha256(tok.value.encode()).hexdigest(),None)
                return self.send(200,{'ok':True},headers={'Set-Cookie':'komus_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'})
            if path=='/api/import/catalog':return self.send(200,catalog_import(self.body()))
            if path=='/api/import/rows':return self.send(200,rows_import(self.body(),query.get('sheet',[None])[0],query.get('year',[None])[0]))
            data=json.loads(self.body())
            if path=='/api/project':
                project=data['project']
                if not isinstance(project,dict) or len(project.get('rows',[]))>5000 or len(project.get('catalog',[]))>10000:raise ValueError('Слишком большой проект')
                with db() as c:
                    c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT revision FROM project WHERE id=1').fetchone();rev=r[0] if r else 0
                    if data.get('revision')!=rev:return self.send(409,{'error':'Проект изменён в другой вкладке. Обновите страницу перед сохранением.'})
                    c.execute('INSERT OR REPLACE INTO project VALUES(1,?,?)',(rev+1,json.dumps(project,ensure_ascii=False)))
                return self.send(200,{'revision':rev+1})
            if path in ('/api/plan','/api/export'):
                plan=build_plan(data['rows'],data['catalog'],data['start'],data.get('strategy','urgent'),data.get('reconciled') is True)
                if path=='/api/export':return self.send(200,export_plan(plan),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'Content-Disposition':'attachment; filename="komus-plan.xlsx"'})
                return self.send(200,plan)
            return self.send(404,{'error':'Не найдено'})
        except (ValueError,KeyError,TypeError) as e:self.send(400,{'error':str(e) if isinstance(e,ValueError) else 'Проверьте формат данных запроса'})
        except Exception:self.send(500,{'error':'Не удалось обработать файл или запрос. Проверьте формат данных.'})

if __name__=='__main__':
    if len(PASSWORD)<12:raise SystemExit('Задайте APP_PASSWORD длиной не менее 12 символов')
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default=os.getenv('HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.getenv('PORT','8080')))
    parser.add_argument('--strictPort',action='store_true')
    args=parser.parse_args(); port=args.port; host=args.host
    db().close()
    print(f'Komus listening on {host}:{port}',flush=True)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
