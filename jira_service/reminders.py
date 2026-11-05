"""Shanghai daily focus: three assigned open issues, durable single-card delivery."""
import json
import calendar
import re
import uuid
from datetime import date,datetime
from zoneinfo import ZoneInfo
from .store import now

TZ=ZoneInfo('Asia/Shanghai')
LIMIT=3
POLICY_VERSION=3
START_FIELDS=('customfield_10702','customfield_12100','customfield_12000')
DUE_FIELDS=('duedate','customfield_12101','customfield_12001','customfield_11703','customfield_10333')


def parse_date(value):
    if not isinstance(value,str):return None
    try:return date.fromisoformat(value[:10]) if re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T)',value) else None
    except ValueError:return None


def six_months_before(day):
    month_index=day.year*12+day.month-1-6
    year,month=divmod(month_index,12);month+=1
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))


def schedule_fields(fields):
    def pick(keys):
        for key in keys:
            value=parse_date(fields.get(key))
            if value:return value.isoformat(),key
        return None,None
    start,skey=pick(START_FIELDS);due,dkey=pick(DUE_FIELDS)
    priority=(fields.get('priority') or {}).get('name') or '未设置'
    literal=re.search(r'(?i)\bP([0-4])\b',priority)
    rank=int(literal[1]) if literal else {'highest':0,'blocker':0,'critical':0,'最高':0,'high':1,'高':1,'medium':2,'normal':2,'中':2,'low':3,'低':3,'lowest':4,'最低':4}.get(priority.lower(),5)
    return {'schedule_version':1,'start_date':start,'start_date_field':skey,'due_date':due,'due_date_field':dkey,
            'priority':priority,'priority_rank':rank,'priority_label':'P'+str(rank) if rank<5 else '未分级',
            'resolved':bool(fields.get('resolution') or fields.get('resolutiondate'))}


def setup(store):
    with store.db() as c:c.execute('''CREATE TABLE IF NOT EXISTS daily_focus(
        day TEXT PRIMARY KEY,issue_keys TEXT NOT NULL,state TEXT NOT NULL,
        message_id TEXT,card TEXT,created TEXT NOT NULL,updated TEXT NOT NULL)''')
    with store.db() as c:
        columns={r[1] for r in c.execute('PRAGMA table_info(daily_focus)')}
        if 'policy_version' not in columns:c.execute('ALTER TABLE daily_focus ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1')
        if 'sent_policy_version' not in columns:c.execute('ALTER TABLE daily_focus ADD COLUMN sent_policy_version INTEGER')


def clock(instant=None):
    if instant is None:return datetime.now(TZ)
    if instant.tzinfo is None:raise ValueError('提醒时间必须包含时区')
    return instant.astimezone(TZ)


def begin_sync(store):
    with store.db() as c:
        row=c.execute("SELECT value FROM settings WHERE key='jira-assigned-snapshot'").fetchone()
        snapshot=json.loads(row['value']) if row else {}
        snapshot['complete']=False
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('jira-assigned-snapshot',json.dumps(snapshot)))


def data_snapshot(service,at):
    rows=service.store.rows("SELECT value FROM settings WHERE key='jira-assigned-snapshot'")
    snapshot=json.loads(rows[0]['value']) if rows else {}
    try:age=(at-datetime.fromisoformat(snapshot['synced_at'])).total_seconds()
    except (KeyError,ValueError,TypeError):age=float('inf')
    fresh=snapshot.get('complete',True) and 0<=age<=max(900,service.settings.poll_seconds*3)
    assigned=set(snapshot.get('keys',[]));today=at.date();active=[];future=0;excluded_overdue_issues=0;cutoff=six_months_before(today)
    for issue in service.store.issues():
        if issue['key'] not in assigned or issue.get('schedule_version')!=1 or issue.get('resolved') or issue.get('status_category')=='done':continue
        start=parse_date(issue.get('start_date'));due=parse_date(issue.get('due_date'))
        if due and due<cutoff:
            excluded_overdue_issues+=1;continue
        if start and start>today and (not due or due>today):future+=1;continue
        days=(due-today).days if due else None
        due_reason='截止未设置' if days is None else '已逾期 '+str(-days)+' 天' if days<0 else '今日截止' if days==0 else str(days)+' 天后截止'
        start_reason='开始未设置' if not start else '今日开始' if start==today else '已开始' if start<today else '尚未到开始日期'
        flags=[]
        if due and due<=today:flags.append('今日截止' if due==today else '已逾期')
        if start==today:flags.append('今日开始')
        active.append({'key':issue['key'],'summary':issue['summary'],'jira_url':issue['jira_url'],'status':issue['status'],
            'start_date':issue.get('start_date'),'due_date':issue.get('due_date'),'priority':issue.get('priority','未设置'),
            'priority_label':issue.get('priority_label','未分级'),'priority_rank':issue.get('priority_rank',5),
            'reason':due_reason+' · '+issue.get('priority_label','未分级')+' · '+start_reason,'date_alerts':flags})
    active.sort(key=lambda x:(x['due_date'] or '9999-12-31',x['priority_rank'],x['start_date'] or '9999-12-31',x['key']))
    return active,{'fresh':fresh,'synced_at':snapshot.get('synced_at'),'future_count':future,'excluded_overdue_issues':excluded_overdue_issues,'overdue_cutoff':cutoff.isoformat()}


