"""Bounded crash preparation. Never infer registers from unparsed dump bytes."""
import json
import re
import struct
from pathlib import Path
from elftools.elf.elffile import ELFFile
from .materials import fetch_artifacts, sha256
from .integrations import run_cli
from .crash_scene import extract_scenes, symbolize, exact_sources, scene_text, select_scene, PARSER_VERSION, tool_prefix


def material_path(settings,store,a):
    p=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(a['id'],))[0]['path']).resolve()
    p.relative_to(settings.data)
    return p


def scene_inventory(settings,store,key,run_id,selected_id=None):
    artifacts=store.artifacts(key);scenes=[]
    for a in artifacts:
        if a['kind']=='log':
            log=material_path(settings,store,a)
            if log.stat().st_size>128*1024*1024:raise ValueError('日志超过单文件128MiB解析上限，请拆分或截取后导入')
            scenes.extend(extract_scenes(log.read_bytes().decode('utf-8',errors='replace'),a['filename'],a['id']))
    dumps=[a for a in artifacts if a['kind']=='binary_log' and 'dump' in a['metadata'].get('original_path',a['filename']).lower()]
    selection=select_scene(scenes,dumps,store.issue(key),selected_id)
    result={'scenes':scenes,'selection':selection,'parser_version':PARSER_VERSION}
    store.event('scene_inventory','发现 '+str(len(scenes))+' 个独立现场；'+selection['reason'],key,run_id,result)
    return result


