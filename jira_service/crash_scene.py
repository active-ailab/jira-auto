"""Crash scene isolation, classification, pairing and source symbolization."""
import hashlib
import posixpath
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from .integrations import run_cli
from .coredump import decode_coredump

PARSER_VERSION='scenes-6-nested-dump-time'
TZ=timezone(timedelta(hours=8))
CORE=re.compile(r'malloc failed|ERROR!!!|Assert on thread|Fault on thread|Firmware name:|\b(?:RA|PC|MTVAL|MEPC|MCAUSE|SP|LR|CFSR|HFSR|BFAR|MMFAR)\s*[:=]?\s*0x|run:\s*addr2line|call\s*stack:|backtrace:|watchdog.*(?:timeout|expired|trigger)|\bHardFault\b|\bpanic\b',re.I)
BOOT=re.compile(r'Welcome to HuamiOS|RAMDUMP\s+sys_init.*vfs register|SYS_INIT.*reset reason\s*\{',re.I)


def extract_scenes(text, filename, artifact_id):
    lines=text.split('\n');starts=[];boot=0;stored_start=None;contexts=[];anchors=[]
    for i,line in enumerate(lines):
        if BOOT.search(line):boot+=1
        if 'last crash:log start' in line:stored_start=i
        contexts.append((boot,stored_start))
        if 'last crash:log end' in line:stored_start=None
        match=re.search(r'run:\s*addr2line\s+-e\s+\S+\.elf\s+(.*)',line)
        if not match and 'update active time' not in line.lower():
            match=re.search(r'(?:call\s*stack|callstack|backtrace)\s*:\s*(.*)',line,re.I)
            if match and not re.search(r'Assert on thread|Fault on thread|ERROR!!!|\bPC\s*[:=]?\s*0x|\bpanic\b|watchdog.*(?:timeout|expired)', '\n'.join(lines[max(0,i-12):i+1]),re.I):match=None
        if match:
            addresses=re.findall(r'(?<![0-9a-zA-Z])(?:0x)?([0-9a-fA-F]{8})(?![0-9a-zA-Z])',match[1])
            if addresses:anchors.append((i,['0x'+a.lower() for a in addresses[:48]]))
    # Fatal markers without a usable stack remain visible; ordinary assertion warnings do not become crashes.
    for i,line in enumerate(lines):
        if not re.search(r'Assert on thread|Fault on thread|ERROR!!!.*(?:Abort|Assert)|\b(?:HardFault|panic)\b|watchdog.*(?:timeout|expired|trigger)',line,re.I):continue
        if any(abs(i-j)<30 and contexts[i]==contexts[j] for j,_ in anchors):continue
        anchors.append((i,[]))
    scenes=[]
    ordered=sorted(anchors)
    for position,(i,addresses) in enumerate(ordered):
        boot,stored=contexts[i]
        # Bound live events to this startup and at most 200 preceding lines; stored logs use their explicit boundary.
        lo=stored if stored is not None else max(0,i-200)
        for j in range(i-1,lo-1,-1):
            if contexts[j][0]!=boot or 'last crash:log end' in lines[j]:lo=j+1;break
        if position and contexts[ordered[position-1][0]]==contexts[i]:lo=max(lo,ordered[position-1][0]+1)
        hi=min(len(lines),i+6000)
        if position+1<len(ordered):hi=min(hi,ordered[position+1][0])
        for j in range(i+1,hi):
            if BOOT.search(lines[j]) or 'last crash:log end' in lines[j] or 'last crash:log start' in lines[j]:hi=j;break
        selected=[{'line':j+1,'text':lines[j].replace('\\r\\n','').strip()[:1600]} for j in range(lo,min(hi,i+1 if addresses else i+8)) if CORE.search(lines[j]) and 'update active time' not in lines[j].lower()]
        joined='\n'.join(x['text'] for x in selected)
        kind='oom_abort' if 'malloc failed' in joined and re.search(r'MallocFailedHook|osAbortHandler',joined) else 'watchdog' if re.search('watchdog.*(?:timeout|expired|trigger)',joined,re.I) else 'fault' if re.search(r'fault load|fault store|Fault on thread|HardFault',joined,re.I) else 'assert' if re.search('Assert on thread|osAssertHandler|osAbortHandler',joined,re.I) else 'panic'
        registers={name.lower():value.lower() for name,value in re.findall(r'\b(RA|PC|MEPC|MTVAL|MCAUSE|SP|LR|CFSR|HFSR|BFAR|MMFAR)\s*[:=]?\s*(0x[0-9a-fA-F]+)',joined)}
        coredump=decode_coredump(lines[i:hi]);registers.update({k:v for k,v in coredump.get('registers',{}).items() if k not in registers})
        if not addresses:
            pc=registers.get('pc') or registers.get('mepc')
            if pc:addresses=[pc]
        version=re.search(r'software version:\s*([\d.]+)',joined)
        if not version:
            nearby='\n'.join(lines[max(0,lo-250):lo])
            versions=re.findall(r'firmware version (?:from /mnt/system/res_info:|=)\s*([\d.]+)',nearby)
            firmware=versions[-1] if versions else None
        else:firmware=version[1]
        thread=re.search(r'(?:Assert|Fault) on thread\s+(\S+)',joined)
        stamp=None;stamp_source=None
        after='\n'.join(lines[i:hi]);rtc=re.search(r'rtc now\s+(\d{10})',after)
        if rtc:
            stamp=datetime.fromtimestamp(int(rtc[1]),TZ).isoformat();stamp_source='fault_rtc'
        else:
            year=re.search(r'(20\d{2})[.\-_]',filename)
            if year:
                for j in range(lo-1,max(-1,lo-1000),-1):
                    m=re.search(r'(\d{1,2})/(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2}).*RAMDUMP',lines[j])
                    if m:
                        try:stamp=datetime(int(year[1]),*map(int,m.groups()),tzinfo=TZ).isoformat();stamp_source='ramdump_boot'
                        except ValueError:pass
                        break
        context=[{'line':j+1,'text':lines[j].replace('\\r\\n','').strip()[:400]} for j in range(max(lo,i-180),i) if re.search(r'Failed to find obj|page.*(?:create|destroy|Scrolled)|sb_renderer_draw|allocated:|malloc failed',lines[j],re.I)][-24:]
        identity=f'{artifact_id}:{i+1}:{kind}'
        scenes.append({'id':hashlib.sha256(identity.encode()).hexdigest()[:16],'parser_version':PARSER_VERSION,'filename':filename,'artifact_id':artifact_id,
            'boot_id':f'{artifact_id}:boot-{boot}','stored_crash':stored is not None,'line_start':lo+1,'line_end':hi,'stack_line':i+1,
            'classification':kind,'thread':thread[1] if thread else None,'firmware':firmware,'timestamp':stamp,'timestamp_source':stamp_source,
            'registers':registers,'coredump':coredump,'raw_logs':selected,'addresses':addresses,'context':context,'frames':[],
            'stack_method':'设备回溯地址列表（可能含扫描候选）' if len(addresses)>1 else '仅故障PC；尚无完整调用栈',
            'warnings':[] if addresses else ['现场没有可用调用栈；不会编造函数或调用顺序']})
    return scenes[:32]


