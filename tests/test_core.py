import io
import json
import zipfile
import sys
import time
import subprocess

import pytest
from fastapi.testclient import TestClient

from jira_service.app import create_app
from jira_service.integrations import fingerprint, normalize, issue_key
from jira_service.materials import unpack_logs
from jira_service.service import Service
from jira_service.settings import Settings
from jira_service.store import Store


def sample():
    return normalize({"key": "COLOGNE-9001", "fields": {"summary": "界面卡住", "updated": "2026-03-02",
        "issuetype": {"name": "Bug"}, "status": {"name": "Open"}, "customfield_10513":
        "绑定设备: Cologne\n固件版本: 3.8.0.1\n反馈详情: 界面卡住\n质量平台上的该反馈: https://devops.zepp.com/zeus/#/feedback?app_name=zepp&id=example-feedback-id"}})


@pytest.fixture
def service(tmp_path):
    svc = Service(Settings(tmp_path / "data", tmp_path / "source"))
    i = sample()
    svc.store.upsert_issue(i, fingerprint(i))
    return svc


def test_custom_description_and_feedback():
    i = sample()
    assert i["device"] == "Cologne" and i["firmware"] == "3.8.0.1"
    assert i["feedback"]["id"] == "example-feedback-id"
    with pytest.raises(ValueError):
        issue_key("COLOGNE-9001;touch /tmp/oops")


def test_queue_deduplicates_and_survives_restart(service):
    a = service.store.enqueue("fetch", "COLOGNE-9001")
    assert service.store.enqueue("fetch", "COLOGNE-9001") == a
    assert service.store.claim()["id"] == a
    restarted = Store(service.settings.data)
    restarted.recover()
    assert restarted.rows("SELECT status FROM runs")[0]["status"] == "interrupted"
    assert restarted.enqueue("fetch", "COLOGNE-9001") != a


def test_approval_invalidated_by_new_revision_and_issue_change(service):
    a = service.save_report("COLOGNE-9001", "事实：版本为 3.8.0.1；根因待验证。")
    service.approve(a["id"])
    service.save_report("COLOGNE-9001", "新分析")
    assert service.store.rows("SELECT status FROM reports WHERE id=?", (a["id"],))[0]["status"] == "superseded"
    latest = service.store.rows("SELECT id FROM reports WHERE status='draft'")[0]["id"]
    i = sample(); i["updated"] = "2026-03-03"
    service.store.upsert_issue(i, fingerprint(i))
    with pytest.raises(ValueError):
        service.approve(latest)


def test_new_evidence_revokes_approval(service):
    report = service.save_report("COLOGNE-9001", "待验证")
    service.approve(report["id"])
    p = service.settings.data / "new.log"; p.write_text("new evidence")
    service.store.artifact("COLOGNE-9001", "log", p, {"bytes": 12})
    assert service.store.rows("SELECT status FROM reports")[0]["status"] == "stale"


def test_identical_artifact_does_not_revoke_review(service):
    p = service.settings.data / "same.log"; p.write_text("same")
    meta = {"bytes": 4, "sha256": "same-hash"}
    aid = service.store.artifact("COLOGNE-9001", "log", p, meta)
    report = service.save_report("COLOGNE-9001", "已审阅")
    service.approve(report["id"])
    assert service.store.artifact("COLOGNE-9001", "log", p, meta) == aid
    assert service.store.rows("SELECT status FROM reports")[0]["status"] == "approved"


def test_publication_requires_approval_and_recovers_uncertainty(service):
    service.settings = Settings(service.settings.data, service.settings.source, jira_write=True)
    r = service.save_report("COLOGNE-9001", "审核内容")
    with pytest.raises(ValueError):
        service.publish(r["id"])
    service.approve(r["id"])
    service.refresh = lambda key: sample()
    def timeout(*args):
        raise TimeoutError()
    service.jira.comment = timeout
    with pytest.raises(ValueError, match="结果未知"):
        service.publish(r["id"])
    assert service.store.rows("SELECT status FROM reports")[0]["status"] == "publication_unknown"
    with pytest.raises(ValueError):
        service.publish(r["id"])


def test_changed_remote_issue_blocks_publish(service):
    service.settings = Settings(service.settings.data, service.settings.source, jira_write=True)
    r = service.save_report("COLOGNE-9001", "审核内容"); service.approve(r["id"])
    def refresh(key):
        i = sample(); i["updated"] = "2026-03-04"
        service.store.upsert_issue(i, fingerprint(i))
    service.refresh = refresh
    service.jira.comment = lambda *args: pytest.fail("must not publish")
    with pytest.raises(ValueError):
        service.publish(r["id"])


