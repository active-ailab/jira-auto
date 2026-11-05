import base64
import hashlib
import io
import json
import time
import zipfile
from urllib.parse import parse_qs,urlparse
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jira_service import quality_client as q
from jira_service.app import create_app
from jira_service.settings import Settings
from jira_service.service import Service


def service(tmp_path):
    s=Service(Settings(tmp_path,tmp_path/'source',jira_write=True))
    s.store.upsert_issue({'key':'ABS-1','attachments':[],'feedback':{'id':'feedback','app_name':'zepp'},'firmware':None,'device':None},'fp')
    q.save_auth(s.settings,{'token':'private-token','expires':time.time()+3600})
    return s


def test_login_nonce_jwt_private_storage_and_no_replay(tmp_path,monkeypatch):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    data={'data':{'access':True,'access_token':'secret-session'},'exp':time.time()+3600}
    payload=base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    monkeypatch.setattr(q,'get_json',lambda url,token:{'data':'header.'+payload+'.signature'})
    with TestClient(app) as c:
        assert c.post('/api/quality/login').status_code==403
        h={'x-jira-auto-token':c.get('/api/session').json()['token']}
        url=c.post('/api/quality/login',headers=h).json()['login_url']
        callback=parse_qs(urlparse(url).query)['referer'][0]
        result=c.get(callback+'?ticket=temporary-ticket',follow_redirects=False)
        assert result.status_code==303 and result.headers['location']=='/#quality=connected'
        assert 'secret-session' not in result.text
        assert c.get(callback+'?ticket=temporary-ticket').status_code==400
        status=c.get('/api/status').json()
        assert 'secret-session' not in json.dumps(status)
        assert next(x for x in status['integrations'] if x['name']=='质量平台')['state']=='ready'
    path=app.state.service.settings.data/'quality-auth.json'
    assert path.stat().st_mode & 0o777==0o600


def test_expired_login_nonce_does_not_exchange_ticket(tmp_path,monkeypatch):
    s=service(tmp_path);url=q.begin_login(s.settings,s.store,'http://localhost:8791')['login_url']
    nonce=urlparse(parse_qs(urlparse(url).query)['referer'][0]).path.rsplit('/',1)[1]
    with s.store.db() as c:c.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps({'created':time.time()-601}),'quality-login:'+hashlib.sha256(nonce.encode()).hexdigest()))
    monkeypatch.setattr(q,'get_json',lambda *a:pytest.fail('expired callback must not exchange'))
    with pytest.raises(ValueError,match='过期'):q.finish_login(s.settings,s.store,nonce,'ticket')


def test_real_download_parameters_and_all_file_roles(tmp_path,monkeypatch):
    s=service(tmp_path);calls=[]
    def get(url,token):
        calls.append(url);assert token=='private-token'
        if '/list?' in url:return {'detail':[{'id':'feedback','index':'daily-index','user_id':'user','sync_time':123}]}
        params=parse_qs(urlparse(url).query)
        assert params=={'id':['feedback'],'index':['daily-index'],'user_id':['user'],'sync_time':['123']}
        return {'url':[{'file_name':'device.zip','url':'https://logs.zepp.com/raw'}],'app':'https://logs.zepp.com/app','APP_OOM_DATA':[{'file_name':'oom','url':'https://logs.zepp.com/oom'}],'fb':[{'file_name':'fb','url':'https://logs.zepp.com/fb'}]}
    monkeypatch.setattr(q,'get_json',get)
    files,meta=q.feedback_files(s.settings,s.store.issue('ABS-1')['feedback'])
    assert len(files)==4 and meta['feedback_id']=='feedback'
    assert [f['role'] for f in files]==['url','APP_OOM_DATA','app','fb']
    assert 'env=production' in calls[0]


@pytest.mark.parametrize('result,state',[({'detail':[]},'feedback_missing'),({'detail':[{'id':'other'}]},'feedback_missing'),({'detail':'bad'},'invalid_response')])
def test_feedback_identity_must_match(tmp_path,monkeypatch,result,state):
    s=service(tmp_path);monkeypatch.setattr(q,'get_json',lambda *a:result)
    with pytest.raises(q.QualityError) as e:q.feedback_files(s.settings,{'id':'feedback','app_name':'zepp'})
    assert e.value.state==state


@pytest.mark.parametrize('url',['http://logs.zepp.com/a','https://zepp.com.evil.test/a','https://127.0.0.1/a','https://u:p@logs.zepp.com/a','https://logs.zepp.com:888/a','https://logs.zepp.com:wrong/a'])
def test_unapproved_storage_url_never_requested(url):
    with pytest.raises(q.QualityError):q.validate_download_url(url)


class Download(io.BytesIO):
    def __init__(self,blob,expected=None):
        super().__init__(blob);self.headers={'Content-Length':str(len(blob) if expected is None else expected)}