def dump_timestamp(name):
    # Archive lineage is outer.zip / ... / dump_TIMESTAMP.zip / dump.bin.
    # The outer device package can carry a different, equally valid date.
    parts=re.split(r'[/\\]',name)
    dated=[p for p in parts if re.search(r'(?<!\d)20\d{12}(?!\d)',p)]
    dump_parts=[p for p in dated if re.search(r'(?:sram|psram\d*|ram)?_?dump',p,re.I)]
    if not dated:return None
    candidate=(dump_parts or dated)[-1]
    stamps=set(re.findall(r'(?<!\d)(20\d{12})(?!\d)',candidate))
    if len(stamps)!=1:return None
    try:return datetime.strptime(stamps.pop(),'%Y%m%d%H%M%S').replace(tzinfo=TZ)
    except ValueError:return None


def select_scene(scenes, dumps, issue, selected_id=None):
    """Only timestamp-supported unique pairing is automatic. Manual selection is recorded as such."""
    groups={}
    for dump in dumps:
        metadata=dump.get('metadata',{})
        for origin in metadata.get('active_origins') or [metadata]:
            # Prefer the actual nested dump path, not its generic outer feedback ZIP.
            names=[origin.get('original_path',''),origin.get('source_archive',''),dump.get('filename','')]
            stamp=next((t for name in names if (t:=dump_timestamp(name))),None)
            if stamp:
                ids=groups.setdefault(stamp.isoformat(),[])
                if dump['id'] not in ids:ids.append(dump['id'])
    for scene in scenes:
        matching=[]
        if scene.get('timestamp'):
            stamp=datetime.fromisoformat(scene['timestamp'])
            matching=[{'timestamp':t,'artifact_ids':ids} for t,ids in groups.items() if 0<=(datetime.fromisoformat(t)-stamp).total_seconds()<=180]
        scene['dump_association']={'status':'matched' if len(matching)==1 else 'ambiguous' if matching else 'unverified','groups':matching}
        scene['version_match']='matched' if scene.get('firmware') and scene['firmware']==issue.get('firmware') else 'conflict' if scene.get('firmware') and issue.get('firmware') else 'unverified'
    if selected_id:
        selected=next((s for s in scenes if s['id']==selected_id),None)
        if not selected:raise ValueError('所选现场已变化，请刷新现场列表')
        return {'status':'selected','scene_id':selected_id,'reason':'用户明确选择；时间/版本限制仍保留'}
    eligible=[s for s in scenes if s['dump_association']['status']=='matched' and s['version_match']!='conflict']
    if len(eligible)==1:return {'status':'selected','scene_id':eligible[0]['id'],'reason':'唯一现场与dump文件时间对应（180秒窗口）；随后仍需代码字节校验'}
    if not dumps and len(scenes)==1 and scenes[0]['version_match']!='conflict':
        return {'status':'selected','scene_id':scenes[0]['id'],'reason':'仅一个现场，无dump；按日志分析并保留构建校验限制'}
    if not scenes:return {'status':'no_scene','scene_id':None,'reason':'未识别到支持格式的致命现场；不能把普通告警当作崩溃'}
    return {'status':'needs_selection','scene_id':None,'reason':'多个现场或缺少唯一的dump时间关联，请在看板选择现场'}


