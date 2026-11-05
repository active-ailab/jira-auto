import json
from dataclasses import replace
import pytest
from jira_service.feishu import ApprovalBot, load_config, save_config, status
from test_analysis import svc


class Transport:
    def __init__(self): self.sent=[];self.updated=[];self.texts=[]
    def send(self,user,card,identifier): self.sent.append((user,card,identifier));return 'om_test'
    def update(self,mid,message): self.updated.append(message)
    def text(self,user,text): self.texts.append((user,text))


def setup(tmp_path):
    service=svc(tmp_path)
    conf={'app_id':'cli_fake','app_secret':'test-only','open_id':'ou_owner','dashboard_url':'http://testserver:8791'}
    transport=Transport();bot=ApprovalBot(service,conf,transport)
    report=service.save_report('COLOGNE-9001','证据不足，无法确认根因。',report_kind='analysis')
    bot.tick()
    d=service.store.rows('SELECT * FROM feishu_deliveries')[0]
    value={'report_id':report['id'],'nonce':d['nonce'],'action':'approve'}
    return service,bot,transport,report,value


def test_card_contains_exact_revision_and_duplicate_delivery_is_avoided(tmp_path):
    s,b,t,r,v=setup(tmp_path);b.tick()
    assert len(t.sent)==1
    assert t.sent[0][1]['elements'][0]['text']['content']=='证据不足，无法确认根因。'
    assert t.sent[0][1]['elements'][-1]['actions'][1]['value']==v


