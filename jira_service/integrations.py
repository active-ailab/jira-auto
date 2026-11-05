import hashlib
import json
import re
import subprocess
import os
import selectors
import time
import codecs
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
DEVICE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
VERSION = re.compile(r"^\d+(?:\.\d+){2,4}(?:[-_][A-Za-z0-9._-]+)?$")


def issue_key(key):
    if not KEY.fullmatch(key):
        raise ValueError("工单号格式无效")
    return key


def issue_reference(value):
    value=value.strip()
    if '://' in value:
        parsed=urlparse(value)
        if parsed.scheme!='https' or parsed.hostname!='jira.huami.com' or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('请使用 jira.huami.com 的工单链接')
        match=re.fullmatch(r'/browse/([A-Za-z][A-Za-z0-9_]*-\d+)/?',parsed.path)
        if not match: raise ValueError('链接应为 https://jira.huami.com/browse/工单号')
        value=match[1]
    return issue_key(value.upper())


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def cli_comment_equal(remote,expected):
    """Accept only CLI-added Jira Wiki escapes; preserve every original character."""
    remote=remote.replace('\r\n','\n');expected=expected.replace('\r\n','\n')
    i=j=0;punctuation=set('[]{}()*+_-!|^~#\\')
    while i<len(expected) and j<len(remote):
        if remote[j]==expected[i]:i+=1;j+=1
        elif remote[j]=='\\' and j+1<len(remote) and remote[j+1]==expected[i] and expected[i] in punctuation:
            j+=1  # Added escaping only; do not strip or rewrite approved text.
        else:return False
    return i==len(expected) and j==len(remote)


def redact_tool_output(line):
    line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).strip()
    if re.search(r'authorization|cookie|token|secret|password', line, re.I):
        return '[凭据相关输出已隐藏]'
    return re.sub(r'https?://[^\s]+', '[链接已隐藏]', line)[:1200]


