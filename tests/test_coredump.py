import struct
from jira_service.coredump import decode_coredump


def wrap(blob):return ['#CD:BEGIN#','#CD:'+blob.hex(),'#CD:END#']


def test_rv32_mhs003_registers_and_memory_bounds():
    values=list(range(33));values[2]=0x71000000;values[32]=0x706b7ace
    blob=struct.pack('<2sHHBBI',b'ZE',1,5,5,0,1)+struct.pack('<BHH',ord('A'),1,132)+struct.pack('<33I',*values)
    blob+=struct.pack('<BBII',ord('M'),1,0x71000000,0x71000004)+b'abcd'
    out=decode_coredump(wrap(blob))
    assert out['status']=='decoded'
    assert out['registers']['pc']=='0x706b7ace' and out['registers']['a5']=='0xf'
    assert out['registers']['sp']==hex(0x71000198)
    assert out['memory_regions'][0]['bytes']==4
    assert decode_coredump(wrap(blob[:-1]))['status']=='invalid'
    assert decode_coredump(wrap(blob)[:-1])['status']=='truncated'


def test_cortex_m_layout_and_unknown_version_fail_closed():
    values=list(range(18))
    blob=struct.pack('<2sHHBBI',b'ZE',1,3,5,0,2)+struct.pack('<BHH',ord('A'),1,72)+struct.pack('<18I',*values)
    out=decode_coredump(wrap(blob))
    assert out['registers']['pc']=='0xf' and out['registers']['r0']=='0x9'
    altered=bytearray(blob);altered[2]=2
    assert decode_coredump(wrap(altered))['status']=='unsupported'


def test_multiple_register_blocks_do_not_silently_mix_thread_context():
    header=struct.pack('<2sHHBBI',b'ZE',1,5,5,0,1)
    block=struct.pack('<BHH',ord('A'),1,132)+struct.pack('<33I',*range(33))
    result=decode_coredump(wrap(header+block+block))
    assert result['status']=='unsupported' and '多个寄存器块' in result['reason']
    assert 'registers' not in result


def test_corrupt_memory_hex_retains_only_preceding_complete_rv32_register_block():
    header=struct.pack('<2sHHBBI',b'ZE',1,5,5,0,1)
    block=struct.pack('<BHH',ord('A'),1,132)+struct.pack('<33I',*range(33))
    memory=struct.pack('<BBII',ord('M'),1,0x71000000,0x71000100)+b'abcd'
    result=decode_coredump(['#CD:BEGIN#','#CD:'+(header+block+memory).hex(),'#CD:abc','#CD:END#'])
    assert result['status']=='registers_only' and result['registers']['pc']=='0x20'
    assert result['memory_regions']==[] and '内存文本损坏' in result['coverage']
    # Corruption within the register block never yields invented saved values.
    assert decode_coredump(['#CD:BEGIN#','#CD:'+(header+block[:20]).hex(),'#CD:abc'])['status']=='invalid'
    # A known second register block is not silently accepted as a single thread.
    assert decode_coredump(['#CD:BEGIN#','#CD:'+(header+block+block+memory).hex(),'#CD:abc'])['status']=='invalid'
