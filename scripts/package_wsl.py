"""Build clean source/tool archives from an allowlist, never from the runtime data tree."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def download(url):
    with urllib.request.urlopen(url,timeout=90) as r:return r.read()
def tree(src,dst):
    for p in sorted(src.rglob('*')):
        if p.is_file() and not p.is_symlink() and not any(x in p.parts for x in ('__pycache__','node_modules','.git','.venv')) and p.suffix not in ('.pyc','.vsix') and not p.name.endswith('.generated.json'):
            q=dst/p.relative_to(src);q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
def checksum(root):
    (root/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(root).as_posix()}\n' for p in sorted(root.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
def archive(folder,out):
    with tarfile.open(out,'w:gz',compresslevel=6) as t:t.add(folder,arcname=folder.name)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--elf-source',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    src=a.output/'jira-auto-service';src.mkdir()
    for name in ('jira_service','plugin','scripts','deploy','docs','tests'):tree(ROOT/name,src/name)
    for name in ('README.md','AGENTS.md','pyproject.toml','uv.lock','.env.example','.gitignore'):shutil.copy2(ROOT/name,src/name)
    kit=a.output/'jira-auto-toolkit';(kit/'bin').mkdir(parents=True);(kit/'licenses').mkdir()
    # These are clean executable code, not their configuration directories.
    for name in ('jira','uv','repo'):shutil.copy2(Path.home()/'.local/bin'/name,kit/'bin'/name)
    tree(a.elf_source,kit/'elf-fetch')
    # tree only traverses known source directories here; do not copy arbitrary root files.
    ef=kit/'elf-fetch'
    for pth in list(ef.iterdir()):
        if pth.name not in ('elf_fetch','vscode-auth-helper','pyproject.toml','README.md','README.zh-CN.md','SKILL.md'):
            if pth.is_dir():shutil.rmtree(pth)
            else:pth.unlink()
    urls={
        'jira-LICENSE':'https://raw.githubusercontent.com/ankitpokhrel/jira-cli/v1.7.0/LICENSE',
        'uv-LICENSE-MIT':'https://raw.githubusercontent.com/astral-sh/uv/0.11.17/LICENSE-MIT',
        'uv-LICENSE-APACHE':'https://raw.githubusercontent.com/astral-sh/uv/0.11.17/LICENSE-APACHE',
        'repo-LICENSE-APACHE':'https://www.apache.org/licenses/LICENSE-2.0.txt',
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name,content in zip(urls,pool.map(download,urls.values())):(kit/'licenses'/name).write_bytes(content)
    provenance={'format':1,'architecture':'Linux x86_64; WSL2 Ubuntu 24.04','tools':[
        {'name':'jira','version':'1.7.0','source':'https://github.com/ankitpokhrel/jira-cli/releases/tag/v1.7.0','sha256':sha(kit/'bin/jira')},
        {'name':'uv','version':'0.11.17','source':'https://github.com/astral-sh/uv/releases/tag/0.11.17','sha256':sha(kit/'bin/uv')},
        {'name':'repo','source':'https://gerrit.googlesource.com/git-repo/','sha256':sha(kit/'bin/repo')},
        {'name':'elf-fetch','version':'0.1.0','revision':'e74c975ef8b5fd477b33bcf3ecf38a53c01d01e5','source':'https://github.com/active-ailab/elf_fetch','license':'MIT (declared in upstream pyproject.toml)'},
    ],'excluded':['credentials','database','real attachments','firmware source','Codex executable','Python wheels','apt packages'],'network_required':True}
    (kit/'PROVENANCE.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+'\n')
    (kit/'README.txt').write_text('Linux x86_64 tools for WSL2 Ubuntu 24.04. Not a fully offline installer.\nRun jira-auto-service/deploy/install-wsl.sh with this toolkit path.\nCodex must be installed from its official source and logged in on the target.\nNo credentials, real logs, firmware checkout or runtime database are included.\nSee PROVENANCE.json, licenses/, and elf-fetch/pyproject.toml for upstream notices.\n')
    for folder in (src,kit):checksum(folder);archive(folder,a.output/(folder.name+'.tar.gz'))
    # Zip contains just the distributable archives and guide, never the temporary trees.
    shutil.copy2(ROOT/'docs/WSL部署指南.md',a.output/'WSL部署指南.md')
    files=[a.output/'jira-auto-service.tar.gz',a.output/'jira-auto-toolkit.tar.gz',a.output/'WSL部署指南.md']
    (a.output/'SHA256SUMS.txt').write_text(''.join(f'{sha(f)}  {f.name}\n' for f in files))
    with zipfile.ZipFile(a.output/'Jira-Auto-WSL2-20261105.zip','w',zipfile.ZIP_DEFLATED) as z:
        for f in files+[a.output/'SHA256SUMS.txt']:z.write(f,f.name)
    print(json.dumps({'files':[{'name':f.name,'bytes':f.stat().st_size,'sha256':sha(f)} for f in files+[a.output/'Jira-Auto-WSL2-20261105.zip']]},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
