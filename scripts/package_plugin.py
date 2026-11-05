import argparse
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--output', required=True, type=Path)
args = p.parse_args()
for name in ['plugin.json','mcp.json']:
    json.loads((root/'plugin'/name).read_text())
if (root/'plugin/bridge.py').read_bytes() != (root/'jira_service/mcp_bridge.py').read_bytes():
    raise SystemExit('Packaged MCP entry point differs from canonical bridge')
args.output.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(args.output,'w',zipfile.ZIP_DEFLATED) as z:
    for name in ['plugin.json','mcp.json','bridge.py']:
        z.write(root/'plugin'/name,name)
    # Bundle only authored skill resources, never caches or local case data.
    skill_root=root/'plugin/skills'
    if skill_root.is_dir():
        for path in sorted(skill_root.rglob('*')):
            if path.is_file() and path.suffix in {'.md','.yaml','.py'} and '__pycache__' not in path.parts:
                z.write(path,path.relative_to(root/'plugin'))
    z.writestr('README.md', '# Jira Auto plugin 0.1.0\n\nDevelopment package for a local desktop MCP client. Requires uv, access to PyPI on first run, and the Jira Auto daemon running at http://127.0.0.1:8791. No secrets or user data are bundled. No approval or publication tools. This package has not been publicly submitted or installed in a client.\n')
print('Created:',args.output)
print('SHA256:',hashlib.sha256(args.output.read_bytes()).hexdigest())
