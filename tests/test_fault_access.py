from copy import deepcopy
from jira_service.fault_access import fault_access


def scene(instruction='lw a0,20(a5)',base='0x0',mtval='0x14'):
    return {'classification':'fault','registers':{'pc':'0x706b7ace','mtval':mtval},
            'coredump':{'status':'decoded','target':5,'registers':{'pc':'0x706b7ace','a5':base}},
            'frames':[{'address':'0x706b7ace','code_matched':True,
                       'disassembly':'706b7aca: 19a7a783 lw a5,410(a5) # unrelated\n706b7ace: 4bc8 '+instruction+'\n706b7ad0: e0fff0ef jal 706b78de'}]}


def test_load_fault_proves_address_arithmetic_and_keeps_lifecycle_limit():
    sample=scene();before=deepcopy(sample);result=fault_access(sample)
    assert sample==before
    assert result['status']=='address_consistent' and result['operation']=='load'
    assert result['instruction']=='lw a0,20(a5)' and result['width_bytes']==4
    assert result['null_base'] and result['effective_address']=='0x14'
    assert result['device_code_matched'] and '不证明' in result['note']


def test_negative_offset_register_alias_and_32_bit_wrap():
    sample=scene('sw a0,-4(x15)',base='0x0',mtval='0xfffffffc')
    result=fault_access(sample)
    assert result['status']=='address_consistent' and result['operation']=='store'
    assert result['base_register']=='a5' and result['offset']==-4


def test_conflicting_or_incomplete_context_does_not_become_confirmed_null_fault():
    sample=scene();sample['coredump']['registers']['pc']='0x1111'
    assert fault_access(sample)['status']=='conflict'
    sample=scene();sample['frames'][0]['address']='0x706b7aca'
    assert fault_access(sample)['status']=='incomplete'
    assert fault_access(scene('lw a0,20(sp)'))['status']=='incomplete'
    assert fault_access(scene('jal 706b78de'))['status']=='unsupported_instruction'
    sample=scene();sample['classification']='oom_abort'
    assert fault_access(sample)['status']=='not_access_fault'
    assert fault_access(scene(mtval='0x18'))['status']=='address_conflict'
    sample=scene();sample['frames'][0]['code_matched']=False
    result=fault_access(sample)
    assert result['status']=='address_consistent' and not result['device_code_matched']
    assert '尚未与dump匹配' in result['note']


def test_valid_register_prefix_can_check_address_but_still_requires_matching_logged_pc():
    sample=scene();sample['coredump']['status']='registers_only'
    result=fault_access(sample)
    assert result['status']=='address_consistent' and result['null_base']
    sample['coredump']['registers']['pc']='0x1234'
    assert fault_access(sample)['status']=='conflict'
