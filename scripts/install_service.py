"""Install a private, loopback-only systemd user service. No credential copying."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if '"' in str(root) or '\n' in str(root):
    raise SystemExit('Unsupported installation path')
unit = f'''[Unit]
Description=Jira Auto local analysis workbench
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={root}/.venv/bin/jira-auto
Environment=PATH={root}/.venv/bin:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=JA_DATA_DIR={root}/data
Environment=JA_SOURCE_DIR={root.parent}
Environment=JA_PORT=8791
Environment=JA_POLL_SECONDS=300
Environment=JA_ALLOW_JIRA_WRITE=0
EnvironmentFile=-{Path.home()}/.config/jira-auto/service.env
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=default.target
'''
target = Path.home()/'.config/systemd/user/jira-auto.service'
if target.exists() and target.read_text()!=unit:
    raise SystemExit('Existing unit differs; inspect before replacing')
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(unit)
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now','jira-auto.service'],check=True)
print('Started: http://127.0.0.1:8791')
