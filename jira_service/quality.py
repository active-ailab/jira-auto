"""Quality archive handoff and narrowly scoped Jira attachment upload.

Issue reads/comments stay on Jira CLI. Its missing attachment command is filled
with curl using the existing netrc, with durable protection against double POSTs.
"""
import json
from pathlib import Path
from .integrations import issue_key,run_cli
from .materials import sha256
from .store import now


def attach_quality_archive(service,key,path,run_id=None):
    key=issue_key(key);path=Path(path).resolve();path.relative_to(service.settings.data)
    if not service.settings.jira_write:raise ValueError('Jira回写未启用，质量日志已保留本地，尚未上传附件')
    if not path.is_file() or not 0<path.stat().st_size<=512*1024*1024:raise ValueError('质量日志包无效或超过512MiB')
    digest=sha256(path);name=path.name;store=service.store
    with store.db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS quality_uploads(
            issue_key TEXT NOT NULL,sha256 TEXT NOT NULL,filename TEXT NOT NULL,
            state TEXT NOT NULL,attachment_id TEXT,updated TEXT NOT NULL,
            PRIMARY KEY(issue_key,sha256))''')
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM quality_uploads WHERE issue_key=? AND sha256=?',(key,digest)).fetchone()
        if old:
            if old['state']=='uploaded':return {'attachment_id':old['attachment_id'],'sha256':digest,'reused':True}
            raise ValueError('该日志包上次上传结果未知；为避免重复附件，需先通过Jira CLI核对，不自动重发')
        c.execute('INSERT INTO quality_uploads VALUES(?,?,?,?,?,?)',(key,digest,name,'sending',None,now()))
    store.event('quality_upload','将质量平台原始日志包上传为Jira附件：'+name,key,run_id,{'bytes':path.stat().st_size,'sha256':digest})
    # curl form quoting is curl syntax, not shell syntax; no redirects or credential arguments.
    quote=lambda x:str(x).replace('\\','\\\\').replace('"','\\"')
    try:
        response=run_cli(['curl','--netrc','--fail','--silent','--show-error','--proto','=https',
            '--max-time','600','--request','POST','--header','X-Atlassian-Token: no-check',
            '--form','file=@"'+quote(path)+'";filename="'+quote(name)+'"',
            'https://jira.huami.com/rest/api/2/issue/'+key+'/attachments'],timeout=620)
        items=json.loads(response)
        if not isinstance(items,list) or len(items)!=1:raise ValueError('附件上传响应不完整，需核对')
        item=items[0];aid=str(item.get('id',''))
        if not aid.isdigit() or item.get('filename')!=name or item.get('size')!=path.stat().st_size:raise ValueError('附件上传响应与本地文件不一致，需核对')
        with store.db() as c:c.execute("UPDATE quality_uploads SET state='uploaded',attachment_id=?,updated=? WHERE issue_key=? AND sha256=?",(aid,now(),key,digest))
        store.artifact(key,'jira_attachment',path,{'jira_attachment_id':aid,'sha256':digest,'bytes':path.stat().st_size,'source_platform':'quality'})
        store.event('quality_upload','质量日志已上传至Jira，等待CLI重新读取附件清单',key,run_id,{'attachment_id':aid,'sha256':digest})
        return {'attachment_id':aid,'sha256':digest,'reused':False}
    except BaseException:
        with store.db() as c:c.execute("UPDATE quality_uploads SET state='unknown',updated=? WHERE issue_key=? AND sha256=?",(now(),key,digest))
        raise


def require_quality_materials(service,key,run_id):
    issue=service.store.issue(key)
    artifacts=service.store.artifacts(key)
    if issue.get('feedback') and not any(a['kind'] in ('log','binary_log','image','image_unavailable') for a in artifacts):
        service.store.event('quality_wait','质量反馈尚未取得日志：需从反馈页“日志→原始日志→下载Zip”获取，上传Jira后继续分析；本轮不生成无材料结论',key,run_id,{'feedback':issue['feedback'],'state':'awaiting_download'})
        raise ValueError('质量平台日志尚未获取，流程停在材料收集；不能跳过下载直接生成证据不足结论')
    if issue.get('feedback'):
        archived={a['metadata'].get('sha256') for a in artifacts if a['kind']=='jira_attachment'}
        if any(a['kind']=='log_archive' and a['metadata'].get('source_platform')=='quality' and a['metadata'].get('sha256') not in archived for a in artifacts):
            raise ValueError('质量日志已在本地，但尚未完成Jira附件归档；请先完成归档任务，再继续分析')


def import_quality_logs(service,key,path,run_id):
    from .materials import import_logs
    issue=service.refresh(key)
    if not issue.get('feedback'):raise ValueError('工单没有质量反馈链接，不能登记为质量平台日志')
    result=import_logs(service.settings,service.store,key,path,run_id,{'source_platform':'quality','feedback_id':issue['feedback']['id']})
    result['jira_attachment']=attach_quality_archive(service,key,path,run_id)
    issue=service.refresh(key)
    if not any(str(a['id'])==result['jira_attachment']['attachment_id'] for a in issue['attachments']):raise ValueError('Jira CLI尚未读到上传附件；本轮不进入AI分析，请稍后只重试核对')
    result['next_run_id']=service.store.enqueue('analysis',key)
    service.store.event('queue','质量日志已归档到Jira，自动继续结合工单与附件分析',key,run_id,{'next_run_id':result['next_run_id']})
    return result
