import json
from dataclasses import replace
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from jira_service import reminders as r
from jira_service.integrations import normalize,fingerprint
from jira_service.service import Service
from jira_service.settings import Settings
from jira_service.app import create_app

TZ=ZoneInfo('Asia/Shanghai')
AT=datetime(2026,11,3,9,5,tzinfo=TZ)


def issue(key,due=None,start=None,priority='Medium',done=False):
    return normalize({'key':key,'fields':{'summary':'处理 '+key,'priority':{'name':priority},'duedate':due,'customfield_10702':start,
        'status':{'name':'Done' if done else 'Open','statusCategory':{'key':'done' if done else 'new'}},'resolution':{'name':'Fixed'} if done else None}})


def svc(tmp_path,issues):
    s=Service(Settings(tmp_path,tmp_path/'source'))
    for i in issues:s.store.upsert_issue(i,fingerprint(i))
    snapshot(s,[i['key'] for i in issues]);return s


def snapshot(s,keys,at=AT):
    with s.store.db() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('jira-assigned-snapshot',json.dumps({'keys':keys,'synced_at':at.isoformat()})))


class Transport:
    def __init__(self,fail=False):self.cards=[];self.fail=fail
    def send(self,user,card,identifier):
        self.cards.append((user,card,identifier))
        if self.fail:raise RuntimeError('network timeout')
        return 'om_receipt'

CONF={'open_id':'ou_owner','dashboard_url':'http://localhost:8791'}


def test_real_jira_schedule_fields_precedence_and_priority():
    i=normalize({'key':'ABS-1','fields':{'priority':{'name':'Highest'},'duedate':'2026-11-05','customfield_10702':'2026-11-03','customfield_12100':'2026-11-01','customfield_12101':'2026-11-09'}})
    assert (i['start_date'],i['start_date_field'])==('2026-11-03','customfield_10702')
    assert (i['due_date'],i['due_date_field'])==('2026-11-05','duedate')
    assert i['priority_label']=='P0' and i['priority']=='Highest'
    fallback=r.schedule_fields({'duedate':'bad','customfield_12101':'2026-12-01','customfield_12100':'2026-11-03','customfield_10704':'2000-01-01','priority':{'name':'P1 - High'}})
    assert fallback['start_date']=='2026-11-03' and fallback['due_date']=='2026-12-01' and fallback['priority_rank']==1
    assert r.schedule_fields({'customfield_10704':'2000-01-01'})['start_date'] is None  # baseline is not the plan


@pytest.mark.parametrize('value',['2026-02-30','wrong','2026-11-03evil',None,{},'2026-13-01'])
def test_invalid_dates_not_invented(value):assert r.parse_date(value) is None


def test_three_only_due_then_priority_then_start_and_no_future_or_closed(tmp_path):
    issues=[issue('ABS-1','2026-11-03','2026-11-03','Medium'),issue('ABS-2','2026-11-03','2026-11-02','Highest'),
        issue('ABS-3','2026-11-02',None,'Low'),issue('ABS-4','2026-11-03','2026-11-01','Highest'),
        issue('ABS-5',None,'2026-11-10','Highest'),issue('ABS-6','2026-10-01',None,'Highest',True),
        issue('ABS-7','2026-09-01')]
    s=svc(tmp_path,issues);snapshot(s,[i['key'] for i in issues if i['key']!='ABS-7'])
    p=r.focus(s,AT)
    assert [i['key'] for i in p['items']]==['ABS-3','ABS-4','ABS-2']
    assert p['future_count']==1 and p['remaining_count']==1
    assert p['items'][0]['reason'].startswith('已逾期 1 天')


def test_future_start_still_included_when_due_today_and_no_date_shown_honestly(tmp_path):
    s=svc(tmp_path,[issue('ABS-1','2026-11-03','2026-11-05'),issue('ABS-2',None,None,'High')])
    p=r.focus(s,AT);assert len(p['items'])==2
    assert p['items'][0]['date_alerts']==['今日截止']
    assert '截止未设置' in p['items'][1]['reason'] and p['items'][1]['priority_label']=='P1'


