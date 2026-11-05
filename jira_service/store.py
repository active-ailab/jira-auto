import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data: Path):
        self.data = data
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        data.chmod(0o700)
        self.path = data / "state.sqlite3"
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS issues (
              key TEXT PRIMARY KEY, body TEXT NOT NULL, fingerprint TEXT NOT NULL,
              updated TEXT NOT NULL, tracked INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, issue_key TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
              payload TEXT NOT NULL, result TEXT, error TEXT, created TEXT NOT NULL,
              updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, issue_key TEXT,
              step TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports (
              id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
              body TEXT NOT NULL, body_hash TEXT NOT NULL, status TEXT NOT NULL,
              created TEXT NOT NULL, approved TEXT, publication TEXT);
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, kind TEXT NOT NULL,
              path TEXT NOT NULL, metadata TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analysis_cases (issue_key TEXT PRIMARY KEY, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_reviews (
              id TEXT PRIMARY KEY, report_id TEXT NOT NULL, issue_key TEXT NOT NULL,
              reason TEXT NOT NULL, created TEXT NOT NULL, run_id TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_job ON runs(kind, COALESCE(issue_key,''))
              WHERE status IN ('queued','running');
            CREATE INDEX IF NOT EXISTS reports_issue_created ON reports(issue_key,created DESC);
            CREATE INDEX IF NOT EXISTS runs_issue_created ON runs(issue_key,created DESC);
            CREATE INDEX IF NOT EXISTS events_issue_id ON events(issue_key,id DESC);
            CREATE INDEX IF NOT EXISTS events_run_id ON events(run_id,id);
            CREATE INDEX IF NOT EXISTS artifacts_issue_kind ON artifacts(issue_key,kind);
            """)
            if 'details' not in {r[1] for r in c.execute('PRAGMA table_info(events)')}:
                c.execute('ALTER TABLE events ADD COLUMN details TEXT')
            report_columns = {r[1] for r in c.execute('PRAGMA table_info(reports)')}
            for name, declaration in [('kind',"TEXT NOT NULL DEFAULT 'manual'"),('analysis','TEXT'),('run_id','TEXT')]:
                if name not in report_columns:
                    c.execute(f'ALTER TABLE reports ADD COLUMN {name} {declaration}')
            if 'kind' not in report_columns:
                c.execute("UPDATE reports SET kind='preflight' WHERE body LIKE '预处理报告：%'")
        self.path.chmod(0o600)
        from .stages import setup
        setup(self)
        from .reminders import setup as reminder_setup
        reminder_setup(self)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.db() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def event(self, step, message, issue=None, run=None, details=None):
        with self.db() as c:
            c.execute("INSERT INTO events(run_id,issue_key,step,message,created,details) VALUES(?,?,?,?,?,?)",
                      (run, issue, step, message[:2000], now(), json.dumps(details, ensure_ascii=False) if details is not None else None))

    def issue(self, key):
        rows = self.rows("SELECT body,fingerprint FROM issues WHERE key=?", (key,))
        if not rows:
            raise ValueError("工单尚未导入")
        d = json.loads(rows[0]["body"])
        d["fingerprint"] = rows[0]["fingerprint"]
        return d

    def issues(self):
        return [json.loads(r["body"]) for r in self.rows("SELECT body FROM issues ORDER BY updated DESC")]

    def upsert_issue(self, issue, fingerprint):
        with self.db() as c:
            old = c.execute("SELECT fingerprint FROM issues WHERE key=?", (issue["key"],)).fetchone()
            changed = old is not None and old["fingerprint"] != fingerprint
            c.execute("INSERT INTO issues(key,body,fingerprint,updated) VALUES(?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET body=excluded.body,fingerprint=excluded.fingerprint,updated=excluded.updated",
                      (issue["key"], json.dumps(issue, ensure_ascii=False), fingerprint, issue.get("updated") or now()))
            if changed:
                c.execute("UPDATE reports SET status='stale',approved=NULL WHERE issue_key=? AND status IN ('draft','approved')",
                          (issue["key"],))
        if not old or changed:
            self.event("jira", "工单已更新；旧审批已失效" if changed else "工单已接入", issue["key"])
        return changed

    def enqueue(self, kind, issue=None, payload=None):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT id,payload FROM runs WHERE kind=? AND COALESCE(issue_key,'')=? "
                            "AND status IN ('queued','running')", (kind, issue or "")).fetchone()
            if old:
                # A second click may reuse the same request, but must not silently
                # discard a newly selected scene, version or review instruction.
                def options(value):
                    return {k:v for k,v in value.items() if v is not None and k!='retry_of'}
                if options(json.loads(old['payload']))!=options(payload or {}):
                    raise ValueError('已有同类任务执行中，但现场或执行选项不同；请等待完成后重新提交')
                return old["id"]
            rid = str(uuid.uuid4())
            c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                      (rid, issue, kind, "queued", json.dumps(payload or {}), None, None, now(), now()))
        self.event("queue", f"任务已排队：{kind}", issue, rid)
        return rid

    def cancel_queued(self,rid):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT issue_key,status FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:raise ValueError('任务不存在')
            if r['status']!='queued':raise ValueError('只能取消尚未开始的排队任务；执行中的任务请等待结束')
            c.execute("UPDATE runs SET status='cancelled',updated=? WHERE id=?",(now(),rid))
        self.event('cancel','用户取消了尚未开始的任务；已保存材料和报告保留',r['issue_key'],rid)
        return {'status':'cancelled'}

    def claim(self):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if r:
                c.execute("UPDATE runs SET status='running',updated=? WHERE id=?", (now(), r["id"]))
            return dict(r) if r else None

    def finish(self, rid, result=None, error=None):
        with self.db() as c:
            c.execute("UPDATE runs SET status=?,result=?,error=?,updated=? WHERE id=?",
                      ("failed" if error else "completed", json.dumps(result, ensure_ascii=False), error, now(), rid))

    def recover(self):
        with self.db() as c:
            # Fetch/import jobs may have partial files. Make interruptions visible; retry is explicit.
            c.execute("UPDATE runs SET status='interrupted',error='服务重启，任务中断，可手动重试',updated=? WHERE status='running'", (now(),))
            c.execute("UPDATE run_stages SET status='interrupted',error='服务重启；已完成阶段仍可复用',updated=? WHERE status='running'", (now(),))
            # External writes are never retried after an uncertain result.
            c.execute("UPDATE reports SET status='publication_unknown' WHERE status='publishing'")
            c.execute("UPDATE daily_focus SET state='delivery_unknown',updated=? WHERE state='sending'",(now(),))

    def artifact(self, issue, kind, path, metadata):
        resolved = Path(path).resolve()
        resolved.relative_to(self.data)
        aid = str(uuid.uuid4())
        with self.db() as c:
            if metadata.get("sha256"):
                for row in c.execute("SELECT id,path,metadata FROM artifacts WHERE issue_key=? AND kind=?", (issue, kind)):
                    if json.loads(row["metadata"]).get("sha256") == metadata["sha256"]:
                        previous=json.loads(row['metadata']);merged={**previous,**metadata}
                        origins=list(previous.get('origins',[]))
                        for origin in [previous,metadata]:
                            item={k:origin[k] for k in ('jira_attachment_id','source_archive','original_path','archive_sha256') if origin.get(k)}
                            if item and item not in origins:origins.append(item)
                        if origins:merged['origins']=origins
                        c.execute('UPDATE artifacts SET metadata=?,path=? WHERE id=?',(json.dumps(merged,ensure_ascii=False),str(resolved),row['id']))
                        return row["id"]
            c.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                      (aid, issue, kind, str(resolved), json.dumps(metadata, ensure_ascii=False), now()))
            c.execute("UPDATE reports SET status='stale',approved=NULL WHERE issue_key=? AND status IN ('draft','approved')", (issue,))
        return aid

    def artifacts(self, issue):
        rs = self.rows("SELECT * FROM artifacts WHERE issue_key=? ORDER BY created DESC", (issue,))
        text_hashes={json.loads(r['metadata']).get('sha256') for r in rs if r['kind']=='log'}-{None,''}
        image_hashes={json.loads(r['metadata']).get('sha256') for r in rs if r['kind'] in ('image','image_unavailable')}-{None,''}
        output, seen = [], set()
        issue_rows=self.rows("SELECT body FROM issues WHERE key=?",(issue,))
        current_ids={str(a["id"]) for a in json.loads(issue_rows[0]["body"]).get("attachments",[])} if issue_rows else None
        for r in rs:
            r["metadata"] = json.loads(r["metadata"])
            if r['metadata'].get('analysis_excluded'):continue
            if r['kind']=='binary_log' and r['metadata'].get('sha256') in image_hashes|text_hashes:
                continue  # Legacy ZIP imports may have classified this screenshot as binary.
            origins=r['metadata'].get('origins') or [r['metadata']]
            if current_ids is not None and all(o.get('jira_attachment_id') and str(o['jira_attachment_id']) not in current_ids for o in origins):continue
            r['metadata']['active_origins']=[{k:o[k] for k in ('jira_attachment_id','source_archive','original_path','archive_sha256') if k in o} for o in origins if current_ids is None or not o.get('jira_attachment_id') or str(o['jira_attachment_id']) in current_ids]
            identity = (r["kind"], r["metadata"].get("sha256") or r["id"])
            if identity in seen:
                continue
            seen.add(identity)
            r["filename"] = Path(r.pop("path")).name
            output.append(r)
        return output
