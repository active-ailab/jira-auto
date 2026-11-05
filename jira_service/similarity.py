"""Bounded, read-only Jira CLI search for comparable crash evidence."""
import json
import re
import subprocess
from pathlib import Path
from .integrations import run_cli, issue_key


def phrase(term):
    return json.dumps('"'+term+'"',ensure_ascii=False)


RUNTIME_FUNCTIONS={'failure_hook_check','ummMalloc','osTagCapsMalloc','_malloc','_greal_malloc_internal','_greal_calloc_internal','_greal_calloc','vg_lite_hal_allocate_contiguous','_vg_lite_allocate','xTaskResumeAll','osAssertHandler','osAbortHandler','vApplicationMallocFailedHook','vglite_surface_create','rext_canvas_create_canvas_v2'}


def fault_family(text):
    families=[]
    if re.search(r'malloc failed|MallocFailedHook|内存申请失败|内存分配失败',text,re.I):families.append('oom_abort')
    if re.search(r'fault load|load fault|加载访问异常',text,re.I):families.append('load_fault')
    if re.search(r'fault store|store fault|存储访问异常',text,re.I):families.append('store_fault')
    if re.search(r'watchdog.*(?:timeout|expired)|看门狗超时',text,re.I):families.append('watchdog')
    return families[0] if len(families)==1 else 'unknown'


