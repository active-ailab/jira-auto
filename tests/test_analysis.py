import json

import pytest
from fastapi.testclient import TestClient

from jira_service.analysis import analyze, SearchPlan, AnalysisResult
from jira_service.app import create_app
from jira_service.integrations import fingerprint
from jira_service.service import Service
from jira_service.settings import Settings
from test_core import sample


@pytest.fixture(autouse=True)
def offline_similarity(monkeypatch):
    monkeypatch.setattr('jira_service.similarity.search_recent',lambda *args,**kwargs: {'status':'searched','days':90,'candidates':[]})


class Model:
    verdict='insufficient_evidence'
    refs=[]
    def __init__(self,*args):self.receipts=[]
    def call(self,stage,instruction,packet,schema):
        self.receipts.append({'stage':stage,'provider':'fake-for-tests'})
        if schema is SearchPlan:
            return SearchPlan(observations=[{'finding':'用户反馈界面卡住','evidence_ids':['E001']}],
                              log_terms=['watchdog'],source_terms=[],evidence_gaps=['缺少现场'])
        return AnalysisResult(verdict=self.verdict,confidence='high',summary='无法确认界面卡住的根因。',root_cause='待验证',
            facts=[{'finding':'反馈界面卡住','evidence_ids':self.refs}] if self.refs else [],
            hypotheses=[],gaps=['缺少与故障对应的现场'],next_steps=['补充故障时间和日志'],review_notes=['请人工核对'])


def svc(tmp_path):
    service=Service(Settings(tmp_path/'data',tmp_path/'source'))
    i=sample();service.store.upsert_issue(i,fingerprint(i))
    return service


def test_insufficient_evidence_is_a_reviewable_analysis(tmp_path):
    service=svc(tmp_path)
    result=analyze(service.settings,service.store,'COLOGNE-9001','run-test',service.save_report,Model)
    assert result['verdict']=='insufficient_evidence' and result['confidence']=='low'
    report=service.store.rows('SELECT * FROM reports')[0]
    assert report['kind']=='analysis' and report['status']=='draft'
    assert '证据不足' in report['body']
    assert json.loads(report['analysis'])['model_completed'] is True
    service.approve(report['id'])


def test_model_failure_still_produces_explicit_undetermined_review(tmp_path):
    class Failure(Model):
        def call(self,*args):raise RuntimeError('模型网络不可达')
    service=svc(tmp_path)
    result=analyze(service.settings,service.store,'COLOGNE-9001','failure',service.save_report,Failure)
    assert result['verdict']=='undetermined' and result['failure']
    report=service.store.rows('SELECT * FROM reports')[0]
    assert report['status']=='draft' and '自动根因分析未能完成' in report['body']
    assert json.loads(report['analysis'])['model_completed'] is False


def test_invented_citation_cannot_become_approved_fact(tmp_path):
    class Invented(Model):refs=['E999']
    service=svc(tmp_path)
    result=analyze(service.settings,service.store,'COLOGNE-9001','bad-ref',service.save_report,Invented)
    assert result['verdict']=='undetermined'
    assert result['analysis']['result']['facts']==[]
    assert '不存在的证据' in result['failure']


def test_confirmed_without_direct_log_evidence_is_downgraded(tmp_path):
    class Overconfident(Model):verdict='confirmed';refs=['E001']
    service=svc(tmp_path)
    result=analyze(service.settings,service.store,'COLOGNE-9001','overconfident',service.save_report,Overconfident)
    assert result['verdict']=='insufficient_evidence' and result['confidence']=='low'


def test_preflight_is_not_approvable_as_root_cause(tmp_path):
    service=svc(tmp_path)
    report=service.save_report('COLOGNE-9001','材料清单',report_kind='preflight')
    with pytest.raises(ValueError,match='材料预检'):
        service.approve(report['id'])


