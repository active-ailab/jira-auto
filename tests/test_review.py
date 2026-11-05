import json
import pytest
from fastapi.testclient import TestClient
from jira_service.review import projection, split_legacy_cause
from jira_service.integrations import digest
from jira_service.app import create_app
from jira_service.settings import Settings
from test_analysis import svc, Model
from jira_service.analysis import analyze, AnalysisResult, SearchPlan, CauseAssessment


def report(analysis):
    return {'id':'report','kind':'analysis','status':'published','created':'2026-10-30T00:00:00Z',
            'body':'exact saved comment','body_hash':digest('exact saved comment'),'analysis':json.dumps(analysis),'run_id':'run'}


def test_legacy_report_keeps_exact_body_and_does_not_invent_confidence():
    r=report({'result':{'root_cause':'直接原因：malloc失败触发abort。更深层诱因：尚不能确认泄漏。','facts':[]},'crash_scenes':[]})
    before=dict(r);view=projection(r)
    assert r==before and view['body']=='exact saved comment'
    assert view['direct_cause']['status']=='legacy' and 'malloc' in view['direct_cause']['explanation']
    assert view['underlying_cause']['status']=='legacy' and '泄漏' in view['underlying_cause']['explanation']


def test_direct_cause_and_underlying_cause_are_independent():
    r=report({'result':{'verdict':'probable','direct_cause':{'status':'confirmed','explanation':'分配失败导致abort','evidence_ids':['E1']},
                       'underlying_cause':{'status':'unknown','explanation':'缺少堆统计','evidence_ids':[]}},'evidence':[]})
    view=projection(r)
    assert not view['legacy'] and view['direct_cause']['status']=='confirmed'
    assert view['underlying_cause']['status']=='unknown'