def tool_prefix(elf):
    from elftools.elf.elffile import ELFFile
    with Path(elf).open('rb') as f:machine=ELFFile(f)['e_machine']
    choices={'EM_RISCV':['riscv64-unknown-elf-','riscv32-unknown-elf-'],'EM_ARM':['arm-none-eabi-']}
    for prefix in choices.get(machine,[]):
        if shutil.which(prefix+'addr2line') and shutil.which(prefix+'objdump'):return prefix
    raise ValueError('ELF架构 '+str(machine)+' 缺少受支持的 addr2line/objdump 工具；未使用错误架构解析')


def symbolize(elf, addresses):
    if not addresses:return []
    text=run_cli([tool_prefix(elf)+'addr2line','-e',str(elf),'-a','-f','-C','-i',*addresses],timeout=60)
    groups=[];current=None
    for line in text.splitlines():
        if re.fullmatch(r'0x[0-9a-fA-F]+',line):
            current={'address':line,'locations':[]};groups.append(current)
        elif current is not None:current['locations'].append(line)
    frames=[]
    for index,g in enumerate(groups):
        locations=g.pop('locations');inline=[]
        for n in range(0,len(locations)-1,2):
            loc=locations[n+1];match=re.match(r'(.+?):(\d+)(?:\s|$)',loc)
            inline.append({'function':locations[n],'file':match[1] if match else loc,'line':int(match[2]) if match else None})
        frames.append({'index':index,**g,'inline':inline})
    return frames


def source_relative(filename):
    normal=posixpath.normpath(filename)
    match=re.search(r'/(?:HuamiOS_multi_platform|HuamiOS_HS3(?:@\d+)?)/(.+)$',normal)
    if not match:return None
    relative=match[1]
    if PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts:return None
    return relative


