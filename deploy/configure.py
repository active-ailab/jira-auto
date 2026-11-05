"""Generate private instance configuration; never overwrite an existing config."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source-dir', required=True, type=Path)
    p.add_argument('--dashboard-url', required=True, help='Windows LAN address, e.g. http://192.168.1.20:8791')
    p.add_argument('--ai-proxy', default='')
    p.add_argument('--codex-bin', default='codex')
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    url = urlparse(args.dashboard_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        p.error('Use a plain dashboard origin')
    try:
        addr = ipaddress.ip_address(url.hostname)
        if addr.is_unspecified or addr.version != 4:
            p.error('Use a concrete Windows LAN IPv4 address or DNS hostname')
    except ValueError:
        if not all(c.isalnum() or c in '.-' for c in url.hostname):
            p.error('Invalid hostname')
    if url.port != 8791:
        p.error('This deployment kit uses port 8791')
    source = args.source_dir.expanduser().resolve()
    if not source.is_dir():
        p.error('Source directory must exist (an empty placeholder is allowed until firmware checkout)')
    for value in (str(root), str(source), args.dashboard_url, args.ai_proxy, args.codex_bin):
        if any(c in value for c in '\r\n"\\'):
            p.error('Unsupported configuration characters')
    target = Path.home()/'.config/jira-auto/service.env'
    target.parent.mkdir(parents=True, exist_ok=True)
    values = {
        'JA_DATA_DIR': str(root/'data'), 'JA_SOURCE_DIR': str(source),
        'JA_HOST': '0.0.0.0', 'JA_PORT': '8791', 'JA_POLL_SECONDS': '300',
        'JA_ALLOWED_HOSTS': 'localhost,127.0.0.1,'+url.hostname,
        'JA_LAN_AUTH': '0', 'JA_ALLOW_JIRA_WRITE': '0',
        'JA_AI_ENABLED': '1', 'JA_CODEX_BIN': args.codex_bin,
        'JA_AI_TIMEOUT': '600', 'JA_AI_PROXY': args.ai_proxy,
        'JA_DASHBOARD_URL': args.dashboard_url.rstrip('/'),
        'JIRA_AUTH_TYPE': 'basic', 'TZ': 'Asia/Shanghai',
    }
    # systemd EnvironmentFile syntax; values reject escaping characters above.
    content = ''.join(f'{key}="{value}"\n' for key, value in values.items())
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    mcp = {'mcpServers': {'jira-auto': {'command': 'wsl.exe', 'args': [
        '-d', 'Ubuntu-24.04', '--', str(root/'.venv/bin/jira-auto-mcp')]}}}
    (root/'deploy/mcp-windows.generated.json').write_text(json.dumps(mcp, ensure_ascii=False, indent=2)+'\n')
    print('Saved', target, '(0600). Jira writes remain disabled.')
    print('MCP example: deploy/mcp-windows.generated.json; adjust the WSL distro name if different.')


if __name__ == '__main__':
    main()
