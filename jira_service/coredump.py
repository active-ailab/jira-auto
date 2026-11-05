"""Strict decoder for Zepp #CD v1 blocks. No execution of dump-provided code.
Layout references: cm_backtrace/common/coredump.{c,h}, build/tools/coredump/gdbstubs/arch.
"""
import re
import struct

RV_NAMES = ['zero','ra','sp','gp','tp','t0','t1','t2','fp','s1',*[f'a{i}' for i in range(8)],*[f's{i}' for i in range(2,12)],'t3','t4','t5','t6','pc']


def register_prefix(data):
    """Recover only an already complete first RV32 register block, never memory."""
    if len(data)<149:return None
    magic,version,target,bits,flags,reason=struct.unpack_from('<2sHHBBI',data)
    block,arch,size=struct.unpack_from('<BHH',data,12)
    if (magic,version,bits,block,arch,size)!=(b'ZE',1,5,ord('A'),1,132) or target not in (4,5):return None
    offset=149
    while offset<len(data):
        if data[offset]!=ord('M') or offset+10>len(data):return None
        _,mv,start,end=struct.unpack_from('<BBII',data,offset)
        if mv!=1 or end<start:return None
        offset+=10+end-start
    regs=dict(zip(RV_NAMES,struct.unpack_from('<33I',data,17)))
    regs['saved_sp']=regs['sp'];regs['sp']+=0x198 if target==5 else 0x120
    return {'status':'registers_only','format':'zepp-coredump-v1','target':target,'reason_code':reason,
            'registers':{k:hex(v) for k,v in regs.items()},'memory_regions':[],
            'coverage':'后续内存文本损坏，仅保留此前格式完整的首个RV32寄存器块；内存及后续块未验证，不代表全部线程'}


def decode_coredump(lines):
    collecting=False;data=bytearray();complete=False
    for line in lines:
        if '#CD:BEGIN#' in line:collecting=True;continue
        if not collecting:continue
        if '#CD:END#' in line:complete=True;break
        match=re.search(r'#CD:([0-9a-fA-F]+)',line)
        if match:
            try:data.extend(bytes.fromhex(match[1]))
            except ValueError:
                partial=register_prefix(data)
                return {**partial,'reason':'后续内存行hex长度错误；仅恢复此前完整寄存器块'} if partial else {'status':'invalid','reason':'hex长度错误'}
        if len(data)>2*1024*1024:return {'status':'invalid','reason':'coredump超过2MiB限制'}
    if not collecting:return {'status':'absent'}
    if not complete:return {'status':'truncated','reason':'缺少#CD:END#'}
    if len(data)<12:return {'status':'invalid','reason':'头部截断'}
    magic,version,target,pointer_bits,flags,reason=struct.unpack_from('<2sHHBBI',data)
    if magic!=b'ZE' or version!=1 or pointer_bits!=5:
        return {'status':'unsupported','reason':'仅支持ZE v1 32位格式','target':target}
    offset=12;registers={};regions=[]
    while offset<len(data):
        block=data[offset]
        if block==ord('A'):
            if registers:return {'status':'unsupported','reason':'存在多个寄存器块，无法唯一确认故障线程，不合并寄存器'}
            if offset+5>len(data):return {'status':'invalid','reason':'寄存器头截断'}
            _,arch_version,size=struct.unpack_from('<BHH',data,offset);offset+=5
            if arch_version!=1 or offset+size>len(data):return {'status':'invalid','reason':'寄存器块版本或长度错误'}
            if target in (4,5) and size==132:
                values=struct.unpack_from('<33I',data,offset);registers=dict(zip(RV_NAMES,values))
                # Keep saved SP as well as the architecture-specific restored task SP.
                registers['saved_sp']=registers['sp'];registers['sp']+=0x198 if target==5 else 0x120
            elif target==3 and size==72:
                values=struct.unpack_from('<18I',data,offset)
                registers={f'r{i+4}':values[i] for i in range(8)}
                registers.update({f'r{i}':values[i+9] for i in range(4)})
                registers.update(dict(zip(['r12','lr','pc','sp','xpsr'],values[13:18])))
            else:return {'status':'unsupported','reason':'未知架构寄存器布局','target':target}
            offset+=size
        elif block==ord('M'):
            if offset+10>len(data):return {'status':'invalid','reason':'内存头截断'}
            _,mem_version,start,end=struct.unpack_from('<BBII',data,offset);offset+=10
            if mem_version!=1 or end<start or offset+end-start>len(data):return {'status':'invalid','reason':'内存范围/长度错误'}
            regions.append({'start':hex(start),'end':hex(end),'bytes':end-start})
            offset+=end-start
        else:return {'status':'unsupported','reason':'未知coredump块，不猜测后续偏移'}
    return {'status':'decoded','format':'zepp-coredump-v1','target':target,'reason_code':reason,
            'registers':{name:hex(value) for name,value in registers.items()},'memory_regions':regions,
            'coverage':'故障/断言所保存的单线程寄存器和内存区域清单；不是所有线程，也不是完整ramdump堆解析'}
