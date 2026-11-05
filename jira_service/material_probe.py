"""Content-based material triage. Never discard a whole log for isolated bad bytes."""
import re
import struct
import zlib

VERSION='material-probe-2'


def inspect_bytes(data,name):
    text=data.decode('utf-8',errors='replace')
    bad=text.count('\ufffd');controls=sum(x<32 and x not in (9,10,13) for x in data)
    readable=(controls/max(1,len(data))<.005 and bad/max(1,len(text))<.005)
    firmware=bool(re.search(rb'(?:SYS_INIT|screenM|HAL_KEY|firmware version|Assert on thread|Fault on thread)',data))
    base={'probe_version':VERSION,'readable_text':readable,'invalid_utf8_chars':bad,'control_bytes':controls,
          'decode_strategy':'UTF-8容错读取；原始字节保留，替换字符所在内容不作为精确证据' if bad else 'UTF-8',
          'analysis_role':'firmware_log' if readable and firmware else 'text' if readable else 'unknown_binary',
          'analysis_value':'运行版本、复位类型、按键事件及故障前后过程' if readable and firmware else '辅助上下文，需结合内容判断' if readable else '尚未识别格式，不能据文件扩展名判为无价值',
          'parse_status':'readable_with_replacements' if readable and bad else 'readable' if readable else 'unsupported_format',
          'inspection':'已检查完整文件的文本比例、编码和格式特征'}
    low=name.lower()
    if data.startswith(b'PK\x03\x04'):
        base.update(readable_text=False,analysis_role='damaged_archive',parse_status='truncated_archive',analysis_value='嵌套芯片日志包；结构损坏，尝试恢复首个数据流')
        try:
            header=struct.unpack_from('<I5H3I2H',data);offset=30+header[-2]+header[-1]
            if header[2]&1:raise ValueError('ZIP加密，未尝试解密')
            if header[3]!=8:raise ValueError('非deflate数据流')
            decoder=zlib.decompressobj(-15);out=decoder.decompress(data[offset:],64*1024*1024)
            base.update(recovery_bytes=len(out),recovery_complete=decoder.eof,recovery_crc_verified=False,
                        inspection='ZIP目录不可读；已尝试deflate恢复：'+str(len(out))+'字节，'+('数据流结束但未校验CRC' if decoder.eof else '数据流截断，未通过完整性校验'))
            base['recovered_role']='bluetooth_trace' if out.startswith(b'btlg') else 'unknown_binary'
        except (ValueError,zlib.error,struct.error) as exc:base['inspection']='嵌套ZIP恢复失败：'+str(exc)[:160]
    elif data.startswith(b'btlg'):
        base.update(readable_text=False,analysis_role='bluetooth_trace',parse_status='identified_no_decoder',analysis_value='蓝牙控制器交互/连接排查；不是主固件文本或已识别ramdump',inspection='已识别btlg文件头；无可用协议解码器，未将随机字节当日志')
    elif re.search(r'(?:ramdump|coredump|(?:p?sram\d*)[_-]?dump|crash[_-]?dump)',low) and not readable:
        base.update(analysis_role='ramdump_candidate',analysis_value='崩溃寄存器、内存和调用栈；需与同一故障现场及版本ELF匹配',parse_status='requires_scene_parser')
    elif not readable and (re.search(r'\d{14}_UTC|20\d{2}-\d{2}-\d{2}-\d{2}[_:]\d{2}[_:]\d{2}',name) or 'hmble_log' in low):
        base.update(analysis_role='mobile_log',analysis_value='手机侧连接、同步辅助记录；优先级低于固件日志',inspection='已检查文件头和文本比例，无法直接解码；可能为应用专用封装，尚无格式依据，不能断言加密')
    return base