def test_daily_card_cap_duplicate_ticks_restart_and_next_day(tmp_path):
    s=svc(tmp_path,[issue('ABS-'+str(n),'2026-11-03','2026-11-03') for n in range(1,7)])
    t=Transport();r.deliver(s,CONF,t,AT);r.deliver(s,CONF,t,AT)
    restarted=Service(s.settings);restarted.store.recover();r.deliver(restarted,CONF,t,AT)
    assert len(t.cards)==1
    body=json.dumps(t.cards[0][1],ensure_ascii=False)
    for key in ('ABS-1','ABS-2','ABS-3'):assert key in body
    for key in ('ABS-4','ABS-5','ABS-6'):assert key not in body
    assert '今日开始' in body and '今日截止' in body and '#view=focus' in body
    assert 'approve' not in body and 'report_id' not in body
    tomorrow=AT+timedelta(days=1);snapshot(restarted,['ABS-'+str(n) for n in range(1,7)],tomorrow)
    r.deliver(restarted,CONF,t,tomorrow)
    assert len(t.cards)==2 and t.cards[0][2]!=t.cards[1][2]


def test_scheduled_at_nine_shanghai_and_same_day_catchup_only(tmp_path):
    s=svc(tmp_path,[issue('ABS-1','2026-11-03')]);t=Transport()
    before=AT.replace(hour=8,minute=59);snapshot(s,['ABS-1'],before)
    r.deliver(s,CONF,t,before);assert not t.cards
    snapshot(s,['ABS-1'],AT);r.deliver(s,CONF,t,AT.astimezone(ZoneInfo('UTC')))
    assert len(t.cards)==1
    assert s.store.rows('SELECT day FROM daily_focus')==[{'day':'2026-11-03'}]
    with pytest.raises(ValueError):r.focus(s,datetime(2026,11,3))


def test_stale_snapshot_unbound_bot_never_send(tmp_path):
    s=svc(tmp_path,[issue('ABS-1','2026-11-03')]);t=Transport()
    r.deliver(s,{},t,AT);assert not t.cards
    snapshot(s,['ABS-1'],AT-timedelta(hours=1));r.deliver(s,CONF,t,AT)
    assert not t.cards and not r.focus(s,AT)['fresh']


def test_unknown_send_outcome_no_automatic_retry_and_survives_restart(tmp_path):
    s=svc(tmp_path,[issue('ABS-1')]);t=Transport(True)
    r.deliver(s,CONF,t,AT);r.deliver(s,CONF,t,AT)
    assert len(t.cards)==1 and r.focus(s,AT)['delivery_state']=='delivery_unknown'
    s.store.recover();r.deliver(s,CONF,t,AT);assert len(t.cards)==1


def test_completed_reassigned_or_rescheduled_items_hide_without_daily_replacement(tmp_path):
    items=[issue('ABS-'+str(n)) for n in range(1,5)];s=svc(tmp_path,items);t=Transport()
    r.deliver(s,CONF,t,AT)
    changed=issue('ABS-1',done=True);s.store.upsert_issue(changed,fingerprint(changed))
    snapshot(s,['ABS-1','ABS-3','ABS-4'])  # ABS-2 moved to another assignee
    p=r.focus(s,AT);assert [i['key'] for i in p['items']]==['ABS-3']
    r.deliver(s,CONF,t,AT);assert len(t.cards)==1


