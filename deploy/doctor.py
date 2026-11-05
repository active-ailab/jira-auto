"""Read-only deployment checks. Never publish, upload or display credentials."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--online', action='store_true', help='Also check Jira login and local service')
    args = parser.parse_args()
    failed = 0
    def report(ok, name, detail=''):
        nonlocal failed
        failed += not ok
        print(('OK   ' if ok else 'FAIL ') + name + (' — '+detail if detail else ''))
    def run(argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            return None
    report(sys.version_info >= (3,12), 'Python 3.12+')
    root = Path(__file__).resolve().parents[1]
    config = Path.home()/'.config/jira-auto/service.env'
    values = {}
    if config.exists():
        for line in config.read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=',1)
                values[key] = value.strip().strip('"')
        report(config.stat().st_mode & 0o077 == 0, 'service.env permissions 0600')
    else:
        report(False, 'service.env', 'Run deploy/configure.py first')
    for tool in ['jira','uv','elf-fetch','git','curl','readelf','arm-none-eabi-addr2line','riscv64-unknown-elf-addr2line']:
        report(bool(shutil.which(tool)), tool)
    codex = values.get('JA_CODEX_BIN','codex')
    help_result = run([codex,'exec','--ignore-user-config','--help'])
    flags = ['--ephemeral','--output-schema','--sandbox','--json','--disable','--image']
    report(help_result is not None and help_result.returncode == 0 and all(f in help_result.stdout for f in flags),
           'Codex exec interface', 'Requires --ignore-user-config and structured output support')
    features = run([codex,'features','list'])
    needed = ['shell_tool','unified_exec','apps','plugins','multi_agent','browser_use','computer_use','in_app_browser','image_generation','hooks','view_image']
    known = {line.split()[0] for line in features.stdout.splitlines() if line.split()} if features and features.returncode == 0 else set()
    report(set(needed).issubset(known), 'Codex feature isolation', 'Do not remove isolation flags to bypass incompatibility')
    login = run([codex,'login','status'])
    report(login is not None and login.returncode == 0, 'Codex login status')
    source = Path(values.get('JA_SOURCE_DIR', str(root.parent)))
    report((source/'.repo').is_dir(), 'Firmware repo checkout', 'Needed for manifest-scoped code analysis')
    report(os.path.isdir('/run/systemd/system'), 'systemd')
    if args.online:
        jira_env = os.environ.copy()
        jira_env['JIRA_AUTH_TYPE'] = 'basic'
        try:
            r = subprocess.run(['jira','me'],env=jira_env,capture_output=True,timeout=25)
            report(r.returncode == 0, 'Jira authentication', 'Response suppressed')
        except (OSError, subprocess.TimeoutExpired):
            report(False, 'Jira authentication', 'Command unavailable or timeout')
        try:
            with urllib.request.urlopen('http://127.0.0.1:8791/api/status',timeout=5) as r:
                state=json.load(r)
            report(state.get('lan_mode') is True, 'Dashboard LAN mode')
            print('Service integrations:', ', '.join(i['name']+':'+i['state'] for i in state.get('integrations',[])))
        except Exception:
            report(False, 'Dashboard', 'Service unavailable')
    print(f'\n{failed} check(s) require attention. Source/SSO/bot checks may remain pending until configured.')
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
