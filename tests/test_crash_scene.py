from jira_service.crash_scene import extract_scenes, source_relative, scene_text


def test_stored_assert_stack_bare_hex_and_lf_line_numbers():
    log='normal\rinterleaved\nlast crash:log start\nmalloc failed for size 123\nERROR!!! osAbortHandler at vApplicationMallocFailedHook 57\nAssert on thread SBEngine\nrun: addr2line -e watch@mhs003.elf -a -f 80d6ee12 7028cc08\n'
    scenes=extract_scenes(log,'test.log','artifact')
    assert len(scenes)==1
    scene=scenes[0]
    assert scene['stack_line']==6
    assert scene['addresses']==['0x80d6ee12','0x7028cc08']
    assert scene['raw_logs'][0]=={'line':3,'text':'malloc failed for size 123'}
    assert 'L3:' in scene_text(scene)


def test_regular_power_backtrace_is_not_a_crash():
    log='hm_power SBEngine update active time backtrace: 7039705a 703970d6\n'
    assert extract_scenes(log,'test.log','a')==[]


def test_fault_stack_accepts_hex_prefix_but_not_unrelated_backtrace():
    log='Fault on thread engine\nPC=0x7039705a MTVAL=0x14\ncall stack: 0x7039705a 0x703970d6\n'
    scenes=extract_scenes(log,'test.log','a')
    assert scenes[0]['addresses']==['0x7039705a','0x703970d6']
    assert extract_scenes('backtrace: 7039705a 703970d6','x','a')==[]


def test_jenkins_build_path_normalizes_to_manifest_project():
    assert source_relative('/home/jenkins/HuamiOS_multi_platform/build/..//./framework/map/src/a.c')=='framework/map/src/a.c'
    assert source_relative('/home/jenkins/FW_ALGO_PACKAGE_BUILD/storyboard/a.c') is None


def test_comment_starts_with_scene_and_keeps_full_frames_for_dashboard():
    from jira_service.analysis import AnalysisResult, EvidencePool, render_comment
    scene=extract_scenes('last crash:log start\nmalloc failed for size 123\nERROR!!! osAbortHandler at vApplicationMallocFailedHook 57\nAssert on thread SBEngine\nrun: addr2line -e watch.elf -a -f 7028cc08\n','test.log','a')[0]
    scene['frames']=[{'index':0,'address':'0x7028cc08','inline':[{'function':'createAtlas','file':'map.c','line':10}]}]
    result=AnalysisResult(verdict='probable',confidence='medium',summary='地图画布分配失败触发abort',root_cause='分配失败触发abort',facts=[],hypotheses=[],gaps=['具体分配失败诱因待核实'],next_steps=[],review_notes=[])
    text=render_comment(result,EvidencePool(),[scene])
    assert text.index('malloc failed') < text.index('createAtlas') < text.index('代码分析与结论')
    assert 'map.c:10' in scene_text(scene)
    assert '0x7028cc08' in scene_text(scene)


def test_multi_boot_scene_isolation_and_manual_selection():
    from jira_service.crash_scene import select_scene
    log='Welcome to HuamiOS\nlast crash:log start\nFirmware name: watch, software version: 1.2.3\nPC 0x70000010 MTVAL 0x14: fault load\nFault on thread first\nrun: addr2line -e watch.elf -a -f 70000010\nlast crash:log end\nWelcome to HuamiOS\nlast crash:log start\nmalloc failed for size 999\nERROR!!! osAbortHandler at vApplicationMallocFailedHook 57\nAssert on thread second\nrun: addr2line -e watch.elf -a -f 70000020\nlast crash:log end'
    scenes=extract_scenes(log,'2026.10.30.log','a')
    assert len(scenes)==2
    assert scenes[0]['thread']=='first' and scenes[1]['thread']=='second'
    assert 'malloc failed' not in str(scenes[0]['raw_logs'])
    assert '0x70000010' not in str(scenes[1]['raw_logs'])
    assert scenes[0]['registers']['mtval']=='0x14'
    assert scenes[0]['boot_id']!=scenes[1]['boot_id']
    assert select_scene(scenes,[],{'firmware':'1.2.3'})['status']=='needs_selection'
    assert select_scene(scenes,[],{},scenes[1]['id'])['scene_id']==scenes[1]['id']


