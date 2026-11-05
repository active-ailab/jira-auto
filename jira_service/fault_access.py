"""Bounded RV32 load/store address check from saved registers and ELF disassembly.

This checks arithmetic at the recorded PC; it never emulates code or establishes
why an object became invalid. ELF/dump code matching remains a separate proof.
"""
import re
from .coredump import RV_NAMES


def fault_access(scene):
    dump=scene.get('coredump',{})
    if dump.get('status') not in ('decoded','registers_only') or dump.get('target') not in (4,5):
        return {'status':'unsupported','note':'仅支持已解码的Zepp v1 RV32现场寄存器'}
    if scene.get('classification')!='fault':
        return {'status':'not_access_fault','note':'当前不是访问异常，不把断言现场PC当作非法访存'}
    regs=dump.get('registers',{});logged=scene.get('registers',{})
    pc=logged.get('pc') or logged.get('mepc') or regs.get('pc')
    if not pc or not regs.get('pc'):
        return {'status':'incomplete','note':'缺少可交叉核对的故障PC'}
    if int(pc,16)!=int(regs['pc'],16):
        return {'status':'conflict','note':'日志PC与保存寄存器PC不一致，不能合并核对'}
    frame=next((f for f in scene.get('frames',[]) if int(f['address'],16)==int(pc,16)),None)
    if not frame:return {'status':'incomplete','note':'回溯中没有故障PC对应的反汇编，不借用返回地址指令'}
    instruction=None
    for line in frame.get('disassembly','').splitlines():
        match=re.match(r'\s*([0-9a-fA-F]+):\s+[0-9a-fA-F]+\s+(.+)',line)
        if match and int(match[1],16)==int(pc,16):instruction=match[2].split('#',1)[0].strip();break
    if not instruction:return {'status':'incomplete','note':'未取得PC处的有效指令'}
    match=re.fullmatch(r'(?:c\.)?(lb|lbu|lh|lhu|lw|sb|sh|sw)\s+([\w]+),\s*(-?(?:0x[0-9a-fA-F]+|\d+))\((\w+)\)',instruction)
    if not match:return {'status':'unsupported_instruction','pc':pc,'instruction':instruction,'note':'未支持的指令形式，未计算访存地址'}
    opcode,_,offset,base=match.groups()
    aliases={f'x{i}':name for i,name in enumerate(RV_NAMES[:32])};aliases['s0']='fp'
    base=aliases.get(base,base)
    # Zepp decoder restores SP from the saved task frame; do not treat that
    # architecture adjustment as an observed fault-time base register here.
    if base=='sp' or (base!='zero' and base not in regs):
        return {'status':'incomplete','pc':pc,'instruction':instruction,'note':'基址寄存器未直接保存，禁止猜测有效地址'}
    displacement=int(offset,16) if '0x' in offset else int(offset)
    value=0 if base=='zero' else int(regs[base],16)
    effective=(value+displacement)&0xffffffff
    mtval=logged.get('mtval');same=mtval is not None and effective==int(mtval,16)
    code_matched=frame.get('code_matched') is True
    return {'status':'address_consistent' if same else 'address_conflict' if mtval is not None else 'calculated',
            'pc':hex(int(pc,16)),'instruction':instruction,'operation':'load' if opcode.startswith('l') else 'store',
            'width_bytes':{'b':1,'h':2,'w':4}[opcode[1]],'base_register':base,'base_value':hex(value),
            'offset':displacement,'effective_address':hex(effective),'observed_fault_address':mtval,
            'null_base':value==0,'device_code_matched':code_matched,
            'note':'仅核对保存现场的寄存器与指令地址计算；'+('该PC代码字节已与dump匹配' if code_matched else '该PC代码字节尚未与dump匹配，指令解释仍有构建限制')+'；不证明对象失效的生命周期根因'}