def focus(service,instant=None):
    at=clock(instant);active,meta=data_snapshot(service,at)
    plan=service.store.rows('SELECT * FROM daily_focus WHERE day=?',(at.date().isoformat(),))
    if plan:
        chosen=json.loads(plan[0]['issue_keys']);by_key={i['key']:i for i in active}
        items=[by_key[k] for k in chosen if k in by_key]
    else:items=active[:LIMIT]
    state=plan[0]['state'] if plan else 'scheduled' if at.hour<9 else 'waiting_sync' if not meta['fresh'] else 'pending'
    return {'day':at.date().isoformat(),'timezone':'Asia/Shanghai','remind_at':'09:00','limit':LIMIT,'items':items,
        'candidate_count':len(active),'remaining_count':max(0,len(active)-len(items)),'locked':bool(plan),
        'policy_version':POLICY_VERSION,'card_outdated':bool(plan and plan[0]['card'] and plan[0]['sent_policy_version']!=POLICY_VERSION),'delivery_state':state,'delivery_updated':plan[0]['updated'] if plan else None,**meta}


def ensure_plan(service,instant=None):
    at=clock(instant)
    if at.hour<9:return
    active,meta=data_snapshot(service,at)
    if not meta['fresh']:return
    with service.store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM daily_focus WHERE day=?',(at.date().isoformat(),)).fetchone()
        keys=json.dumps([i['key'] for i in active[:LIMIT]])
        if old and old['policy_version']!=POLICY_VERSION and old['state']!='sending':
            # A requested policy change may rebuild today's list, never replay a sent/unknown card.
            c.execute("UPDATE daily_focus SET issue_keys=?,policy_version=?,updated=?,state=CASE WHEN state='skipped' AND ?!='[]' THEN 'queued' ELSE state END WHERE day=?",
                (keys,POLICY_VERSION,now(),keys,at.date().isoformat()))
        elif not old and active:
            c.execute("INSERT INTO daily_focus(day,issue_keys,state,created,updated,policy_version) VALUES(?,?,'queued',?,?,?)",
                (at.date().isoformat(),keys,now(),now(),POLICY_VERSION))


def reminder_card(payload,dashboard_url):
    elements=[{'tag':'div','text':{'tag':'plain_text','content':'今天只聚焦 '+str(len(payload['items']))+' 项。顺序：截止日期 → 优先级 → 开始日期。'}}]
    for index,item in enumerate(payload['items'],1):
        title=f"{index}. {item['key']} · {item['summary'][:240]}"
        detail='\n'+item['reason']+'\n开始 '+(item['start_date'] or '未设置')+' ｜ 截止 '+(item['due_date'] or '未设置')+'\n优先级 '+item['priority_label']+'（Jira: '+item['priority']+'）'
        elements.extend([{'tag':'div','text':{'tag':'plain_text','content':title+detail}},
            {'tag':'action','actions':[{'tag':'button','text':{'tag':'plain_text','content':'打开 '+item['key']},'url':item['jira_url']}]}])
    elements.append({'tag':'note','elements':[{'tag':'plain_text','content':'截止日期已过去超过6个自然月的工单不推荐。当天最多3项；完成或转出隐藏，次日重排。'}]})
    elements.append({'tag':'action','actions':[{'tag':'button','type':'primary','text':{'tag':'plain_text','content':'今日优先处理'},'url':dashboard_url.rstrip('/')+'/#view=focus'}]})
    return {'config':{'wide_screen_mode':True},'header':{'template':'orange','title':{'tag':'plain_text','content':'Jira Auto · 今日优先处理 · '+payload['day']}},'elements':elements}


def deliver(service,conf,transport,instant=None):
    if not conf.get('open_id'):return
    at=clock(instant);ensure_plan(service,at);payload=focus(service,at)
    if not payload['fresh']:return
    day=payload['day']
    with service.store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        plan=c.execute('SELECT * FROM daily_focus WHERE day=?',(day,)).fetchone()
        if not plan or plan['state']!='queued':return
        if not payload['items']:
            c.execute("UPDATE daily_focus SET state='skipped',updated=? WHERE day=?",(now(),day));return
        card=reminder_card(payload,conf['dashboard_url'])
        c.execute("UPDATE daily_focus SET state='sending',card=?,updated=?,sent_policy_version=policy_version WHERE day=?",(json.dumps(card,ensure_ascii=False),now(),day))
    try:
        mid=transport.send(conf['open_id'],card,str(uuid.uuid5(uuid.NAMESPACE_URL,'jira-auto:daily-focus:'+conf['open_id']+':'+day)))
        if not mid:raise RuntimeError('missing message receipt')
        state='sent'
    except Exception:mid=None;state='delivery_unknown'
    with service.store.db() as c:c.execute('UPDATE daily_focus SET state=?,message_id=?,updated=? WHERE day=?',(state,mid,now(),day))
    service.store.event('reminder','飞书今日优先处理卡片已发送' if mid else '飞书日程提醒发送结果待核对，不自动重发',details={'day':day,'keys':[i['key'] for i in payload['items']],'state':state})