def run_cli(args, timeout=90, input_text=None, on_output=None):
    # No shell; credentials stay in the user's existing CLI configuration.
    if on_output is not None:
        if input_text is not None:
            raise ValueError('流式工具执行不接受凭据或标准输入')
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        started = last_heartbeat = time.monotonic()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        pending, output = '', []
        selector = selectors.DefaultSelector()
        selector.register(p.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                for event, _ in selector.select(timeout=.5):
                    chunk = os.read(event.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(event.fileobj)
                        continue
                    pending += decoder.decode(chunk)
                    parts = re.split(r'[\r\n]', pending)
                    pending = parts.pop()
                    for line in parts:
                        if line.strip():
                            safe = redact_tool_output(line)
                            on_output(safe)
                            if len(output) < 10000:
                                output.append(safe)
                    if len(pending) > 8192:
                        on_output(redact_tool_output(pending[:1200]))
                        pending = ''
                if time.monotonic() - last_heartbeat > 10:
                    on_output(f'[等待] 工具已运行 {int(elapsed)} 秒，等待下一条输出')
                    last_heartbeat = time.monotonic()
            pending += decoder.decode(b'', final=True)
            if pending.strip():
                on_output(redact_tool_output(pending))
            p.wait(timeout=max(1, timeout-(time.monotonic()-started)))
            if p.returncode:
                raise RuntimeError(f'{Path(args[0]).name} 执行失败（退出码 {p.returncode}），详见任务过程')
            return '\n'.join(output)
        finally:
            selector.close()
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
    p = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        # Do not persist CLI stderr: signed URLs or auth data may be included.
        code = re.search(r"(?:response|status)[^\n]{0,25}?([45]\d\d)", p.stderr, re.I)
        raise RuntimeError(f"{Path(args[0]).name} 执行失败" + (f"（HTTP {code[1]}）" if code else "，请在本地终端检查认证或参数"))
    return p.stdout


def render_text(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("text", "") + "\n".join(render_text(x) for x in v.get("content", []))
    return ""


def prior_comment_notes(page):
    comments=page.get('comments',[]) if isinstance(page,dict) else []
    notes=[]
    for comment in comments[-4:]:
        body=render_text(comment.get('body'))
        lines=[line for line in body.splitlines() if not re.search(r'联系方式|联系电话|手机号|电子邮箱|e-?mail\s*[:：]',line,re.I)]
        excerpt=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}','[邮箱已省略]','\n'.join(lines))
        if not excerpt.strip():continue
        if len(excerpt)>1500:excerpt=excerpt[:1000]+'\n[中间内容未纳入证据包]\n'+excerpt[-450:]
        notes.append({'comment_id':str(comment.get('id','')),'created':comment.get('created'),'text':excerpt,
                      'provenance':'本工单已返回评论页中的历史摘录；不是本轮重新解析日志或dump的结论'})
    return notes,{'returned':len(comments),'total':page.get('total') if isinstance(page,dict) else None,
                  'complete_page':isinstance(page,dict) and page.get('startAt',0)==0 and page.get('total')==len(comments),
                  'excerpt_limit':4,'note':'返回页不完整时，不保证包含最近评论；摘录不等于全部历史'}


def normalize(raw):
    from .reminders import schedule_fields
    f = raw.get("fields", {})
    detail = render_text(f.get("customfield_10513")) or render_text(f.get("description"))
    summary = f.get("summary", "")
    feedback = None
    for u in re.findall(r"https://devops\.zepp\.com/zeus/[^\s<>\]\"']+", detail):
        u = u.replace("\\&", "&").rstrip(").,，。")
        parsed = urlparse(u)
        query = parse_qs(parsed.fragment.partition("?")[2])
        fid = query.get("id", [""])[0]
        app = query.get("app_name", [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fid) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", app):
            feedback = {"id": fid, "app_name": app,
                        "url": f"https://devops.zepp.com/zeus/#/feedback?app_name={app}&id={fid}"}
            break

    def field(label):
        m = re.search(rf"(?m)^{label}\s*[:：]\s*([^\r\n]+)", detail)
        return m[1].strip() if m else None

    kind = f.get("issuetype") or {}
    attachments = [{k: a.get(k) for k in ["id", "filename", "size", "mimeType", "created"]}
                   for a in f.get("attachment", [])]
    summary_version=re.search(r"[【\[]([A-Za-z][A-Za-z0-9_]*?)-(\d+(?:\.\d+){2,4})[】\]]", summary)
    status = f.get("status") or {}
    assignee = f.get("assignee") or {}
    prior_notes,comment_window=prior_comment_notes(f.get('comment') or {})
    return {**schedule_fields(f),"key": raw["key"], "summary": summary, "description": detail,
            "type": kind.get("name") or "未读取", "status": status.get("name", "未知"),
            "status_category": (status.get("statusCategory") or {}).get("key"),
            "assignee": assignee.get("name") or assignee.get("accountId"),
            "updated": f.get("updated"), "feedback": feedback,
            "device": field("绑定设备") or (summary_version[1] if summary_version else None), "firmware": field("固件版本") or (summary_version[2] if summary_version else None),
            "feedback_time": field("反馈时间"), "symptom": field("反馈详情"),
            "attachments": attachments,
            "prior_comments":prior_notes,"prior_comment_window":comment_window,
            "jira_url": "https://jira.huami.com/browse/" + issue_key(raw["key"])}


def fingerprint(issue):
    return digest(json.dumps(issue, sort_keys=True, ensure_ascii=False))


class Jira:
    def __init__(self, settings):
        self.settings = settings

    def get(self, key):
        raw = json.loads(run_cli([self.settings.jira_bin, "issue", "view", issue_key(key), "--raw"]))
        return normalize(raw)

    def assigned_updates(self):
        start = 0
        updates = {}
        while True:
            data = json.loads(run_cli([self.settings.jira_bin, "issue", "list", "--jql",
                "project IS NOT EMPTY AND assignee = currentUser() AND resolution IS EMPTY", "--order-by", "updated",
                "--raw", "--paginate", f"{start}:100"]))
            items = data if isinstance(data, list) else data.get("issues", [])
            batch = [issue_key(x["key"]) for x in items]
            if set(batch) <= set(updates) and batch:
                raise RuntimeError("Jira 分页未前进，已停止同步以避免遗漏")
            updates.update({x["key"]: x.get("fields", {}).get("updated") for x in items})
            if len(items) < 100:
                return updates
            start += 100
            if start >= 10000:
                raise RuntimeError("Jira 工单超过单轮上限，请收窄监测范围")

    def comment(self, key, body):
        if not self.settings.jira_write:
            raise ValueError("Jira 回写未启用：需设置 JA_ALLOW_JIRA_WRITE=1")
        run_cli([self.settings.jira_bin, "issue", "comment", "add", issue_key(key),
                 "--template", "-", "--no-input"], input_text=body)

    def publication_evidence(self,key,report_id,body):
        raw=json.loads(run_cli([self.settings.jira_bin,'issue','view',issue_key(key),'--raw','--comments','100']))
        page=raw.get('fields',{}).get('comment',{})
        comments=page.get('comments',[])[:200]
        marker='[jira-auto:'+report_id+']'
        expected=(body+'\n\n'+marker).replace('\r\n','\n')
        matches=[c for c in comments if isinstance(c.get('body'),str) and any(cli_comment_equal(line,marker) for line in c['body'].replace('\r\n','\n').split('\n'))]
        exact=[c for c in matches if cli_comment_equal(c['body'],expected)]
        state='duplicate_markers' if len(matches)>1 else 'exact_match' if len(exact)==1 else 'body_changed' if matches else 'not_seen'
        return {'state':state,'comment_id':exact[0].get('id') if state=='exact_match' else None,
                'created':exact[0].get('created') if state=='exact_match' else None,
                'comparison':'original_characters_preserved_with_cli_wiki_escaping','matched_markers':len(matches),'scanned_comments':len(comments),
                'complete_page':page.get('startAt',0)==0 and isinstance(page.get('total'),int) and len(comments)>=page['total']}
