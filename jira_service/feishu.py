"""Private Feishu bot configuration, durable delivery and approval processing."""
import json
import os
import secrets
import threading
import time
import uuid
import re
import unicodedata
from pathlib import Path

from .store import now
from .integrations import digest


def config_path(settings):
    return settings.data / 'feishu.json'


def load_config(settings):
    path = config_path(settings)
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ValueError('飞书配置文件权限必须为 600')
    conf = json.loads(path.read_text())
    if not all(conf.get(k) for k in ('app_id','app_secret','dashboard_url')):
        raise ValueError('飞书配置不完整')
    if not conf.get('open_id') and not conf.get('pairing_token'):
        raise ValueError('缺少接收人或绑定口令')
    return conf


def save_config(settings, conf):
    path=config_path(settings)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as f: f.write(json.dumps(conf,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


def init_store(store):
    with store.db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS feishu_deliveries (
          report_id TEXT PRIMARY KEY, nonce TEXT NOT NULL, body_hash TEXT NOT NULL,
          message_id TEXT, state TEXT NOT NULL, command TEXT, actor TEXT,
          updated TEXT NOT NULL);
        ''')


def status(settings, store):
    try:
        conf = load_config(settings)
    except (ValueError,OSError):
        return {'name':'飞书审批','state':'failed','detail':'本地飞书配置无效，请运行配置脚本修复'}
    if not conf:
        return {'name':'飞书审批','state':'unconfigured','detail':'审批功能已实现；待本地配置 App 凭据，再私聊机器人完成绑定，无需手动填写 open_id'}
    if not conf.get('open_id'):
        return {'name':'飞书审批','state':'pairing','detail':'专用机器人待绑定：请在飞书私聊发送配置脚本显示的绑定口令'}
    rows=store.rows("SELECT value FROM settings WHERE key='feishu_health'")
    health=json.loads(rows[0]['value']) if rows else {}
    fresh=health.get('at',0)>time.time()-45
    return {'name':'飞书审批','state':health.get('state','pending') if fresh else 'pending',
            'detail':health.get('detail','等待长连接启动') if fresh else '等待长连接启动或重连；网页审批仍可用'}


class ApprovalBot:
    def __init__(self, service, conf, transport):
        self.service,self.store,self.conf,self.transport=service,service.store,conf,transport
        init_store(self.store)

    def card(self, report, nonce):
        # Always show the exact saved comment. Oversize comments require web review.
        failed=report['kind']=='analysis_failed'
        full=len(report['body'].encode('utf-8'))<=18000
        text=report['body'] if full else '评论较长，请打开网页查看完整版本并审批。'
        actions=[{'tag':'button','text':{'tag':'plain_text','content':'查看分析过程 / 编辑'},
                  'url':self.conf['dashboard_url'].rstrip('/')+'/#issue='+report['issue_key']+'&tab=draft'}]
        if full and not failed:
            for action,label,typ in [('approve','批准并评论' if self.service.settings.jira_write else '批准当前版本','primary'),('reject','退回，不评论','default')]:
                actions.append({'tag':'button','text':{'tag':'plain_text','content':label},'type':typ,
                    'value':{'report_id':report['id'],'nonce':nonce,'action':action},
                    'confirm':{'title':{'tag':'plain_text','content':label},'text':{'tag':'plain_text','content':'确认操作卡片展示的这一版评论？'}}})
        return {'config':{'wide_screen_mode':True},'header':{'template':'orange','title':{'tag':'plain_text','content':report['issue_key']+(' · 分析执行失败' if failed else ' · 分析待审批')}},
                'elements':[{'tag':'div','text':{'tag':'plain_text','content':text}},
                {'tag':'note','elements':[{'tag':'plain_text','content':'草稿 '+report['id']+' · 修改或材料变化后旧卡片失效。审批后仅添加评论，不流转工单。'}]},
                {'tag':'action','actions':actions}]}

    def receive(self, actor, message_id, value):
        rid=value.get('report_id','')
        action=value.get('action')
        if not self.conf.get('open_id') or actor!=self.conf['open_id']:
            raise ValueError('仅指定接收人可以审批')
        if action=='connection_test':
            with self.store.db() as c:
                row=c.execute("SELECT value FROM settings WHERE key='feishu_test_card'").fetchone()
                test=json.loads(row['value']) if row else {}
                if test.get('message_id')!=message_id or not secrets.compare_digest(test.get('nonce',''),str(value.get('nonce',''))):
                    raise ValueError('测试卡片校验失败')
                test['status']='passed'
                c.execute("UPDATE settings SET value=? WHERE key='feishu_test_card'",(json.dumps(test),))
            self.store.event('feishu','飞书连接测试通过：卡片按钮回调已收到，未审批或发布 Jira 评论')
            return '连接测试通过，未操作 Jira'
        if action not in ('approve','reject'): raise ValueError('未知审批操作')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            d=c.execute('SELECT * FROM feishu_deliveries WHERE report_id=?',(rid,)).fetchone()
            r=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
            if not d or not secrets.compare_digest(d['nonce'],str(value.get('nonce',''))) or d['message_id']!=message_id:
                raise ValueError('审批卡片校验失败')
            if d['state']!='sent':
                return '该操作已接收，请在看板查看结果'
            current=c.execute('SELECT fingerprint FROM issues WHERE key=?',(r['issue_key'],)).fetchone() if r else None
            if not r or r['status']!='draft' or r['kind'] in ('preflight','analysis_failed') or not current or current['fingerprint']!=r['fingerprint'] or r['body_hash']!=d['body_hash'] or digest(r['body'])!=d['body_hash']:
                raise ValueError('草稿或材料已变化，请审阅最新版本')
            c.execute("UPDATE feishu_deliveries SET state='queued',command=?,actor=?,updated=? WHERE report_id=?",(action,actor,now(),rid))
        self.store.event('feishu','飞书审批操作已接收，等待后台处理',r['issue_key'],details={'report_id':rid,'action':action})
        return '已接收，后台处理结果将更新卡片和看板'

    def receive_message(self, actor, chat_type, text):
        if chat_type!='p2p': return
        token=self.conf.get('pairing_token')
        if not self.conf.get('open_id'):
            normalized=' '.join(unicodedata.normalize('NFKC',text).replace('`','').replace('\u200b','').split())
            candidates=re.findall(r'[A-Za-z0-9_-]+',normalized)
            matched=bool(token) and any(secrets.compare_digest(part,token) for part in candidates)
            if not matched:
                self.store.event('feishu','已收到飞书私聊，但绑定口令不匹配；请发送配置脚本生成的完整口令')
                return
            self.conf['open_id']=actor
            self.conf.pop('pairing_token',None)
            save_config(self.service.settings,self.conf)
            self.store.event('feishu','专用飞书机器人已绑定接收人')
            self.transport.text(actor,'绑定成功。Jira Auto 分析完成后会发送待审卡片。发送“状态”查看待审数量，发送“帮助”查看指令。')
            return
        if actor!=self.conf['open_id']: return
        if text.strip()=='状态':
            count=self.store.rows("SELECT COUNT(*) AS n FROM reports WHERE status='draft' AND kind IN ('analysis','analysis_revision','manual')")[0]['n']
            self.transport.text(actor,f'当前有 {count} 份分析待审批。看板：'+self.conf['dashboard_url'])
        elif text.strip()=='帮助':
            self.transport.text(actor,'我是 Jira Auto 专用机器人。分析完成后会推送待审卡片；请查看完整评论后点击批准或退回。修改评论请打开看板。支持“状态”和“帮助”。当前尚不支持自由对话分析。')

    def tick(self):
        if not self.conf.get('open_id'): return
        # Restarted commands are never replayed: Jira may already have received them.
        for d in self.store.rows("SELECT * FROM feishu_deliveries WHERE state='queued'"):
            with self.store.db() as c:
                claimed=c.execute("UPDATE feishu_deliveries SET state='processing',updated=? WHERE report_id=? AND state='queued'",(now(),d['report_id'])).rowcount
            if not claimed: continue
            rid=d['report_id']; r=self.store.rows('SELECT * FROM reports WHERE id=?',(rid,))[0]
            try:
                if d['command']=='reject':
                    with self.store.db() as c:
                        changed=c.execute("UPDATE reports SET status='rejected',approved=NULL WHERE id=? AND status='draft'",(rid,)).rowcount
                    if not changed: raise ValueError('草稿已变化，退回未执行')
                    result='已退回，未发布 Jira 评论'
                else:
                    self.service.approve(rid,origin='飞书')
                    result='已批准；Jira 回写关闭，未发布'
                    if self.service.settings.jira_write:
                        self.service.publish(rid)
                        result='已通过 Jira CLI 发布批准的评论'
                state='done'
            except Exception:
                state='failed'; result='操作未完成或发布结果待核对，请打开看板查看；不会自动重试'
            with self.store.db() as c:
                c.execute('UPDATE feishu_deliveries SET state=?,updated=? WHERE report_id=?',(state,now(),rid))
            self.store.event('feishu',result,r['issue_key'],details={'report_id':rid,'state':state})
            try: self.transport.update(d['message_id'],result)
            except Exception: self.store.event('feishu','飞书卡片结果更新失败，请查看看板',r['issue_key'])
        for r in self.store.rows("SELECT * FROM reports WHERE status='draft' AND kind IN ('analysis','analysis_revision','manual','analysis_failed') AND id NOT IN (SELECT report_id FROM feishu_deliveries) ORDER BY created LIMIT 10"):
            nonce=secrets.token_urlsafe(24)
            with self.store.db() as c:
                claimed=c.execute("INSERT OR IGNORE INTO feishu_deliveries(report_id,nonce,body_hash,state,updated) VALUES(?,?,?,'sending',?)",(r['id'],nonce,r['body_hash'],now())).rowcount
            if not claimed: continue
            try:
                mid=self.transport.send(self.conf['open_id'],self.card(r,nonce),r['id'])
                state='sent'
            except Exception:
                mid=None;state='delivery_unknown'
            with self.store.db() as c:
                c.execute('UPDATE feishu_deliveries SET state=?,message_id=?,updated=? WHERE report_id=?',(state,mid,now(),r['id']))
            self.store.event('feishu','飞书待审卡片已发送' if mid else '飞书卡片发送失败或结果未知，请通过网页审批',r['issue_key'],details={'report_id':r['id'],'state':state})

        from .reminders import deliver
        deliver(self.service,self.conf,self.transport)


def send_connection_test(settings):
    import requests
    from .store import Store
    conf=load_config(settings)
    if not conf.get('open_id'): raise ValueError('请先绑定机器人')
    store=Store(settings.data)
    nonce=secrets.token_urlsafe(24)
    card={'header':{'template':'blue','title':{'tag':'plain_text','content':'Jira Auto · 连接测试'}},
          'elements':[{'tag':'div','text':{'tag':'plain_text','content':'机器人已绑定。点击下方按钮验证飞书回调；本卡片不会审批工单或发布 Jira 评论。'}},
          {'tag':'action','actions':[{'tag':'button','type':'primary','text':{'tag':'plain_text','content':'验证回调（不评论）'},'value':{'action':'connection_test','nonce':nonce}},
          {'tag':'button','text':{'tag':'plain_text','content':'打开看板'},'url':conf['dashboard_url']}]}]}
    session=requests.Session()
    auth=session.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',json={'app_id':conf['app_id'],'app_secret':conf['app_secret']},timeout=15).json()
    if auth.get('code')!=0: raise ValueError('飞书认证失败')
    result=session.post('https://open.feishu.cn/open-apis/im/v1/messages',params={'receive_id_type':'open_id'},headers={'Authorization':'Bearer '+auth['tenant_access_token']},json={'receive_id':conf['open_id'],'msg_type':'interactive','content':json.dumps(card,ensure_ascii=False),'uuid':str(uuid.uuid4())},timeout=15).json()
    if result.get('code')!=0: raise ValueError('飞书测试卡片发送失败，错误码 '+str(result.get('code')))
    with store.db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('feishu_test_card',?)",(json.dumps({'nonce':nonce,'message_id':result['data']['message_id'],'status':'sent'}),))
    store.event('feishu','已发送飞书连接测试卡片；不包含 Jira 审批操作')
    return {'status':'sent'}


def card_client_class():
    import lark_oapi as lark
    class CardClient(lark.ws.Client):
        async def _handle_data_frame(self, frame):
            # SDK 1.7.5 drops CARD frames. Reuse its authenticated event
            # dispatcher, response handling and fragment reassembly.
            for header in frame.headers:
                if header.key=='type' and header.value=='card': header.value='event'
            return await super()._handle_data_frame(frame)
    return CardClient


def main():
    import logging
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest,CreateMessageRequestBody,PatchMessageRequest,PatchMessageRequestBody
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    from .service import Service
    from .settings import Settings
    settings=Settings.load(); service=Service(settings); conf=load_config(settings)
    if not conf: return
    # SDK logs can contain request URLs, payloads and access tokens; never persist them.
    logging.disable(logging.CRITICAL)
    client=lark.Client.builder().app_id(conf['app_id']).app_secret(conf['app_secret']).timeout(15).build()
    class Transport:
        def send(self, user, card, identifier):
            req=CreateMessageRequest.builder().receive_id_type('open_id').request_body(CreateMessageRequestBody.builder().receive_id(user).msg_type('interactive').content(json.dumps(card,ensure_ascii=False)).uuid(identifier).build()).build()
            resp=client.im.v1.message.create(req)
            if not resp.success(): raise RuntimeError('send failed')
            return resp.data.message_id
        def text(self, user, text):
            req=CreateMessageRequest.builder().receive_id_type('open_id').request_body(CreateMessageRequestBody.builder().receive_id(user).msg_type('text').content(json.dumps({'text':text},ensure_ascii=False)).uuid(str(uuid.uuid4())).build()).build()
            if not client.im.v1.message.create(req).success(): raise RuntimeError('reply failed')
        def update(self, mid, message):
            card={'elements':[{'tag':'div','text':{'tag':'plain_text','content':message+'。请在 Jira Auto 看板查看记录。'}}]}
            resp=client.im.v1.message.patch(PatchMessageRequest.builder().message_id(mid).request_body(PatchMessageRequestBody.builder().content(json.dumps(card,ensure_ascii=False)).build()).build())
            if not resp.success(): raise RuntimeError('update failed')
    bot=ApprovalBot(service,conf,Transport())
    with service.store.db() as c:
        c.execute("UPDATE feishu_deliveries SET state='processing_unknown' WHERE state='processing'")
        c.execute("UPDATE feishu_deliveries SET state='delivery_unknown' WHERE state='sending'")
    def callback(data):
        try:
            e=data.event
            message=bot.receive(e.operator.open_id,e.context.open_message_id,e.action.value or {})
            typ='info'
        except ValueError as exc: message=str(exc);typ='error'
        except Exception: message='处理失败，请通过网页查看';typ='error'
        return P2CardActionTriggerResponse({'toast':{'type':typ,'content':message}})
    def incoming(data):
        try:
            e=data.event
            service.store.event('feishu','收到飞书消息事件',details={'message_type':e.message.message_type,'chat_type':e.message.chat_type})
            if e.message.message_type!='text': return
            content=json.loads(e.message.content)
            bot.receive_message(e.sender.sender_id.open_id,e.message.chat_type,content.get('text',''))
        except Exception:
            service.store.event('feishu','飞书私聊处理失败，请查看本地配置')
    handler=lark.EventDispatcherHandler.builder('','').register_p2_card_action_trigger(callback).register_p2_im_message_receive_v1(incoming).build()
    ws=card_client_class()(conf['app_id'],conf['app_secret'],event_handler=handler,log_level=lark.LogLevel.ERROR)
    def health(state,detail):
        with service.store.db() as c:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('feishu_health',?)",(json.dumps({'state':state,'detail':detail,'at':time.time()}),))
    ws.on_reconnecting=lambda:health('pending','飞书连接中断，正在重连')
    ws.on_reconnected=lambda:health('ready','飞书长连接已恢复；待审卡片自动推送')
    def poll():
        while True:
            connected=getattr(ws,'_conn',None) is not None
            health('ready' if connected else 'pending','飞书长连接已建立；待审卡片自动推送' if connected else '飞书长连接尚未建立，请检查应用配置')
            if connected:
                try: bot.tick()
                except Exception: health('failed','飞书处理失败，请检查看板记录和应用配置')
            time.sleep(5)
    threading.Thread(target=poll,daemon=True).start()
    ws.start()

if __name__=='__main__': main()