def exact_sources(settings, manifest, frames, emit):
    projects=ET.parse(manifest).getroot().findall('project')
    result=[];cache={};failed=set()
    for frame in frames:
        for loc in frame['inline']:
            relative=source_relative(loc['file'])
            if not relative or not loc['line']:continue
            loc['relative_file']=relative
            choices=[p for p in projects if relative.startswith((p.get('path') or p.get('name'))+'/')]
            if not choices:continue
            project=max(choices,key=lambda p:len(p.get('path') or p.get('name')))
            folder=project.get('path') or project.get('name');revision=project.get('revision','')
            if not re.fullmatch('[a-fA-F0-9]{40}',revision):continue
            repo=(settings.source/folder).resolve();repo.relative_to(settings.source)
            identity=(folder,revision,relative)
            if identity in failed:continue
            if identity not in cache:
                if len(cache)>=12:continue
                path=relative[len(folder)+1:]
                try:
                    try:
                        content=run_cli(['git','-C',str(repo),'show',revision+':'+path],timeout=30)
                    except RuntimeError:
                        emit('source','只读获取 manifest 指定提交：'+folder,{'revision':revision})
                        run_cli(['git','-C',str(repo),'fetch','--no-tags','origin',revision],timeout=120)
                        content=run_cli(['git','-C',str(repo),'show',revision+':'+path],timeout=30)
                except (RuntimeError,OSError):
                    failed.add(identity);continue
                cache[identity]=content.splitlines()
            lines=cache[identity];number=loc['line'];start=max(1,number-12);end=min(len(lines),number+18)
            if not 1<=number<=len(lines):continue
            loc['source_revision']=revision
            snippet='\n'.join(f'{j}: {lines[j-1]}' for j in range(start,end+1))
            if any(x['path']==relative and x['line']==number for x in result):continue
            result.append({'path':relative,'line':number,'revision':revision,'revision_matched':True,
                           'function':loc['function'],'text':snippet})
    return result


def scene_text(scene, compact=False):
    lines=['现场核心日志（'+scene['filename']+'；LF 行号）：']
    raw=scene['raw_logs']
    if compact:raw=[x for x in raw if re.search(r'malloc failed|ERROR!!!|Assert on thread|Fault on thread|\b(?:PC|MEPC|MTVAL|MCAUSE|CFSR|HFSR|BFAR)\s*[:=]?\s*0x',x['text'],re.I)][:5]
    lines.extend('L'+str(x['line'])+': '+x['text'] for x in raw)
    lines+=['','关键回溯（故障点 ← 调用方；…表示省略帧）：' if compact else '调用栈解析（设备回溯原序；内联函数标为 /）：']
    frames=scene.get('frames',[])
    if compact:
        functions=[];business=[]
        for frame in frames:
            for loc in frame['inline']:
                name=loc['function']
                if name not in ('??','xTaskResumeAll','_malloc','_greal_malloc_internal','_greal_calloc_internal','_greal_calloc','osTagCapsMalloc') and name not in functions:
                    functions.append(name)
                    relative=loc.get('relative_file') or source_relative(loc.get('file','')) or ''
                    if relative.startswith(('ui/','framework/','uiframework/')):business.append(len(functions)-1)
        selected=list(range(min(2,len(functions))))
        # A stack-scan list may contain incidental RTOS/GPU addresses. Keep the
        # business path in compact comments; all candidates stay on the dashboard.
        for index in (business if business else list(range(len(functions)))):
            if len(selected)>=8:break
            if index not in selected:selected.append(index)
        parts=[];previous=-1
        for index in sorted(selected):
            if previous>=0 and index>previous+1:parts.append('…')
            parts.append(functions[index]);previous=index
        if functions and previous<len(functions)-1:parts.append('…')
        lines.append(' ← '.join(parts) if parts else '未取得可解析函数；不补造调用路径。')
        return '\n'.join(lines)
    for frame in frames:
        locations=[]
        for loc in frame['inline']:
            name=loc.get('relative_file') or loc['file'].split('/')[-1]
            locations.append(loc['function']+' ['+name+':'+str(loc['line'] or '?')+']')
        lines.append('#'+str(frame['index'])+' '+frame['address']+' '+' / '.join(locations))
    if not frames:lines.append('未取得符号解析结果；保留上方原始地址，不补造函数。')
    return '\n'.join(lines)
