"""Exercise the skill helper against isolated state, never a running worker/Jira."""
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest
from jira_service.store import Store

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('triage_handoff',ROOT/'plugin/skills/zepp-jira-triage/scripts/queue_quality_archive.py')
helper=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(helper)


def fixture_case(tmp_path):
    data=tmp_path/'data';store=Store(data)
    store.upsert_issue({'key':'ABS-1','feedback':{'id':'example','url':'https://devops.zepp.com/zeus/#/feedback?id=example'},'attachments':[]},'fp')
    archive=tmp_path/'feedback.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('firmware.log','I>1 firmware')
    return data,store,archive


def test_inspection_does_not_copy_archive_queue_or_touch_real_jira(tmp_path):
    data,store,archive=fixture_case(tmp_path)
    result=helper.handoff(ROOT,data,'ABS-1',archive)
    assert not result['submitted'] and result['bytes']==archive.stat().st_size
    assert store.rows('SELECT * FROM runs')==[]
    assert not (data/'cases').exists()


def test_explicit_submission_queues_exact_original_and_prevents_concurrent_duplicate(tmp_path):
    data,store,archive=fixture_case(tmp_path)
    result=helper.handoff(ROOT,data,'ABS-1',archive,True)
    [job]=store.rows('SELECT * FROM runs')
    target=Path(json.loads(job['payload'])['path'])
    assert result['submitted'] and job['kind']=='import_quality_logs' and job['status']=='queued'
    assert target.read_bytes()==archive.read_bytes() and target.name==archive.name
    with pytest.raises(ValueError,match='任务执行中'):helper.handoff(ROOT,data,'ABS-1',archive,True)
    assert len(store.rows('SELECT * FROM runs'))==1


def test_existing_receipt_and_changed_content_address_never_submit_again(tmp_path):
    data,store,archive=fixture_case(tmp_path);digest=helper.sha256(archive)
    with store.db() as c:
        c.execute('CREATE TABLE quality_uploads(issue_key TEXT,sha256 TEXT,state TEXT,attachment_id TEXT)')
        c.execute('INSERT INTO quality_uploads VALUES(?,?,?,?)',('ABS-1',digest,'unknown',None))
    assert helper.handoff(ROOT,data,'ABS-1',archive)['upload_receipt']['state']=='unknown'
    with pytest.raises(ValueError,match='已有上传收据'):helper.handoff(ROOT,data,'ABS-1',archive,True)
    with store.db() as c:c.execute('DELETE FROM quality_uploads')
    target=data/'cases'/'ABS-1'/'quality'/digest/archive.name;target.parent.mkdir(parents=True);target.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='未覆盖原件'):helper.handoff(ROOT,data,'ABS-1',archive,True)
    assert target.read_bytes()==b'corrupt' and store.rows('SELECT * FROM runs')==[]


def test_bad_download_and_non_feedback_issue_are_rejected_before_queueing(tmp_path):
    data,store,archive=fixture_case(tmp_path)
    bad=tmp_path/'login.zip';bad.write_text('<html>login</html>')
    with pytest.raises(ValueError,match='不是有效ZIP'):helper.handoff(ROOT,data,'ABS-1',bad,True)
    store.upsert_issue({'key':'ABS-2','attachments':[]},'fp2')
    with pytest.raises(ValueError,match='没有质量反馈链接'):helper.handoff(ROOT,data,'ABS-2',archive,True)
    assert store.rows('SELECT * FROM runs')==[]