def test_only_bound_operator_and_original_message_can_approve(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    for actor,message,value in [('ou_other','om_test',v),('ou_owner','om_other',v),('ou_owner','om_test',{**v,'nonce':'wrong'})]:
        with pytest.raises(ValueError): b.receive(actor,message,value)
    assert s.store.rows('SELECT status FROM reports')[0]['status']=='draft'


def test_callback_is_fast_durable_and_publishes_once(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    s.settings=replace(s.settings,jira_write=True)
    s.refresh=lambda key: s.store.issue(key)
    comments=[];s.jira.comment=lambda key,body: comments.append((key,body))
    b.receive('ou_owner','om_test',v)
    b.receive('ou_owner','om_test',v)
    assert not comments
    b.tick();b.tick()
    assert len(comments)==1
    assert '证据不足，无法确认根因。' in comments[0][1]
    assert s.store.rows('SELECT status FROM reports')[0]['status']=='published'


def test_stale_card_cannot_approve(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    s.save_report('COLOGNE-9001','新版本',report_kind='analysis')
    with pytest.raises(ValueError):b.receive('ou_owner','om_test',v)


def test_queued_approval_rechecks_revision_before_publish(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    b.receive('ou_owner','om_test',v)
    s.save_report('COLOGNE-9001','新版本',report_kind='analysis')
    b.tick()
    assert s.store.rows('SELECT status FROM reports WHERE id=?',(r['id'],))[0]['status']=='superseded'
    assert s.store.rows('SELECT state FROM feishu_deliveries WHERE report_id=?',(r['id'],))[0]['state']=='failed'


def test_reject_does_not_publish(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    b.receive('ou_owner','om_test',{**v,'action':'reject'});b.tick()
    assert s.store.rows('SELECT status FROM reports')[0]['status']=='rejected'


def test_private_pairing_identifies_user_without_manual_open_id(tmp_path):
    s=svc(tmp_path);t=Transport()
    conf={'app_id':'cli_fake','app_secret':'test-only','dashboard_url':'http://testserver','pairing_token':'private-code'}
    save_config(s.settings,conf)
    b=ApprovalBot(s,conf,t)
    b.receive_message('ou_intruder','group','绑定 jira-auto private-code')
    b.receive_message('ou_intruder','p2p','绑定 jira-auto wrong')
    assert not conf.get('open_id')
    b.receive_message('ou_owner','p2p','请绑定：`绑定 jira-auto private-code`。')
    stored=load_config(s.settings)
    assert stored['open_id']=='ou_owner' and 'pairing_token' not in stored
    assert (s.settings.data/'feishu.json').stat().st_mode & 0o077==0
    assert t.texts[0][0]=='ou_owner'
    b.receive_message('ou_owner','p2p','状态')
    assert '0 份' in t.texts[-1][1]


def test_uncertain_send_is_not_retried(tmp_path):
    s=svc(tmp_path);t=Transport()
    def failed(*args):raise TimeoutError()
    t.send=failed
    b=ApprovalBot(s,{'open_id':'ou_owner','dashboard_url':'http://testserver'},t)
    s.save_report('COLOGNE-9001','无法判断',report_kind='analysis')
    b.tick();b.tick()
    assert s.store.rows('SELECT state FROM feishu_deliveries')[0]['state']=='delivery_unknown'


def test_sdk_card_frame_reaches_registered_callback_and_returns_ack():
    import asyncio
    import base64
    import lark_oapi as lark
    from lark_oapi.ws.client import Frame
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    from jira_service.feishu import card_client_class
    received=[]
    def callback(data):
        received.append(data.event.operator.open_id)
        return P2CardActionTriggerResponse({'toast':{'type':'info','content':'已接收'}})
    handler=lark.EventDispatcherHandler.builder('','').register_p2_card_action_trigger(callback).build()
    client=card_client_class()('cli_fake','fake',event_handler=handler)
    frame=Frame()
    frame.SeqID=1;frame.LogID=1;frame.service=1;frame.method=1
    for key,value in {'type':'card','message_id':'m1','trace_id':'t1','sum':'1','seq':'0'}.items():
        h=frame.headers.add();h.key=key;h.value=value
    frame.payload=json.dumps({'schema':'2.0','header':{'event_id':'e1','event_type':'card.action.trigger','app_id':'cli_fake','tenant_key':'t1','create_time':'1'},'event':{'operator':{'open_id':'ou_owner'},'context':{'open_message_id':'om_test'},'action':{'value':{}}}}).encode()
    responses=[]
    async def write(raw):responses.append(raw)
    client._write_message=write
    asyncio.run(client._handle_data_frame(frame))
    assert received==['ou_owner']
    response=Frame();response.ParseFromString(responses[0]);payload=json.loads(response.payload)
    assert payload['code']==200
    assert json.loads(base64.b64decode(payload['data']))['toast']['content']=='已接收'

    # SDK creates its cache cleanup task on its module loop during construction.
    from lark_oapi.ws.client import loop
    tasks=asyncio.all_tasks(loop)
    for task in tasks: task.cancel()
    if tasks: loop.run_until_complete(asyncio.gather(*tasks,return_exceptions=True))


def test_connection_test_callback_cannot_approve_or_publish(tmp_path):
    s,b,t,r,v=setup(tmp_path)
    with s.store.db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('feishu_test_card',?)",(json.dumps({'nonce':'test-nonce','message_id':'om_ping','status':'sent'}),))
    result=b.receive('ou_owner','om_ping',{'action':'connection_test','nonce':'test-nonce'})
    assert '未操作 Jira' in result
    assert s.store.rows('SELECT status FROM reports')[0]['status']=='draft'
    with pytest.raises(ValueError):b.receive('ou_owner','om_ping',{'action':'connection_test','nonce':'wrong'})


def test_execution_failure_card_has_no_approval_buttons(tmp_path):
    s=svc(tmp_path);t=Transport();b=ApprovalBot(s,{'open_id':'ou_owner','dashboard_url':'http://testserver'},t)
    report=s.save_report('COLOGNE-9001','分析执行失败，已有证据保留',report_kind='analysis_failed')
    b.tick()
    card=t.sent[0][1]
    assert '分析执行失败' in card['header']['title']['content']
    assert not any('value' in action for action in card['elements'][-1]['actions'])
    with pytest.raises(ValueError):s.approve(report['id'])
