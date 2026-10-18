"""Adaptive-only persistence, adapted from the reviewed research saver.

Kept isolated so existing Research/511 persistence is unchanged.

Only the Actions workflow may run this against origin. Tests use temporary local
bare repositories. Recovery output must be outside the checkout.
"""
import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import time

VERSION = 1
GROUPS = ('research', '511', 'adaptive')
PREFIX = '511_adaptive/forward/'
RESEARCH = (
    'strategy_high_edge_state.json', 'strategy_high_edge.csv',
    'strategy_trend_volume_state.json', 'strategy_trend_volume.csv',
    'strategy_pullback_state.json', 'strategy_pullback.csv',
    'strategy_oi_funding_state.json', 'strategy_oi_funding.csv',
    'mexc_oi_state_research.json', 'research_exit_state.json',
    'research_exit_trades.csv', 'research_exit_results.csv',
    'research_exit_trades_*.csv', 'research_exit_results_*.csv',
)


class PersistenceError(RuntimeError):
    pass


class GitFailure(PersistenceError):
    def __init__(self, operation, code, diagnostic=''):
        self.operation = operation
        self.code = code
        text = diagnostic.lower()
        self.category = next((label for needle, label in (
            ('could not resolve', 'DNS'), ('500', 'HTTP_500'),
            ('403', 'AUTHORIZATION'), ('401', 'AUTHORIZATION'),
            ('non-fast-forward', 'NON_FAST_FORWARD'),
            ('fetch first', 'NON_FAST_FORWARD'), ('timed out', 'TIMEOUT'),
        ) if needle in text), 'GIT_ERROR')
        # Do not retain raw stderr, which can contain credential-bearing URLs.
        super().__init__(f'{operation}: {self.category} (exit {code})')


def git(root, *args, data=None, env=None):
    command = ['git', '-c', 'core.autocrlf=false', '-C', str(root), *args]
    try:
        result = subprocess.run(command, input=data, capture_output=True,
                                env=env, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise GitFailure(args[0], 'TIMEOUT', 'timed out') from exc
    if result.returncode:
        raise GitFailure(args[0], result.returncode,
                         result.stderr.decode('utf-8', errors='replace'))
    return result.stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def write_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True, indent=2).encode())