def prepare_crash(settings, store, key, run_id, inventory=None):
    inventory=inventory or scene_inventory(settings,store,key,run_id)
    selection=inventory['selection']
    if selection['status']=='needs_selection':raise ValueError(selection['reason'])
    if selection['status']=='no_scene':return {'status':'no_scene','selection':selection}
    scenes=[s for s in inventory['scenes'] if s['id']==selection['scene_id']]
    issue=store.issue(key);version=scenes[0].get('firmware') or issue.get('firmware')
    artifacts=store.artifacts(key)
    association=scenes[0].get('dump_association',{})
    linked={aid for g in association.get('groups',[]) for aid in g['artifact_ids']} if association.get('status')=='matched' else set()
    # Never combine a dump from a different scene just because it was uploaded last.
    dumps=[a for a in artifacts if a['kind']=='binary_log' and a['id'] in linked]
    if not issue.get('device') or not version:
        raise ValueError('现场缺少设备/版本，无法取得对应符号文件；请先核对')
    builds=[a for a in artifacts if a['kind']=='elf' and a['metadata'].get('device')==issue['device'] and a['metadata'].get('version')==version]
    if not builds:fetch_artifacts(settings,store,key,issue['device'],version,run_id)
    artifacts=store.artifacts(key)
    path=lambda a:material_path(settings,store,a)
    elf=next(path(a) for a in artifacts if a['kind']=='elf' and a['metadata'].get('device')==issue['device'] and a['metadata'].get('version')==version)
    findings=[]
    for scene in scenes:
        findings.append('现场类型 '+scene['classification']+'，线程 '+str(scene.get('thread'))+'；寄存器 '+json.dumps(scene.get('registers',{}),ensure_ascii=False)+'；coredump解析 '+json.dumps(scene.get('coredump',{}),ensure_ascii=False))
        findings.append('材料关联：'+json.dumps(scene['dump_association'],ensure_ascii=False)+'；版本核对 '+scene['version_match']+'；选择原因 '+selection['reason'])
    for scene in scenes[-3:]:
        scene['frames']=symbolize(elf,scene['addresses'])
        store.event('crash_stack','提取并解析现场调用栈，日志 L'+str(scene['stack_line']),key,run_id,scene)
        for frame in scene['frames']:
            address=int(frame['address'],16)
            disassembly=run_cli([tool_prefix(elf)+'objdump','-d','--start-address='+hex(max(0,address-32)),'--stop-address='+hex(address+12),str(elf)],timeout=30)
            frame['disassembly']=disassembly.split('Disassembly of section',1)[-1][-2200:]
            if any(re.search(r'li\s+[ast]\d+,\s*(?:[1-9]\d{2,})',line) for line in disassembly.splitlines()):
                findings.append('现场回溯帧 '+frame['address']+' 附近反汇编（返回地址附近，不一定为故障指令）：\n'+frame['disassembly'][:1600])
        for raw in scene['raw_logs']:
            findings.append('现场原始日志 '+scene['filename']+' L'+str(raw['line'])+': '+raw['text'])
        for frame in scene['frames']:
            findings.append('现场回溯地址 #'+str(frame['index'])+' '+frame['address']+'：'+json.dumps(frame['inline'],ensure_ascii=False))
    manifests=[path(a) for a in artifacts if a['kind']=='manifest' and a['metadata'].get('version')==version]
    sources=[]
    if manifests:
        sources=exact_sources(settings,manifests[0],[f for scene in scenes for f in scene['frames']],
            lambda step,message,details:store.event(step,message,key,run_id,details))
    for source in sources:
        # Split substantial excerpts to avoid evidence pool's per-item truncation.
        findings.append('目标源码 '+source['path']+':'+str(source['line'])+'，manifest提交 '+source['revision']+'；函数 '+source['function']+'：\n'+source['text'][:1650])
    matches=[]
    # Geneva layout is supported only when the SRAM-resident descriptor validates both file lengths.
    # No assumption that another device uses this layout.
    if issue['device'] in ('Geneva','GenevaW','GenevaWN'):
        sram=next((path(a) for a in dumps if re.search(r'(?:^|/)sram_dump',a['metadata'].get('original_path',''))),None)
        psram=next((path(a) for a in dumps if re.search(r'(?:^|/)psram_dump',a['metadata'].get('original_path',''))),None)
        if sram and psram:
            sb=sram.read_bytes();pb=psram.read_bytes()
            with elf.open('rb') as f:
                e=ELFFile(f);sym=e.get_section_by_name('.symtab')
                rs=sym.get_symbol_by_name('r') if sym else []
                for symbol in rs or []:
                    offset=symbol['st_value']-0x80c00000
                    if not 0<=offset<=len(sb)-24:continue
                    params=struct.unpack_from('<6I',sb,offset)
                    if params[:2]!=(0x80c00000,len(sb)) or params[3]!=len(pb):continue
                    base=params[2]
                    findings.append(f'由 ELF 符号 r 和 SRAM dump 校验内存描述符：SRAM base=0x{params[0]:x}, length={len(sb)}；PSRAM base=0x{base:x}, length={len(pb)}。未提取全部线程或异常寄存器。')
                    for region_base,region_data,region_name in [(base,pb,'PSRAM'),(0x80c00000,sb,'SRAM')]:
                        for sec in e.iter_sections():
                            if sec['sh_type']!='SHT_PROGBITS' or not sec['sh_flags']&4:continue
                            lo=max(region_base,sec['sh_addr']);hi=min(region_base+len(region_data),sec['sh_addr']+sec['sh_size'])
                            if hi<=lo:continue
                            same=sec.data()[lo-sec['sh_addr']:hi-sec['sh_addr']]==region_data[lo-region_base:hi-region_base]
                            matches.append({'section':sec.name,'region':region_name,'start':lo,'end':hi,'bytes':hi-lo,'match':same})
                            findings.append(f'ELF 与 dump 的 {region_name} 代码段 {sec.name} 重叠 {hi-lo} 字节，逐字节比较：'+('一致' if same else '不一致')+'；此结果不证明所有固件段均一致。')
                    break
    if any('psram2_dump' in a['metadata'].get('original_path',a['filename']) for a in dumps):
        findings.append('PSRAM2 dump已识别并关联到此现场；本解析器未恢复其堆对象或全部线程，不将此文件的存在当作已完成内存根因验证。')
    if not matches:
        findings.append('本次未能验证 dump 地址布局及代码字节与 ELF 的匹配；禁止把候选符号当作确定的现场调用栈。')
    for scene in scenes:
        for frame in scene['frames']:
            address=int(frame['address'],16)
            frame['code_matched']=any(m['match'] and m['start']<=address<m['end'] for m in matches)
        from .fault_access import fault_access
        scene['fault_access']=fault_access(scene)
        if scene['fault_access']['status'] not in ('unsupported','not_access_fault'):
            findings.insert(0,'故障访存核对：'+json.dumps(scene['fault_access'],ensure_ascii=False))
    findings.append('分析边界：调用栈来自同一 last crash 区段的设备回溯地址，已按 ELF 符号和 manifest 源码解析；设备可能通过栈扫描生成列表，个别候选帧需结合源码调用关系判断，不可把正常电源 backtrace 或后续启动的断言并入此现场。未实现全部线程遍历。')
    report={'schema':'jira-auto-forensic-v1','parser_version':PARSER_VERSION,'findings':findings,'code_comparison':matches,'scenes':scenes,'sources':sources,'selection':selection,
            'build':{'device':issue['device'],'version':version,'version_source':'scene_log' if scenes[0].get('firmware') else 'jira_field'}}
    encoded=json.dumps(report,ensure_ascii=False,indent=2)
    from .integrations import digest
    target=settings.data/'cases'/key/'crash'/(digest(encoded)+'.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(encoded)
    aid=store.artifact(key,'forensic',target,{'bytes':target.stat().st_size,'sha256':sha256(target),'role':'crash_preparation','scene_id':selection['scene_id'],'parser_version':PARSER_VERSION})
    store.event('crash','现场核心日志、调用栈、目标源码及代码匹配已记录',key,run_id,{'code_comparison':matches,'dump_files':len(dumps)})

    return {'status':'parsed','artifact_id':aid,'scene_id':selection['scene_id'],'path':str(target.relative_to(settings.data)),'sha256':sha256(target)}
