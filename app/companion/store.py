"""Local data. Only explicit user memories enter the memory table."""
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from limits import CONTEXT_SIZES,REPLY_TOKENS,THINKING_BUDGETS,validate_settings

DEFAULTS={
    'address':'王子','profile':'8B','ai_enabled':True,'voice_enabled':False,
    'voice_rate':0,'voice_backend':'auto','dnd':False,'hide_fullscreen':True,'greetings_enabled':False,
    'greeting_interval':3600,'quiet_start':'23:00','quiet_end':'08:00','quiet_hours':False,
    'context_tokens':16384,'response_length':'balanced','thinking_mode':'fast',
}

class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        with self.db:
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA secure_delete=ON;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,role TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,epoch INTEGER NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY,text TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY,title TEXT NOT NULL,due REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',notified REAL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,created REAL NOT NULL);
            ''')
            if 'finish_reason' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
                self.db.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE messages SET status='error' WHERE status='generating'")
    def get(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else DEFAULTS.get(key,default)
    def set(self,key,value):
        with self.lock,self.db:self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    def settings(self):return {key:self.get(key) for key in DEFAULTS}
    def patch(self,changes):
        with self.lock:return self._patch(changes)
    def _patch(self,changes):
        if not isinstance(changes,dict):raise ValueError('無效的設定內容')
        values=self.settings()
        for key,value in changes.items():
            if key not in DEFAULTS:raise ValueError('不支援的設定')
            if isinstance(DEFAULTS[key],bool) and not isinstance(value,bool):raise ValueError('開關必須是布林值')
            if key=='address':
                if not isinstance(value,str) or not 1<=len(value.strip())<=24:raise ValueError('稱呼請使用 1～24 個字')
                value=value.strip().replace('\n',' ')
            if key=='profile' and (not isinstance(value,str) or value not in ('4B','8B','14B')):raise ValueError('無效的模型配置')
            if key=='context_tokens' and (type(value)!=int or value not in CONTEXT_SIZES):raise ValueError('無效的對話容量')
            if key=='response_length' and (not isinstance(value,str) or value not in REPLY_TOKENS):raise ValueError('無效的回覆長度')
            if key=='thinking_mode' and (not isinstance(value,str) or value not in THINKING_BUDGETS):raise ValueError('無效的思考模式')
            if key=='voice_rate' and (type(value)!=int or not -3<=value<=3):raise ValueError('語速範圍為 -3～3')
            if key=='voice_backend' and (not isinstance(value,str) or value not in ('auto','windows','local')):raise ValueError('無效的朗讀語音選項')
            if key=='greeting_interval' and (type(value)!=int or not 1800<=value<=7200):raise ValueError('主動問候間隔為 30～120 分鐘')
            if key in ('quiet_start','quiet_end'):
                import datetime
                try:
                    if not isinstance(value,str) or len(value)!=5:raise ValueError()
                    datetime.time.fromisoformat(value)
                except (ValueError,TypeError):raise ValueError('時間格式為 HH:MM')
            values[key]=value
        validate_settings(values)
        with self.lock,self.db:
            for key in changes:self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(values[key],ensure_ascii=False)))
        return self.settings()
    def epoch(self):return self.get('_context_epoch',0)
    def reset_context(self):
        with self.lock,self.db:self.set('_context_epoch',self.epoch()+1)
    def message(self,role,text,status='complete'):
        with self.lock,self.db:
            cursor=self.db.execute('INSERT INTO messages(role,text,status,epoch,created) VALUES(?,?,?,?,?)',(role,text,status,self.epoch(),time.time()))
            return cursor.lastrowid
    def finish(self,identifier,text,status,finish_reason=''):
        with self.lock,self.db:self.db.execute('UPDATE messages SET text=?,status=?,finish_reason=? WHERE id=?',(text,status,finish_reason,identifier))
    def history(self,context=False,before=None):
        with self.lock:
            if context:rows=self.db.execute("SELECT * FROM messages WHERE epoch=? AND status='complete' ORDER BY id DESC LIMIT 96",(self.epoch(),)).fetchall()
            elif before is not None:rows=self.db.execute('SELECT * FROM messages WHERE id<? ORDER BY id DESC LIMIT 200',(int(before),)).fetchall()
            else:rows=self.db.execute('SELECT * FROM messages ORDER BY id DESC LIMIT 200').fetchall()
            result=[dict(row) for row in reversed(rows)]
            metadata=self.get('_generation_metrics',{})
            if isinstance(metadata,dict):
                for row in result:
                    value=metadata.get(str(row['id']))
                    if isinstance(value,dict):row['metrics']=value
            return result
    def clear_history(self):
        with self.lock,self.db:self.db.execute('DELETE FROM messages');self.reset_context();self.set('_generation_metrics',{})
        with self.lock:self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    def save_generation_metrics(self,identifier,metrics):
        # Keep the existing five-table backup schema. Only bounded numerical
        # metadata is saved; reasoning text never enters settings or history.
        allowed={'input_tokens','output_tokens','max_tokens','context_tokens','thinking_mode',
                 'reasoning_tokens','reasoning_seconds','answer_tokens','total_seconds',
                 'combined_max_tokens','reasoning_budget_tokens','budget_scope','finish_reason',
                 'reasoning_tokens_source','reasoning_seconds_scope','native_completion_tokens','answer_limit_enforced'}
        cleaned={key:value for key,value in metrics.items() if key in allowed
                 and (value is None or type(value) is int or (key=='answer_limit_enforced' and type(value) is bool) or
                      (type(value) is float and math.isfinite(value)) or
                      (key in ('thinking_mode','budget_scope','finish_reason','reasoning_tokens_source','reasoning_seconds_scope') and isinstance(value,str) and len(value)<=64))}
        with self.lock,self.db:
            saved=self.get('_generation_metrics',{})
            if not isinstance(saved,dict):saved={}
            saved[str(identifier)]=cleaned
            ordered=sorted((key for key in saved if key.isdigit()),key=int)[-200:]
            self.set('_generation_metrics',{key:saved[key] for key in ordered})
    def memories(self):
        with self.lock:return [dict(row) for row in self.db.execute('SELECT * FROM memories ORDER BY updated DESC,id DESC')]
    def save_memory(self,text,identifier=None):
        if not isinstance(text,str) or not 1<=len(text.strip())<=240:raise ValueError('每筆記憶請使用 1～240 個字')
        with self.lock,self.db:
            if identifier is None:
                if self.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]>=50:raise ValueError('最多保存 50 筆明確記憶，請先整理舊項目')
                identifier=self.db.execute('INSERT INTO memories(text,created,updated) VALUES(?,?,?)',(text.strip(),time.time(),time.time())).lastrowid
            elif not self.db.execute('UPDATE memories SET text=?,updated=? WHERE id=?',(text.strip(),time.time(),int(identifier))).rowcount:raise ValueError('找不到這筆記憶')
            self.reset_context()
        return identifier
    def delete_memory(self,identifier=None):
        with self.lock,self.db:
            if identifier is None:self.db.execute('DELETE FROM memories')
            else:self.db.execute('DELETE FROM memories WHERE id=?',(int(identifier),))
            self.reset_context()
        with self.lock:self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    def reminder(self,title,due):
        if not isinstance(title,str) or not 1<=len(title.strip())<=120:raise ValueError('提醒內容請使用 1～120 個字')
        if not isinstance(due,(float,int)) or not 0<float(due)<time.time()+366*86400:raise ValueError('請選擇一年內的提醒時間')
        with self.lock,self.db:return self.db.execute('INSERT INTO reminders(title,due,created) VALUES(?,?,?)',(title.strip(),float(due),time.time())).lastrowid
    def reminders(self):
        with self.lock:return [dict(row) for row in self.db.execute('SELECT * FROM reminders ORDER BY due DESC LIMIT 100')]
    def change_reminder(self,identifier,action):
        with self.lock,self.db:
            if action=='delete':self.db.execute('DELETE FROM reminders WHERE id=?',(int(identifier),))
            elif action=='done':self.db.execute("UPDATE reminders SET status='done' WHERE id=?",(int(identifier),))
            elif action=='snooze':self.db.execute("UPDATE reminders SET due=?,status='pending',notified=NULL WHERE id=?",(time.time()+300,int(identifier)))
            else:raise ValueError('無效的提醒操作')
    def due(self,quiet=False):
        with self.lock,self.db:
            self.db.execute("UPDATE reminders SET status='due' WHERE due<=? AND status='pending'",(time.time(),))
            if quiet:return []
            rows=self.db.execute("SELECT * FROM reminders WHERE status='due' AND notified IS NULL ORDER BY due LIMIT 3").fetchall()
            for row in rows:self.db.execute('UPDATE reminders SET notified=? WHERE id=?',(time.time(),row['id']))
            return [dict(row) for row in rows]
    def event(self,kind,text):
        with self.lock,self.db:
            cursor=self.db.execute('INSERT INTO events(kind,text,created) VALUES(?,?,?)',(kind,text,time.time()))
            self.db.execute('DELETE FROM events WHERE id<?',(max(0,cursor.lastrowid-100),));return cursor.lastrowid
    def events(self,after=0):
        with self.lock:return [dict(row) for row in self.db.execute('SELECT * FROM events WHERE id>? ORDER BY id',(int(after),))]
    def close(self):
        with self.lock:self.db.close()