def audit(backup, action, status, **detail):
    backup.mkdir(parents=True, exist_ok=True)
    record = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                  action=action, status=status, **detail)
    with (backup / 'save_audit.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def owned(path, group):
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts or '\\' in path or ':' in path:
        return False
    if group == 'adaptive':
        return (path.startswith(PREFIX) and len(p.parts) == 3
                and not p.name.startswith('.') and not p.name.endswith('.tmp'))
    if group == '511':
        return (len(p.parts) > 1 and p.parts[0] == '511_data'
                and p.name != '.writer.lock' and not p.name.endswith('.tmp'))
    return len(p.parts) == 1 and any(fnmatch.fnmatchcase(path, x) for x in RESEARCH)


def external(root, backup):
    if backup.resolve().is_relative_to(root.resolve()):
        raise PersistenceError('RECOVERY_MUST_BE_OUTSIDE_CHECKOUT')


def tree(root, ref, group):
    answer = {}
    for entry in git(root, 'ls-tree', '-r', '-z', ref).split(b'\0'):
        if not entry:
            continue
        header, raw_path = entry.split(b'\t', 1)
        path = raw_path.decode('utf-8')
        if owned(path, group):
            mode, kind, oid = header.decode().split()
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise PersistenceError('UNSAFE_DATA_TREE')
            answer[path] = dict(mode=mode, blob=oid)
    return answer


def local_files(root, group):
    if group == 'adaptive':
        directory = root / PREFIX
        if (root / '511_adaptive').is_symlink() or directory.is_symlink():
            raise PersistenceError('SYMLINK_IN_DATA')
        answer = {}
        for path in directory.rglob('*'):
            if path.is_symlink() or path.is_dir():
                raise PersistenceError('UNEXPECTED_ADAPTIVE_PATH')
            name = path.relative_to(root).as_posix()
            if owned(name, group):
                answer[name] = path
        return answer
    if group == '511' and (root / '511_data').is_symlink():
        raise PersistenceError('SYMLINK_IN_DATA')
    paths = (root / '511_data').rglob('*') if group == '511' else root.iterdir()
    answer = {}
    for p in paths:
        name = p.relative_to(root).as_posix()
        if p.is_symlink() and (group == '511' or owned(name, group)):
            raise PersistenceError('SYMLINK_IN_DATA')
        if p.is_file() and owned(name, group):
            answer[name] = p
    return answer


def latest(root):
    git(root, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
    return git(root, 'rev-parse', 'FETCH_HEAD').decode().strip()


def code_changed(root, before, after):
    changed = git(root, 'diff', '--name-only', before, after).decode().splitlines()
    return any(p.endswith('.py') or p.startswith('.github/workflows/')
               or Path(p).name.startswith('requirements') or p.startswith('docs/511_ADAPTIVE') for p in changed)


def prepare(root, backup, attempts=5, delay=3):
    """Refresh only formal data before computation; code remains frozen at HEAD."""
    external(root, backup)
    if (backup / 'baseline.json').exists():
        raise PersistenceError('BASELINE_ALREADY_EXISTS_USE_NEW_RUN_DIRECTORY')
    try:
        old = git(root, 'rev-parse', 'HEAD').decode().strip()
        before = {g: tree(root, old, g) for g in GROUPS}
        for g in GROUPS:
            files = local_files(root, g)
            if set(files) != set(before[g]) or any(
                    blob(files[p].read_bytes()) != v['blob'] for p, v in before[g].items()):
                raise PersistenceError('DIRTY_INPUT_REFUSED')
        for attempt in range(attempts):
            try:
                ref = latest(root)
                break
            except GitFailure as exc:
                audit(backup, 'prepare_fetch', 'RETRY', error=str(exc), attempt=attempt + 1)
                if attempt + 1 == attempts:
                    raise
                time.sleep(delay)
        if code_changed(root, old, ref):
            raise PersistenceError('CODE_CHANGED_RESTART_WITH_CURRENT_WORKFLOW')
        baseline = {g: tree(root, ref, g) for g in GROUPS}
        for g in GROUPS:
            if set(before[g]) - set(baseline[g]):
                raise PersistenceError('REMOTE_INPUT_REMOVED_REVIEW_REQUIRED')
        for g in GROUPS:
            for path, item in baseline[g].items():
                data = git(root, 'cat-file', 'blob', item['blob'])
                if blob(data) != item['blob']:
                    raise PersistenceError('INPUT_BLOB_MISMATCH')
                atomic(root / path, data)
        adaptive_files = local_files(root, 'adaptive')
        if adaptive_files:
            validate_output({p: f.read_bytes() for p, f in adaptive_files.items()})
        write_json(backup / 'baseline.json', dict(version=VERSION, checkout=old,
                   base=ref, groups=baseline, prepared=True))
        audit(backup, 'prepare', 'SUCCESS', checkout=old, base=ref)
        return ref
    except Exception as exc:
        audit(backup, 'prepare', 'FAILED', error=str(exc))
        raise


def archive(root, backup, group='adaptive'):
    if group != 'adaptive':
        raise PersistenceError('ADAPTIVE_WRITES_ONLY')
    external(root, backup)
    target = backup / group
    try:
        baseline = json.loads((backup / 'baseline.json').read_text())
        if baseline['version'] != VERSION or not baseline['prepared']:
            raise PersistenceError('BASELINE_NOT_PREPARED')
        if (target / 'manifest.json').exists():
            raise PersistenceError('ARCHIVE_ALREADY_EXISTS')
        files = local_files(root, group)
        expected = baseline['groups'][group]
        if set(expected) - set(files):
            raise PersistenceError('OUTPUT_FILE_MISSING')
        target.mkdir(parents=True, exist_ok=True)
        temporary = target / 'state.tar.part'
        contents = {}
        with tarfile.open(temporary, 'w') as tar:
            for path, source in sorted(files.items()):
                data = source.read_bytes()
                contents[path] = dict(blob=blob(data), sha256=sha(data),
                                     mode=expected.get(path, {}).get('mode', '100644'))
                entry = tarfile.TarInfo(path)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        archive_hash = sha(temporary.read_bytes())
        os.replace(temporary, target / 'state.tar')
        write_json(target / 'manifest.json', dict(version=VERSION, group=group,
                   base=baseline['base'], baseline=expected, files=contents,
                   archive_sha256=archive_hash))
        audit(target, 'archive', 'SUCCESS', files=len(contents), base=baseline['base'])
    except Exception as exc:
        audit(target, 'archive', 'FAILED', error=str(exc))
        raise


def payload(backup, group):
    target = backup / group
    manifest = json.loads((target / 'manifest.json').read_text())
    if manifest['version'] != VERSION or manifest['group'] != group:
        raise PersistenceError('INVALID_MANIFEST')
    data = (target / 'state.tar').read_bytes()
    if sha(data) != manifest['archive_sha256']:
        raise PersistenceError('ARCHIVE_HASH_MISMATCH')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for entry in tar:
            if not entry.isfile() or not owned(entry.name, group) or entry.name in files:
                raise PersistenceError('UNSAFE_ARCHIVE_MEMBER')
            content = tar.extractfile(entry).read()
            item = manifest['files'][entry.name]
            if sha(content) != item['sha256'] or blob(content) != item['blob']:
                raise PersistenceError('PAYLOAD_HASH_MISMATCH')
            if item['mode'] not in ('100644', '100755'):
                raise PersistenceError('INVALID_MODE')
            files[entry.name] = content
    if set(files) != set(manifest['files']) or set(manifest['baseline']) - set(files):
        raise PersistenceError('INCOMPLETE_ARCHIVE')
    return manifest, files


def candidate(root, parent, manifest, files):
    if manifest['group'] != 'adaptive' or any(not owned(name, 'adaptive') for name in files):
        raise PersistenceError('ADAPTIVE_WRITES_ONLY')
    # A private index avoids resetting HEAD, deleting outputs or staging unrelated data.
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'),
                   GIT_AUTHOR_NAME='mexc-research-bot',
                   GIT_AUTHOR_EMAIL='mexc-research-bot@users.noreply.github.com',
                   GIT_COMMITTER_NAME='mexc-research-bot',
                   GIT_COMMITTER_EMAIL='mexc-research-bot@users.noreply.github.com')
        git(root, 'read-tree', parent, env=env)
        for path, data in files.items():
            oid = git(root, 'hash-object', '-w', '--stdin', data=data).decode().strip()
            item = manifest['files'][path]
            if oid != item['blob']:
                raise PersistenceError('GIT_OBJECT_MISMATCH')
            git(root, 'update-index', '--add', '--cacheinfo',
                item['mode'] + ',' + oid + ',' + path, env=env)
        oid = git(root, 'write-tree', env=env).decode().strip()
        message = 'Update 511 Adaptive research state'
        return git(root, 'commit-tree', oid, '-p', parent, '-m', message, env=env).decode().strip()


def save(root, backup, group='adaptive', attempts=5, delay=3):
    if group != 'adaptive':
        raise PersistenceError('ADAPTIVE_WRITES_ONLY')
    external(root, backup)
    target = backup / group
    try:
        manifest, files = payload(backup, group)
        validate_output(files)
        output = {p: {k: item[k] for k in ('blob', 'mode')}
                  for p, item in manifest['files'].items()}
        # The final iteration only reconciles an accepted push with a lost reply.
        for attempt in range(attempts + 1):
            try:
                ref = latest(root)
                remote = tree(root, ref, group)
                if remote == output:
                    audit(target, 'save', 'ALREADY_SAVED', remote=ref)
                    return ref
                if remote != manifest['baseline']:
                    audit(target, 'compare', 'CONFLICT', base=manifest['base'], remote=ref)
                    raise PersistenceError('ADAPTIVE' + '_REMOTE_CONFLICT')
                if code_changed(root, manifest['base'], ref):
                    raise PersistenceError('CODE_CHANGED_DURING_SAVE')
                if attempt == attempts:
                    break
                commit = candidate(root, ref, manifest, files)
                audit(target, 'push', 'ATTEMPT', attempt=attempt + 1, parent=ref, candidate=commit)
                git(root, 'push', 'origin', commit + ':refs/heads/main')
                audit(target, 'save', 'SUCCESS', commit=commit)
                return commit
            except GitFailure as exc:
                audit(target, 'transport', 'RETRY', attempt=attempt + 1, error=str(exc))
                if attempt < attempts:
                    time.sleep(delay)
        raise PersistenceError(group.upper() + '_SAVE_RETRIES_EXHAUSTED')
    except Exception as exc:
        audit(target, 'save', 'FAILED', error=str(exc))
        raise



def validate_output(files):
    """Validate original bytes; never migrate, repair or re-evaluate evidence here."""
    from . import storage
    name = PREFIX + 'adaptive_state.json'
    state = json.loads(files[name])
    storage.validate(state)
    if state['mode'] != 'FORWARD':
        raise PersistenceError('FORWARD_ONLY')
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        storage.export(directory, state)
        expected = {PREFIX + p.name: p.read_bytes() for p in directory.iterdir()}
    expected[name] = files[name]
    if files != expected:
        raise PersistenceError('INCOMPLETE_OR_STALE_PROJECTIONS')


def recover(root, backup, destination):
    """Verified offline quarantine only, never restoration into a checkout/state."""
    external(root, destination)
    if destination.is_symlink() or any((p / '.git').exists() for p in (destination, *destination.parents)):
        raise PersistenceError('RECOVERY_REQUIRES_NON_REPOSITORY_QUARANTINE')
    if destination.resolve().is_relative_to(backup.resolve()) or backup.resolve().is_relative_to(destination.resolve()):
        raise PersistenceError('RECOVERY_DESTINATION_OVERLAP')
    manifest, files = payload(backup, 'adaptive')
    # Partial output remains useful forensic evidence, but cannot be published.
    try:
        validate_output(files)
        valid = True
    except (ValueError, KeyError, PersistenceError):
        valid = False
    expected = dict(files)
    expected['QUARANTINE.json'] = json.dumps(dict(
        formal_forward=False, publishable_output=valid,
        base=manifest['base'], archive_sha256=manifest['archive_sha256']), sort_keys=True).encode()
    if destination.exists():
        actual = {}
        for p in destination.rglob('*'):
            if p.is_symlink():
                raise PersistenceError('UNSAFE_RECOVERY_DESTINATION')
            if p.is_file():
                actual[p.relative_to(destination).as_posix()] = p.read_bytes()
        if actual != expected:
            raise PersistenceError('RECOVERY_DESTINATION_NOT_IDENTICAL')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            staging = Path(tmp) / 'verified'
            staging.mkdir()
            for name, data in expected.items():
                atomic(staging / name, data)
            # Rename into a new destination; never overwrite a previous restore.
            staging.rename(destination)
    audit(backup, 'quarantine_recovery', 'SUCCESS', files=len(files), publishable_output=valid)
    return valid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'archive', 'save', 'recover'))
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    if args.action == 'recover':
        if args.destination is None:
            parser.error('--destination required')
        recover(root, args.backup, args.destination)
    else:
        result = {'prepare': prepare, 'archive': archive, 'save': save}[args.action](root, args.backup)
        if result:
            print(result)


if __name__ == '__main__':
    main()
