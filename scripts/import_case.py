"""Import previously downloaded case materials without contacting external services."""
import argparse
import shutil

from jira_service.integrations import issue_key
from jira_service.materials import import_logs, inspect_elf, inspect_manifest, sha256
from jira_service.service import Service
from jira_service.settings import Settings


def main():
    p = argparse.ArgumentParser()
    p.add_argument("key")
    p.add_argument("--archive", type=__import__('pathlib').Path)
    p.add_argument("--build-directory", type=__import__('pathlib').Path)
    p.add_argument("--device")
    p.add_argument("--version")
    args = p.parse_args()
    key = issue_key(args.key)
    settings = Settings.load()
    svc = Service(settings)
    svc.refresh(key)
    if args.archive:
        target = settings.data / "cases" / key / "uploads" / "imported-feedback.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.archive, target)
        print(import_logs(settings, svc.store, key, target, "initial-import"))
    if args.build_directory:
        issue = svc.store.issue(key)
        device, version = args.device or issue['device'], args.version or issue['firmware']
        target = settings.data / "cases" / key / "builds" / device / version
        target.mkdir(parents=True, exist_ok=True)
        for kind, name in [('manifest','manifest.xml'),('elf','watch@mhs003.elf'),('map','watch@mhs003.map')]:
            source = args.build_directory / name
            dest = target / name
            shutil.copy2(source, dest)
            meta = {'device':device,'version':version,'bytes':dest.stat().st_size}
            meta.update(inspect_manifest(dest) if kind=='manifest' else inspect_elf(dest) if kind=='elf' else {'sha256':sha256(dest)})
            svc.store.artifact(key, kind, dest, meta)
            print(f'Imported {kind}: {dest.name}')
    print('Preflight queued:',svc.store.enqueue('preflight',key))
    print('Source verification queued:',svc.store.enqueue('source_check',key))


if __name__ == '__main__':
    main()
