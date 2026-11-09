import json
from pathlib import Path
import pytest
from jira_service.store import Store
from jira_service.stages import StageRunner
from jira_service.materials import sha256
from jira_service.service import Service
from jira_service.settings import Settings
from jira_service.app import create_app
from fastapi.testclient import TestClient


def test_resume_after_failure_skips_success_and_checks_output(tmp_path):
    store=Store(tmp_path);calls=[];p=tmp_path/'download'
    def download():calls.append('download');p.write_bytes(b'valid');return {'n':5}
    files=lambda _: [{'path':'download','sha256':sha256(p)}]
    first=StageRunner(store,'ABS-1','r1')
    first.run('attachments',{'id':1},download,files)
    with pytest.raises(RuntimeError):first.run('crash_parse',{'id':1},lambda:(_ for _ in ()).throw(RuntimeError('tool missing')))
    second=StageRunner(store,'ABS-1','r2')
    assert second.run('attachments',{'id':1},download,files)=={'n':5}
    assert calls==['download']
    assert store.rows("SELECT reused_from FROM run_stages WHERE run_id='r2'")[0]['reused_from']=='r1'
    p.write_bytes(b'broken')
    StageRunner(store,'ABS-1','r3').run('attachments',{'id':1},download,files)
    assert calls==['download','download']


def test_changed_input_and_forced_stage_do_not_reuse(tmp_path):
    store=Store(tmp_path);calls=[]
    action=lambda: calls.append(1) or {'ok':True}
    StageRunner(store,'ABS-1','a').run('build',{'version':'1'},action)
    StageRunner(store,'ABS-1','b').run('build',{'version':'2'},action)
    StageRunner(store,'ABS-1','c','build').run('build',{'version':'2'},action)
    assert len(calls)==3


def test_restart_marks_stage_interrupted_preserves_checkpoints_and_no_publish_retry(tmp_path):
    store=Store(tmp_path)
    runner=StageRunner(store,'ABS-1','r')
    runner.run('attachments',{},lambda:{'ok':1})
    runner.record('crash_parse','hash','running')
    store.recover()
    rows={r['name']:r['status'] for r in store.rows('SELECT name,status FROM run_stages')}
    assert rows=={'attachments':'completed','crash_parse':'interrupted'}


def test_retry_api_does_not_allow_publication_or_active_run(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    store=app.state.service.store
    original=store.enqueue('analysis','ABS-1');store.finish(original,error='download failed')
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        assert client.post('/api/runs/'+original+'/retry',json={'force_from':'publish'},headers=headers).status_code==400
        res=client.post('/api/runs/'+original+'/retry',json={},headers=headers)
        assert res.status_code==200
        new=store.rows('SELECT * FROM runs WHERE id=?',(res.json()['run_id'],))[0]
        assert new['kind']=='analysis' and json.loads(new['payload'])['retry_of']==original
        assert client.post('/api/runs/'+new['id']+'/retry',json={},headers=headers).status_code==400


def test_corrupt_build_cache_is_quarantined_before_fetch(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import hashlib
    from jira_service.materials import fetch_artifacts
    store=Store(tmp_path);target=tmp_path/'cases'/'ABS-1'/'builds'/'Geneva'/'1.2.3';target.mkdir(parents=True)
    elf=target/'watch@mhs003.elf';elf.write_bytes(b'good')
    store.artifact('ABS-1','elf',elf,{'device':'Geneva','version':'1.2.3','sha256':hashlib.sha256(b'good').hexdigest()})
    elf.write_bytes(b'bad')
    def fetch(*args,**kwargs):
        assert not elf.exists()
        elf.write_bytes(b'good')
        (target/'watch@mhs003.map').write_text('map')
        (target/'manifest.xml').write_text('<manifest/>')
        return ''
    monkeypatch.setattr('jira_service.materials.run_cli',fetch)
    monkeypatch.setattr('jira_service.materials.inspect_elf',lambda p:{'sha256':sha256(p),'architecture':'fixture'})
    result=fetch_artifacts(SimpleNamespace(data=tmp_path,elf_bin='fake-elf-fetch'),store,'ABS-1','Geneva','1.2.3',None)
    assert len(result['files'])==3 and elf.read_bytes()==b'good'
    assert len(list(target.glob('*.invalid-*')))==1


def test_queue_dedup_never_drops_a_different_scene_or_review_request(tmp_path):
    store=Store(tmp_path);rid=store.enqueue('analysis','ABS-1',{'scene_id':'a','version':None})
    assert store.enqueue('analysis','ABS-1',{'scene_id':'a'})==rid
    for changed in ({'scene_id':'b'},{'scene_id':'a','force_from':'ai_analysis'},{'review_request_id':'new'}):
        with pytest.raises(ValueError,match='选项不同'):store.enqueue('analysis','ABS-1',changed)
    assert json.loads(store.rows('SELECT payload FROM runs WHERE id=?',(rid,))[0]['payload'])['scene_id']=='a'


def test_cancel_queued_is_atomic_and_does_not_interrupt_claimed_job(tmp_path):
    store=Store(tmp_path);first=store.enqueue('analysis','ABS-1');second=store.enqueue('analysis','ABS-2')
    assert store.claim()['id']==first
    with pytest.raises(ValueError,match='只能取消'):store.cancel_queued(first)
    assert store.cancel_queued(second)=={'status':'cancelled'}
    assert store.claim() is None
    assert store.rows('SELECT status FROM runs WHERE id=?',(first,))[0]['status']=='running'
    assert store.enqueue('analysis','ABS-2')!=second


def test_queue_cancel_api_and_cross_kind_analysis_guard(tmp_path):
    from test_core import sample
    from jira_service.integrations import fingerprint
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    store=app.state.service.store;i=sample();store.upsert_issue(i,fingerprint(i))
    rid=store.enqueue('analysis',i['key'],{'scene_id':None})
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        assert client.get('/api/runs/'+rid).json()['ahead_count']==0
        assert client.post('/api/issues/'+i['key']+'/jobs',json={'kind':'preflight'},headers=headers).status_code==400
        assert client.post('/api/runs/'+rid+'/cancel',json={},headers=headers).json()['status']=='cancelled'
        assert client.post('/api/runs/'+rid+'/cancel',json={},headers=headers).status_code==400
        assert client.post('/api/issues/'+i['key']+'/jobs',json={'kind':'preflight'},headers=headers).status_code==200


@pytest.mark.parametrize('device',['GenevaW','GenevaWN'])
def test_verified_quality_device_alias_selects_ci_artifacts_without_changing_issue_device(tmp_path,monkeypatch,device):
    from types import SimpleNamespace
    from jira_service.materials import fetch_artifacts,sha256
    store=Store(tmp_path);calls=[]
    def fetch(args,**kwargs):
        calls.append(args);target=tmp_path/'cases'/'ABS-1'/'builds'/'Geneva'/'3.25.0.1';target.mkdir(parents=True)
        (target/'watch@mhs003.elf').write_bytes(b'elf')
        (target/'watch@mhs003.map').write_text('map')
        (target/'manifest.xml').write_text('<manifest/>')
        return ''
    monkeypatch.setattr('jira_service.materials.run_cli',fetch)
    monkeypatch.setattr('jira_service.materials.inspect_elf',lambda p:{'sha256':sha256(p),'architecture':'fixture'})
    result=fetch_artifacts(SimpleNamespace(data=tmp_path,elf_bin='fake'),store,'ABS-1',device,'3.25.0.1',None)
    assert calls[0][calls[0].index('--device')+1]=='Geneva'
    assert all(x['device']==device and x['build_device']=='Geneva' for x in result['files'])
