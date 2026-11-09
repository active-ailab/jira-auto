"""Quality-platform session and direct HTTP collection; no browser cookie scraping."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError,URLError
from http.client import HTTPException
from urllib.parse import urlencode,urlparse,unquote
from urllib.request import Request,build_opener,HTTPRedirectHandler
import zipfile
from .materials import sha256
from .store import now

# Storage buckets observed on authenticated production feedback; expand only after verifying a new origin.
STORAGE_HOSTS={'zepp-us3-1311088246.cos.na-ashburn.myqcloud.com','zepp-cn3-1311088246.cos.ap-guangzhou.myqcloud.com'}
# Path-style S3 endpoints serve many unrelated tenants: approve the bucket too.
# Observed in the authenticated quality-platform download response, not user text.
S3_PATH_BUCKETS={'s3.eu-central-1.amazonaws.com':{'huami-de'}}
API='https://api.zepp.top'
AUTH='https://open.zepp.top/auth'


class QualityError(RuntimeError):
    def __init__(self,state,message):self.state=state;super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def response(url,headers=None):
    try:return build_opener(NoRedirect).open(Request(url,headers=headers or {}),timeout=45)
    except HTTPError as exc:
        if exc.code in (401,403) and headers and headers.get('Authorization'):raise QualityError('auth_required','质量平台登录已失效，请在接入与配置重新连接') from None
        raise QualityError('network_error','质量平台请求失败（HTTP '+str(exc.code)+'）') from None
    except (URLError,TimeoutError,OSError):raise QualityError('network_error','质量平台网络请求失败；可从采集阶段重试') from None


def get_json(url,token):
    try:
        with response(url,{'Authorization':token,'ClientName':'qaplatform','Accept':'application/json'}) as r:
            raw=r.read(4*1024*1024+1)
    except (OSError,HTTPException):raise QualityError('network_error','质量平台响应读取中断，可重试查询') from None
    if len(raw)>4*1024*1024:raise QualityError('invalid_response','质量平台响应过大，未读取为有效清单')
    try:data=json.loads(raw)
    except (ValueError,UnicodeError):raise QualityError('invalid_response','质量平台未返回有效JSON，可能需要重新登录') from None
    if isinstance(data,dict) and data.get('status') is False:
        message=str(data.get('message','')).lower()
        if any(x in message for x in ('auth','token','secret','login','登录','权限')):raise QualityError('auth_required','质量平台认证失败，请在接入与配置重新连接')
    return data


def auth_status(settings):
    try:
        data=json.loads((settings.data/'quality-auth.json').read_text())
        if not data.get('token') or data.get('state')=='auth_required' or (data.get('expires') and data['expires']<=time.time()):raise ValueError()
        return {'name':'质量平台','state':'ready','detail':'后台接口采集原始日志 → Jira归档 → 自动分析；登录会话已保存本机'}
    except (OSError,ValueError,TypeError):return {'name':'质量平台','state':'auth_required','detail':'需连接一次质量平台账号；之后由后台采集，登录失效会明确提示'}


def save_auth(settings,data):
    target=settings.data/'quality-auth.json';temporary=target.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f)
    temporary.chmod(0o600);temporary.replace(target)


def begin_login(settings,store,origin):
    state=secrets.token_urlsafe(32);key='quality-login:'+hashlib.sha256(state.encode()).hexdigest()
    callback=origin+'/quality/callback/'+state
    with store.db() as c:c.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps({'created':time.time()})))
    return {'login_url':AUTH+'/login?'+urlencode({'referer':callback,'clientName':'qaplatform'})}


def finish_login(settings,store,state,ticket):
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',state) or not ticket or len(ticket)>4096:raise ValueError('登录回调无效，请重新连接')
    key='quality-login:'+hashlib.sha256(state.encode()).hexdigest()
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        c.execute('DELETE FROM settings WHERE key=?',(key,))
    if not row or time.time()-json.loads(row['value'])['created']>600:raise ValueError('登录请求已过期或已使用，请重新连接')
    result=get_json(AUTH+'/verify',ticket)
    if not isinstance(result,dict):raise ValueError('登录授权响应格式无效')
    envelope=result.get('data')
    if isinstance(envelope,str):
        try:
            encoded=envelope.split('.')[1] if envelope.count('.')==2 else envelope
            envelope=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        except (ValueError,UnicodeError):raise ValueError('登录结果格式无效，未保存会话') from None
    if not isinstance(envelope,dict):raise ValueError('未取得登录授权结果')
    identity=envelope.get('data',envelope)
    if not isinstance(identity,dict):raise ValueError('登录身份格式无效')
    token=identity.get('access_token')
    if not identity.get('access') or not isinstance(token,str) or not token:raise ValueError('质量平台未授予访问权限')
    save_auth(settings,{'token':token,'expires':envelope.get('exp'),'state':'ready','connected':now()})
    store.event('quality_auth','质量平台登录已连接；后台可以直接读取日志清单',None)


def token_for(settings):
    if auth_status(settings)['state']!='ready':raise QualityError('auth_required','质量平台尚未连接或登录已失效，请在接入与配置连接')
    return json.loads((settings.data/'quality-auth.json').read_text())['token']


def feedback_files(settings,feedback):
    token=token_for(settings);fid=feedback['id'];app=feedback['app_name']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',fid) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',app):raise QualityError('invalid_feedback','反馈标识无效')
    listing=get_json(API+'/api/feedback/list?'+urlencode({'id':fid,'app_name':app,'env':'production','page':1,'page_size':20}),token)
    if not isinstance(listing,dict) or not isinstance(listing.get('detail'),list):raise QualityError('invalid_response','反馈清单格式无效')
    rows=listing['detail']
    matches=[r for r in rows if isinstance(r,dict) and str(r.get('id'))==str(fid)]
    if len(matches)!=1:raise QualityError('feedback_missing','质量平台未返回唯一对应反馈，未下载其他用户材料')
    row=matches[0]
    if not all(row.get(k) is not None for k in ('index','user_id','sync_time')):raise QualityError('invalid_response','反馈缺少下载所需的索引、用户或同步时间')
    detail=get_json(API+'/api/feedback/download?'+urlencode({k:row[k] for k in ('id','index','user_id','sync_time')}),token)
    if not isinstance(detail,dict):raise QualityError('invalid_response','日志下载清单格式无效')
    files=[]
    # raw URL may be a direct archive or the exact member list used by Download ZIP.
    for field in ('url','APP_OOM_DATA','app','fb'):
        value=detail.get(field)
        if isinstance(value,str) and value:value=[{'url':value,'file_name':field+'.zip'}]
        for item in value if isinstance(value,list) else []:
            if not isinstance(item,dict):raise QualityError('invalid_response','日志清单项格式无效')
            url=item.get('url') or item.get('_url')
            if url and not isinstance(url,str):raise QualityError('invalid_response','日志地址格式无效')
            if url:files.append({'url':url,'filename':item.get('file_name') or field+'.zip','role':field})
    if not files:
        state=str(detail.get('state','pending'))
        if state in ('failed','account_deletion','empty_url'):raise QualityError('no_logs','平台未提供可下载日志（'+state+'）；已查询，不能解释为软件根因')
        raise QualityError('pending','平台日志暂未就绪，保留收集状态，稍后可继续查询')
    if len(files)>100:raise QualityError('invalid_response','平台返回日志文件过多，未开始下载')
    return files,{'feedback_id':fid,'app_name':app,'index':str(row['index']),'files':len(files)}


def validate_download_url(url):
    try:
        parsed=urlparse(url);host=(parsed.hostname or '').lower();port=parsed.port
    except ValueError:raise QualityError('unsupported_host','日志地址格式无效') from None
    segments=unquote(parsed.path).split('/')
    s3_allowed=(host in S3_PATH_BUCKETS and len(segments)>2 and
                parsed.path.split('/')[1] in S3_PATH_BUCKETS[host] and
                segments[1] in S3_PATH_BUCKETS[host] and
                not any(part in ('.','..') for part in segments) and
                '\\' not in unquote(parsed.path) and bool('/'.join(segments[2:])))
    allowed=(host in STORAGE_HOSTS or s3_allowed or any(host==d or host.endswith('.'+d) for d in ('zepp.top','zepp.com','huami.com')))
    if parsed.scheme!='https' or parsed.username or parsed.password or port not in (None,443) or not allowed:
        safe_host=re.sub(r'[^A-Za-z0-9.-]','?',host)[:150] or '无有效主机'
        raise QualityError('unsupported_host',f'平台返回了未支持的日志存储位置（主机：{safe_host}），未向该地址发送请求')


def collect(service,key,rid,archive_only=False):
    settings,store=service.settings,service.store;issue=store.issue(key);feedback=issue.get('feedback')
    collection_key=('quality-probe:' if archive_only else 'quality-case:')+key
    if not feedback:return {'state':'not_quality'}
    def mark(state,message,extra=None):
        rows=store.rows('SELECT value FROM settings WHERE key=?',(collection_key,));old=json.loads(rows[0]['value']) if rows else {}
        if old.get('feedback_id')!=feedback['id'] or old.get('app_name')!=feedback['app_name']:old={}
        metadata={**old,**(extra or {}),'feedback_id':feedback['id'],'app_name':feedback['app_name'],'state':state,'message':message,'run_id':rid,'updated':now(),'attempts':old.get('attempts',0)+(1 if state=='listing' else 0),'retry_after':time.time()+300}
        with store.db() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(collection_key,json.dumps(metadata)))
        store.event('quality_collect',message,key,rid,metadata)
    # Immutable completed download may be reused after upload or processing interruption.
    cached=store.rows('SELECT value FROM settings WHERE key=?',(collection_key,))
    cache=json.loads(cached[0]['value']) if cached else {}
    if cache.get('feedback_id')!=feedback['id'] or cache.get('app_name')!=feedback['app_name']:cache={}
    candidate=(settings.data/cache.get('archive','__missing__')).resolve();candidate.relative_to(settings.data)
    if cache.get('feedback_id')==feedback['id'] and cache.get('app_name')==feedback['app_name'] and cache.get('sha256') and candidate.is_file() and sha256(candidate)==cache['sha256']:
        path=candidate;metadata=cache
    else:
        mark('listing','正在查询质量平台反馈及原始日志清单')
        try:
            files,metadata=feedback_files(settings,feedback)
            # Reject unsupported later members before downloading any earlier one.
            for item in files:validate_download_url(item['url'])
            directory=settings.data/'cases'/key/'quality'/'background';directory.mkdir(parents=True,exist_ok=True)
            path=directory/('quality_'+feedback['id']+'.zip');temporary=path.with_suffix('.zip.part');total=0
            try:
                with zipfile.ZipFile(temporary,'w',zipfile.ZIP_STORED) as out:
                    for i,item in enumerate(files):
                        validate_download_url(item['url']);name=re.sub(r'[^A-Za-z0-9._-]','_',Path(item['filename'].replace('\\','/')).name)[:160] or 'file'
                        mark('downloading',f'质量日志下载：{i+1}/{len(files)} · {name}',{'current':i,'total':len(files)})
                        # Download URLs are signed; never send the SSO credential to storage hosts.
                        with response(item['url']) as source,out.open(f'{i:03d}-'+name,'w') as dest:
                            expected=source.headers.get('Content-Length');received=0;reported=0
                            prefix=source.read(1024)
                            if prefix.lstrip().lower().startswith((b'<html',b'<!doctype html')):raise QualityError('invalid_response','日志地址返回HTML页面，未当作日志归档')
                            while prefix:
                                total+=len(prefix);received+=len(prefix)
                                if total>512*1024*1024:raise QualityError('too_large','质量日志下载总大小超过512MiB')
                                dest.write(prefix)
                                if received-reported>=8*1024*1024:
                                    reported=received
                                    mark('downloading',f'质量日志下载：{i+1}/{len(files)} · {name} · 已下载 {received//1024//1024} MiB',{'current':i,'total':len(files),'file_bytes':received,'downloaded_bytes':total})
                                prefix=source.read(1024*1024)
                            if not received or (expected and (not expected.isdigit() or received!=int(expected))):raise QualityError('network_error','日志文件为空或下载长度不完整，未归档')
                path=directory/('quality_'+feedback['id']+'_'+sha256(temporary)[:16]+'.zip')
                temporary.replace(path)
            finally:temporary.unlink(missing_ok=True)
            metadata={**metadata,'archive':str(path.relative_to(settings.data)),'sha256':sha256(path),'bytes':path.stat().st_size}
            mark('downloaded','质量平台原始日志已下载并打包',metadata)
        except (OSError,HTTPException) as exc:
            mark('network_error','质量日志下载中断，未将不完整文件归档');raise QualityError('network_error','质量日志下载中断，未将不完整文件归档') from None
        except QualityError as exc:
            if exc.state=='auth_required':
                try:
                    auth=json.loads((settings.data/'quality-auth.json').read_text());auth['state']='auth_required';save_auth(settings,auth)
                except (OSError,ValueError):pass
            mark(exc.state,str(exc));raise
    if archive_only:return {'state':'downloaded','path':str(path),'metadata':metadata}
    from .materials import import_logs
    from .quality import attach_quality_archive
    try:
        mark('inspecting','正在解包检查每个质量日志文件',metadata)
        import_logs(settings,store,key,path,rid,{'source_platform':'quality','feedback_id':feedback['id']})
        mark('archiving','正在将质量日志归档到Jira',metadata)
        upload=attach_quality_archive(service,key,path,rid)
        refreshed=service.refresh(key)
        if not any(str(x['id'])==upload['attachment_id'] for x in refreshed['attachments']):raise QualityError('archive_pending','日志已上传，但CLI尚未确认附件清单；未进入分析')
    except QualityError as exc:
        mark(exc.state,str(exc),metadata);raise
    except Exception:
        # Unknown POST outcomes are protected by the durable upload receipt; no automatic retry.
        mark('archive_failed','质量日志本地检查或Jira归档未完成，请查看任务错误；不会自动重复上传',metadata);raise
    mark('archived','质量日志已归档Jira，继续分析',{**metadata,'attachment_id':upload['attachment_id']})
    return {'state':'archived','attachment_id':upload['attachment_id']}



def resume_waiting(service,after_login=False):
    """Bounded read/collection retries for explicitly selected cases, never publication."""
    connected=''
    if auth_status(service.settings)['state']=='ready':
        try:connected=json.loads((service.settings.data/'quality-auth.json').read_text()).get('connected','')
        except (OSError,ValueError):pass
    for row in service.store.rows("SELECT s.key,s.value FROM settings s JOIN analysis_cases a ON s.key='quality-case:'||a.issue_key"):
        state=json.loads(row['value']);key=row['key'].split(':',1)[1]
        eligible=((after_login or (connected and connected>state.get('updated',''))) and state.get('state')=='auth_required') or (not after_login and state.get('state') in ('pending','network_error','archive_pending') and state.get('retry_after',0)<=time.time() and state.get('auto_retries',0)<2)
        if not eligible:continue
        latest=service.store.rows("SELECT id,status,kind FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight') ORDER BY created DESC LIMIT 1",(key,))
        if latest and latest[0]['status'] in ('failed','interrupted'):
            try:service.retry(latest[0]['id'])
            except ValueError:continue
            state['retry_after']=time.time()+300
            if not after_login:state['auto_retries']=state.get('auto_retries',0)+1
            with service.store.db() as c:c.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(state),row['key']))