def package():
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:z.writestr('firmware.log','I> SYS_INIT firmware version = 1.2.3\n')
    return data.getvalue()


def install_download(monkeypatch,blob,expected=None):
    files=[{'filename':'device.zip','url':'https://zepp-us3-1311088246.cos.na-ashburn.myqcloud.com/signed?private=hidden','role':'url'}]
    monkeypatch.setattr(q,'feedback_files',lambda *a:(files,{'feedback_id':'feedback','app_name':'zepp','files':1}))
    requests=[]
    def get(url,headers=None):
        assert headers is None  # SSO token must never be forwarded to object storage.
        requests.append(url);return Download(blob,expected)
    monkeypatch.setattr(q,'response',get)
    return requests


def test_background_handoff_validates_files_uploads_and_refreshes_once(tmp_path,monkeypatch):
    s=service(tmp_path);requests=install_download(monkeypatch,package());posts=[];refreshes=[]
    def upload(args,**kwargs):
        posts.append(args)
        path=next(a for a in s.store.artifacts('ABS-1') if a['kind']=='log_archive')
        return json.dumps([{'id':'123','filename':path['filename'],'size':path['metadata']['bytes']}])
    monkeypatch.setattr('jira_service.quality.run_cli',upload)
    def refresh(key):refreshes.append(key);return {'attachments':[{'id':'123'}]}
    s.refresh=refresh
    assert q.collect(s,'ABS-1','run')['state']=='archived'
    assert q.collect(s,'ABS-1','retry')['state']=='archived'
    assert len(requests)==1 and len(posts)==1 and len(refreshes)==2
    state=s.quality_collection('ABS-1')
    assert state['state']=='archived' and state['attachment_id']=='123'
    events=json.dumps(s.store.rows('SELECT * FROM events'))
    assert 'hidden' not in events and 'private-token' not in events
    assert s.store.rows('SELECT * FROM reports')==[]


@pytest.mark.parametrize('blob,expected,state',[(b'<html>Login</html>',None,'invalid_response'),(b'partial',999,'network_error'),(b'',None,'network_error')])
def test_invalid_download_never_imports_or_uploads(tmp_path,monkeypatch,blob,expected,state):
    s=service(tmp_path);install_download(monkeypatch,blob,expected)
    monkeypatch.setattr('jira_service.quality.attach_quality_archive',lambda *a:pytest.fail('must not upload'))
    with pytest.raises(q.QualityError) as e:q.collect(s,'ABS-1','run')
    assert e.value.state==state and s.quality_collection('ABS-1')['state']==state
    assert not list(tmp_path.rglob('*.part')) and not s.store.artifacts('ABS-1')


def test_read_only_probe_does_not_mark_case_for_reupload(tmp_path,monkeypatch):
    s=service(tmp_path);requests=install_download(monkeypatch,package())
    assert q.collect(s,'ABS-1',None,archive_only=True)['state']=='downloaded'
    assert q.collect(s,'ABS-1',None,archive_only=True)['state']=='downloaded'
    assert len(requests)==1 and s.quality_collection('ABS-1') is None
    assert not s.store.artifacts('ABS-1')


def test_unknown_upload_retains_cached_download_without_automatic_retry(tmp_path,monkeypatch):
    s=service(tmp_path);requests=install_download(monkeypatch,package());posts=[]
    def upload(*a,**kw):posts.append(1);raise RuntimeError('timeout')
    monkeypatch.setattr('jira_service.quality.run_cli',upload)
    with pytest.raises(RuntimeError):q.collect(s,'ABS-1','run')
    assert s.quality_collection('ABS-1')['state']=='archive_failed'
    with pytest.raises(ValueError,match='结果未知'):q.collect(s,'ABS-1','retry')
    assert len(posts)==len(requests)==1 and s.quality_collection('ABS-1')['archive']


def test_pending_collection_stops_before_scene_or_model_and_records_stage(tmp_path,monkeypatch):
    from jira_service.pipeline import prepare
    s=service(tmp_path);s.refresh=lambda key:s.store.issue(key)
    monkeypatch.setattr('jira_service.attachments.prepare_attachments',lambda *a:{})
    monkeypatch.setattr('jira_service.crash.scene_inventory',lambda *a:pytest.fail('collection must finish first'))
    monkeypatch.setattr(q,'feedback_files',lambda *a:(_ for _ in ()).throw(q.QualityError('pending','平台处理中')))
    rid=s.store.enqueue('analysis','ABS-1')
    with pytest.raises(q.QualityError):prepare(s,'ABS-1',rid,{})
    assert s.store.rows("SELECT status FROM run_stages WHERE name='quality_collection'")==[{'status':'failed'}]
    assert not s.store.rows('SELECT * FROM reports')


