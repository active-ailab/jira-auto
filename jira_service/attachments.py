"""Download Jira file attachments using existing netrc; Jira metadata stays on CLI."""
import re
from pathlib import Path
from urllib.parse import quote
from .integrations import run_cli
from .materials import sha256, import_logs
from .material_probe import VERSION as PROBE_VERSION,inspect_bytes


def prepare_attachments(settings, store, key, run_id):
    issue = store.issue(key)
    attachments = issue.get('attachments', [])
    existing = store.artifacts(key)
    for item in attachments:
        name = item.get('filename') or ''
        if not name or Path(name.replace('\\','/')).name in ('','.','..'):
            raise ValueError('Jira 附件文件名无效')
        aid = str(item.get('id', ''))
        if not aid.isdigit():
            raise ValueError('Jira 附件 ID 无效')
        known = next((a for a in existing if any(o.get('jira_attachment_id')==aid for o in (a['metadata'].get('active_origins') or [a['metadata']])) and
                      (a['kind']=='jira_attachment' if name.lower().endswith('.zip') else a['kind'] in ('log','binary_log','image','image_unavailable') and not a['metadata'].get('archive_sha256'))), None)
        if known:
            rows = store.rows('SELECT path FROM artifacts WHERE id=?', (known['id'],))
            path = Path(rows[0]['path'])
            if path.is_file() and sha256(path) == known['metadata'].get('sha256'):
                if name.lower().endswith('.zip'):
                    derived=[a for a in existing if a['metadata'].get('archive_sha256')==known['metadata'].get('sha256')]
                    def valid(a):
                        derived_path=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(a['id'],))[0]['path'])
                        return derived_path.is_file() and sha256(derived_path)==a['metadata'].get('sha256')
                    unclassified=any(a['metadata'].get('probe_version')!=PROBE_VERSION for a in derived)
                    if not derived or unclassified or not all(valid(a) for a in derived):
                        import_logs(settings,store,key,str(path),run_id,{'jira_attachment_id':aid})
                if not name.lower().endswith('.zip') and known['kind'] in ('log','binary_log') and known['metadata'].get('probe_version')!=PROBE_VERSION:
                    meta={**known['metadata'],**inspect_bytes(path.read_bytes(),name)}
                    store.artifact(key,'log' if meta['readable_text'] else 'binary_log',path,meta)
                store.event('attachments', '复用已校验附件：' + name, key, run_id)
                continue
        size = item.get('size')
        if not isinstance(size, int) or not 0 < size <= 512*1024*1024:
            raise ValueError('附件大小无效或超过512MiB：' + name)
        directory = settings.data/'cases'/key/'attachments'/aid
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / Path(name.replace('\\', '/')).name
        temporary = path.with_suffix(path.suffix + '.part')
        store.event('attachments', '下载附件：' + name, key, run_id, {'total':size,'unit':'bytes'})
        # Fixed origin, no redirects or shell; no credentials in arguments or event output.
        url = 'https://jira.huami.com/secure/attachment/' + aid + '/' + quote(name, safe='')
        try:
            run_cli(['curl','--netrc','--fail','--silent','--show-error','--proto','=https',
                     '--max-time','600','--max-filesize',str(size),'--output',str(temporary),url], timeout=620)
            if temporary.stat().st_size != size:
                raise ValueError('附件长度与 Jira 清单不一致：' + name)
            with temporary.open('rb') as downloaded:
                prefix = downloaded.read(256)
            if prefix.lstrip().lower().startswith((b'<!doctype html',b'<html')):
                raise ValueError('下载返回登录页面，未取得附件：' + name)
            temporary.replace(path)
            image_meta={}
            if re.search(r'\.(png|jpe?g|webp)$',name,re.I):
                from .images import inspect_image
                try:image_meta=inspect_image(path);kind='image'
                except ValueError as exc:image_meta={'image_error':str(exc)};kind='image_unavailable'
            elif name.lower().endswith('.zip'):
                import_logs(settings, store, key, str(path), run_id, {'jira_attachment_id':aid})
                kind = 'jira_attachment'
            else:
                image_meta=inspect_bytes(path.read_bytes(),name)
                kind='log' if image_meta['readable_text'] else 'binary_log'
            store.artifact(key, kind, path, {'jira_attachment_id':aid,'bytes':size,'sha256':sha256(path),'original_path':name,**image_meta})
            store.event('attachments','附件校验及登记完成：'+name,key,run_id)
        finally:
            temporary.unlink(missing_ok=True)
    return {'attachments':len(attachments)}
