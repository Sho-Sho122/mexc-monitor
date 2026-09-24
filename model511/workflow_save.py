"""511-only persistence, run AFTER the independent legacy research save.

Never call this module during a local validation: save commits/pushes in Actions.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
from datetime import datetime, timezone


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def audit(backup, action, status, detail=''):
    with (backup / 'save_audit.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(),
                               action=action, status=status, detail=detail)) + '\n')


def archive(root, backup):
    base = git(root, 'rev-parse', 'HEAD')
    source = root / '511_data'
    if not source.is_dir():
        (backup / 'manifest.json').write_text(json.dumps({'present': False}), encoding='utf-8')
        return
    with tarfile.open(backup / 'state.tar', 'w') as tar:
        for path in sorted(source.rglob('*')):
            if path.is_file() and path.name != '.writer.lock' and not path.name.endswith('.tmp'):
                tar.add(path, arcname=path.relative_to(root), recursive=False)
    (backup / 'manifest.json').write_text(json.dumps({'present': True, 'base': base}), encoding='utf-8')


def save(root, backup):
    manifest = json.loads((backup / 'manifest.json').read_text('utf-8'))
    if not manifest['present']:
        return
    for attempt in range(5):
        git(root, 'fetch', 'origin', 'main')
        changed = git(root, 'diff', '--name-only', manifest['base'], 'origin/main', '--', '511_data')
        if changed:
            raise RuntimeError('511_REMOTE_CONFLICT: recovery archive retained; legacy save already completed')
        git(root, 'reset', '--hard', 'origin/main')
        with tarfile.open(backup / 'state.tar') as tar:
            # Only files from our own archive under 511_data may be restored.
            for member in tar.getmembers():
                target = (root / member.name).resolve()
                if not member.isfile() or not target.is_relative_to((root / '511_data').resolve()):
                    raise ValueError('Unsafe 511 archive member')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('wb') as dest:
                    dest.write(source.read())
        git(root, 'add', '--', '511_data')
        if not git(root, 'diff', '--cached', '--name-only', '--', '511_data'):
            return
        git(root, 'commit', '-m', 'Update 511 shadow state', '--', '511_data')
        try:
            git(root, 'push', 'origin', 'HEAD:main')
            return
        except subprocess.CalledProcessError:
            audit(backup, 'push', 'RETRY', str(attempt + 1))
    raise RuntimeError('511_PUSH_FAILED: recovery archive retained')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('archive', 'save'))
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    args.backup.mkdir(parents=True, exist_ok=True)
    try:
        {'archive': archive, 'save': save}[args.action](Path.cwd(), args.backup)
    except Exception as exc:
        audit(args.backup, args.action, 'FAILED', str(exc))
        raise
    audit(args.backup, args.action, 'SUCCESS')


if __name__ == '__main__':
    main()
