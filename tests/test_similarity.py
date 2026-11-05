import json
from jira_service.similarity import search_recent
from jira_service.analysis import AnalysisResult, EvidencePool, render_comment
from jira_service.settings import Settings


def test_search_reads_recent_resolved_and_keeps_evidence_provenance(tmp_path,monkeypatch):
    calls=[]
    def cli(args,timeout):
        calls.append(args)
        if 'list' in args:
            return json.dumps([{'key':'ABS-9002','fields':{'summary':'Geneva Monkey','status':{'name':'Resolved'}}}])
        return 'PC 0x706b7ace MTVAL 0x14 Fault on thread SBEngine\n_beginPicker SportSwimDrillContainer.c:595\nsContainer=NULL active=true'
    monkeypatch.setattr('jira_service.similarity.run_cli',cli)
    result=search_recent(Settings(tmp_path,tmp_path),{'key':'ABS-9001','summary':'Geneva Monkey死机','device':'Geneva'},[{'text':'PC=0x706b7ace _beginPicker sContainer=NULL'}],['_beginPicker'],lambda *a:None)
    assert result['candidates'][0]['key']=='ABS-9002'
    assert result['candidates'][0]['status']=='Resolved'
    assert '未重新下载' in result['candidates'][0]['provenance']
    assert all('resolution IS EMPTY' not in arg for call in calls for arg in call)
    assert any('updated >= -90d' in arg and 'ABS-9001' in arg for call in calls for arg in call)
    assert len([c for c in calls if 'view' in c])<=8


def test_unavailable_search_is_partial_not_no_similar_issue(tmp_path,monkeypatch):
    def fail(*a,**kw):raise RuntimeError('offline')
    monkeypatch.setattr('jira_service.similarity.run_cli',fail)
    result=search_recent(Settings(tmp_path,tmp_path),{'key':'ABS-9001','summary':'崩溃','device':'Geneva'},[{'text':'_beginPicker fault'}],[],lambda *a:None)
    assert result['status']=='partial' and result['errors']>0


def test_comment_is_short_and_keeps_only_decisive_evidence():
    pool=EvidencePool();pool.add('log','PC=0x706b7ace MTVAL=0x14',artifact_id='private-id',line=425532)
    result=AnalysisResult(verdict='probable',confidence='medium',summary='长篇分析'*200,root_cause='空指针读访问',facts=[{'finding':'SBEngine 访问 0x14。','evidence_ids':['E001']}],hypotheses=[],gaps=['触发时序待确认'],next_steps=['很多验证步骤']*100,review_notes=['很多审阅提示']*100,
      comment_conclusion='长按暂停进入_beginPicker时sContainer为空，访问0x14崩溃。',comment_evidence=[{'finding':'PC=0x706b7ace，MTVAL=0x14；dump中指针为0。','evidence_ids':['E001']}])
    comment=render_comment(result,pool)
    assert len(comment)<350
    assert '日志L425532' in comment and 'private-id' not in comment
    assert '候选原因' not in comment and '验证步骤' not in comment and '审阅提示' not in comment


def test_comparison_rejects_shared_allocator_as_duplicate_and_exposes_conflict():
    from jira_service.similarity import compare_candidate
    scene={'firmware':'3.26.1.2','frames':[{'inline':[{'function':'failure_hook_check'},{'function':'LabelLayer_createTextSprite'}]}]}
    evidence=[{'text':'malloc failed for size 1843276 vApplicationMallocFailedHook'}]
    candidate={'summary':'Geneva','snippets':['malloc failed for size 67584 failure_hook_check']}
    comparison=compare_candidate({'firmware':'3.26.1.2'},evidence,[],candidate,scene)
    assert comparison['prohibits_duplicate'] and comparison['level']=='lead_only'
    assert any('分配大小不同' in x for x in comparison['different'])
    candidate['snippets']=['PC 0x70000010 MTVAL 0xc fault load LabelLayer_createTextSprite']
    comparison=compare_candidate({},evidence,[],candidate,scene)
    assert comparison['level']=='incompatible' and comparison['prohibits_duplicate']


def test_same_business_function_and_fault_family_are_ranked_as_overlap():
    from jira_service.similarity import compare_candidate
    scene={'firmware':'3.26.1.2','registers':{'pc':'0x706b7ace'},'frames':[{'inline':[{'function':'_beginPicker'}]}]}
    comparison=compare_candidate({},[{'text':'fault load PC=0x706b7ace'}],[],{'summary':'【Geneva-3.26.1.2】','snippets':['fault load PC 0x706b7ace _beginPicker']},scene)
    assert comparison['level']=='business_overlap' and comparison['common_functions']==['_beginPicker']
    assert not comparison['prohibits_duplicate']


def test_fix_ancestry_verifies_git_history_without_claiming_complete_firmware(tmp_path):
    import subprocess
    from jira_service.similarity import verify_fix_ancestry
    repo=tmp_path/'map';repo.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
    git('init');(repo/'f.c').write_text('first')
    git('add','f.c');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fix')
    fix=git('rev-parse','HEAD')
    (repo/'f.c').write_text('second');git('add','f.c');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','next')
    target=git('rev-parse','HEAD')
    evidence=[{'kind':'source','path':'map/f.c','revision':target,'revision_matched':True}]
    proof=verify_fix_ancestry(Settings(tmp_path/'data',tmp_path),evidence,['修复提交 '+fix])
    assert proof['status']=='ancestor_verified' and proof['checks'][0]['is_ancestor']
    assert '不能证明完整固件入包' in proof['note']
    assert verify_fix_ancestry(Settings(tmp_path,tmp_path),evidence,['已解决'])['status']=='unknown'