def archive(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries:
            z.writestr(name, content)


def test_archive_traversal_and_size_limits(tmp_path):
    path = tmp_path / "bad.zip"
    archive(path, [("../../escape", "oops")])
    with pytest.raises(ValueError, match="不安全路径"):
        unpack_logs(path, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    archive(path, [("normal.log", "large")])
    with pytest.raises(ValueError, match="大小超限"):
        unpack_logs(path, tmp_path / "out", limit=2)


def test_nested_log_import_and_line_evidence(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("log/device.log", "boot\nfirmware version = 3.8.0.1\nwatchdog fired\n")
    path = tmp_path / "good.zip"; archive(path, [("nested.zip", inner.getvalue())])
    entries = unpack_logs(path, tmp_path / "out")
    assert len(entries) == 1 and entries[0][1]["readable_text"]


def test_local_api_rejects_cross_origin_and_missing_token(tmp_path):
    app = create_app(Settings(tmp_path / "data", tmp_path / "src"), start_worker=False)
    with TestClient(app) as c:
        assert c.post("/api/sync", json={}).status_code == 403
        assert c.get("/api/issues", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/issues", headers={"Host": "evil.example"}).status_code == 400
        token = c.get("/api/session").json()["token"]
        assert c.post("/api/sync", json={}, headers={"x-jira-auto-token": token}).status_code == 200
        assert c.get("/").status_code == 200


def test_disabled_write_never_calls_cli(service):
    r = service.save_report("COLOGNE-9001", "审核内容"); service.approve(r["id"])
    service.jira.comment = lambda *args: pytest.fail("must not publish")
    with pytest.raises(ValueError, match="关闭"):
        service.publish(r["id"])


def test_lan_auth_and_origin_controls(tmp_path):
    settings = Settings(tmp_path/'data', tmp_path/'src', host='0.0.0.0', lan_auth=True,
                        allowed_hosts=('localhost','127.0.0.1','192.168.1.10'))
    app = create_app(settings, start_worker=False)
    password = (settings.data/'lan-password').read_text()
    with TestClient(app,base_url='http://192.168.1.10:8791',client=('192.168.1.20',50000)) as c:
        for path in ['/', '/api/session', '/api/issues', '/api/artifacts/unknown/download']:
            assert c.get(path).status_code == 401
        assert c.get('/api/issues',auth=('admin','wrong')).status_code == 401
        assert c.get('/api/issues',auth=('admin',password)).status_code == 200
        token = c.get('/api/session',auth=('admin',password)).json()['token']
        headers={'x-jira-auto-token':token,'Origin':'http://192.168.1.10:8791'}
        assert c.post('/api/sync',json={},auth=('admin',password),headers=headers).status_code == 200
        headers['Origin']='https://evil.example'
        assert c.post('/api/sync',json={},auth=('admin',password),headers=headers).status_code == 403
        assert c.get('/api/issues',auth=('admin',password),headers={'Host':'evil.example'}).status_code == 400
    with TestClient(app,client=('127.0.0.1',50000),base_url='http://localhost:8791') as c:
        assert c.get('/api/issues').status_code == 200


def test_lan_without_login(tmp_path):
    settings = Settings(tmp_path/'data', tmp_path/'src', host='0.0.0.0',
                        allowed_hosts=('192.168.1.10',))
    app = create_app(settings, start_worker=False)
    with TestClient(app,base_url='http://192.168.1.10:8791',client=('192.168.1.20',50000)) as c:
        for path in ['/', '/api/session', '/api/issues']:
            response = c.get(path)
            assert response.status_code == 200
            assert 'www-authenticate' not in response.headers
        assert c.get('/api/status').json()['lan_auth'] is False
        token = c.get('/api/session').json()['token']
        assert c.post('/api/sync',json={},headers={'x-jira-auto-token':token,'Origin':'http://192.168.1.10:8791'}).status_code == 200


def test_streamed_tool_output_is_live_and_redacted():
    from jira_service.integrations import run_cli
    observed=[]; started=time.monotonic()
    script="import time; print('[BUILD] start',flush=True); time.sleep(0.3); print('https://host/download?signature=private'); print('token=private')"
    run_cli([sys.executable,'-u','-c',script],on_output=lambda line:observed.append((time.monotonic()-started,line)))
    assert observed[0][0] < .25
    assert not any('private' in line for _,line in observed)
    assert '[BUILD] start' == observed[0][1]
    with pytest.raises(subprocess.TimeoutExpired):
        run_cli([sys.executable,'-c','import time;time.sleep(3)'],timeout=.1,on_output=lambda line:None)


def test_preflight_records_observable_evidence_steps(service):
    from jira_service.materials import prepare_report
    p=service.settings.data/'device.log'
    p.write_text('OTA firmware version: 9.9.9.9\nfirmware version = 3.8.0.1\nwatchdog fired\n')
    service.store.artifact('COLOGNE-9001','log',p,{'bytes':p.stat().st_size})
    trace=[]
    body,evidence=prepare_report(service.store,'COLOGNE-9001',lambda *entry:trace.append(entry))
    assert evidence['files'][0]['last_runtime_version']=='3.8.0.1'
    assert evidence['versions'][0]['line']==2
    assert all(x['version']!='9.9.9.9' for x in evidence['versions'])
    assert {'context','materials','scope','scan','evidence','validate','conclusion'} <= {x[0] for x in trace}
    assert '尚未执行模型根因分析' in body


def test_completed_run_stream_replays_events_and_result(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'src'),start_worker=False)
    store=app.state.service.store
    rid=store.enqueue('preflight','COLOGNE-9001')
    store.event('scan','扫描完成','COLOGNE-9001',rid,{'current':10,'total':10})
    store.finish(rid,{'report_id':'test-report'})
    with TestClient(app) as c:
        response=c.get('/api/runs/'+rid+'/stream')
        assert response.status_code==200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'event: snapshot' in response.text and 'event: trace' in response.text
        assert '扫描完成' in response.text and 'test-report' in response.text
        assert 'event: done' in response.text
        last=store.rows('SELECT MAX(id) AS id FROM events WHERE run_id=?',(rid,))[0]['id']
        resumed=c.get('/api/runs/'+rid+'/stream',headers={'Last-Event-ID':str(last)})
        assert 'event: trace' not in resumed.text
        assert c.get('/api/runs/not-found/stream').status_code==404
