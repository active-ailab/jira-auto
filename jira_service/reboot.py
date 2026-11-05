"""Reset classification is distinct from the physical/user trigger of a reboot."""
import collections
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from .integrations import run_cli

VERSION='reboot-2-context'


def reset_record(text):
    match=re.search(r'reset reason\s*\{([^}]+)\}\s*(\d+)\b',text,re.I)
    if not match:return None
    legend={int(n):name.upper() for n,name in re.findall(r'\(\s*(\d+)\s*:\s*([A-Z_]+)\s*\)',match[1],re.I)}
    code=int(match[2])
    return {'code':code,'type':legend.get(code,'UNKNOWN'),'mapping_source':'日志内枚举映射'}


def extract_reboots(lines,artifact_id,filename,partial=False,scope=None):
    resets=[];menu=None;recent=collections.deque(maxlen=300)
    for number,line in enumerate(lines,1):
        if scope and not any(s['artifact_id']==artifact_id and s['line_start']<=number<=s['line_end'] for s in scope):continue
        prefix=re.match(r'^\s*(\d+):\s*I>',line)
        original=int(prefix[1]) if prefix else number
        entry={'artifact_id':artifact_id,'filename':filename,'line':number,'original_line':original,'text':line.strip()[:1600]}
        if re.search(r'screenM\s+goto screen\s+HmReStartScreen\b',line):menu=entry
        reason=reset_record(line)
        if reason:
            # Nearby in the same boot segment is an association, never elapsed time.
            nearby=menu if menu and 0<original-menu['original_line']<=1000 else None
            context=[r for r in recent if re.search(r'8s pressed|5s pressed|reset source reg|_pm_reset|reboot send message|power manager call reboot|selConfirm|from Settings_systemScreen|HAL_KEY.*evt (?:4096|8192)|key.*(?:down|up)',r['text'],re.I)]
            resets.append({**entry,**reason,'preceding_menu':nearby,'context':context[-14:],'partial_input':partial})
            menu=None;recent.clear()
        recent.append(entry)
    return resets


def collect_reboots(settings,store,key,scope=None):
    events=[];files=[];omitted=0
    for a in store.artifacts(key):
        if a['kind']!='log':continue
        if scope and not any(s['artifact_id']==a['id'] for s in scope):continue
        if len(files)>=8:omitted+=1;continue
        path=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(a['id'],))[0]['path']).resolve();path.relative_to(settings.data)
        if not path.is_file() or path.stat().st_size>128*1024*1024:omitted+=1;continue
        files.append(a['id'])
        with path.open('rb') as f:
            events.extend(extract_reboots((line.decode('utf8','replace') for line in f),a['id'],a['filename'],a['metadata'].get('partial',False),scope))
    counts=dict(collections.Counter(e['type'] for e in events))
    return {'version':VERSION,'counts':counts,'event_count':len(events),'menu_before_reset':sum(bool(e['preceding_menu']) for e in events),
            'long_hold_before_reset':sum(any('8s pressed' in r['text'] for r in e.get('context',[])) for e in events),'events':events[:64],'events_truncated':len(events)>64,'omitted_files':omitted,
            'scope_note':('仅统计选定崩溃现场的行号范围，不代表完整日志的重启总数。' if scope else '只统计本轮已导入且已读取的日志；用户节选不代表全量记录。')+'菜单先后关系不等于按压时长或因果证明。',
            'limits':['POR是复位类型，不直接证明长按、进水、硬件故障或用户误按。',
                      'HmReStartScreen也可能来自设置菜单；需按键down/up、长按事件和硬件复位源核对触发链。']}


def add_reboot_evidence(report,pool):
    if not report['event_count']:return
    summary={k:v for k,v in report.items() if k!='events'}
    pool.add('forensic','重启记录分类：'+json.dumps(summary,ensure_ascii=False),role='reboot_classification')
    ordered=sorted(report['events'],key=lambda e:(e['type']=='POR',not any('8s pressed' in r['text'] for r in e.get('context',[])),not bool(e['preceding_menu'])))
    for event in ordered[:8]:
        for raw in [event.get('preceding_menu'),*event.get('context',[]),event]:
            if not raw:continue
            pool.add('log',raw['text'],artifact_id=raw['artifact_id'],line=raw['line'],original_line=raw['original_line'],
                     provenance='用户提供日志节选，原始行号见original_line' if event['partial_input'] else '已导入日志')


def add_reboot_sources(settings,store,key,pool):
    issue=store.issue(key)
    manifest=next((a for a in store.artifacts(key) if a['kind']=='manifest' and a['metadata'].get('version')==issue.get('firmware') and a['metadata'].get('device')==issue.get('device')),None)
    if not manifest:return {'status':'no_target_manifest'}
    path=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(manifest['id'],))[0]['path']).resolve();path.relative_to(settings.data)
    from .materials import inspect_manifest
    if inspect_manifest(path)['sha256']!=manifest['metadata']['sha256']:raise ValueError('重启分析的目标manifest校验失败')
    projects=ET.parse(path).getroot().findall('project');added=0
    targets={
        'components/fs/storage/src/app_fs.c':['key[%d] 8s pressed'],
        'packages/services/syssevice/sys_keyhandler/sys_keyhandler.c':['static void systemRestart','HM_KEY_ACTION_LONGLONG_PRESS'],
        'ui/Settings/settings/src/restart/hm_restart.c':['进入界面的入口flag','static void onNewScreenData'],
        'ui/Settings/settings/src/system/settings_system.c':['H_Uint8 restart = FROM_SETTINGS'],
        'platform/mcu/mhs003/hmos/drivers/cpuinfo/cpuinfo_mhs003.c':['hal_get_bootup_reason_with_source','HAL_BOOTUP_EVENT_POR'],
        'platform/mcu/mhs003/sdk/drivers/mhs_hal.c':['hal_bootup_event_e hal_get_bootup_reason_with_source'],
    }
    for relative,terms in targets.items():
        choices=[p for p in projects if relative.startswith((p.get('path') or p.get('name'))+'/')]
        if not choices:continue
        project=max(choices,key=lambda p:len(p.get('path') or p.get('name')));folder=project.get('path') or project.get('name');revision=project.get('revision','')
        if not re.fullmatch(r'[a-fA-F0-9]{40}',revision):continue
        repo=(settings.source/folder).resolve();repo.relative_to(settings.source)
        try:content=run_cli(['git','-C',str(repo),'show',revision+':'+relative[len(folder)+1:]],timeout=20)
        except (RuntimeError,OSError):continue
        lines=content.splitlines()
        for term in terms:
            n=next((i for i,line in enumerate(lines) if term in line),None)
            if n is None:continue
            text='\n'.join(f'{i+1}: {lines[i]}' for i in range(max(0,n-5),min(len(lines),n+20)))
            pool.add('source',text,path=relative,line=n+1,revision=revision,revision_matched=True,
                     provenance='工单版本manifest对应源码；尚未证明设备现场构建完全一致')
            added+=1
    return {'status':'target_sources' if added else 'no_local_target_sources','excerpts':added}
