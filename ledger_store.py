"""Versioned SQLite ledger; preserves the v1 database and immutable uploaded files."""
from pathlib import Path
from contextlib import closing
import json,sqlite3
from workflow import upgrade,now

class Conflict(ValueError):pass

def connect(directory):
    Path(directory).mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(Path(directory)/'komus.db',timeout=30)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS project (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS state_history (revision INTEGER PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS uploads (sha TEXT PRIMARY KEY, filename TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, body BLOB NOT NULL)')
    return c

def _read(c):
    row=c.execute('SELECT revision,body FROM project WHERE id=1').fetchone()
    if not row:return 0,upgrade(None)
    rev,raw=row;project=json.loads(raw)
    if project.get('schema')!=2:
        c.execute('INSERT OR IGNORE INTO state_history VALUES(?,?,?)',(rev,now(),raw))
        project=upgrade(project);rev+=1
        c.execute('UPDATE project SET revision=?,body=? WHERE id=1',(rev,json.dumps(project,ensure_ascii=False)))
    return rev,project

def read(directory):
    with closing(connect(directory)) as c, c:
        c.execute('BEGIN IMMEDIATE');return _read(c)

def mutate(directory,expected,action,upload=None):
    with closing(connect(directory)) as c, c:
        c.execute('BEGIN IMMEDIATE');rev,state=_read(c)
        if expected!=rev:raise Conflict('Данные уже изменились в другой вкладке. Обновите журнал и повторите действие.')
        before=json.dumps(state,ensure_ascii=False);result=action(state)
        after=json.dumps(state,ensure_ascii=False)
        if before==after:return rev,state,result
        c.execute('INSERT OR IGNORE INTO state_history VALUES(?,?,?)',(rev,now(),before))
        c.execute('INSERT OR REPLACE INTO project VALUES(1,?,?)',(rev+1,after))
        c.execute('DELETE FROM state_history WHERE revision NOT IN (SELECT revision FROM state_history ORDER BY revision DESC LIMIT 20)')
        if upload:
            meta,raw=upload;c.execute('INSERT OR IGNORE INTO uploads VALUES(?,?,?,?,?)',(meta['sha'],meta['filename'],meta['kind'],meta['at'],raw))
        return rev+1,state,result

def source(directory,sha):
    with closing(connect(directory)) as c, c:return c.execute('SELECT filename,body FROM uploads WHERE sha=?',(sha,)).fetchone()
