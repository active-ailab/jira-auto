import json
import fcntl
import re
import threading
import time
import uuid
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

from .integrations import Jira, digest, fingerprint, issue_key, run_cli
from .materials import fetch_artifacts, import_logs, inspect_manifest, prepare_report
from .store import Store, now


class Service:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.data)
        self.jira = Jira(settings)
        self.stop = threading.Event()
        self.thread = None
        self.last_poll = 0
        self.worker_lock = None
        self.feishu_process = None
        self.feishu_last_start = 0
        self.last_quality_retry = 0

    def start(self):
        self.worker_lock = (self.settings.data / "worker.lock").open("a")
        try:
            fcntl.flock(self.worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.worker_lock.close()
            raise RuntimeError("此数据目录已有正在运行的后台执行器") from None
        self.store.recover()
        self.thread = threading.Thread(target=self.work, daemon=True, name="jira-worker")
        self.thread.start()
        self.maintain_feishu()

    def maintain_feishu(self):
        if self.feishu_process and self.feishu_process.poll() is None:
            return
        if time.monotonic()-self.feishu_last_start<30:
            return
        self.feishu_last_start=time.monotonic()
        from .feishu import load_config
        try:
            if load_config(self.settings):
                self.feishu_process = subprocess.Popen([sys.executable,"-m","jira_service.feishu"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (ValueError,OSError):
            self.store.event("feishu","飞书配置无效，请运行本地配置脚本")

    def close(self):
        self.stop.set()
        if self.feishu_process:
            self.feishu_process.terminate()
            try: self.feishu_process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.feishu_process.kill()
        if self.thread:
            self.thread.join(timeout=2)
        if self.worker_lock and (not self.thread or not self.thread.is_alive()):
            self.worker_lock.close()

    def refresh(self, key):
        issue = self.jira.get(key)
        self.store.upsert_issue(issue, fingerprint(issue))
        return issue

    def sync(self, rid):
        from .reminders import begin_sync
        begin_sync(self.store)
        assigned = self.jira.assigned_updates()
        tracked = {x["key"]: x for x in self.store.issues()}
        keys = [k for k, updated in assigned.items() if k not in tracked or not updated or tracked[k].get("updated") != updated or tracked[k].get("schedule_version")!=1]
        # Previously tracked issues remain watched after closure or reassignment.
        keys += [k for k in tracked if k not in assigned]
        self.store.event("jira", f"读取名下工单及持续跟踪工单，共 {len(keys)} 条", run=rid)
        failed = []
        for index, key in enumerate(keys):
            if self.stop.is_set():
                raise RuntimeError("同步被中断")
            try:
                self.store.event('jira', f'正在读取 {key}', key, rid)
                self.refresh(key)
                self.store.event('jira', f'已读取 {index+1}/{len(keys)}：{key}', key, rid)
            except Exception:
                failed.append(key)
                self.store.event("jira", "读取失败，保留上次快照", key, rid)
            if (index + 1) % 20 == 0:
                self.store.event("jira", f"已读取 {index+1}/{len(keys)}", run=rid)
        if failed:
            raise RuntimeError(f"部分工单读取失败：{', '.join(failed[:12])}；成功快照已保留")
        with self.store.db() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('jira-assigned-snapshot',json.dumps({'keys':list(assigned),'synced_at':now(),'complete':True})))
        from .reminders import ensure_plan
        ensure_plan(self)
        return {"assigned_unresolved": len(assigned), "refreshed": len(keys), "tracked": len(self.store.issues())}

    def source_check(self, key, run_id=None):
        rows = self.store.rows("SELECT path FROM artifacts WHERE issue_key=? AND kind='manifest' ORDER BY created DESC LIMIT 1", (key,))
        if not rows:
            raise ValueError("请先下载版本 XML")
        target = Path(rows[0]["path"]).resolve()
        target.relative_to(self.settings.data)
        info = inspect_manifest(target)
        root = ET.parse(target).getroot()
        results = []
        projects = root.findall('project')
        self.store.event('source', f'核对 {len(projects)} 个仓库提交；只读检查，不执行同步', key, run_id)
        for index, project in enumerate(projects, 1):
            relative = project.get("path") or project.get("name")
            if not relative:
                raise ValueError("manifest project 缺少路径")
            path = (self.settings.source / relative).resolve()
            path.relative_to(self.settings.source)
            expected = project.get("revision")
            if not re.fullmatch(r"[a-fA-F0-9]{40}", expected or ""):
                raise ValueError("manifest 中存在未锁定提交的项目")
            try:
                actual = run_cli(["git", "-C", str(path), "rev-parse", "HEAD"], timeout=10).strip()
                state = "matched" if actual == expected else "different"
            except RuntimeError:
                actual, state = None, "missing"
            results.append({"path": relative, "expected": expected, "actual": actual, "state": state})
            if index % 25 == 0 or index == len(projects):
                self.store.event('source', f'已核对 {index}/{len(projects)}；一致 {sum(x["state"]=="matched" for x in results)} 个', key, run_id,
                                 details={'current':index,'total':len(projects),'unit':'repositories'})
        return {"manifest": info, "matched": sum(x["state"] == "matched" for x in results),
                "total": len(results), "projects": results,
                "note": "只读比较，未切换 manifest 或执行 repo sync"}

    def work(self):
        while not self.stop.is_set():
            self.maintain_feishu()
            if time.monotonic()-self.last_quality_retry>=60:
                self.last_quality_retry=time.monotonic()
                from .quality_client import resume_waiting
                resume_waiting(self)
            if time.monotonic() - self.last_poll >= self.settings.poll_seconds:
                self.store.enqueue("sync")
                self.last_poll = time.monotonic()
            job = self.store.claim()
            if not job:
                self.stop.wait(.5)
                continue
            key, rid = job["issue_key"], job["id"]
            try:
                payload = json.loads(job["payload"])
                self.store.event("start", f"开始 {job['kind']}", key, rid)
                if job["kind"] == "sync":
                    result = self.sync(rid)
                elif job["kind"] == "refresh":
                    result = {"key": self.refresh(key)["key"]}
                elif job["kind"] == "fetch":
                    issue = self.store.issue(key)
                    result = fetch_artifacts(self.settings, self.store, key,
                        payload.get("device") or issue.get("device"),
                        payload.get("version") or issue.get("firmware"), rid)
                elif job['kind'] == 'import_quality_logs':
                    from .quality import import_quality_logs
                    result=import_quality_logs(self,key,payload['path'],rid)
                elif job["kind"] == "import_logs":
                    result = import_logs(self.settings, self.store, key, payload["path"], rid)
                elif job["kind"] == "preflight":
                    from .pipeline import prepare
                    prepare(self,key,rid,payload,with_ai=False)
                    body, evidence = prepare_report(self.store, key,
                        emit=lambda step, message, details: self.store.event(step,message,key,rid,details))
                    self.store.event('draft', '保存材料预检草稿，等待人工进一步分析与审阅', key, rid)
                    report = self.save_report(key, body, report_kind='preflight', run_id=rid)
                    result = {"report_id": report["id"], "evidence": evidence}
                    if self.settings.ai_enabled:
                        result['next_run_id'] = self.store.enqueue('analysis',key,payload)
                        self.store.event('queue','材料预检完成，AI 根因分析已排队',key,rid,{'next_run_id':result['next_run_id']})
                elif job['kind'] == 'analysis':
                    from .pipeline import prepare
                    result = prepare(self,key,rid,payload)
                elif job["kind"] == "source_check":
                    result = self.source_check(key, rid)
                else:
                    raise ValueError("未知任务类型")
                if job["kind"] in {"import_logs", "fetch"}:
                    latest = self.store.rows("SELECT status FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 1", (key,))
                    if not latest or latest[0]["status"] == "stale":
                        active=self.store.rows("SELECT id FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') AND status IN ('queued','running') ORDER BY created LIMIT 1",(key,))
                        next_run = active[0]['id'] if active else self.store.enqueue("preflight", key)
                        result['next_run_id'] = next_run
                        self.store.event('queue', '材料已准备，后续材料预检已排队', key, rid, {'next_run_id':next_run})
                self.store.finish(rid, result=result, error=result.get('failure'))
                self.store.event('failed' if result.get('failure') else 'complete',
                                 '分析执行失败；已保留错误记录，不可作为根因结论审批' if result.get('failure') else '任务完成',key,rid)
            except Exception as exc:
                if isinstance(exc, (ValueError, RuntimeError)):
                    message = str(exc)[:500]
                else:
                    message = f"{type(exc).__name__}：任务失败，请检查本地配置或文件格式"
                self.store.finish(rid, error=message)
                self.store.event("failed", message, key, rid)

    def retry(self, rid, force_from=None):
        from .stages import STAGES
        rows=self.store.rows('SELECT * FROM runs WHERE id=?',(rid,))
        if not rows:raise ValueError('任务不存在')
        previous=rows[0]
        if previous['status'] not in ('failed','interrupted'):raise ValueError('只有失败或中断任务可以续跑')
        if previous['kind'] not in ('analysis','preflight'):raise ValueError('此任务不支持阶段续跑')
        if force_from and force_from not in STAGES:raise ValueError('无效的重跑阶段')
        payload=json.loads(previous['payload']);payload.update({'retry_of':rid})
        if force_from:payload['force_from']=force_from
        else:payload.pop('force_from',None)
        new_id=self.store.enqueue(previous['kind'],previous['issue_key'],payload)
        self.store.event('retry','继续失败任务；复用通过校验的阶段，发布操作不在续跑范围内',previous['issue_key'],new_id,{'retry_of':rid,'force_from':force_from})
        return new_id

    def add_analysis_case(self,key):
        key=issue_key(key);created=False
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            selected=c.execute('SELECT 1 FROM analysis_cases WHERE issue_key=?',(key,)).fetchone()
            c.execute("INSERT OR IGNORE INTO analysis_cases VALUES(?,datetime('now'))",(key,))
            active=c.execute("SELECT id FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') AND status IN ('queued','running') ORDER BY created LIMIT 1",(key,)).fetchone()
            previous=c.execute("SELECT id FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') ORDER BY created DESC LIMIT 1",(key,)).fetchone()
            if active:rid=active['id']
            elif selected and previous:rid=previous['id']
            else:
                rid=str(uuid.uuid4());kind='analysis' if self.settings.ai_enabled else 'preflight'
                c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',(rid,key,kind,'queued','{}',None,None,now(),now()))
                created=True
        if created:self.store.event('queue','已加入分析列表，自动开始读取工单、准备附件并分析；评论仍需审批',key,rid)
        return {'key':key,'in_analysis':True,'run_id':rid,'queued':created}

    def save_report(self, key, body, analysis=None, run_id=None, report_kind='manual', expected_fingerprint=None, source_report_id=None):
        if not body.strip() or len(body) > 50000:
            raise ValueError("草稿应为 1–50000 字符")
        rid = str(uuid.uuid4())
        with self.store.db() as c:
            c.execute("BEGIN IMMEDIATE")
            issue=c.execute('SELECT fingerprint FROM issues WHERE key=?',(key,)).fetchone()
            if not issue:raise ValueError('工单尚未导入')
            if expected_fingerprint and issue['fingerprint']!=expected_fingerprint:
                raise ValueError('分析期间工单发生变化，请重新分析')
            if source_report_id:
                source=c.execute('SELECT * FROM reports WHERE id=? AND issue_key=?',(source_report_id,key)).fetchone()
                latest=c.execute('SELECT id FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 1',(key,)).fetchone()
                if not source or latest['id']!=source_report_id:raise ValueError('已有更新版本，请打开最新报告后编辑')
                if source['fingerprint']!=issue['fingerprint'] or source['status'] in ('stale','superseded','publishing','publication_unknown'):
                    raise ValueError('原分析已过期或结果待核对，请重新审阅')
            if c.execute("SELECT 1 FROM reports WHERE issue_key=? AND status='publishing'", (key,)).fetchone():
                raise ValueError("评论正在发布，请稍后修改")
            c.execute("UPDATE reports SET status='superseded',approved=NULL WHERE issue_key=? AND status IN ('draft','approved')", (key,))
            c.execute("INSERT INTO reports(id,issue_key,fingerprint,body,body_hash,status,created,approved,publication,kind,analysis,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, key, issue["fingerprint"], body, digest(body), "draft", now(), None, None,
                       report_kind,json.dumps(analysis,ensure_ascii=False) if analysis else None,run_id))
        self.store.event("draft", "已保存新草稿；需人工审阅，未写入 Jira", key)
        return {"id": rid, "status": "draft"}

    def request_revision(self, report_id, reason):
        reason=reason.strip()
        if not 1<=len(reason)<=2000:raise ValueError('请填写1–2000字的补充分析要求')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            report=c.execute('SELECT * FROM reports WHERE id=?',(report_id,)).fetchone()
            if not report or report['status'] not in ('draft','approved','published','rejected'):raise ValueError('此版本不能提出补充分析')
            latest=c.execute('SELECT id FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 1',(report['issue_key'],)).fetchone()
            if latest['id']!=report_id:raise ValueError('已有更新版本，请打开最新报告再提出要求')
            if c.execute("SELECT 1 FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') AND status IN ('queued','running')",(report['issue_key'],)).fetchone():raise ValueError('此工单已有分析任务，请等待完成后再提交')
            analysis=json.loads(report['analysis']) if report['analysis'] else {}
            scenes=analysis.get('crash_scenes',[])
            request_id=str(uuid.uuid4());run_id=str(uuid.uuid4());created=now()
            payload={'review_request_id':request_id,'force_from':'ai_analysis','scene_id':scenes[0].get('id') if scenes else None}
            if report['status'] in ('draft','approved'):
                c.execute("UPDATE reports SET status='rejected',approved=NULL WHERE id=?",(report_id,))
            c.execute('INSERT INTO report_reviews VALUES(?,?,?,?,?,?)',(request_id,report_id,report['issue_key'],reason,created,run_id))
            c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',(run_id,report['issue_key'],'analysis','queued',json.dumps(payload),None,None,created,created))
        self.store.event('review_request','已记录补充要求并排队分析；本次不审批或评论',report['issue_key'],run_id,{'report_id':report_id,'reason':reason})
        return {'run_id':run_id,'review_request_id':request_id}

    def approve(self, report_id, origin="Dashboard"):
        with self.store.db() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if not r or r["status"] != "draft":
                raise ValueError("只有当前草稿可以审批")
            if r['kind']=='analysis_failed':
                raise ValueError('本轮分析执行失败，请修复后重跑；不能将执行错误作为根因结论审批')
            if r['kind']=='preflight':
                raise ValueError('材料预检清单不能作为根因结论审批，请先运行 AI 根因分析')
            current = c.execute("SELECT fingerprint FROM issues WHERE key=?", (r["issue_key"],)).fetchone()
            if not current or current["fingerprint"] != r["fingerprint"] or digest(r["body"]) != r["body_hash"]:
                raise ValueError("工单或草稿已变化，请重新生成")
            c.execute("UPDATE reports SET status='approved',approved=? WHERE id=?", (now(), report_id))
        self.store.event("approve", f"用户在 {origin} 批准了此版本草稿", r["issue_key"])

    def publish(self, report_id):
        if not self.settings.jira_write:
            raise ValueError("Jira 回写处于关闭状态")
        rows = self.store.rows("SELECT * FROM reports WHERE id=?", (report_id,))
        if not rows or rows[0]["status"] != "approved":
            raise ValueError("此草稿未获批准或已发布")
        report = rows[0]
        self.refresh(report["issue_key"])
        with self.store.db() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            issue = c.execute("SELECT fingerprint FROM issues WHERE key=?", (r["issue_key"],)).fetchone()
            if r["status"] != "approved" or r["fingerprint"] != issue["fingerprint"] or digest(r["body"]) != r["body_hash"]:
                raise ValueError("工单或审批已变化，请重新审阅")
            c.execute("UPDATE reports SET status='publishing' WHERE id=?", (report_id,))
        try:
            self.jira.comment(r["issue_key"], r["body"] + f"\n\n[jira-auto:{report_id}]")
        except Exception:
            with self.store.db() as c:
                c.execute("UPDATE reports SET status='publication_unknown' WHERE id=?", (report_id,))
            self.store.event("publish", "发布结果未知，禁止自动重试，请在 Jira 核对标记", r["issue_key"])
            raise ValueError("发布结果未知，请到 Jira 核对，避免重复评论")
        with self.store.db() as c:
            c.execute("UPDATE reports SET status='published',publication=? WHERE id=?", (now(), report_id))
        self.store.event("publish", "已通过 Jira CLI 发布批准的评论", r["issue_key"])
        return {"status": "published", "marker": f"jira-auto:{report_id}",
                "note": "CLI 未提供 Comment ID；使用唯一标记核对。工单状态未流转。"}

    def check_publication(self,report_id):
        rows=self.store.rows('SELECT * FROM reports WHERE id=?',(report_id,))
        if not rows or rows[0]['status']!='publication_unknown':raise ValueError('仅核对发布结果未知的版本')
        original=rows[0]
        evidence=self.jira.publication_evidence(original['issue_key'],report_id,original['body'])
        notes={'exact_match':'已通过Jira CLI找到唯一标记且文案完全一致的评论，已登记为发布成功；未新增评论',
               'body_changed':'找到标记，但远端评论与批准文案不同；保留结果未知，请人工核对',
               'duplicate_markers':'发现多个相同标记，请人工核对重复评论；未自动修改或重发',
               'not_seen':'本次返回的评论中未找到标记，仍不能认定未发布；保留结果未知，禁止自动重发'}
        state=evidence['state']
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT * FROM reports WHERE id=?',(report_id,)).fetchone()
            if current['status']!='publication_unknown' or current['body_hash']!=original['body_hash'] or digest(current['body'])!=current['body_hash']:
                raise ValueError('核对期间报告变化，请重新读取')
            if state=='exact_match':
                c.execute("UPDATE reports SET status='published',publication=? WHERE id=?",(evidence.get('created') or now(),report_id))
        self.store.event('publication_check',notes[state],original['issue_key'],details={'report_id':report_id,**evidence})
        return {'status':'published' if state=='exact_match' else 'publication_unknown','note':notes[state],'evidence':evidence}

    def detail(self, key, report_id=None, compact=False):
        issue = self.store.issue(issue_key(key))
        from .review import projection
        reports=self.store.rows('SELECT * FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 15',(key,))
        selected=reports[0] if reports else None
        if report_id:
            rows=self.store.rows('SELECT * FROM reports WHERE issue_key=? AND id=?',(key,report_id))
            if not rows:raise ValueError('报告不存在或不属于该工单')
            selected=rows[0]
        previous=self.store.rows('SELECT id,body,status,created FROM reports WHERE issue_key=? AND created<? ORDER BY created DESC LIMIT 1',(key,selected['created'])) if selected else []
        detail={"review":projection(selected) if selected else None,"selected_report_id":selected['id'] if selected else None,
                "selected_report":selected,"previous_report":previous[0] if previous else None,
                "latest_report_id":reports[0]['id'] if reports else None,
                "report_history":self.store.rows('SELECT id,kind,status,created FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 100',(key,)),
                "review_requests":self.store.rows('SELECT report_id,reason,created,run_id FROM report_reviews WHERE issue_key=? ORDER BY created DESC LIMIT 10',(key,)),"issue": issue, "quality_collection":self.quality_collection(key), "artifacts": self.store.artifacts(key),
                "runs": self.store.rows("SELECT * FROM runs WHERE issue_key=? ORDER BY created DESC LIMIT 30", (key,)),
                "reports": reports,
                "events": self.store.rows("SELECT * FROM events WHERE issue_key=? ORDER BY id DESC LIMIT 100", (key,)),
                "scene_inventory": self.latest_scenes(key)}
        if compact:
            # Load full evidence only for the selected revision, not every past model run.
            if selected:detail['selected_report']={k:v for k,v in selected.items() if k!='analysis'}
            detail['reports']=[{k:v for k,v in r.items() if k not in ('analysis','body')} for r in reports]
            detail['runs']=[{k:v for k,v in r.items() if k not in ('payload','result')} for r in detail['runs']]
            detail['events']=[{k:v for k,v in e.items() if k!='details'} for e in detail['events']]
        if selected and not any(r['id']==selected['id'] for r in detail['report_history']):
            detail['report_history'].append({k:selected[k] for k in ('id','kind','status','created')})
        return detail

    def quality_collection(self,key):
        rows=self.store.rows('SELECT value FROM settings WHERE key=?',('quality-case:'+key,))
        return json.loads(rows[0]['value']) if rows else None

    def latest_scenes(self,key):
        rows=self.store.rows("SELECT result FROM run_stages WHERE issue_key=? AND name='scene_inventory' AND status='completed' ORDER BY updated DESC LIMIT 1",(key,))
        return json.loads(rows[0]['result'])['value'] if rows else None