def test_approval_publishes_exact_reviewed_body_once(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source',jira_write=True),start_worker=False)
    service=app.state.service;i=sample();service.store.upsert_issue(i,fingerprint(i))
    report=service.save_report('COLOGNE-9001','证据不足，需要补充复现时间。',report_kind='analysis')
    calls=[];service.refresh=lambda key:sample();service.jira.comment=lambda key,body:calls.append((key,body))
    with TestClient(app) as client:
        token=client.get('/api/session').json()['token'];headers={'x-jira-auto-token':token}
        response=client.post('/api/reports/'+report['id']+'/approve',json={'publish':True},headers=headers)
        assert response.status_code==200 and response.json()['status']=='published'
        assert calls[0][1]=='证据不足，需要补充复现时间。\n\n[jira-auto:'+report['id']+']'
        assert client.post('/api/reports/'+report['id']+'/approve',json={'publish':True},headers=headers).status_code==400
        assert len(calls)==1


def test_manual_edit_keeps_analysis_lineage_and_revokes_approval(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False)
    service=app.state.service;i=sample();service.store.upsert_issue(i,fingerprint(i))
    report=service.save_report('COLOGNE-9001','原 AI 草稿',analysis={'result':{'verdict':'insufficient_evidence'}},
                               run_id='original-ai-run',report_kind='analysis')
    service.approve(report['id'])
    with TestClient(app) as client:
        token=client.get('/api/session').json()['token']
        r=client.post('/api/issues/COLOGNE-9001/reports',json={'body':'人工修改后草稿','source_report_id':report['id']},headers={'x-jira-auto-token':token})
        assert r.status_code==200
        updated=service.store.rows('SELECT * FROM reports WHERE id=?',(r.json()['id'],))[0]
        assert updated['status']=='draft' and updated['kind']=='analysis_revision'
        assert updated['run_id']=='original-ai-run' and json.loads(updated['analysis'])['manual_edit'] is True
        assert service.store.rows('SELECT status FROM reports WHERE id=?',(report['id'],))[0]['status']=='superseded'


def test_parsed_forensic_evidence_reaches_model(tmp_path):
    service=svc(tmp_path)
    p=service.settings.data/'forensic.json'
    p.write_text(json.dumps({'schema':'jira-auto-forensic-v1','findings':['dump 中 sContainer 为 NULL，符号与指令核对一致']}))
    service.store.artifact('COLOGNE-9001','forensic',p,{'bytes':p.stat().st_size})
    class EvidenceModel(Model):
        def call(self,stage,instruction,packet,schema):
            assert any(e['kind']=='forensic' and 'sContainer' in e['text'] for e in packet['evidence'])
            assert not any('未经专用解析器解析' in x for x in packet['limitations'])
            return super().call(stage,instruction,packet,schema)
    result=analyze(service.settings,service.store,'COLOGNE-9001','forensic-test',service.save_report,EvidenceModel)
    assert not result.get('failure')
    report=service.store.rows('SELECT analysis FROM reports')[0]
    assert json.loads(report['analysis'])['model_completed'] is True


def test_unknown_similar_issue_cannot_be_saved_as_a_duplicate_suggestion(tmp_path):
    class InventedRelated(Model):
        def call(self,stage,instruction,packet,schema):
            result=super().call(stage,instruction,packet,schema)
            if schema is AnalysisResult:
                from jira_service.analysis import SimilarIssue
                result.similar_issues=[SimilarIssue(key='ABS-999999',relation='possible_duplicate',reason='未经检索的猜测',evidence_ids=['E001'])]
            return result
    service=svc(tmp_path)
    result=analyze(service.settings,service.store,'COLOGNE-9001','bad-related',service.save_report,InventedRelated)
    assert result['failure'] and result['verdict']=='undetermined'
    assert 'ABS-999999' not in service.store.rows('SELECT body FROM reports')[0]['body']


def test_output_schema_requires_all_fields_recursively():
    from jira_service.analysis import strict_output_schema
    schema=strict_output_schema(AnalysisResult)
    def check(node):
        if isinstance(node,dict):
            if node.get('type')=='object' and 'properties' in node:
                assert set(node['required'])==set(node['properties'])
                assert node['additionalProperties'] is False
            assert 'default' not in node
            for value in node.values():check(value)
        elif isinstance(node,list):
            for value in node:check(value)
    check(schema)


def test_schema_evidence_ids_are_closed_to_actual_packet():
    from jira_service.analysis import strict_output_schema, AnalysisResult
    schema = strict_output_schema(AnalysisResult, ['E001', 'E002'])
    found=[]
    def visit(node):
        if isinstance(node,dict):
            for name,value in node.get('properties',{}).items():
                if name.endswith('evidence_ids'):
                    found.append(value['items']['enum'])
            for value in node.values():visit(value)
        elif isinstance(node,list):
            for value in node:visit(value)
    visit(schema)
    assert len(found)>=3
    assert all(ids==['E001','E002'] for ids in found)


def test_selected_scene_excludes_old_forensic_and_other_boot_logs(tmp_path):
    from jira_service.materials import sha256
    service=svc(tmp_path);key='COLOGNE-9001'
    log=service.settings.data/'scoped.log';log.write_text('OLD BOOT watchdog\nselected panic\nOLD TAIL watchdog\n')
    aid=service.store.artifact(key,'log',log,{'bytes':log.stat().st_size,'sha256':sha256(log)})
    scene={'id':'0123456789abcdef','artifact_id':aid,'filename':'scoped.log','line_start':2,'line_end':2,'raw_logs':[{'line':2,'text':'selected panic'}],'frames':[]}
    p=service.settings.data/'current.json';p.write_text(json.dumps({'schema':'jira-auto-forensic-v1','scenes':[scene],'findings':['selected finding']}))
    service.store.artifact(key,'forensic',p,{'role':'crash_preparation','scene_id':scene['id']})
    old=service.settings.data/'old.json';old.write_text(json.dumps({'schema':'jira-auto-forensic-v1','findings':['WRONG OLD FORENSIC']}))
    service.store.artifact(key,'forensic',old,{})
    class ScopedModel(Model):
        def call(self,stage,instruction,packet,schema):
            text='\n'.join(e['text'] for e in packet['evidence'])
            assert 'OLD BOOT' not in text and 'OLD TAIL' not in text and 'WRONG OLD' not in text
            assert 'selected panic' in text
            return super().call(stage,instruction,packet,schema)
    r=analyze(service.settings,service.store,key,'scope',service.save_report,ScopedModel,scene_id=scene['id'],scope_selection={'status':'selected'})
    assert not r['failure']


def test_unmatched_counter_evidence_does_not_erase_proven_direct_fault(tmp_path,monkeypatch):
    service=svc(tmp_path);key='COLOGNE-9001'
    p=service.settings.data/'fault.log';p.write_text('PC 0x70000010 MTVAL 0x14: fault load\n')
    service.store.artifact(key,'log',p,{'bytes':p.stat().st_size})
    def source(settings,store,key,term,pool):
        pool.add('source','other revision; not used for cause',path='other.c',line=1,revision_matched=False)
        return {'matches':1}
    monkeypatch.setattr('jira_service.analysis.search_source',source)
    class Proven(Model):
        def call(self,stage,instruction,packet,schema):
            if schema is SearchPlan:
                return SearchPlan(observations=[],log_terms=[],source_terms=['specific_function'],evidence_gaps=[])
            log_id=next(e['id'] for e in packet['evidence'] if e['kind']=='log')
            source_id=next(e['id'] for e in packet['evidence'] if e['kind']=='source')
            return AnalysisResult(verdict='confirmed',confidence='high',summary='直接异常已确认，上游待查',root_cause='load fault',
                facts=[{'finding':'原始日志记录load fault','evidence_ids':[log_id]}],
                hypotheses=[{'cause':'上游生命周期问题','supporting_evidence_ids':[log_id],'opposing_evidence_ids':[source_id],'assessment':'其他版本源码不能证明本次生命周期'}],
                gaps=['上游生命周期未确认'],next_steps=[],review_notes=[])
    result=analyze(service.settings,service.store,key,'counter-evidence',service.save_report,Proven)
    assert result['verdict']=='probable' and not result['failure']


def test_forensic_budget_retains_build_verification_and_parser_limits():
    from jira_service.analysis import forensic_findings
    findings=['现场回溯地址 '+str(i) for i in range(48)]+['ELF 与 dump 的 .text 一致；只覆盖该段。','分析边界：未遍历所有线程。']
    selected=forensic_findings({'findings':findings})
    assert len(selected)==32
    assert selected[:2]==findings[-2:]


def test_analysis_uses_exact_stage_artifact_even_if_another_report_is_newer(tmp_path):
    from jira_service.materials import sha256
    service=svc(tmp_path);key='COLOGNE-9001';scene='abc123'
    payload={'schema':'jira-auto-forensic-v1','findings':['本次未能验证 dump 代码字节。'],
             'scenes':[{'id':scene,'artifact_id':'log','filename':'log','line_start':1,'line_end':2,'raw_logs':[],'frames':[]}],'sources':[]}
    first=service.settings.data/'first.json';first.write_text(json.dumps(payload))
    aid=service.store.artifact(key,'forensic',first,{'role':'crash_preparation','scene_id':scene,'sha256':sha256(first)})
    second=service.settings.data/'second.json';second.write_text(json.dumps({**payload,'findings':['other parser output']}))
    service.store.artifact(key,'forensic',second,{'role':'crash_preparation','scene_id':scene,'sha256':sha256(second)})
    class Exact(Model):
        def call(self,stage,instruction,packet,schema):
            texts=[e['text'] for e in packet['evidence']]
            assert 'other parser output' not in texts
            assert '本次未能验证 dump 代码字节。' in texts
            return super().call(stage,instruction,packet,schema)
    result=analyze(service.settings,service.store,key,'exact',service.save_report,Exact,scene_id=scene,
                   scope_selection={'scene_id':scene},forensic_artifact_id=aid)
    assert not result['failure']
    first.write_text('{}')
    with pytest.raises(ValueError,match='校验失败'):
        analyze(service.settings,service.store,key,'corrupt',service.save_report,Exact,scene_id=scene,
                scope_selection={'scene_id':scene},forensic_artifact_id=aid)
    assert len(service.store.rows('SELECT id FROM reports'))==1


def test_target_source_search_never_uses_another_manifest_or_forensic_revision(tmp_path,monkeypatch):
    from jira_service.analysis import search_target_source,EvidencePool
    from jira_service.materials import sha256
    service=svc(tmp_path);key='COLOGNE-9001';revision='a'*40
    target=service.settings.data/'target.json'
    target.write_text(json.dumps({'schema':'jira-auto-forensic-v1','build':{'version':'1.2.3'},'scenes':[{'id':'scene'}],
                                 'sources':[{'path':'ui/App/test.c','revision':revision}]}))
    aid=service.store.artifact(key,'forensic',target,{'role':'crash_preparation','scene_id':'scene','sha256':sha256(target)})
    correct=service.settings.data/'correct.xml';correct.write_text('<manifest><project path="ui/App" revision="'+revision+'"/></manifest>')
    service.store.artifact(key,'manifest',correct,{'version':'1.2.3'})
    wrong=service.settings.data/'wrong.xml';wrong.write_text('<manifest><project path="ui" revision="'+('b'*40)+'"/></manifest>')
    service.store.artifact(key,'manifest',wrong,{'version':'9.9.9'})
    calls=[]
    def show(args,**kw):
        calls.append(args);return 'void targetFunction(void) {}'
    monkeypatch.setattr('jira_service.analysis.run_cli',show)
    result=search_target_source(service.settings,service.store,key,'targetFunction',EvidencePool(),'scene',aid)
    assert result['matches']==1
    assert calls[0][-1]==revision+':test.c'
    assert calls[0][2]==str(service.settings.source/'ui/App')


def test_selected_scene_in_older_log_is_not_excluded_by_three_file_budget(tmp_path):
    from jira_service.analysis import log_context,EvidencePool,readable_logs
    from jira_service.materials import log_evidence,sha256
    service=svc(tmp_path);key='COLOGNE-9001';selected=None
    for i in range(5):
        p=service.settings.data/('log'+str(i)+'.txt');p.write_text('outside scene\nneedle scene '+str(i)+'\nafter scene\n')
        aid=service.store.artifact(key,'log',p,{'sha256':sha256(p)})
        if i==0:selected=aid
    scope=[{'artifact_id':selected,'line_start':2,'line_end':2}]
    assert len(readable_logs(service.store,key,scope))==1
    pool=EvidencePool();counts=log_context(service.store,key,pool,['needle'],scope)
    assert counts=={'needle':1} and len(pool.items)==1
    assert pool.items[0]['artifact_id']==selected and pool.items[0]['line']==2
    assert pool.items[0]['text']=='needle scene 0'
    overview=log_evidence(service.store,key,scope=scope)
    assert len(overview['files'])==1 and overview['files'][0]['artifact_id']==selected


def test_prior_human_notes_are_bounded_attributed_leads_not_fresh_dump_evidence(tmp_path,monkeypatch):
    from jira_service.integrations import prior_comment_notes
    notes,window=prior_comment_notes({'comments':[{'id':str(i),'body':'联系方式: a@example.com\n已有分析指出堆碎片；详见dev@example.com\n'+'x'*1800} for i in range(8)],'startAt':0,'total':20})
    assert len(notes)==4 and not window['complete_page']
    assert all(len(n['text'])<1500 and 'example.com' not in n['text'] and '不是本轮' in n['provenance'] for n in notes)
    service=svc(tmp_path);i=service.store.issue('COLOGNE-9001');i.pop('fingerprint');i['prior_comments']=notes
    service.store.upsert_issue(i,fingerprint(i))
    class WithNotes(Model):
        def call(self,stage,instruction,packet,schema):
            prior=[e for e in packet['evidence'] if e['kind']=='jira_comment']
            assert len(prior)==4 and all('本工单' in e['provenance'] for e in prior)
            return super().call(stage,instruction,packet,schema)
    result=analyze(service.settings,service.store,'COLOGNE-9001','notes',service.save_report,WithNotes)
    assert not result['failure'] and result['verdict']=='insufficient_evidence'


def test_evidence_dedup_preserves_source_revision_path_and_comment_identity():
    from jira_service.analysis import EvidencePool
    p=EvidencePool()
    first=p.add('source','same code',path='ui/One.c',line=10,revision='a'*40)
    assert p.add('source','same code',path='ui/One.c',line=10,revision='a'*40)==first
    assert p.add('source','same code',path='ui/Two.c',line=10,revision='a'*40)!=first
    assert p.add('source','same code',path='ui/One.c',line=10,revision='b'*40)!=first
    assert p.add('jira_comment','same analysis',comment_id='one')!=p.add('jira_comment','same analysis',comment_id='two')