def compare_candidate(issue,evidence,source_terms,candidate,scene=None):
    current_text='\n'.join(e['text'] for e in evidence)
    history='\n'.join(candidate['snippets'])
    functions=list(dict.fromkeys(loc['function'] for f in (scene or {}).get('frames',[]) for loc in f.get('inline',[]) if loc['function']!='??')) or source_terms
    business=[f for f in functions if f not in RUNTIME_FUNCTIONS and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{3,80}',f)]
    common=[f for f in business if re.search(r'(?<![A-Za-z0-9_])'+re.escape(f)+r'(?![A-Za-z0-9_])',history)]
    a=fault_family('\n'.join(x['text'] for x in (scene or {}).get('raw_logs',[]))) if scene and scene.get('raw_logs') else fault_family(current_text);b=fault_family(history);same=[];different=[];unknown=[]
    if common:same.append('历史文本提及相同业务函数：'+', '.join(common[:5]))
    if a!='unknown' and b!='unknown':
        (same if a==b else different).append('异常机制：本单 '+a+'，历史文本 '+b)
    else:unknown.append('历史或当前异常机制未完整提取')
    current_version=(scene or {}).get('firmware') or issue.get('firmware')
    m=re.search(r'[【\[][^】\]]*?-(\d+(?:\.\d+){2,4})[】\]]',candidate.get('summary',''))
    old_version=m[1] if m else None
    if current_version and old_version and current_version!=old_version:different.append('版本不同：'+current_version+' / '+old_version+'；地址不能直接比较')
    reg=(scene or {}).get('registers',{});pc=reg.get('pc')
    hpc=re.search(r'\bPC\s*[:=]?\s*(0x[0-9a-fA-F]+)',history,re.I)
    if pc and hpc and current_version and current_version==old_version and int(pc,16)==int(hpc[1],16):same.append('同版本PC相同；仍需结合函数和对象状态')
    def sizes(text):return sorted({int(n) for n in re.findall(r'(?:malloc failed for size|申请|分配)\s*(\d{4,})',text,re.I)})
    sizes_a,sizes_b=sizes(current_text),sizes(history)
    if sizes_a and sizes_b and not set(sizes_a)&set(sizes_b):different.append('分配大小不同：'+str(sizes_a)+' / '+str(sizes_b))
    conflict=a!='unknown' and b!='unknown' and a!=b
    prohibited=conflict or not common
    level='incompatible' if conflict else 'business_overlap' if common and a==b and a!='unknown' else 'lead_only'
    if not common:unknown.append('没有可对照的相同业务栈；公共内存/调度函数不构成同因证明')
    unknown.append('历史附件未重新解析，比较依据是已有描述和评论')
    return {'level':level,'common_functions':common,'same':same,'different':different,'unknown':unknown,
            'prohibits_duplicate':level!='business_overlap','rank':len(common)*4+(2 if a==b and a!='unknown' else 0)-(10 if conflict else 0)}


def verify_fix_ancestry(settings,evidence,snippets):
    text='\n'.join(snippets)
    commits=list(dict.fromkeys(re.findall(r'(?:修复提交|fix(?:ed)? commit|commit)\s*[:：=]?\s*([0-9a-fA-F]{40})(?![0-9a-fA-F])',text,re.I)))[:2]
    if not commits:return {'status':'unknown','note':'历史文本未提供可核实的完整修复提交；已解决状态不代表修复入包','checks':[]}
    repos={}
    for e in sorted(evidence,key=lambda e:0 if str(e.get('path','')).startswith(('ui/','framework/')) else 1):
        if e['kind']!='source' or not e.get('revision_matched') or not re.fullmatch(r'[a-fA-F0-9]{40}',e.get('revision','')):continue
        path=(settings.source/e.get('path','')).resolve()
        try:path.relative_to(settings.source)
        except ValueError:continue
        if not path.parent.is_dir():continue
        try:
            result=subprocess.run(['git','-C',str(path.parent),'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=5)
            if result.returncode:continue
            root=Path(result.stdout.strip()).resolve();root.relative_to(settings.source)
            repos[(str(root),e['revision'])]=True
        except (ValueError,OSError,subprocess.TimeoutExpired):continue
        if len(repos)>=3:break
    checks=[]
    for commit in commits:
        for root,target in repos:
            try:
                exists=subprocess.run(['git','-C',root,'cat-file','-e',commit+'^{commit}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
                if exists.returncode:continue
                check=subprocess.run(['git','-C',root,'merge-base','--is-ancestor',commit,target],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                if check.returncode not in (0,1):continue
                checks.append({'commit':commit,'target':target,'project':str(Path(root).relative_to(settings.source)),'is_ancestor':check.returncode==0})
            except (OSError,subprocess.TimeoutExpired):continue
    status='not_ancestor' if any(not c['is_ancestor'] for c in checks) else 'ancestor_verified' if checks and all(any(c['commit']==commit for c in checks) for commit in commits) else 'unknown'
    return {'status':status,'note':'仅核对历史文本给出的提交与目标源码祖先关系；不能证明完整固件入包或修复有效性，非祖先也未排除cherry-pick/等效修复','checks':checks}


def search_recent(settings, issue, evidence, source_terms, emit,scene=None):
    texts='\n'.join(e['text'] for e in evidence)
    if not re.search(r'crash|fault|panic|崩溃|死机|ramdump|watchdog|malloc failed|MallocFailedHook',issue['summary']+'\n'+texts,re.I):
        return {'status':'not_crash','days':90,'candidates':[]}
    terms=[loc['function'] for frame in (scene or {}).get('frames',[]) for loc in frame.get('inline',[]) if loc['function'] not in RUNTIME_FUNCTIONS and loc['function']!='??'][:5]
    # Functions retain meaning across builds; PCs alone do not.
    for term in source_terms:
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{4,70}',term) and term in texts:terms.append(term)
    terms+=re.findall(r'\b(_[A-Za-z][A-Za-z0-9_]{5,60})\b',texts)
    terms+=re.findall(r'\b([A-Za-z][A-Za-z0-9_]{6,60}(?:Container|HandleLongPressPause))\b',texts)
    pcs=re.findall(r'\bPC\s*=?\s*(0x[0-9a-fA-F]{8})',texts)
    terms=list(dict.fromkeys(terms+pcs))[:6]
    project=issue_key(issue['key']).split('-')[0]
    common=f'project IS NOT EMPTY AND issuetype = Bug AND updated >= -90d AND key != {issue["key"]}'
    queries=[]
    if terms:queries.append(common+' AND ('+' OR '.join('text ~ '+phrase(t) for t in terms)+')')
    device=issue.get('device')
    if device and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',device):
        queries.append(common+' AND summary ~ '+phrase(device)+' AND (summary ~ '+phrase('死机')+' OR summary ~ '+phrase('monkey')+' OR summary ~ '+phrase('崩溃')+')')
    if not queries:
        queries.append(common+f' AND project = {project} AND (summary ~ '+phrase('死机')+' OR summary ~ '+phrase('崩溃')+')')
    emit('similar','检索近90天更新的同类崩溃工单，包含已解决问题',{'terms':terms,'days':90})
    found={};errors=0;truncated=False
    for q in queries[:2]:
        try:
            raw=json.loads(run_cli([settings.jira_bin,'issue','list','--jql',q,'--order-by','updated','--raw','--paginate','0:30'],timeout=40))
            rows=raw if isinstance(raw,list) else raw.get('issues',[])
            truncated|=len(rows)>=30
            for r in rows:
                key=issue_key(r['key'])
                if key==issue['key']:continue
                found.setdefault(key,r)
        except (RuntimeError,ValueError,KeyError,OSError):errors+=1
    candidates=[]
    for key,raw in list(found.items())[:8]:
        try:
            text=run_cli([settings.jira_bin,'issue','view',key,'--plain','--comments','20'],timeout=30)
        except (RuntimeError,OSError):
            errors+=1;continue
        text=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',text)
        lines=text.splitlines();snippets=[]
        for line in lines:
            if any(t.casefold() in line.casefold() for t in terms) or re.search(r'PC\s*[:=]?\s*0x|MTVAL|Fault on thread|Assert on thread|malloc failed|MallocFailedHook|修复提交|fixed commit|sContainer\s*[=：]|active\s*=|关键调用链|已修复|修复提交',line,re.I):
                line=re.sub(r'\S+@\S+','[邮箱]',line.strip())
                if line and line not in snippets:snippets.append(line[:400])
            if len(snippets)>=14:break
        fields=raw.get('fields',{})
        matched=[term for term in terms if term.casefold() in '\n'.join(snippets).casefold()]
        if not snippets:continue
        candidate={'key':key,'summary':fields.get('summary',''),'status':(fields.get('status') or {}).get('name','未知'),
                   'updated':fields.get('updated'),'matched_terms':matched,'snippets':snippets,
                   'url':'https://jira.huami.com/browse/'+key,
                   'provenance':'来自 Jira 工单描述/最近20条评论；未重新下载或解析该工单附件，既有结论需人工核对'}
        candidate['comparison']=compare_candidate(issue,evidence,source_terms,candidate,scene)
        candidate['fix_inclusion']=verify_fix_ancestry(settings,evidence,snippets)
        candidates.append(candidate)
    candidates.sort(key=lambda x:(x['comparison']['rank'],len(x['matched_terms'])),reverse=True)
    result={'status':'partial' if errors else 'searched','days':90,'queries':queries,'candidate_limit':8,
            'truncated':truncated or len(found)>8,'errors':errors,'candidates':candidates[:6]}
    emit('similar','相似工单检索完成；仅提供关联建议，不自动判重或流转',{'status':result['status'],'candidates':[x['key'] for x in result['candidates']],'truncated':result['truncated']})
    return result
