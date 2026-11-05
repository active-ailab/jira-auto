import io
import json
import zipfile
from types import SimpleNamespace
import pytest
from jira_service.quality import attach_quality_archive,require_quality_materials,import_quality_logs
from jira_service.store import Store
from jira_service.materials import unpack_logs


def service(tmp_path):
    s=SimpleNamespace(settings=SimpleNamespace(data=tmp_path,jira_write=True),store=Store(tmp_path))
    s.store.upsert_issue({'key':'ABS-1','attachments':[],'feedback':{'id':'feedback','url':'https://devops.zepp.com/zeus/'}},'fp')
    return s


def test_attachment_upload_receipt_reuses_success_and_fixed_existing_netrc(tmp_path,monkeypatch):
    s=service(tmp_path);p=tmp_path/'feedback.zip';p.write_bytes(b'archive');calls=[]
    def send(args,**kwargs):
        calls.append(args);return json.dumps([{'id':'123','filename':p.name,'size':p.stat().st_size}])
    monkeypatch.setattr('jira_service.quality.run_cli',send)
    assert attach_quality_archive(s,'ABS-1',p)['attachment_id']=='123'
    assert attach_quality_archive(s,'ABS-1',p)['reused']
    assert len(calls)==1 and '--netrc' in calls[0] and '--location' not in calls[0]
    assert calls[0][-1]=='https://jira.huami.com/rest/api/2/issue/ABS-1/attachments'


def test_unknown_upload_never_posts_again_and_disabled_writes_do_not_post(tmp_path,monkeypatch):
    s=service(tmp_path);p=tmp_path/'feedback.zip';p.write_bytes(b'archive');calls=[]
    def send(*a,**kw):calls.append(1);raise RuntimeError('timeout')
    monkeypatch.setattr('jira_service.quality.run_cli',send)
    with pytest.raises(RuntimeError):attach_quality_archive(s,'ABS-1',p)
    with pytest.raises(ValueError,match='结果未知'):attach_quality_archive(s,'ABS-1',p)
    assert len(calls)==1
    s.settings.jira_write=False
    with pytest.raises(ValueError,match='未启用'):attach_quality_archive(s,'ABS-1',p)
    assert len(calls)==1


def test_empty_quality_case_stops_before_generating_a_no_log_conclusion(tmp_path):
    s=service(tmp_path)
    with pytest.raises(ValueError,match='不能跳过下载'):require_quality_materials(s,'ABS-1',None)
    assert s.store.rows('SELECT * FROM reports')==[]
    p=tmp_path/'fw.txt';p.write_text('I> firmware')
    s.store.artifact('ABS-1','log',p,{})
    require_quality_materials(s,'ABS-1',None)


def zipped(name,blob):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr(name,blob)
    return b.getvalue()


def test_ios_dump_inside_four_packages_is_extracted_with_full_provenance(tmp_path):
    blob=zipped('iOS.zip',zipped('device.zip',zipped('sram_dump_timestamp.zip',zipped('sram_dump.bin',b'\0'*128))))
    p=tmp_path/'feedback.zip';p.write_bytes(blob)
    [(leaf,meta)]=unpack_logs(p,tmp_path/'out')
    assert leaf.read_bytes()==b'\0'*128 and meta['analysis_role']=='ramdump_candidate'
    assert 'iOS.zip / device.zip / sram_dump_timestamp.zip / sram_dump.bin'==meta['original_path']


def test_quality_import_refreshes_jira_after_archive_upload_before_enqueuing_analysis(tmp_path,monkeypatch):
    s=service(tmp_path);p=tmp_path/'feedback.zip';p.write_bytes(zipped('fw.log',b'I>1 SYS_INIT firmware version = 1.2.3\n'))
    calls=[]
    def refresh(key):
        calls.append('refresh');return {**s.store.issue(key),'attachments':[] if len(calls)==1 else [{'id':'123'}]}
    s.refresh=refresh
    monkeypatch.setattr('jira_service.quality.attach_quality_archive',lambda *a: calls.append('upload') or {'attachment_id':'123'})
    result=import_quality_logs(s,'ABS-1',p,None)
    assert calls==['refresh','upload','refresh']
    job=s.store.rows('SELECT kind,status FROM runs WHERE id=?',(result['next_run_id'],))[0]
    assert job=={'kind':'analysis','status':'queued'}


def test_quality_archive_on_disk_does_not_bypass_required_jira_handoff(tmp_path):
    s=service(tmp_path);p=tmp_path/'quality.zip';p.write_bytes(b'zip')
    s.store.artifact('ABS-1','log',p,{})
    s.store.artifact('ABS-1','log_archive',p,{'source_platform':'quality','sha256':'abc'})
    with pytest.raises(ValueError,match='尚未完成Jira附件归档'):require_quality_materials(s,'ABS-1',None)
    s.store.artifact('ABS-1','jira_attachment',p,{'sha256':'abc'})
    require_quality_materials(s,'ABS-1',None)


def test_quality_upload_api_preserves_archive_name_and_queues_the_handoff(tmp_path):
    from jira_service.app import create_app
    from jira_service.settings import Settings
    from fastapi.testclient import TestClient
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    store=app.state.service.store;store.upsert_issue({'key':'ABS-1','attachments':[],'feedback':{'id':'feedback'}},'fp')
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        response=client.post('/api/issues/ABS-1/logs',files={'file':('feedback_original.zip',zipped('fw.log',b'hello'))},headers=headers)
        assert response.status_code==200
        row=store.rows('SELECT * FROM runs WHERE id=?',(response.json()['run_id'],))[0]
        assert row['kind']=='import_quality_logs'
        from pathlib import Path
        assert Path(json.loads(row['payload'])['path']).name=='feedback_original.zip'
