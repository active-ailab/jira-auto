from jira_service.reboot import reset_record,extract_reboots,collect_reboots,add_reboot_evidence
from jira_service.analysis import EvidencePool,analyze
from jira_service.materials import sha256,log_evidence
from test_analysis import svc,Model

RESET='I>0>0.021 SYS_INIT reset reason {(0: POR), (1:NRST), (2: WDT), (3: SOFTWARE), (4: EXCEPTION), (5: ASSERT),(6: POWEROFF),(7: HARDWARE_EXCEPTION), (8: UNKNOWN)} '


def test_reset_type_uses_actual_trailing_value_not_words_in_enum():
    assert reset_record(RESET+'0')['type']=='POR'
    assert reset_record(RESET+'2')['type']=='WDT'
    assert reset_record(RESET+'5')['type']=='ASSERT'
    assert reset_record('reset reason {(0: WDT), (4: POR)} 0')['type']=='WDT'
    assert reset_record('reset reason: 0') is None


def test_restart_menu_is_only_a_same_segment_nearby_lead_not_cause_or_duration():
    text='100: I>8.0 screenM goto screen HmReStartScreen\n110: '+RESET+'0\n120: '+RESET+'0\n200: I>4 screenM goto screen LauncherScreen\n210: '+RESET+'2\n'
    events=extract_reboots(text.splitlines(),'a','user excerpt',True)
    assert len(events)==3 and events[0]['preceding_menu']['original_line']==100
    assert events[0]['line']==2 and events[0]['original_line']==110
    assert events[1]['preceding_menu'] is None and events[2]['preceding_menu'] is None
    assert all(e['partial_input'] for e in events)
    assert 'duration' not in str(events)
    assert extract_reboots(('10: I>2 screenM goto screen HmReStartScreen\n2000: '+RESET+'0').splitlines(),'a','x')[0]['preceding_menu'] is None


def test_enum_legend_is_not_a_software_crash_and_partial_reboot_evidence_reaches_model(tmp_path,monkeypatch):
    monkeypatch.setattr('jira_service.similarity.search_recent',lambda *a,**kw:{'status':'searched','candidates':[]})
    service=svc(tmp_path);key='COLOGNE-9001';path=service.settings.data/'excerpt.log'
    path.write_text('100: I>8 screenM goto screen HmReStartScreen\n110: '+RESET+'0\n120: '+RESET+'0\n')
    aid=service.store.artifact(key,'log',path,{'sha256':sha256(path),'partial':True})
    report=collect_reboots(service.settings,service.store,key)
    assert report['counts']=={'POR':2} and report['menu_before_reset']==1
    assert log_evidence(service.store,key)['exception_candidates']==[]
    pool=EvidencePool();add_reboot_evidence(report,pool)
    assert any(e.get('original_line')==110 and e['line']==2 for e in pool.items)
    class RebootModel(Model):
        def call(self,stage,instruction,packet,schema):
            assert packet['reboots']['counts']=={'POR':2}
            assert any(e.get('role')=='reboot_classification' for e in packet['evidence'])
            assert packet['reboots']['limits']
            return super().call(stage,instruction,packet,schema)
    result=analyze(service.settings,service.store,key,'reset-review',service.save_report,RebootModel)
    assert not result['failure'] and result['analysis']['reboots']['counts']=={'POR':2}
    assert result['analysis']['reboots']['events'][0]['artifact_id']==aid


def test_compact_comment_preserves_trigger_and_physical_cause_limit_after_first_sentence():
    from jira_service.analysis import AnalysisResult,render_comment
    result=AnalysisResult(verdict='insufficient_evidence',confidence='low',summary='POR',root_cause='trigger unknown',facts=[],hypotheses=[],gaps=[],next_steps=[],review_notes=[],
       comment_conclusion='已记录POR复位。优先核查持续按键和强制复位，尚不能区分人为长按、误触、按键卡滞或进水短路。',
       comment_caveat='二进制附件未解析；需key down/up和reset source reg。')
    body=render_comment(result,EvidencePool())
    assert '优先核查持续按键' in body and '进水短路' in body
    assert '需key down/up和reset source reg' in body
    assert len(body)<400


def test_duplicate_log_observation_retains_later_original_line_provenance():
    pool=EvidencePool();eid=pool.add('log','110: reset',artifact_id='a',line=2)
    assert pool.add('log','110: reset',artifact_id='a',line=2,original_line=110,provenance='用户节选')==eid
    assert pool.items[0]['original_line']==110 and pool.items[0]['provenance']=='用户节选'


def test_narrative_comment_orders_scoped_process_cause_then_conclusion_with_refs():
    import pytest
    from jira_service.analysis import AnalysisResult,render_comment,validate_response_refs,strict_output_schema
    pool=EvidencePool()
    log=pool.add('log','reset POR',line=2,original_line=110)
    source=pool.add('source','long press opens menu',path='sys/key.c',line=270,revision_matched=True)
    result=AnalysisResult(verdict='insufficient_evidence',confidence='low',summary='summary',root_cause='',facts=[],hypotheses=[],gaps=[],next_steps=[],review_notes=[],
        comment_process=[{'finding':'提供的节选共13次重启，均为POR；其中5次此前出现重启菜单。','evidence_ids':[log]}],
        comment_reason=[{'finding':'超长按可打开菜单，但设置也可进入，尚不能确认触发来源。','evidence_ids':[source]}],
        comment_conclusion='优先排查持续按键信号及强制复位路径，诱因待验证。',
        comment_evidence=[{'finding':'备查重复事实','evidence_ids':[log]}])
    body=render_comment(result,pool)
    assert body.index('问题过程：')<body.index('原因分析：')<body.index('结论（证据不足）：')
    assert '节选原L110' in body and 'key.c:270' in body
    assert '备查重复事实' not in body and len(body)<350
    schema=strict_output_schema(AnalysisResult,[log,source])
    assert {'comment_process','comment_reason'} <= set(schema['required'])
    validate_response_refs(result,{'evidence':pool.items})
    result.comment_reason[0].evidence_ids=[]
    with pytest.raises(ValueError,match='事实没有证据引用'):
        validate_response_refs(result,{'evidence':pool.items})


def test_reboot_context_retains_software_request_and_hold_evidence_in_lf_numbering(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001';p=service.settings.data/'fw.log'
    p.write_bytes(b'I>0 audio odd\rdata\r\nI>1 FS key[8] 8s pressed...\r\n'+(RESET+'0\r\n').encode()+b'I>2 hm_power reboot send message reboot\r\nI>3 PM_LOCKS _pm_reset ...\n'+(RESET+'3\n').encode())
    service.store.artifact(key,'log',p,{'sha256':sha256(p)})
    r=collect_reboots(service.settings,service.store,key)
    assert r['counts']=={'POR':1,'SOFTWARE':1}
    assert r['long_hold_before_reset']==1 and r['events'][0]['line']==3
    pool=EvidencePool();add_reboot_evidence(r,pool)
    assert any('_pm_reset' in e['text'] for e in pool.items)
    assert any('8s pressed' in e['text'] for e in pool.items)