def test_failed_sync_does_not_advance_assignment_snapshot(tmp_path,monkeypatch):
    s=svc(tmp_path,[issue('ABS-1')])
    monkeypatch.setattr(s.jira,'assigned_updates',lambda:{'ABS-1':'changed','ABS-2':'changed'})
    monkeypatch.setattr(s,'refresh',lambda key:(_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError):s.sync(None)
    state=json.loads(s.store.rows("SELECT value FROM settings WHERE key='jira-assigned-snapshot'")[0]['value'])
    assert state['keys']==['ABS-1'] and state['synced_at']==AT.isoformat() and not state['complete']
    assert not r.focus(s,AT)['fresh']


def test_api_focus_is_read_only_and_preserves_pending_approvals(tmp_path,monkeypatch):
    app=create_app(Settings(tmp_path,tmp_path/'source'),start_worker=False);s=app.state.service
    i=issue('ABS-1','2026-11-03');s.store.upsert_issue(i,fingerprint(i));snapshot(s,['ABS-1'])
    s.save_report('ABS-1','Existing review',report_kind='analysis')
    monkeypatch.setattr(r,'clock',lambda instant=None:AT)
    with TestClient(app) as c:
        result=c.get('/api/focus');assert result.status_code==200 and result.json()['items'][0]['key']=='ABS-1'
    assert not s.store.rows('SELECT * FROM daily_focus')
    assert s.store.rows('SELECT status FROM reports')==[{'status':'draft'}]


def test_all_issue_types_overdue_more_than_six_calendar_months_excluded_boundary_kept(tmp_path):
    def bug(key,due):
        i=issue(key,due);i['type']='Bug';return i
    s=svc(tmp_path,[bug('ABS-1','2026-05-02'),bug('ABS-2','2026-05-03'),bug('ABS-3',None),issue('ABS-4','2024-01-01')])
    payload=r.focus(s,AT)
    assert payload['overdue_cutoff']=='2026-05-03' and payload['excluded_overdue_issues']==2
    assert [i['key'] for i in payload['items']]==['ABS-2','ABS-3']
    # Updating an old Bug does not reset its deadline age.
    i=bug('ABS-1','2026-05-02');i['updated']=AT.isoformat();s.store.upsert_issue(i,fingerprint(i))
    assert 'ABS-1' not in [x['key'] for x in r.focus(s,AT)['items']]


@pytest.mark.parametrize('day,expected',[('2026-08-31','2026-02-28'),('2024-08-31','2024-02-29'),('2026-01-31','2025-07-31')])
def test_half_year_cutoff_handles_month_ends_and_leap_year(day,expected):
    assert r.six_months_before(r.parse_date(day)).isoformat()==expected


def test_policy_upgrade_rebuilds_today_but_never_resends_existing_card(tmp_path):
    old=issue('ABS-1','2024-01-01');old['type']='Bug'
    s=svc(tmp_path,[old,issue('ABS-2','2026-11-03')]);t=Transport()
    with s.store.db() as c:
        c.execute("INSERT INTO daily_focus(day,issue_keys,state,message_id,card,created,updated,policy_version) VALUES(?,?,'sent','original',?, ?, ?,1)",
            ('2026-11-03',json.dumps(['ABS-1']),json.dumps({'old':'card'}),AT.isoformat(),AT.isoformat()))
    r.deliver(s,CONF,t,AT)
    p=r.focus(s,AT)
    assert [x['key'] for x in p['items']]==['ABS-2'] and p['card_outdated']
    assert not t.cards and p['delivery_state']=='sent'
    row=s.store.rows('SELECT message_id,card,policy_version FROM daily_focus')[0]
    assert row['message_id']=='original' and json.loads(row['card'])=={'old':'card'} and row['policy_version']==r.POLICY_VERSION
    tomorrow=AT+timedelta(days=1);snapshot(s,['ABS-1','ABS-2'],tomorrow);r.deliver(s,CONF,t,tomorrow)
    assert len(t.cards)==1 and 'ABS-1' not in json.dumps(t.cards[0][1]) and not r.focus(s,tomorrow)['card_outdated']


def test_real_reported_old_tasks_excluded_even_with_highest_priority(tmp_path):
    old=[issue('MAT-9001','2024-04-27',priority='Highest'),issue('COLOGNE-9002','2026-03-06',priority='Highest')]
    for i in old:i['type']='Task'
    s=svc(tmp_path,old+[issue('MOD-10887','2026-06-08')])
    r.ensure_plan(s,AT);payload=r.focus(s,AT)
    assert [x['key'] for x in payload['items']]==['MOD-10887']
    assert payload['excluded_overdue_issues']==2
    body=json.dumps(r.reminder_card(payload,CONF['dashboard_url']))
    assert 'MAT-9001' not in body and 'COLOGNE-9002' not in body
