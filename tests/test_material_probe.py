import io
import struct
import zlib
import zipfile
from jira_service.material_probe import inspect_bytes,VERSION
from jira_service.materials import unpack_logs


def test_firmware_log_with_rare_bad_utf8_and_nul_remains_readable(tmp_path):
    blob=(b'I>1 SYS_INIT firmware version = 1.2.3\r\n'*1500)+b'bad audio tag \xe4\0\r\n'
    p=inspect_bytes(blob,'firmware.log')
    assert p['readable_text'] and p['analysis_role']=='firmware_log'
    assert p['invalid_utf8_chars']==1 and p['parse_status']=='readable_with_replacements'
    archive=tmp_path/'logs.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('firmware.log',blob)
    [(file,meta)]=unpack_logs(archive,tmp_path/'unpacked')
    assert file.read_bytes()==blob and meta['readable_text'] and meta['probe_version']==VERSION


def test_binary_trace_and_dump_are_distinct_material_roles():
    assert inspect_bytes(b'btlg'+bytes(range(256))*10,'trace.bin')['analysis_role']=='bluetooth_trace'
    dump=inspect_bytes(b'\0'*256,'sram_dump.bin')
    assert not dump['readable_text'] and dump['analysis_role']=='ramdump_candidate'
    assert inspect_bytes(bytes(range(256))*10,'20261029000000_UTC_9.0_')['analysis_role']=='mobile_log'


def test_damaged_nested_zip_attempts_bounded_deflate_recovery_without_claiming_crc():
    stream=zlib.compressobj(wbits=-15)
    payload=stream.compress(b'btlg'+b'\0'*10000)+stream.flush()
    name=b'log/bt_uart_log.bin'
    header=struct.pack('<I5H3I2H',0x04034b50,20,8,8,0,0,0,0,0,len(name),0)
    result=inspect_bytes(header+name+payload[:-3],'chip.zip')
    assert result['analysis_role']=='damaged_archive'
    assert result['recovery_bytes']>0 and not result['recovery_complete']
    assert not result['recovery_crc_verified'] and result['recovered_role']=='bluetooth_trace'
