import pytest
from fastapi.testclient import TestClient
from jira_service.app import create_app
from jira_service.settings import Settings
from jira_service.integrations import issue_reference, fingerprint, normalize
from test_core import sample


def test_issue_reference_accepts_key_and_own_jira_url():
    assert issue_reference(' abs-9001 ')=='ABS-9001'
    assert issue_reference('https://jira.huami.com/browse/ABS-9001?x=1#test')=='ABS-9001'
    for bad in ['https://example.com/browse/ABS-9001','https://jira.huami.com.evil/browse/ABS-9001','https://user:pass@jira.huami.com/browse/ABS-9001','ABS-9001; rm -rf /','https://jira.huami.com/browse/ABS-9001/other']:
        with pytest.raises(ValueError): issue_reference(bad)


def test_manual_selection_is_persistent_independent_of_assignment(tmp_path):
    settings=Settings(tmp_path/'data',tmp_path/'source')
    app=create_app(settings,start_worker=False)
    s=app.state.service
    i=sample();i['assignee']='someone_else';s.store.upsert_issue(i,fingerprint(i))
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        response=client.post('/api/issues',json={'key':'https://jira.huami.com/browse/COLOGNE-9001'},headers=headers)
        assert response.status_code==200 and response.json()['key']=='COLOGNE-9001'
        assert client.get('/api/issues').json()['issues'][0]['in_analysis'] is True
        assert client.delete('/api/issues/COLOGNE-9001/analysis-list',headers=headers).status_code==200
        assert client.get('/api/issues').json()['issues'][0]['in_analysis'] is False
        assert client.post('/api/issues/COLOGNE-9001/analysis-list',headers=headers).status_code==200
    again=create_app(settings,start_worker=False)
    assert again.state.service.store.rows('SELECT issue_key FROM analysis_cases')[0]['issue_key']=='COLOGNE-9001'
    assert again.state.service.store.issue('COLOGNE-9001')


def test_summary_version_is_used_when_description_is_template_only():
    i=normalize({'key':'ABS-9001','fields':{'summary':'【LWG-10月第一轮】【Geneva-3.26.1.2】【Monkey】运行135h停止','customfield_10513':'【绑定设备及固件版本】'}})
    assert i['device']=='Geneva' and i['firmware']=='3.26.1.2'


def test_join_queues_analysis_once_and_repairs_legacy_selection_without_a_run(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False);s=app.state.service
    i=sample();s.store.upsert_issue(i,fingerprint(i));key=i['key']
    with s.store.db() as c:c.execute("INSERT INTO analysis_cases VALUES(?,datetime('now'))",(key,))
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        first=client.post('/api/issues/'+key+'/analysis-list',headers=headers).json()
        assert first['queued']
        second=client.post('/api/issues',json={'key':key},headers=headers).json()
        assert not second['queued'] and first['run_id']==second['run_id']
        job=s.store.rows('SELECT * FROM runs')[0]
        assert job['kind']=='analysis' and job['status']=='queued'
        assert client.get('/api/issues').json()['issues'][0]['analysis_run']['id']==job['id']
        s.store.finish(job['id'],result={'review':'ready'})
        assert client.post('/api/issues/'+key+'/analysis-list',headers=headers).json()['run_id']==job['id']
        assert len(s.store.rows('SELECT id FROM runs'))==1


def test_new_unknown_issue_can_enter_pipeline_and_ai_disabled_uses_preflight(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source',ai_enabled=False),start_worker=False);s=app.state.service
    result=s.add_analysis_case('ABS-9003')
    assert result['queued'] and not s.store.rows('SELECT key FROM issues')
    assert s.store.rows('SELECT kind FROM runs')[0]['kind']=='preflight'
    s.jira.comment=lambda *a:pytest.fail('joining must never post a comment')
    assert s.add_analysis_case('ABS-9003')['run_id']==result['run_id']
