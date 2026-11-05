#!/usr/bin/env python3
"""Validate a quality ZIP; explicitly submit it to the existing daemon workflow.

Default is inspection only. --submit queues Jira attachment upload followed by
analysis, never approval, comment publication, or issue transition.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile


def sha256(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def handoff(service_dir,data,key,archive,submit=False):
    service_dir=Path(service_dir).resolve();data=Path(data).resolve();archive=Path(archive).resolve()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+',key):raise ValueError('需要有效Jira工单号')
    if not archive.is_file() or not 0<archive.stat().st_size<=128*1024*1024:raise ValueError('原始ZIP必须是1字节至128MiB的本地文件')
    if not zipfile.is_zipfile(archive):raise ValueError('文件不是有效ZIP，可能下载未完成或取得登录页')
    database=data/'state.sqlite3'
    if not database.is_file():raise ValueError('没有找到现有服务数据库，请检查--service-dir或--data-dir')
    digest=sha256(archive)
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT body FROM issues WHERE key=?',(key,)).fetchone()
        if not row:raise ValueError('工单尚未导入服务，请先使用添加工单入口')
        issue=json.loads(row[0])
        if not issue.get('feedback'):raise ValueError('工单没有质量反馈链接，不能登记为质量平台日志')
        active=db.execute("SELECT id,kind,status FROM runs WHERE issue_key=? AND kind IN ('analysis','preflight','import_logs','import_quality_logs') AND status IN ('queued','running') ORDER BY created",(key,)).fetchall()
        has_receipts=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_uploads'").fetchone()
        receipt=db.execute('SELECT state,attachment_id FROM quality_uploads WHERE issue_key=? AND sha256=?',(key,digest)).fetchone() if has_receipts else None
    result={'issue':key,'sha256':digest,'bytes':archive.stat().st_size,'archive':archive.name,
            'feedback_url':issue['feedback'].get('url'),'active_runs':[dict(zip(('id','kind','status'),r)) for r in active],
            'upload_receipt':dict(zip(('state','attachment_id'),receipt)) if receipt else None,'submitted':False}
    if not submit:return result
    if active:raise ValueError('已有材料或分析任务执行中，请跟进现有任务，不重复排队')
    if receipt:raise ValueError('该包已有上传收据，请核对已有归档/分析状态；不自动重复提交')
    if not (service_dir/'jira_service/service.py').is_file():raise ValueError('服务源码目录无效')
    target=data/'cases'/key/'quality'/digest/archive.name
    target.parent.mkdir(parents=True,exist_ok=True);target.resolve().relative_to(data)
    if target.exists():
        if sha256(target)!=digest:raise ValueError('同一内容地址已有不同文件，请核对；未覆盖原件')
    else:
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
                temporary=Path(f.name)
                with archive.open('rb') as source:shutil.copyfileobj(source,f,1024*1024)
            if sha256(temporary)!=digest:raise ValueError('复制时源文件发生变化，未排队')
            try:os.link(temporary,target)
            except FileExistsError:
                if sha256(target)!=digest:raise ValueError('目标文件发生变化，未排队')
        finally:
            if temporary:temporary.unlink(missing_ok=True)
    # Load only after explicit submission; daemon owns auth and external actions.
    sys.path.insert(0,str(service_dir))
    from jira_service.service import Service
    from jira_service.settings import Settings
    service=Service(Settings(data=data,source=service_dir.parent))
    rid=service.store.enqueue('import_quality_logs',key,{'path':str(target)})
    result.update(submitted=True,run_id=rid)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue',required=True)
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--service-dir',type=Path,default=Path(os.getenv('JA_SERVICE_DIR', str(Path.cwd()))), help='服务源码目录；默认 JA_SERVICE_DIR 或当前工作目录')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--submit',action='store_true',help='排队原包上传Jira并分析；不审批或发布评论')
    args=parser.parse_args()
    try:result=handoff(args.service_dir,args.data_dir or args.service_dir/'data',args.issue,args.archive,args.submit)
    except (ValueError,OSError,sqlite3.Error) as exc:parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