def test_auto_retry_selected_only_bounded_and_reconnect_specific(tmp_path):
    s=service(tmp_path);attempts=[];s.retry=lambda rid:attempts.append(rid)
    for key,state in [('ABS-1','pending'),('ABS-2','auth_required'),('ABS-3','archive_failed'),('ABS-4','pending'),('ABS-5','pending')]:
        rid=s.store.enqueue('analysis',key);s.store.finish(rid,error='collection')
        with s.store.db() as c:
            if key!='ABS-4':c.execute('INSERT INTO analysis_cases VALUES(?,?)',(key,'now'))
            c.execute('INSERT INTO settings VALUES(?,?)',('quality-case:'+key,json.dumps({'state':state,'retry_after':0,'auto_retries':2 if key=='ABS-5' else 0})))
    q.resume_waiting(s);assert len(attempts)==1
    q.resume_waiting(s);assert len(attempts)==1  # cooldown
    q.resume_waiting(s,after_login=True);assert len(attempts)==2


def test_feedback_change_does_not_reuse_old_archive(tmp_path,monkeypatch):
    s=service(tmp_path);requests=install_download(monkeypatch,package())
    q.collect(s,'ABS-1',None,archive_only=True)
    case=s.store.issue('ABS-1');case['feedback']['id']='new-feedback';s.store.upsert_issue(case,'new-fp')
    q.collect(s,'ABS-1',None,archive_only=True)
    assert len(requests)==2


def test_api_403_and_storage_403_have_distinct_recovery(monkeypatch):
    from urllib.error import HTTPError
    class Opener:
        def open(self,*a,**kw):raise HTTPError('https://logs.zepp.com/signed?secret=private',403,'forbidden',{},None)
    monkeypatch.setattr(q,'build_opener',lambda *a:Opener())
    with pytest.raises(q.QualityError) as a:q.response('https://api.zepp.top/api/feedback/list',{'Authorization':'private-token'})
    with pytest.raises(q.QualityError) as b:q.response('https://logs.zepp.com/signed?secret=private')
    assert a.value.state=='auth_required' and b.value.state=='network_error'
    assert 'private' not in str(a.value)+str(b.value)


def test_expired_session_and_two_auto_resumptions_only(tmp_path):
    s=service(tmp_path);q.save_auth(s.settings,{'token':'expired','expires':time.time()-1})
    assert q.auth_status(s.settings)['state']=='auth_required'
    rid=s.store.enqueue('analysis','ABS-1');s.store.finish(rid,error='failed');calls=[];s.retry=lambda r:calls.append(r)
    with s.store.db() as c:
        c.execute('INSERT INTO analysis_cases VALUES(?,?)',('ABS-1','now'))
        c.execute('INSERT INTO settings VALUES(?,?)',('quality-case:ABS-1',json.dumps({'state':'archive_pending','retry_after':0})))
    for _ in range(4):
        q.resume_waiting(s)
        state=s.quality_collection('ABS-1');state['retry_after']=0
        with s.store.db() as c:c.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(state),'quality-case:ABS-1'))
    assert len(calls)==2


def test_analysis_pipeline_continues_only_after_verified_jira_archive(tmp_path,monkeypatch):
    from jira_service.pipeline import prepare
    s=service(tmp_path);install_download(monkeypatch,package());steps=[]
    monkeypatch.setattr('jira_service.attachments.prepare_attachments',lambda *a:{})
    def upload(args,**kwargs):
        steps.append('upload');artifact=next(a for a in s.store.artifacts('ABS-1') if a['kind']=='log_archive')
        return json.dumps([{'id':'123','filename':artifact['filename'],'size':artifact['metadata']['bytes']}])
    monkeypatch.setattr('jira_service.quality.run_cli',upload)
    def refresh(key):
        case=s.store.issue(key)
        if 'upload' in steps:
            steps.append('verified');case['attachments']=[{'id':'123'}];s.store.upsert_issue(case,'with-archive')
        return case
    s.refresh=refresh
    monkeypatch.setattr('jira_service.crash.scene_inventory',lambda *a:{'selection':{'status':'no_scene','scene_id':None},'scenes':[]})
    def analyze(*a,**kw):
        assert steps.index('upload')<steps.index('verified')
        assert s.quality_collection('ABS-1')['state']=='archived'
        steps.append('analysis');return {'report_id':'isolated-draft'}
    monkeypatch.setattr('jira_service.analysis.analyze',analyze)
    rid=s.store.enqueue('analysis','ABS-1')
    assert prepare(s,'ABS-1',rid,{})['report_id']=='isolated-draft'
    assert steps==['upload','verified','analysis']
    assert s.store.rows("SELECT status FROM run_stages WHERE name='quality_collection'")==[{'status':'completed'}]
