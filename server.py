from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import hashlib, hmac, json, mimetypes, os, secrets, sqlite3, time, threading
from datetime import date
from planner import build_plan, LAYOUTS
from importer import (calculate_pallets, catalog_import, export_pallets, export_plan,
                      moscow_articles_import, moscow_catalog_import, pallet_template)
import ledger_store
from workflow import empty_state,public_state,make_plan,apply_import,update_row,dispatch,now,today,canonical_order,validate_row
from workflow import clear_plan, transition_row
from import_v2 import parse_initial,parse_order,parse_response,template_file
from planner import number, pallet_limiter_model

ROOT=Path(__file__).resolve().parent
DATA=Path(os.getenv('DATA_DIR',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True)
PASSWORD=os.getenv('APP_PASSWORD','')
SESSIONS={};ATTEMPTS={};LOCK=threading.Lock()

def db(): return ledger_store.connect(DATA)

def demo():
    state=empty_state();state['bootstrap']={'filename':'Демонстрационный журнал','at':now()}
    state['catalog']=[{'name':'Демонстрационный сейф','type':'single','capacity':10,'vertical':False},
                      {'name':'Демонстрационный шкаф','type':'double','capacity':20,'vertical':True}]
    state['rows']=[{'id':'demo1','order':'ДЕМО-001','position':'10','invoice':'ДЕМО-СЧ-001','name':'Демонстрационный сейф','qty':520,'shipped':0,'due':today(),'sku':'','komusSku':'','replyAsOf':today(),'lots':[{'part':'1','qty':520,'ready':today(),'asOf':today()}]},
                   {'id':'demo2','order':'ДЕМО-002','position':'10','invoice':'ДЕМО-СЧ-002','name':'Демонстрационный шкаф','qty':220,'shipped':0,'due':today(),'sku':'','komusSku':'','replyAsOf':today(),'lots':[{'part':'1','qty':220,'ready':today(),'asOf':today()}]},
                   {'id':'demo3','order':'ДЕМО-003','position':'10','invoice':'ДЕМО-СЧ-003','name':'Демонстрационный сейф','qty':60,'shipped':0,'due':today(),'sku':'','komusSku':'','replyAsOf':'','lots':[]}]
    state['reconciled']=True;make_plan(state)
    return public_state(state)

PREVIEWS={}

def envelope(rev,state,result=None):return {'revision':rev,'state':public_state(state),'result':result}

def preview_import(raw,query,revision):
    kind=query.get('kind',[''])[0];sheet=query.get('sheet',[None])[0]
    year=int(query.get('year',[today()[:4]])[0])
    if year<2000 or year>2100:raise ValueError('Год должен быть от 2000 до 2100')
    rev,state=ledger_store.read(DATA)
    if revision is not None and rev!=revision:raise ledger_store.Conflict('Журнал изменился. Обновите страницу перед загрузкой.')
    sha=hashlib.sha256(raw).hexdigest()
    if any(h['sha']==sha and h['kind']==kind for h in state['imports']):return {'duplicate':True,'message':'Этот файл уже загружен. Данные не добавлены повторно.'}
    if kind=='initial':
        if state['bootstrap'] or state['rows']:raise ValueError('Журнал уже инициализирован. Новые заказы добавляются отдельно.')
        parsed=parse_initial(raw,sheet,year,query.get('mode',[''])[0])
    elif kind=='order':
        parsed=parse_order(raw,sheet,year)
        existing={canonical_order(r.get('order')) for r in state['rows']}
        for r in parsed.get('rows',[]):
            if r['order'] in existing:parsed['errors'].append({'line':r['source'],'reason':'Такой номер заказа уже есть в журнале'})
    elif kind=='response':parsed=parse_response(raw,sheet,year,state)
    elif kind=='catalog':
        parsed=catalog_import(raw);parsed['errors']=[];parsed['count']=len(parsed['catalog'])
    elif kind=='moscow':
        parsed=moscow_catalog_import(raw)
    elif kind=='moscow-articles':
        parsed=moscow_articles_import(raw,state.get('moscowCatalog',[]))
    else:raise ValueError('Неизвестный тип загрузки')
    if 'sheets' in parsed:return parsed
    if not parsed.get('count',len(parsed.get('rows',[]))):
        if not parsed.get('errors'):raise ValueError('В файле нет заполненных строк для обработки')
    filename=query.get('filename',['upload.xlsx'])[0][:240]
    meta={'sha':sha,'kind':kind,'filename':filename,'sheet':sheet,'at':now()}
    token=secrets.token_urlsafe(30)
    with LOCK:
        current=time.time()
        for k in list(PREVIEWS):
            if current-PREVIEWS[k]['time']>600:del PREVIEWS[k]
        while len(PREVIEWS)>=3:del PREVIEWS[next(iter(PREVIEWS))]
        PREVIEWS[token]={'parsed':parsed,'meta':meta,'raw':raw,'revision':rev,'time':current}
    return dict(parsed,token=token,revision=rev,kind=kind,filename=filename)

def restore_pallet_limiters(state):
    """Recover limiters skipped by older imports while preserving catalogue edits."""
    latest = next((entry for entry in reversed(state['imports']) if entry['kind']=='catalog'), None)
    if not latest or state.get('checkedLimiterCatalog') == latest['sha']:
        return
    uploaded = ledger_store.source(DATA,latest['sha'])
    if not uploaded:
        return
    existing_models = {pallet_limiter_model(item.get('name')) for item in state['catalog']}
    existing_sources = {item.get('source') for item in state['catalog']}
    for item in catalog_import(uploaded[1])['catalog']:
        model = pallet_limiter_model(item['name'])
        if item['type'] == 'loose' and model and model not in existing_models and item['source'] not in existing_sources:
            state['catalog'].append(item)
    state['checkedLimiterCatalog'] = latest['sha']

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
            path=urlparse(self.path).path;query=parse_qs(urlparse(self.path).query)
            if path=='/health':return self.send(200,{'ok':True,'version':2})
            if path=='/api/session':return self.send(200,{'authenticated':self.auth()})
            if path=='/api/v2/demo':return self.send(200,{'revision':0,'state':demo()})
            if path=='/api/layouts':return self.send(200,LAYOUTS)
            if path.startswith('/api/'):
                if not self.auth():return self.send(401,{'error':'Войдите в приложение'})
                if path=='/api/v2/state':
                    rev,state=ledger_store.read(DATA);return self.send(200,envelope(rev,state))
                if path=='/api/v2/templates/order':data=template_file('order');filename='komus-new-order.xlsx'
                elif path=='/api/v2/templates/pallets':data=pallet_template();filename='komus-pallets-template.xlsx'
                elif path=='/api/v2/templates/response':
                    rev,state=ledger_store.read(DATA);order=query.get('order',[''])[0]
                    rows=[r for r in state['rows'] if not order or r.get('order')==order]
                    data=template_file('response',rows);filename='dontsova-response.xlsx'
                elif path=='/api/v2/export':
                    rev,state=ledger_store.read(DATA)
                    if not state.get('plan') or not state['plan'].get('wholePalletsOnly') or state['plan'].get('readinessMode')!='position':raise ValueError('Сначала пересоберите машины: одна дата готовности позиции, отгрузка целыми паллетами')
                    data=export_plan(state['plan']);filename='komus-machines.xlsx'
                elif path=='/api/v2/source':
                    found=ledger_store.source(DATA,query.get('sha',[''])[0])
                    if not found:return self.send(404,{'error':'Исходный файл не найден'})
                    data=found[1];filename='original.xlsx'
                else:return self.send(404,{'error':'Обновите страницу: приложение перешло на журнал версии 2'})
                return self.send(200,data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'Content-Disposition':'attachment; filename="'+filename+'"'})
            allowed={'/':'index.html','/demo':'index.html','/app.js':'app.js','/style.css':'style.css','/assets/promet.png':'assets/promet.png','/assets/layouts.svg':'assets/layouts.svg'}
            if path not in allowed:return self.send(404,{'error':'Не найдено'})
            f=ROOT/'public'/allowed[path]
            return self.send(200,f.read_bytes(),mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
        except ValueError as e:self.send(400,{'error':str(e)})
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
            if path=='/api/v2/import-preview':
                revheader=self.headers.get('X-Komus-Revision')
                return self.send(200,preview_import(self.body(),query,int(revheader) if revheader is not None else None))
            if path=='/api/v2/pallets':
                raw=self.body(15*1024*1024)
                _,state=ledger_store.read(DATA)
                return self.send(200,calculate_pallets(raw,state['catalog'],state.get('moscowCatalog',[]),state['rows']))
            data=json.loads(self.body());expected=data.get('revision')
            if path=='/api/v2/pallets/export':
                return self.send(200,export_pallets(data.get('rows')),
                                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                 {'Content-Disposition':'attachment; filename="komus-pallets.xlsx"'})
            if path=='/api/v2/import-apply':
                token=data.get('token','')
                with LOCK:entry=PREVIEWS.get(token)
                if not entry or time.time()-entry['time']>600:raise ValueError('Предварительный просмотр устарел. Выберите файл заново.')
                if expected!=entry['revision']:raise ledger_store.Conflict('Предварительный просмотр относится к другой версии журнала.')
                parsed,meta=entry['parsed'],entry['meta']
                rev,state,result=ledger_store.mutate(DATA,expected,lambda s:apply_import(s,meta['kind'],parsed,meta),upload=(meta,entry['raw']))
                with LOCK:PREVIEWS.pop(token,None)
            elif path=='/api/v2/row':
                rev,state,result=ledger_store.mutate(DATA,expected,lambda s:update_row(s,data['id'],data['changes']))
            elif path=='/api/v2/row-transition':
                rev,state,result=ledger_store.mutate(DATA,expected,lambda s:transition_row(
                    s,data['id'],data['operation'],data['quantity'],data.get('actualDate')))
            elif path=='/api/v2/reconcile':
                def reconcile(s):
                    for r in s['rows']:validate_row(r)
                    s['reconciled']=True
                    return {'message':'Сверка первоначальных остатков подтверждена.'}
                rev,state,result=ledger_store.mutate(DATA,expected,reconcile)
            elif path=='/api/v2/catalog-row':
                def catalog_row(s):
                    i=number(data['index'],'Строка');item=dict(data['item'])
                    if i>=len(s['catalog']):raise ValueError('Строка справочника не найдена')
                    if not item.get('name') or item.get('type') not in ('single','double','split','loose'):raise ValueError('Проверьте товар и тип паллеты')
                    item['capacity']=number(item.get('capacity',0),'Кратность')
                    if not item.get('needsReview') and not item['capacity']:raise ValueError('Укажите кратность отгрузки, в том числе для груза без отдельного места')
                    if item['type']=='split' and not item.get('needsReview') and item['capacity']%2:raise ValueError('Числовая кратность красной 1 должна делиться на 2')
                    s['catalog'][i]=item;s['catalogUpdated']=now();s['plan']=None
                rev,state,result=ledger_store.mutate(DATA,expected,catalog_row)
            elif path=='/api/v2/build':
                def build(s):
                    start=data.get('start') or today();date.fromisoformat(start)
                    strategy=data.get('strategy','urgent')
                    restore_pallet_limiters(s)
                    s['settings']={'start':start,'strategy':strategy};make_plan(s)
                    return {'message':'Полные машины собраны и сохранены.'}
                rev,state,result=ledger_store.mutate(DATA,expected,build)
            elif path=='/api/v2/dispatch':
                rev,state,result=ledger_store.mutate(DATA,expected,lambda s:dispatch(s,data['planId'],number(data['truckId'],'Машина',False),data['actualDate']))
            elif path=='/api/v2/clear-plan':
                rev,state,result=ledger_store.mutate(DATA,expected,lambda s:clear_plan(s,data['planId']))
            else:return self.send(410,{'error':'Откройте новую версию приложения и обновите страницу (Ctrl+F5).'})
            return self.send(200,envelope(rev,state,result))
        except ledger_store.Conflict as e:self.send(409,{'error':str(e)})
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
