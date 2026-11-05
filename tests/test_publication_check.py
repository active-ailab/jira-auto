import json
import pytest
from jira_service.integrations import Jira
from jira_service.settings import Settings
from test_analysis import svc


@pytest.mark.parametrize('mode,expected',[('exact','exact_match'),('changed','body_changed'),('duplicate','duplicate_markers'),('missing','not_seen')])
def test_readonly_cli_publication_probe_never_treats_a_marker_alone_as_success(tmp_path,monkeypatch,mode,expected):
    body='approved body';marker='[jira-auto:report]';comment={'id':'123','body':body+'\n\n'+marker,'created':'2026-10-30'}
    comments=[] if mode=='missing' else [comment]
    if mode=='changed':comment['body']='edited remotely\n'+marker
    if mode=='duplicate':comments.append({**comment,'id':'456'})
    calls=[]
    def cli(args,**kw):
        calls.append(args);return json.dumps({'fields':{'comment':{'comments':comments,'total':999,'startAt':0}}})
    monkeypatch.setattr('jira_service.integrations.run_cli',cli)
    result=Jira(Settings(tmp_path,tmp_path)).publication_evidence('ABS-1','report',body)
    assert result['state']==expected and not result['complete_page']
    assert calls[0][1:3]==['issue','view'] and '--raw' in calls[0]
    assert result['comment_id']==('123' if mode=='exact' else None)


@pytest.mark.parametrize('state',['exact_match','body_changed','not_seen','duplicate_markers'])
def test_reconciliation_never_reposts_and_only_exact_unique_match_restores_success(tmp_path,state):
    s=svc(tmp_path);r=s.save_report('COLOGNE-9001','approved exact content',report_kind='analysis')
    s.approve(r['id'])
    with s.store.db() as c:c.execute("UPDATE reports SET status='publication_unknown' WHERE id=?",(r['id'],))
    def probe(key,rid,body):
        assert key=='COLOGNE-9001' and rid==r['id'] and body=='approved exact content'
        return {'state':state,'comment_id':'123' if state=='exact_match' else None,'created':'2026-10-30'}
    s.jira.publication_evidence=probe;s.jira.comment=lambda *a:pytest.fail('readonly check cannot post')
    result=s.check_publication(r['id'])
    stored=s.store.rows('SELECT status,body,body_hash FROM reports WHERE id=?',(r['id'],))[0]
    assert stored['status']==result['status']==('published' if state=='exact_match' else 'publication_unknown')
    assert stored['body']=='approved exact content'
    if state=='exact_match':
        with pytest.raises(ValueError):s.check_publication(r['id'])


def test_report_change_while_reading_remote_comments_prevents_reconciliation(tmp_path):
    s=svc(tmp_path);r=s.save_report('COLOGNE-9001','body')
    with s.store.db() as c:c.execute("UPDATE reports SET status='publication_unknown' WHERE id=?",(r['id'],))
    def probe(*args):
        with s.store.db() as c:c.execute("UPDATE reports SET body='tampered' WHERE id=?",(r['id'],))
        return {'state':'exact_match'}
    s.jira.publication_evidence=probe
    with pytest.raises(ValueError,match='报告变化'):s.check_publication(r['id'])
    assert s.store.rows('SELECT status FROM reports WHERE id=?',(r['id'],))[0]['status']=='publication_unknown'


def test_jira_cli_wiki_escaping_preserves_exact_approved_text(monkeypatch,tmp_path):
    from jira_service.integrations import cli_comment_equal
    expected='- fault _beginPicker!\n[jira-auto:abc-123]'
    remote='\\- fault \\_beginPicker\\!\n\\[jira\\-auto:abc\\-123\\]'
    assert cli_comment_equal(remote,expected)
    assert not cli_comment_equal(remote.replace('fault','resolved'),expected)
    assert not cli_comment_equal('literal [x]','literal \\[x]')
    assert not cli_comment_equal('a b','a  b')
    body='- fault _beginPicker!'
    holder={'body':remote}
    monkeypatch.setattr('jira_service.integrations.run_cli',lambda *a,**kw:json.dumps({'fields':{'comment':{'comments':[{'id':'1','body':holder['body']}],'total':1}}}))
    jira=Jira(Settings(tmp_path,tmp_path))
    assert jira.publication_evidence('ABS-1','abc-123',body)['state']=='body_changed'
    holder['body']='\n\n'.join(remote.splitlines())
    assert jira.publication_evidence('ABS-1','abc-123',body)['state']=='exact_match'


def test_publication_check_api_is_readonly_even_with_jira_writes_disabled(tmp_path):
    from fastapi.testclient import TestClient
    from jira_service.app import create_app
    from jira_service.integrations import fingerprint
    from test_core import sample
    app=create_app(Settings(tmp_path/'data',tmp_path/'source',jira_write=False),start_worker=False)
    s=app.state.service;i=sample();s.store.upsert_issue(i,fingerprint(i));r=s.save_report(i['key'],'body')
    with s.store.db() as c:c.execute("UPDATE reports SET status='publication_unknown' WHERE id=?",(r['id'],))
    s.jira.comment=lambda *a:pytest.fail('no writes permitted')
    s.jira.publication_evidence=lambda *a:{'state':'exact_match','comment_id':'123','created':'2026-10-30'}
    with TestClient(app) as client:
        url='/api/reports/'+r['id']+'/check-publication'
        assert client.post(url,json={}).status_code==403
        token=client.get('/api/session').json()['token']
        result=client.post(url,json={},headers={'x-jira-auto-token':token})
        assert result.status_code==200 and result.json()['status']=='published'
        assert client.post(url,json={},headers={'x-jira-auto-token':token}).status_code==400