def test_request_revision_rejects_exact_draft_and_enqueues_feedback_without_comment(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001'
    r=service.save_report(key,'original saved body',report_kind='analysis')
    service.approve(r['id'])
    service.jira.comment=lambda *a:pytest.fail('revision must not post comments')
    requested=service.request_revision(r['id'],'请核对对象销毁时序并补充源码')
    saved=service.store.rows('SELECT * FROM reports WHERE id=?',(r['id'],))[0]
    assert saved['status']=='rejected' and saved['approved'] is None
    assert saved['body']=='original saved body' and saved['body_hash']==digest(saved['body'])
    run=service.store.rows('SELECT * FROM runs WHERE id=?',(requested['run_id'],))[0]
    payload=json.loads(run['payload'])
    assert run['kind']=='analysis' and payload['force_from']=='ai_analysis'
    assert service.store.rows('SELECT reason FROM report_reviews')[0]['reason']=='请核对对象销毁时序并补充源码'
    with pytest.raises(ValueError,match='已有分析任务'):service.request_revision(r['id'],'重复提交')
    with pytest.raises(ValueError):service.approve(r['id'])


def test_request_revision_preserves_published_history_and_refuses_obsolete_report(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001';r=service.save_report(key,'published body',report_kind='analysis')
    with service.store.db() as c:c.execute("UPDATE reports SET status='published' WHERE id=?",(r['id'],))
    service.request_revision(r['id'],'补查堆统计')
    assert service.store.rows('SELECT status,body FROM reports WHERE id=?',(r['id'],))[0]=={'status':'published','body':'published body'}
    new=service.save_report(key,'new draft')
    with pytest.raises(ValueError,match='更新版本'):service.request_revision(r['id'],'old')


def test_failure_cannot_be_turned_into_approvable_revision_by_editing(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    from test_core import sample
    from jira_service.integrations import fingerprint
    i=sample();service=app.state.service;service.store.upsert_issue(i,fingerprint(i))
    r=service.save_report(i['key'],'execution failed',report_kind='analysis_failed',analysis={'result':{}})
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        edited=client.post('/api/issues/'+i['key']+'/reports',json={'body':'edited failure','source_report_id':r['id']},headers=headers)
        assert edited.status_code==200
        rid=edited.json()['id']
        assert service.store.rows('SELECT kind FROM reports WHERE id=?',(rid,))[0]['kind']=='analysis_failed'
        assert client.post('/api/reports/'+rid+'/approve',json={},headers=headers).status_code==400


def test_new_cause_fields_require_real_evidence_and_feedback_reaches_model(tmp_path,monkeypatch):
    monkeypatch.setattr('jira_service.similarity.search_recent',lambda *a,**kw:{'status':'searched','candidates':[]})
    service=svc(tmp_path)
    class Reviewed(Model):
        def call(self,stage,instruction,packet,schema):
            assert packet['review_feedback']['reason']=='请核对现场'
            result=super().call(stage,instruction,packet,schema)
            if schema is AnalysisResult:
                result.direct_cause=CauseAssessment(status='confirmed',explanation='invented',evidence_ids=['E999'])
            return result
    r=analyze(service.settings,service.store,'COLOGNE-9001','review-feedback',service.save_report,Reviewed,review_feedback={'reason':'请核对现场'})
    assert r['failure'] and r['analysis']['model_completed'] is False


@pytest.mark.parametrize("label",["更深层原因仍待确认：","深层诱因尚未确认：","待查诱因："])
def test_explicit_legacy_uncertainty_label_preserves_separate_causes(label):
    direct,underlying=split_legacy_cause("直接原因：分配失败。"+label+"不能证明泄漏。")
    assert direct=="分配失败。"
    assert underlying=="不能证明泄漏。"


def test_history_is_issue_bound_and_compact_view_keeps_selected_evidence(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001'
    old=service.save_report(key,'old body',analysis={'result':{'root_cause':'old cause'}},report_kind='analysis')
    new=service.save_report(key,'new body',analysis={'result':{'root_cause':'new cause'}},report_kind='analysis')
    full=service.detail(key,old['id']);compact=service.detail(key,old['id'],compact=True)
    assert full['review']['direct_cause']['explanation']=='old cause'
    assert compact['selected_report']['body']=='old body'
    assert compact['review']==full['review']
    assert compact['selected_report_id']==old['id'] and compact['latest_report_id']==new['id']
    assert 'analysis' not in compact['selected_report']
    assert all('analysis' not in r and 'body' not in r for r in compact['reports'])
    assert service.detail(key)['previous_report']['id']==old['id']
    assert service.detail(key,old['id'])['previous_report'] is None
    with pytest.raises(ValueError,match='不属于'):service.detail(key,'other-report')
    with pytest.raises(ValueError,match='更新版本'):
        service.save_report(key,'obsolete edit',source_report_id=old['id'])
    assert service.detail(key)['selected_report_id']==new['id']


def test_history_api_and_edit_refuse_a_cross_issue_or_old_revision(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    from test_core import sample
    from jira_service.integrations import fingerprint
    service=app.state.service;i=sample();service.store.upsert_issue(i,fingerprint(i));key=i['key']
    old=service.save_report(key,'old',report_kind='analysis')
    new=service.save_report(key,'new',report_kind='analysis')
    other={**i,'key':'ABS-1'};service.store.upsert_issue(other,fingerprint(other))
    with TestClient(app) as client:
        headers={'x-jira-auto-token':client.get('/api/session').json()['token']}
        viewed=client.get('/api/issues/'+key,params={'report_id':old['id'],'compact':1})
        assert viewed.status_code==200 and viewed.json()['review']['body']=='old'
        assert client.get('/api/issues/ABS-1',params={'report_id':old['id']}).status_code==400
        edited=client.post('/api/issues/'+key+'/reports',json={'body':'wrong edit','source_report_id':old['id']},headers=headers)
        assert edited.status_code==400
        feedback=client.post('/api/reports/'+new['id']+'/request-revision',json={'reason':'补充具体代码证据'},headers=headers)
        assert feedback.status_code==200
        assert service.store.rows('SELECT COUNT(*) AS n FROM report_reviews')[0]['n']==1


def test_issue_list_distinguishes_failed_execution_preflight_and_reviewable_result(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    from test_core import sample
    from jira_service.integrations import fingerprint
    s=app.state.service;i=sample();s.store.upsert_issue(i,fingerprint(i));key=i['key']
    with TestClient(app) as client:
        for kind,state,reviewable in [('analysis_failed','analysis_failed',False),('preflight','preflight',False),('analysis','draft',True),('manual','draft',True)]:
            s.save_report(key,'record',report_kind=kind)
            listed=client.get('/api/issues').json()['issues'][0]
            assert listed['analysis_state']==state and listed['reviewable'] is reviewable
            assert listed['report_kind']==kind and listed['report_status']=='draft'
