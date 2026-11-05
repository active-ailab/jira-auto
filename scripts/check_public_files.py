"""Reject common private files and credential formats in Git's index.

This is a guardrail, not a guarantee of secret detection. Review every staged
file and never use the real runtime directory as a source fixture.
"""
from pathlib import Path
import re
import subprocess


def main():
    names = subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    errors=[]
    private_dirs={'data','.venv','outputs','__pycache__','node_modules','.ssh','.codex','.elf-fetch','.jira','backups','private'}
    suffixes=('.sqlite','.sqlite3','.db','.pem','.key','.p12','.pfx','.log','.dump','.elf','.bin','.zip','.tar.gz','.vsix','.png','.jpg','.jpeg','.webp')
    patterns=[re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
              re.compile(rb'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,})\b'),
              re.compile(rb'\b(?:cli_[a-z0-9]{12,}|ou_[a-zA-Z0-9]{20,})\b')]
    for name in filter(None,names):
        p=Path(name)
        if private_dirs.intersection(p.parts) or name.endswith(suffixes) or p.name in {'.netrc','service.env','feishu.json','quality-auth.json','auth.json','auth-token'} or (p.name.startswith('.env') and p.name!='.env.example') or name.endswith('.generated.json'):
            errors.append(name+': private file type');continue
        content=subprocess.check_output(['git','show',':'+name])
        if any(pattern.search(content) for pattern in patterns):errors.append(name+': credential-like content (value suppressed)')
    if errors:
        print('\n'.join(errors));raise SystemExit(1)
    print(f'Public-file guard passed: {sum(bool(n) for n in names)} tracked files; no matched private paths or credential formats.')


if __name__=='__main__':main()
