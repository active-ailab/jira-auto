"""Read-only review projection; published bodies and old model records stay immutable."""
import json
import re


def split_legacy_cause(text):
    # Only split explicit labels, never infer a confirmation grade from prose.
    match=re.search(r'(?:更深层(?:原因|诱因)|深层(?:原因|诱因)|待查诱因)(?:仍待确认|尚未确认|待确认)?\s*[:：]',text)
    direct=text[:match.start()].strip() if match else text
    direct=re.sub(r'^直接原因\s*[:：]\s*','',direct)
    return direct,(text[match.end():].strip() if match else '')


def projection(report):
    analysis=json.loads(report['analysis']) if isinstance(report.get('analysis'),str) else report.get('analysis') or {}
    result=analysis.get('result',{})
    evidence=analysis.get('evidence',[]);by_id={e['id']:e for e in evidence}
    direct=result.get('direct_cause');underlying=result.get('underlying_cause')
    legacy=not isinstance(direct,dict)
    if legacy:
        first,second=split_legacy_cause(result.get('root_cause') or result.get('summary') or '')
        direct={'status':'legacy','explanation':first,'evidence_ids':[]}
        underlying={'status':'legacy' if second else 'unknown','explanation':second or '历史报告未单独记录深层诱因，不能从整体置信度推定。','evidence_ids':[]}
    if not underlying:underlying={'status':'unknown','explanation':'尚未记录深层诱因判断。','evidence_ids':[]}
    code=[]
    explanations=result.get('code_explanation') or result.get('facts',[])
    for claim in explanations:
        refs=[by_id[x] for x in claim.get('evidence_ids',[]) if x in by_id]
        sources=[e for e in refs if e['kind']=='source']
        if result.get('code_explanation') or sources:
            code.append({'explanation':claim['finding'],'evidence_ids':claim.get('evidence_ids',[]),'sources':sources})
    # Address stack source locations remain usable even for reports predating code_explanation.
    if not code:
        code=[{'explanation':e.get('path','源码')+':'+str(e.get('line','')),'evidence_ids':[e['id']],'sources':[e]} for e in evidence if e['kind']=='source'][:6]
    scenes=[]
    for original in analysis.get('crash_scenes',[]):
        scene=dict(original);raw='\n'.join(x['text'] for x in scene.get('raw_logs',[]))
        thread=re.search(r'(?:Assert|Fault) on thread\s+(\S+)',raw)
        version=re.search(r'software version:\s*([\d.]+)',raw)
        if not scene.get('thread') and thread:scene['thread']=thread[1]
        if not scene.get('firmware') and version:scene['firmware']=version[1]
        if not scene.get('classification'):
            scene['classification']='oom_abort' if 'malloc failed' in raw and 'MallocFailedHook' in raw else 'fault' if 'Fault on thread' in raw else 'assert' if 'Assert on thread' in raw else None
        scenes.append(scene)
    return {'report_id':report['id'],'kind':report['kind'],'status':report['status'],'created':report['created'],
        'body_hash':report['body_hash'],'body':report['body'],'manual_edit':bool(analysis.get('manual_edit')),
        'legacy':legacy,'scenes':scenes,'summary':result.get('summary',''),
        'direct_cause':direct,'underlying_cause':underlying,'code':code,'gaps':result.get('gaps',[]),
        'similarity':analysis.get('similarity',{}),'next_steps':result.get('next_steps',[]),'evidence':evidence,'similar_issues':result.get('similar_issues',[]),
        'detail_text':analysis.get('detail_text',''),'run_id':report.get('run_id'),
        'reboots':analysis.get('reboots',{}),'images':analysis.get('images',[]),'images_not_viewed':analysis.get('images_not_viewed',[]),
        'execution_failed':report['kind']=='analysis_failed','model_completed':analysis.get('model_completed')}
