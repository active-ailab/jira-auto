import json
import os
import secrets
import shutil
import uuid
import re
import base64
import ipaddress
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .integrations import issue_key, issue_reference
from .materials import log_evidence
from .service import Service
from .settings import Settings
from .feishu import status as feishu_status
from .quality_client import auth_status as quality_status,begin_login,finish_login,QualityError


class KeyInput(BaseModel):
    key: str


class JobInput(BaseModel):
    kind: str
    device: str | None = None
    version: str | None = None
    scene_id: str | None = Field(default=None,pattern=r"^[a-f0-9]{16}$")


class RetryInput(BaseModel):
    force_from: str | None = None


class DraftInput(BaseModel):
    body: str = Field(min_length=1, max_length=50000)
    source_report_id: str | None = None


class RevisionRequest(BaseModel):
    reason: str = Field(min_length=1,max_length=2000)


class ApprovalInput(BaseModel):
    publish: bool = False


def create_app(settings=None, start_worker=True):
    settings = settings or Settings.load()
    service = Service(settings)
    token_path = settings.data / "api-token"
    if token_path.exists():
        token = token_path.read_text().strip()
    else:
        token = secrets.token_urlsafe(32)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)

    lan_mode = settings.host not in ("127.0.0.1", "localhost", "::1")
    password = None
    if lan_mode and settings.lan_auth:
        password_path = settings.data / "lan-password"
        if not password_path.exists():
            fd = os.open(password_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(24))
        password_path.chmod(0o600)
        password = password_path.read_text().strip()
        if len(password) < 16:
            raise ValueError("局域网访问密码至少应有 16 个字符")

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            service.start()
        yield
        service.close()

    app = FastAPI(title="Jira Auto", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def local_boundary(request, call_next):
        try:
            loopback = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            loopback = False
        if lan_mode and settings.lan_auth and not loopback:
            try:
                scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
                supplied = base64.b64decode(encoded, validate=True).decode()
                username, supplied_password = supplied.split(":", 1)
                authenticated = (scheme.lower() == "basic" and secrets.compare_digest(username.encode(), b"admin")
                                 and secrets.compare_digest(supplied_password.encode(), password.encode()))
            except (ValueError, UnicodeError):
                authenticated = False
            if not authenticated:
                return JSONResponse({"detail": "请使用局域网访问账号登录"}, 401,
                                    headers={"WWW-Authenticate": 'Basic realm="Jira Auto", charset="UTF-8"', "Cache-Control": "no-store"})
        origin = request.headers.get("origin")
        allowed = {f"{scheme}://{host}:{settings.port}" for host in settings.allowed_hosts
                   if "*" not in host for scheme in ("http", "https")}
        if origin and origin not in allowed:
            return JSONResponse({"detail": "请求来源不在允许的服务地址列表中"}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-jira-auto-token", ""), token):
                return JSONResponse({"detail": "缺少本地操作凭据"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(QualityError)
    async def quality_error(request,exc):
        return JSONResponse({'detail':str(exc),'state':exc.state},409)

    @app.post('/api/quality/login')
    def quality_login(request: Request):
        return begin_login(settings,service.store,str(request.base_url).rstrip('/'))

    @app.get('/quality/callback/{state}')
    def quality_callback(state: str,ticket: str=''):
        finish_login(settings,service.store,state,ticket)
        from .quality_client import resume_waiting
        resume_waiting(service,after_login=True)
        return RedirectResponse('/#quality=connected',status_code=303)

    @app.get("/api/session")
    def session():
        return {"token": token}

    @app.get("/api/status")
    def status():
        return {"version": "0.1.0", "poll_seconds": settings.poll_seconds,
                "jira_write": settings.jira_write, "source": str(settings.source), "lan_mode": lan_mode,
                "lan_auth": settings.lan_auth,
                "active_count": service.store.rows("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued','running')")[0]['n'],
                "active_runs": service.store.rows("SELECT id,issue_key,kind,status,error,created,updated FROM runs WHERE status IN ('queued','running') ORDER BY created LIMIT 100"),
                "integrations": [
                    {"name": "Jira CLI", "state": "ready" if shutil.which(settings.jira_bin) else "missing", "detail": "本地 CLI · 每轮同步持续跟踪转出工单"},
                    {"name": "elf-fetch", "state": "ready" if shutil.which(settings.elf_bin) else "missing", "detail": "XML / ELF / MAP 下载与校验"},
                    quality_status(settings),
                    feishu_status(settings, service.store),
                    {"name": "AI 根因分析", "state": "ready" if settings.ai_enabled and shutil.which(settings.codex_bin) else "pending", "detail": "已登录 Codex → 证据评估 → 定向检索 → 根因判断 → 待审评论草稿"},
                    {"name": "源码同步", "state": "verify", "detail": "可比较 manifest 与源码提交；自动 repo sync 待接入"}],
                "runs": service.store.rows("SELECT id,issue_key,kind,status,error,created,updated FROM runs ORDER BY created DESC LIMIT 30"),
                "events": service.store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 30")}

    @app.get('/api/focus')
    def daily_focus():
        from .reminders import focus
        return focus(service)

    @app.get("/api/issues")
    def issues():
        selected={r["issue_key"] for r in service.store.rows("SELECT issue_key FROM analysis_cases")}
        latest={r['issue_key']:r for r in service.store.rows('''SELECT issue_key,status,kind FROM
            (SELECT issue_key,status,kind,ROW_NUMBER() OVER(PARTITION BY issue_key ORDER BY created DESC) AS position FROM reports)
            WHERE position=1''')}
        analysis_runs={r['issue_key']:r for r in service.store.rows('''SELECT id,issue_key,status,kind,error FROM
            (SELECT id,issue_key,status,kind,error,ROW_NUMBER() OVER(PARTITION BY issue_key ORDER BY (status IN ('queued','running')) DESC,created DESC) AS position
             FROM runs WHERE kind IN ('analysis','preflight')) WHERE position=1''')}
        items = service.store.issues()
        for i in items:
            i.pop("description", None)
            i["in_analysis"] = i["key"] in selected
            report=latest.get(i['key']) or {}
            i['report_status']=report.get('status');i['report_kind']=report.get('kind')
            i['reviewable']=report.get('status')=='draft' and report.get('kind') not in ('analysis_failed','preflight')
            i['analysis_state']=report.get('kind') if report.get('status')=='draft' and report.get('kind') in ('analysis_failed','preflight') else report.get('status')
            i['analysis_run']=analysis_runs.get(i['key'])
        return {"issues": items}

    @app.post("/api/sync")
    def sync():
        return {"run_id": service.store.enqueue("sync")}

    @app.post("/api/issues")
    def add_issue(body: KeyInput):
        key = issue_reference(body.key)
        return service.add_analysis_case(key)

    @app.post("/api/issues/{key}/analysis-list")
    def select_analysis(key: str):
        key=issue_key(key)
        service.store.issue(key)
        return service.add_analysis_case(key)

    @app.delete("/api/issues/{key}/analysis-list")
    def unselect_analysis(key: str):
        key=issue_key(key)
        with service.store.db() as c:
            c.execute("DELETE FROM analysis_cases WHERE issue_key=?",(key,))
        return {"key":key,"in_analysis":False}

    @app.get("/api/issues/{key}")
    def detail(key: str, report_id: str | None = None, compact: bool = False):
        return service.detail(key,report_id,compact)

    @app.post("/api/issues/{key}/jobs")
    def queue(key: str, body: JobInput):
        issue_key(key)
        service.store.issue(key)
        if body.kind not in {"refresh", "fetch", "preflight", "source_check", "analysis"}:
            raise ValueError("不支持的任务类型")
        if body.kind in ('analysis','preflight'):
            other=service.store.rows("SELECT id FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') AND kind!=? AND status IN ('queued','running')",(key,body.kind))
            if other:raise ValueError('此工单已有预处理或分析任务，请先查看执行过程并等待完成')
        return {"run_id": service.store.enqueue(body.kind, key, {"device": body.device, "version": body.version,"scene_id":body.scene_id})}

    @app.post("/api/issues/{key}/logs")
    async def upload(key: str, file: UploadFile):
        issue_key(key)
        service.store.issue(key)
        directory = settings.data / "cases" / key / "uploads" / str(uuid.uuid4())
        directory.mkdir(parents=True, exist_ok=True)
        original=Path((file.filename or 'quality_logs.zip').replace('\\','/')).name
        original=re.sub(r'[^A-Za-z0-9._-]','_',original)[:160]
        path = directory / (original if original.lower().endswith('.zip') else original+'.zip')
        size = 0
        try:
            with path.open("xb") as f:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 128 * 1024 * 1024:
                        raise ValueError("上传 ZIP 不能超过 128 MB")
                    f.write(chunk)
            if not size:
                raise ValueError("文件为空")
            active = service.store.rows("SELECT id FROM runs WHERE issue_key=? AND kind IN ('import_logs','import_quality_logs') AND status IN ('queued','running')", (key,))
            if active:
                raise ValueError("已有日志导入任务，请完成后重试")
            kind='import_quality_logs' if service.store.issue(key).get('feedback') else 'import_logs'
            return {"run_id": service.store.enqueue(kind, key, {"path": str(path)})}
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get("/api/issues/{key}/evidence")
    def evidence(key: str, q: str = "", limit: int = 30):
        service.store.issue(issue_key(key))
        if len(q) > 200:
            raise ValueError("搜索词过长")
        return log_evidence(service.store, key, q or None, max(1, min(limit, 100)))

    @app.post("/api/runs/{rid}/retry")
    def retry(rid: str, body: RetryInput):
        return {"run_id":service.retry(rid,body.force_from)}

    @app.post('/api/runs/{rid}/cancel')
    def cancel_run(rid: str):
        return service.store.cancel_queued(rid)

    @app.get("/api/runs/{rid}")
    def run(rid: str):
        rows = service.store.rows("SELECT * FROM runs WHERE id=?", (rid,))
        if not rows:
            raise HTTPException(404, "任务不存在")
        r = rows[0]
        r["result"] = json.loads(r["result"]) if r["result"] else None
        r.pop("payload")
        r["stages"] = service.store.rows("SELECT name,status,error,reused_from,started,updated FROM run_stages WHERE run_id=? ORDER BY started",(rid,))
        r["events"] = service.store.rows("SELECT * FROM events WHERE run_id=? ORDER BY id", (rid,))
        if r['status']=='queued':
            r['ahead_count']=service.store.rows("SELECT COUNT(*) AS n FROM runs WHERE status='running' OR (status='queued' AND created<?)",(r['created'],))[0]['n']
        return r

    @app.get('/api/runs/{rid}/stream')
    async def run_stream(rid: str, request: Request, after: int = 0):
        initial = run(rid)
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(400, '事件游标无效')
        if cursor < 0:
            raise HTTPException(400, '事件游标无效')

        def frame(kind, data, event_id=None):
            return (f'id: {event_id}\n' if event_id is not None else '') + f'event: {kind}\ndata: '+json.dumps(data, ensure_ascii=False)+'\n\n'

        async def generate():
            nonlocal cursor
            initial.pop('events', None)
            yield frame('snapshot', initial)
            last_status = initial['status']
            while not await request.is_disconnected():
                events = service.store.rows('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 500', (rid,cursor))
                for event in events:
                    cursor = event['id']
                    yield frame('trace',event,event['id'])
                current = service.store.rows('SELECT status,updated,result,error FROM runs WHERE id=?', (rid,))[0]
                terminal = current['status'] not in ('queued','running')
                if current['status'] != last_status or terminal:
                    current['result'] = json.loads(current['result']) if current['result'] else None
                    current['stages']=service.store.rows('SELECT name,status,error,reused_from,started,updated FROM run_stages WHERE run_id=? ORDER BY started',(rid,))
                    yield frame('state',current)
                    last_status = current['status']
                if terminal:
                    # Flush a terminal event committed immediately after the run status update.
                    tail = service.store.rows('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id', (rid,cursor))
                    for event in tail:
                        yield frame('trace',event,event['id'])
                    yield frame('done',{'status':current['status']})
                    break
                yield ': heartbeat\n\n'
                await asyncio.sleep(.5)

        return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.post("/api/issues/{key}/reports")
    def draft(key: str, body: DraftInput):
        issue_key(key)
        if body.source_report_id:
            rows=service.store.rows('SELECT * FROM reports WHERE id=? AND issue_key=?',(body.source_report_id,key))
            if not rows:raise ValueError('原始草稿不存在或不属于该工单')
            source=rows[0]
            latest=service.store.rows('SELECT id FROM reports WHERE issue_key=? ORDER BY created DESC LIMIT 1',(key,))
            if not latest or latest[0]['id']!=source['id']:
                raise ValueError('已有更新版本，请打开最新报告后编辑')
            if source['fingerprint']!=service.store.issue(key)['fingerprint'] or source['status']=='stale':
                raise ValueError('原分析已过期，请重新分析后编辑')
            analysis=json.loads(source['analysis']) if source['analysis'] else None
            if analysis:analysis['manual_edit']=True
            kind='analysis_revision' if source['kind'].startswith('analysis') and source['kind']!='analysis_failed' else source['kind']
            return service.save_report(key,body.body,analysis=analysis,run_id=source['run_id'],report_kind=kind,
                                       expected_fingerprint=source['fingerprint'],source_report_id=source['id'])
        return service.save_report(key, body.body)

    @app.post("/api/reports/{rid}/request-revision")
    def request_revision(rid: str, body: RevisionRequest):
        return service.request_revision(rid,body.reason)

    @app.post("/api/reports/{rid}/approve")
    def approve(rid: str, body: ApprovalInput = ApprovalInput()):
        if body.publish and not settings.jira_write:
            raise ValueError('Jira 回写未启用，不能批准并发布')
        service.approve(rid)
        return service.publish(rid) if body.publish else {"status": "approved"}

    @app.post("/api/reports/{rid}/publish")
    def publish(rid: str):
        return service.publish(rid)

    @app.post('/api/reports/{rid}/check-publication')
    def check_publication(rid: str):
        return service.check_publication(rid)

    @app.get("/api/artifacts/{aid}/download")
    def download(aid: str):
        rows = service.store.rows("SELECT path,kind FROM artifacts WHERE id=?", (aid,))
        if not rows:
            raise HTTPException(404, "文件不存在")
        p = Path(rows[0]["path"]).resolve()
        p.relative_to(settings.data)
        if not p.is_file():
            raise HTTPException(404, "文件已移走")
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")

    @app.get('/api/artifacts/{aid}/preview')
    def preview_image(aid: str, sha256: str):
        from .images import inspect_image
        rows=service.store.rows('SELECT path,kind,metadata FROM artifacts WHERE id=?',(aid,))
        if not rows or rows[0]['kind']!='image':raise HTTPException(404,'截图不存在或不能预览')
        path=Path(rows[0]['path']).resolve();path.relative_to(settings.data)
        if not path.is_file():raise HTTPException(404,'截图已移走')
        info=inspect_image(path);saved=json.loads(rows[0]['metadata'])
        if info['sha256']!=sha256 or info['sha256']!=saved.get('sha256'):
            raise ValueError('截图已变化，不能替换报告中的原始证据')
        return FileResponse(path,media_type=info['media_type'],content_disposition_type='inline')

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    return app


def main():
    import uvicorn
    settings = Settings.load()
    os.umask(0o077)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
