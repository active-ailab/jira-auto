"""Durable read-only stage checkpoints; retries never include external publication."""
import json
import sqlite3
from pathlib import Path
from .integrations import digest
from .materials import sha256
from .store import now

STAGES=['attachments','quality_collection','scene_inventory','build','crash_parse','ai_analysis']
NAMES={'quality_collection':'质量日志采集与归档','attachments':'下载与解包','scene_inventory':'现场分段与关联','build':'版本产物','crash_parse':'符号、寄存器与源码','ai_analysis':'AI分析与草稿'}


def setup(store):
    with store.db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS run_stages(
          run_id TEXT NOT NULL, name TEXT NOT NULL, issue_key TEXT NOT NULL,
          input_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
          reused_from TEXT, started TEXT NOT NULL, updated TEXT NOT NULL,
          PRIMARY KEY(run_id,name));
          CREATE INDEX IF NOT EXISTS stage_cache ON run_stages(issue_key,name,input_hash,status);
        ''')


class StageRunner:
    def __init__(self,store,key,run_id,force_from=None):
        self.store,self.key,self.run_id=store,key,run_id
        self.force_from=STAGES.index(force_from) if force_from in STAGES else None
        setup(store)

    def run(self,name,inputs,action,files=None,cache=True):
        fingerprint=digest(json.dumps(inputs,sort_keys=True,ensure_ascii=False))
        reusable=cache and (self.force_from is None or STAGES.index(name)<self.force_from)
        if reusable:
            for old in self.store.rows("SELECT * FROM run_stages WHERE issue_key=? AND name=? AND input_hash=? AND status='completed' ORDER BY updated DESC LIMIT 5",(self.key,name,fingerprint)):
                envelope=json.loads(old['result'])
                if self.valid_files(envelope.get('files',[])):
                    self.record(name,fingerprint,'completed',envelope,None,old['run_id'])
                    self.store.event('stage_reuse','复用已校验阶段：'+NAMES[name],self.key,self.run_id,{'stage':name,'reused_from':old['run_id']})
                    return envelope['value']
        self.record(name,fingerprint,'running')
        self.store.event('stage_start','开始阶段：'+NAMES[name],self.key,self.run_id,{'stage':name})
        try:
            value=action()
            if isinstance(value,dict) and value.get('failure'):raise RuntimeError(value['failure'])
            outputs=files(value) if files else []
            envelope={'value':value,'files':outputs}
            if not self.valid_files(outputs):raise ValueError('阶段产物校验失败，不保存可复用检查点')
            self.record(name,fingerprint,'completed',envelope)
            self.store.event('stage_done','阶段完成：'+NAMES[name],self.key,self.run_id,{'stage':name})
            return value
        except Exception as exc:
            message=str(exc)[:400] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__+'：阶段执行失败'
            self.record(name,fingerprint,'failed',error=message)
            self.store.event('stage_failed','阶段失败：'+NAMES[name]+'；可修复后从此继续',self.key,self.run_id,{'stage':name,'reason':message})
            raise

    def valid_files(self,files):
        for item in files:
            try:
                path=(self.store.data/item['path']).resolve();path.relative_to(self.store.data)
                if not path.is_file() or sha256(path)!=item['sha256']:return False
            except (ValueError,OSError,KeyError):return False
        return True

    def record(self,name,fingerprint,status,result=None,error=None,reused=None):
        with self.store.db() as c:
            c.execute('''INSERT INTO run_stages VALUES(?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(run_id,name) DO UPDATE SET input_hash=excluded.input_hash,status=excluded.status,
             result=excluded.result,error=excluded.error,reused_from=excluded.reused_from,updated=excluded.updated''',
             (self.run_id,name,self.key,fingerprint,status,json.dumps(result,ensure_ascii=False) if result is not None else None,error,reused,now(),now()))


def artifact_files(store,key,kinds):
    files=[]
    for a in store.artifacts(key):
        if a['kind'] not in kinds:continue
        p=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(a['id'],))[0]['path']).resolve();p.relative_to(store.data)
        files.append({'path':str(p.relative_to(store.data)),'sha256':a['metadata'].get('sha256') or sha256(p)})
    return files
