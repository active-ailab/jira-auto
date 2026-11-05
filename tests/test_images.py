import io
import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from jira_service.images import inspect_image,curated_images
from jira_service.materials import import_logs,sha256
from jira_service.integrations import fingerprint
from jira_service.settings import Settings
from jira_service.app import create_app
from test_analysis import svc,Model
from jira_service.analysis import analyze,SearchPlan


def png(path,color='red'):
    Image.new('RGB',(40,30),color).save(path)
    return path


def test_images_are_validated_and_selection_is_bounded_and_hash_checked(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001'
    for i,color in enumerate(['red','green','blue']):
        p=png(service.settings.data/(str(i)+'.png'),color)
        meta=inspect_image(p);assert meta['width']==40 and meta['media_type']=='image/png'
        service.store.artifact(key,'image',p,meta)
    selected,skipped=curated_images(service.settings,service.store,key)
    assert len(selected)==2 and len(skipped)==1 and '最多分析2张' in skipped[0]['reason']
    Path(selected[0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError):curated_images(service.settings,service.store,key)
    bad=tmp_path/'fake.png';bad.write_text('<svg onload="alert(1)"/>')
    with pytest.raises(ValueError):inspect_image(bad)
    animated=tmp_path/'animated.webp'
    Image.new('RGB',(20,20),'red').save(animated,save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100)
    with pytest.raises(ValueError,match='动态图'):inspect_image(animated)


def test_zip_import_classifies_screenshots_and_invalid_images_without_inventing_logs(tmp_path):
    service=svc(tmp_path);key='COLOGNE-9001';p=png(service.settings.data/'shot.png')
    archive=service.settings.data/'logs.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.write(p,'feedback/shot.png');z.writestr('broken.png',b'broken raster');z.writestr('user.log','normal log\n')
    import_logs(service.settings,service.store,key,archive,None)
    kinds={a['kind'] for a in service.store.artifacts(key)}
    assert {'image','image_unavailable','log','log_archive'}<=kinds
    _,skipped=curated_images(service.settings,service.store,key)
    assert any('格式不支持' in i['reason'] for i in skipped)


def test_direct_jira_screenshot_download_is_reused(tmp_path,monkeypatch):
    from jira_service.attachments import prepare_attachments
    service=svc(tmp_path);key='COLOGNE-9001';blob=io.BytesIO();Image.new('RGB',(20,20),'blue').save(blob,format='PNG');data=blob.getvalue()
    issue=service.store.issue(key);issue['attachments']=[{'id':'123','filename':'image.png','size':len(data)}]
    issue.pop('fingerprint');service.store.upsert_issue(issue,fingerprint(issue));calls=[]
    def download(args,**kw):calls.append(args);Path(args[args.index('--output')+1]).write_bytes(data)
    monkeypatch.setattr('jira_service.attachments.run_cli',download)
    prepare_attachments(service.settings,service.store,key,None);prepare_attachments(service.settings,service.store,key,None)
    assert len(calls)==1 and service.store.artifacts(key)[0]['kind']=='image'


def test_model_gets_screenshot_bytes_and_citable_evidence_without_paths_in_report(tmp_path,monkeypatch):
    monkeypatch.setattr('jira_service.similarity.search_recent',lambda *a,**kw:{'status':'not_crash','candidates':[]})
    service=svc(tmp_path);key='COLOGNE-9001';p=png(service.settings.data/'shot.png')
    service.store.artifact(key,'image',p,inspect_image(p))
    class Vision(Model):
        def call(self,stage,instruction,packet,schema):
            assert len(self.image_inputs)==1 and Path(self.image_inputs[0]['path']).read_bytes()==p.read_bytes()
            assert packet['images'][0]['image_number']==1 and 'path' not in packet['images'][0]
            assert packet['images'][0]['evidence_id']=='E002'
            assert packet['evidence'][1]['kind']=='image'
            return super().call(stage,instruction,packet,schema)
    result=analyze(service.settings,service.store,key,'vision-test',service.save_report,Vision)
    assert not result['failure'] and result['analysis']['images'][0]['sha256']==sha256(p)
    assert str(p) not in json.dumps(result['analysis'])


def test_preview_serves_only_validated_raster_of_the_requested_report_hash(tmp_path):
    app=create_app(Settings(tmp_path/'data',tmp_path/'source'),start_worker=False);s=app.state.service
    p=png(s.settings.data/'shot.png');meta=inspect_image(p);aid=s.store.artifact('ABS-1','image',p,meta)
    with TestClient(app) as client:
        url='/api/artifacts/'+aid+'/preview'
        response=client.get(url,params={'sha256':meta['sha256']})
        assert response.status_code==200 and response.headers['content-type']=='image/png'
        assert response.content==p.read_bytes()
        assert client.get(url,params={'sha256':'0'*64}).status_code==400
        png(p,'green')
        assert client.get(url,params={'sha256':meta['sha256']}).status_code==400


def test_old_binary_screenshot_classification_is_hidden_after_validated_reimport(tmp_path):
    s=svc(tmp_path);key='COLOGNE-9001';path=png(s.settings.data/'old.png');metadata=inspect_image(path)
    s.store.artifact(key,'binary_log',path,{'sha256':metadata['sha256']})
    s.store.artifact(key,'image',path,metadata)
    assert [a['kind'] for a in s.store.artifacts(key)]==['image']