def test_dump_pairing_is_unique_and_version_conflict_blocks_auto_choice():
    from jira_service.crash_scene import select_scene
    scene={'id':'a','timestamp':'2026-10-29T16:05:07+08:00','firmware':'3.26.1.2'}
    dumps=[{'id':'psram','metadata':{'source_archive':'psram_dump_20261029160519.zip'}}]
    assert select_scene([scene],dumps,{'firmware':'3.26.1.2'})['scene_id']=='a'
    assert select_scene([scene],dumps,{'firmware':'9.9.9'})['status']=='needs_selection'
    assert select_scene([dict(scene,id='b'),scene],dumps,{'firmware':'3.26.1.2'})['status']=='needs_selection'


def test_watchdog_without_stack_stays_a_scene_but_not_watchdog_init():
    scenes=extract_scenes('Welcome to HuamiOS\nwatchdog timeout task=render\n','2026.log','a')
    assert len(scenes)==1 and scenes[0]['classification']=='watchdog'
    assert scenes[0]['addresses']==[]
    assert extract_scenes('watchdog init seconds = 200','2026.log','a')==[]


def test_compact_fault_comment_keeps_pc_mtval_and_limits_runtime_frames():
    scene=extract_scenes('last crash:log start\nPC 0x706b7ace MTVAL 0x14: fault load\nFault on thread SBEngine\ncall stack: 706b7ace 706b7ad0\n','a.log','a')[0]
    scene['frames']=[{'index':i,'address':hex(0x706b7ace+i*4),'inline':[{'function':'func'+str(i),'file':'/build/framework/example/test.c','line':i+1}]} for i in range(20)]
    text=scene_text(scene,compact=True)
    assert 'PC 0x706b7ace MTVAL 0x14' in text
    assert 'func0' in text and 'func7' in text and 'func8' not in text
    assert '…表示省略帧' in text


def test_nested_dump_timestamp_takes_precedence_over_generic_feedback_archive():
    from jira_service.crash_scene import select_scene
    scene={'id':'one','timestamp':'2026-10-29T05:07:14+08:00','firmware':'3.25.0.1'}
    dumps=[{'id':'sram','filename':'0007-sram_dump.bin','metadata':{'source_archive':'feedback_log_example-user_1793224887.zip','original_path':'iOS.zip / device.zip / log/ramdump/sram_dump_20261029050718.zip / sram_dump.bin'}}]
    selected=select_scene([scene],dumps,{'firmware':'3.25.0.1'})
    assert selected['scene_id']=='one' and scene['dump_association']['status']=='matched'
    assert scene['dump_association']['groups'][0]['artifact_ids']==['sram']


def test_jenkins_hs3_workspace_paths_resolve_with_manifest_scoped_relative_names():
    from jira_service.crash_scene import source_relative
    assert source_relative('/home/jenkins/workspace/firmware_auto_trigger/HuamiOS_HS3@13/build/..//./ui/Launcher/launcher/src/notification_layer_v1.c')=='ui/Launcher/launcher/src/notification_layer_v1.c'
    assert source_relative('/tmp/unrelated/ui/Launcher/foo.c') is None
    assert source_relative('/home/jenkins/HuamiOS_HS3@13/../../etc/passwd') is None


def test_compact_business_stack_does_not_promote_unverified_rtos_gpu_candidates():
    from jira_service.crash_scene import scene_text
    def frame(name,path):return {'inline':[{'function':name,'file':'/ci/HuamiOS_HS3@2/'+path,'line':1}]}
    sample={'filename':'fw.log','raw_logs':[],'frames':[frame('updateTime','ui/Launcher/a.c'),frame('expand','ui/Launcher/a.c'),frame('handleTouch','uiframework/ui_adapter/touch.c'),frame('xQueueSemaphoreTake','core/freertos/queue.c'),frame('gpu_submit','platform/mcu/gpu.c')]}
    body=scene_text(sample,compact=True)
    assert 'updateTime ← expand ← handleTouch' in body
    assert 'xQueueSemaphoreTake' not in body and 'gpu_submit' not in body
