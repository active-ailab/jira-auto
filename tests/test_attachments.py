import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from jira_service.store import Store
from jira_service.integrations import fingerprint
from jira_service.attachments import prepare_attachments


def setup_case(tmp_path, name, blob):
    settings=SimpleNamespace(data=tmp_path)
    store=Store(tmp_path)
    issue={'key':'ABS-1','attachments':[{'id':'123','filename':name,'size':len(blob)}]}
    store.upsert_issue(issue,fingerprint(issue))
    return settings,store


def test_download_extract_and_reuse(tmp_path, monkeypatch):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:z.writestr('sram_dump.bin',b'\0'*64)
    blob=data.getvalue();settings,store=setup_case(tmp_path,'sram.zip',blob);calls=[]
    def download(args,**kwargs):
        calls.append(args);Path(args[args.index('--output')+1]).write_bytes(blob)
    monkeypatch.setattr('jira_service.attachments.run_cli',download)
    prepare_attachments(settings,store,'ABS-1',None)
    prepare_attachments(settings,store,'ABS-1',None)
    assert len(calls)==1
    assert any(a['kind']=='binary_log' for a in store.artifacts('ABS-1'))
    assert calls[0][-1]=='https://jira.huami.com/secure/attachment/123/sram.zip'
    assert '--location' not in calls[0]


def test_download_failure_is_not_missing_evidence(tmp_path, monkeypatch):
    settings,store=setup_case(tmp_path,'a.log',b'123456')
    monkeypatch.setattr('jira_service.attachments.run_cli',lambda args,**kw:Path(args[args.index('--output')+1]).write_bytes(b'bad'))
    with pytest.raises(ValueError,match='长度'):prepare_attachments(settings,store,'ABS-1',None)
    assert store.artifacts('ABS-1')==[]
    assert not list(tmp_path.rglob('*.part'))


def test_tampered_cached_attachment_is_downloaded_again(tmp_path, monkeypatch):
    blob=b'valid log\n';settings,store=setup_case(tmp_path,'a.log',blob);calls=[]
    def download(args,**kw):
        calls.append(args);Path(args[args.index('--output')+1]).write_bytes(blob)
    monkeypatch.setattr('jira_service.attachments.run_cli',download)
    prepare_attachments(settings,store,'ABS-1',None)
    cached=Path(store.rows("SELECT path FROM artifacts WHERE kind='log'")[0]['path'])
    cached.write_bytes(b'changed')
    prepare_attachments(settings,store,'ABS-1',None)
    assert len(calls)==2
    assert cached.read_bytes()==blob


def test_cached_zip_repairs_corrupted_extracted_dump_without_redownload(tmp_path,monkeypatch):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:z.writestr('sram_dump.bin',b'\0'*64)
    blob=data.getvalue();settings,store=setup_case(tmp_path,'sram.zip',blob);calls=[]
    def download(args,**kw):
        calls.append(args);Path(args[args.index('--output')+1]).write_bytes(blob)
    monkeypatch.setattr('jira_service.attachments.run_cli',download)
    prepare_attachments(settings,store,'ABS-1',None)
    row=store.rows("SELECT * FROM artifacts WHERE kind='binary_log'")[0]
    Path(row['path']).write_bytes(b'bad')
    prepare_attachments(settings,store,'ABS-1',None)
    repaired=store.rows('SELECT path FROM artifacts WHERE id=?',(row['id'],))[0]
    assert Path(repaired['path']).read_bytes()==b'\0'*64
    assert len(calls)==1


def test_identical_material_preserves_multiple_attachment_origins(tmp_path):
    import hashlib
    settings,store=setup_case(tmp_path,'a.log',b'hello')
    issue=store.issue('ABS-1');issue['attachments'].append({'id':'456','filename':'b.log','size':5})
    store.upsert_issue(issue,fingerprint(issue))
    p=tmp_path/'a.log';p.write_bytes(b'hello');digest=hashlib.sha256(b'hello').hexdigest()
    aid=store.artifact('ABS-1','log',p,{'sha256':digest,'jira_attachment_id':'123'})
    assert store.artifact('ABS-1','log',p,{'sha256':digest,'jira_attachment_id':'456'})==aid
    issue['attachments']=[a for a in issue['attachments'] if a['id']=='123'];store.upsert_issue(issue,fingerprint(issue))
    assert store.artifacts('ABS-1')[0]['id']==aid
    issue['attachments']=[];store.upsert_issue(issue,fingerprint(issue))
    assert store.artifacts('ABS-1')==[]


def test_legacy_artifacts_and_provenance_are_json_serializable(tmp_path):
    import json
    settings,store=setup_case(tmp_path,'a.log',b'hello')
    p=tmp_path/'manual.log';p.write_bytes(b'hello')
    store.artifact('ABS-1','log',p,{'bytes':5})
    encoded=json.dumps(store.artifacts('ABS-1'))
    assert 'active_origins' in encoded


def test_cached_zip_reclassifies_legacy_mixed_encoding_log_and_hides_old_binary(tmp_path,monkeypatch):
    import hashlib
    bloblog=b'I>1 SYS_INIT firmware version = 1.2.3\r\n'*500+b'bad \xff\r\n'
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:z.writestr('device.log',bloblog)
    blob=data.getvalue();settings,store=setup_case(tmp_path,'feedback.zip',blob)
    archive=tmp_path/'feedback.zip';archive.write_bytes(blob)
    meta={'jira_attachment_id':'123','sha256':hashlib.sha256(blob).hexdigest()}
    store.artifact('ABS-1','jira_attachment',archive,meta)
    old=tmp_path/'legacy.log';old.write_bytes(bloblog)
    store.artifact('ABS-1','binary_log',old,{'sha256':hashlib.sha256(bloblog).hexdigest(),'archive_sha256':meta['sha256'],'jira_attachment_id':'123'})
    monkeypatch.setattr('jira_service.attachments.run_cli',lambda *a,**kw:pytest.fail('should reuse cached archive'))
    prepare_attachments(settings,store,'ABS-1',None)
    materials=store.artifacts('ABS-1')
    assert len([a for a in materials if a['kind']=='log'])==1
    assert not [a for a in materials if a['kind']=='binary_log']


def test_unusual_attachment_extension_is_inspected_instead_of_silently_skipped(tmp_path,monkeypatch):
    blob=b'\0'*128;settings,store=setup_case(tmp_path,'ramdump.dmp',blob)
    monkeypatch.setattr('jira_service.attachments.run_cli',lambda args,**kw:Path(args[args.index('--output')+1]).write_bytes(blob))
    prepare_attachments(settings,store,'ABS-1',None)
    [a]=store.artifacts('ABS-1')
    assert a['metadata']['analysis_role']=='ramdump_candidate'
