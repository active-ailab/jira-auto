"""Resumable preparation and analysis. Quality archives are handed off before analysis; comments require separate approval."""
import json
from pathlib import Path
from .stages import StageRunner, artifact_files
from .crash_scene import PARSER_VERSION
from .materials import fetch_artifacts,build_device


def prepare(service,key,rid,payload,with_ai=True):
    from .attachments import prepare_attachments
    from .crash import scene_inventory,prepare_crash
    settings,store=service.settings,service.store
    issue=service.refresh(key)  # Always refresh; a retry must see changed attachments.
    stages=StageRunner(store,key,rid,payload.get('force_from'))
    stages.run('attachments',{'v':5,'attachments':issue.get('attachments',[])},
        lambda:prepare_attachments(settings,store,key,rid),
        lambda _:artifact_files(store,key,{'log','binary_log','image','image_unavailable','log_archive','jira_attachment'}))
    quality_state=service.quality_collection(key) or {}
    unarchived={a['metadata'].get('sha256') for a in store.artifacts(key) if a['kind']=='log_archive' and a['metadata'].get('source_platform')=='quality'}-{a['metadata'].get('sha256') for a in store.artifacts(key) if a['kind']=='jira_attachment'}
    if issue.get('feedback'):
        from .quality_client import collect
        needs_collection=(not any(a['kind'] in ('log','binary_log') for a in store.artifacts(key)) or unarchived or (quality_state and quality_state.get('state')!='archived'))
        stages.run('quality_collection',{'feedback':issue['feedback'],'needs_collection':bool(needs_collection)},
            lambda:collect(service,key,rid) if needs_collection else {'state':'existing_materials'},cache=False)
        issue=store.issue(key)
    from .quality import require_quality_materials
    require_quality_materials(service,key,rid)
    material_files=artifact_files(store,key,{'log','binary_log'})
    inventory=stages.run('scene_inventory',{'v':PARSER_VERSION,'files':material_files,'device':issue.get('device'),'firmware':issue.get('firmware'),'selected_id':payload.get('scene_id')},
        lambda:scene_inventory(settings,store,key,rid,payload.get('scene_id')))
    selection=inventory['selection']
    if selection['status']=='needs_selection':
        store.event('scene_selection',selection['reason'],key,rid,{'scene_ids':[s['id'] for s in inventory['scenes']]})
        raise ValueError(selection['reason'])
    if selection['scene_id']:
        scene=next(s for s in inventory['scenes'] if s['id']==selection['scene_id'])
        version=scene.get('firmware') or issue.get('firmware');device=issue.get('device')
        def build():
            # fetch_artifacts uses elf-fetch's existing cache, but all outputs are revalidated.
            return fetch_artifacts(settings,store,key,device,version,rid)
        stages.run('build',{'device':device,'version':version},build,
            lambda _: [f for f in artifact_files(store,key,{'manifest','elf','map'}) if '/'+str(build_device(device))+'/'+str(version)+'/' in '/'+f['path']])
        build_files=artifact_files(store,key,{'manifest','elf','map'})
        parsed=stages.run('crash_parse',{'v':PARSER_VERSION,'inventory':inventory,'builds':build_files},
            lambda:prepare_crash(settings,store,key,rid,inventory),
            lambda value:[{'path':value['path'],'sha256':value['sha256']}] if value.get('path') else [])
    else:
        parsed={'status':'no_scene','selection':selection}
        from .reboot import collect_reboots
        restarts=collect_reboots(settings,store,key)
        if restarts['event_count'] and issue.get('device') and issue.get('firmware'):
            from .materials import fetch_manifest
            stages.run('build',{'mode':'reboot_manifest','device':issue['device'],'version':issue['firmware']},
                lambda:fetch_manifest(settings,store,key,issue['device'],issue['firmware'],rid),
                lambda _:artifact_files(store,key,{'manifest'}))
    if not with_ai:return {'inventory':inventory,'crash':parsed}
    feedback=None
    if payload.get('review_request_id'):
        rows=store.rows('SELECT report_id,reason,created FROM report_reviews WHERE id=? AND issue_key=?',(payload['review_request_id'],key))
        if not rows:raise ValueError('补充分析要求不存在或不属于本工单')
        feedback=rows[0]
    from .analysis import analyze
    # A report is an approval revision: never reuse an old report as a fresh approval.
    # Individual successful model calls are cached by their exact evidence packet instead.
    return stages.run('ai_analysis',{'issue':store.issue(key)['fingerprint'],'scene_id':selection['scene_id'],'v':PARSER_VERSION},
        lambda:analyze(settings,store,key,rid,service.save_report,force_model=payload.get('force_from')=='ai_analysis',scene_id=selection['scene_id'],scope_selection=selection,review_feedback=feedback,forensic_artifact_id=parsed.get('artifact_id')),cache=False)
