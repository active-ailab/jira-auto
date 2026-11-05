import json
from pathlib import Path
import sys
from jira_service.analysis import CodexModel, SearchPlan
from jira_service.settings import Settings


def test_successful_model_call_reused_only_for_identical_packet(tmp_path):
    tool=tmp_path/'fake-codex';counter=tmp_path/'calls'
    tool.write_text('#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
sys.stdin.read()
counter=Path('''+repr(str(counter))+''')
counter.write_text(counter.read_text()+'1' if counter.exists() else '1')
result={'observations':[{'finding':'fixture fact','evidence_ids':['E001']}],'log_terms':[],'source_terms':[],'evidence_gaps':[]}
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{}}))
''');tool.chmod(0o700)
    settings=Settings(tmp_path/'data',tmp_path/'src',codex_bin=str(tool))
    packet={'evidence':[{'id':'E001','text':'original'}]}
    for name in ['run1','run2']:
        d=tmp_path/'analysis'/name;d.mkdir(parents=True)
        model=CodexModel(settings,d,lambda *a:None)
        assert model.call('test','inspect',packet,SearchPlan).observations[0].evidence_ids==['E001']
    assert counter.read_text()=='1'
    d=tmp_path/'analysis'/'run3';d.mkdir();model=CodexModel(settings,d,lambda *a:None)
    model.call('test','inspect',{'evidence':[{'id':'E001','text':'changed'}]},SearchPlan)
    assert counter.read_text()=='11'
    model.cache_enabled=False;model.call('test','inspect',packet,SearchPlan)
    assert counter.read_text()=='111'


def test_provider_failures_are_actionable_and_never_treated_as_missing_logs(tmp_path):
    import pytest
    from jira_service.analysis import model_failure,ModelExecutionError
    cases=[('usage_limit_reached: reset later','quota','额度'),
           ('401 authentication failed: private_secret','authentication','登录'),
           ('stream disconnected: https://private.example/auth','network','网络'),
           ('invalid_json_schema','schema','结构')]
    for raw,category,word in cases:
        failure=model_failure(raw)
        assert isinstance(failure,ModelExecutionError) and failure.category==category
        assert word in str(failure) and 'private' not in str(failure)
    tool=tmp_path/'fake-codex'
    tool.write_text('#!'+sys.executable+'\n'+'''import json,sys
sys.stdin.read()
print(json.dumps({'type':'turn.failed','error':{'message':'usage_limit_reached: reset later'}}))
sys.exit(1)
''');tool.chmod(0o700)
    d=tmp_path/'analysis'/'run';d.mkdir(parents=True)
    model=CodexModel(Settings(tmp_path/'data',tmp_path/'src',codex_bin=str(tool)),d,lambda *a:None)
    with pytest.raises(ModelExecutionError) as exc:
        model.call('test','inspect',{'evidence':[]},SearchPlan)
    assert exc.value.category=='quota'
    assert not list((d.parent/'.model-cache').glob('*.json'))


def test_native_image_inputs_are_passed_to_cli_and_invalidate_cache_by_pixels(tmp_path):
    from PIL import Image
    from jira_service.images import inspect_image
    import pytest
    data=tmp_path/'data';data.mkdir();shot=data/'image.png';Image.new('RGB',(20,20),'red').save(shot)
    tool=tmp_path/'fake-codex';calls=tmp_path/'calls'
    tool.write_text('#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
sys.stdin.read()
assert '--image' in sys.argv
assert Path(sys.argv[sys.argv.index('--image')+1]).read_bytes().startswith(b'\\x89PNG')
counter=Path('''+repr(str(calls))+''')
counter.write_text(counter.read_text()+'1' if counter.exists() else '1')
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'observations':[{'finding':'image fixture','evidence_ids':['E001']}],'log_terms':[],'source_terms':[],'evidence_gaps':[]}))
print(json.dumps({'type':'turn.completed','usage':{}}))
''');tool.chmod(0o700)
    settings=Settings(data,tmp_path/'src',codex_bin=str(tool));packet={'evidence':[{'id':'E001','kind':'image','text':'synthetic'}]}
    for name in ['one','two']:
        directory=tmp_path/'analysis'/name;directory.mkdir(parents=True)
        model=CodexModel(settings,directory,lambda *a:None);model.image_inputs=[{'path':str(shot),**inspect_image(shot)}]
        model.call('inspect','describe',packet,SearchPlan)
        assert model.receipts[0]['image_count']==1
    assert calls.read_text()=='1'
    Image.new('RGB',(20,20),'blue').save(shot)
    model.image_inputs=[{'path':str(shot),**inspect_image(shot)}];model.call('inspect','describe',packet,SearchPlan)
    assert calls.read_text()=='11'
    Image.new('RGB',(20,20),'green').save(shot)
    with pytest.raises(ValueError,match='截图校验失败'):model.call('inspect','describe',packet,SearchPlan)
    assert calls.read_text()=='11'
