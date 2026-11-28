"""HYBRID-only optimistic persistence. No trading logic or automatic replay.

Only a verified immutable output bundle may be published. Real GitHub writes
are restricted to the workflow; tests use disposable local bare repositories.
"""
import argparse
import base64
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile

import research_persistence as core

VERSION = 1
OWNED = ('mexc_hybrid_state.json', 'mexc_hybrid_trades.csv')
STATE, TRADES = OWNED
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024  # Resource limit, never truncate.
Error = core.PersistenceError
git = core.git


def require(value, reason):
    if not value:
        raise Error(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def origin(root):
    url = git(root, 'remote', 'get-url', 'origin').decode().strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in ('http', 'https'):
        url = parsed.scheme + '://' + (parsed.hostname or '').lower() + parsed.path.rstrip('/').removesuffix('.git').lower()
    elif parsed.scheme == 'file':
        url = parsed.geturl()
    else:
        url = str(Path(url).resolve())
    return core.sha(url.encode())


def tree(root, ref, code=False):
    result = {}
    for entry in git(root, 'ls-tree', '-r', '-z', ref).split(b'\0'):
        if not entry:
            continue
        header, raw = entry.split(b'\t', 1)
        name = raw.decode()
        include = (name.endswith('.py') or name.startswith('.github/workflows/') or Path(name).name.startswith('requirements')) if code else name in OWNED
        if include:
            mode, kind, oid = header.decode().split()
            require(kind == 'blob' and mode in ('100644', '100755'), 'UNSAFE_TREE')
            result[name] = {'mode': mode, 'blob': oid}
    return result


def local(root):
    result = {}
    for name in OWNED:
        path = root/name
        require(not path.is_symlink(), 'SYMLINK_REFUSED')
        if path.exists():
            require(path.is_file(), 'INVALID_INPUT_FILE')
            result[name] = path.read_bytes()
    return result


def validate(files):
    require(set(files) <= set(OWNED) and STATE in files, 'MISSING_OR_UNKNOWN_HYBRID_FILES')
    require(isinstance(json.loads(files[STATE]), dict), 'INVALID_STATE_JSON')
    if TRADES in files:
        rows = csv.DictReader(io.StringIO(files[TRADES].decode('utf-8-sig'), newline=''))
        require(rows.fieldnames and len(set(rows.fieldnames)) == len(rows.fieldnames), 'INVALID_CSV_HEADER')
        require(all(None not in row and None not in row.values() for row in rows), 'INVALID_CSV_ROW')


def encode(files):
    return {n: {'sha256': core.sha(v), 'blob': core.blob(v), 'data': base64.b64encode(v).decode()} for n, v in files.items()}


def decode(records):
    require(set(records) <= set(OWNED), 'UNSAFE_PAYLOAD_PATH')
    files = {}
    for n, item in records.items():
        raw = base64.b64decode(item['data'], validate=True)
        require(core.sha(raw) == item['sha256'] and core.blob(raw) == item['blob'], 'PAYLOAD_HASH_MISMATCH')
        files[n] = raw
    validate(files)
    return files


def load(path):
    value = json.loads(path.read_bytes())
    require(value['version'] == VERSION, 'UNKNOWN_RECOVERY_VERSION')
    return value


def validate_evidence(bundle):
    evidence = bundle.get('evidence', {})
    check = evidence.get('checkpoint', {})
    completion = evidence.get('completion', {})
    require(check.get('version') == VERSION and completion.get('version') == VERSION, 'RECOVERY_EVIDENCE_MISSING')
    base_hash = core.sha(canonical(bundle['base']))
    require(check.get('baseline_sha256') == base_hash and completion.get('baseline_sha256') == base_hash, 'RECOVERY_EVIDENCE_BASELINE_MISMATCH')
    require(completion.get('files') == bundle['files'], 'RECOVERY_COMPLETION_MISMATCH')
    raw = base64.b64decode(check['scanner_data'], validate=True)
    require(core.sha(raw) == check['scanner_sha256'] and check['replay_allowed'] is False, 'RECOVERY_SCANNER_MISMATCH')


def latest(root):
    git(root, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
    return git(root, 'rev-parse', 'FETCH_HEAD').decode().strip()


def prepare(root, backup, attempts=3, delay=3):
    core.external(root, backup)
    require(not (backup/'baseline.json').exists(), 'BASELINE_ALREADY_EXISTS')
    try:
        head = git(root, 'rev-parse', 'HEAD').decode().strip()
        before = tree(root, head)
        files = local(root)
        require(set(files) == set(before) and all(core.blob(files[n]) == v['blob'] for n, v in before.items()), 'DIRTY_INPUT_REFUSED')
        for attempt in range(attempts):
            try:
                ref = latest(root)
                break
            except core.GitFailure:
                core.audit(backup, 'prepare_fetch', 'RETRY', attempt=attempt+1)
                if attempt+1 == attempts:
                    raise
                time.sleep(delay)
        code = tree(root, head, code=True)
        require(code == tree(root, ref, code=True), 'CODE_CHANGED_RESTART')
        baseline = tree(root, ref)
        require(not set(before)-set(baseline), 'REMOTE_INPUT_REMOVED')
        files = {n: git(root, 'cat-file', 'blob', v['blob']) for n, v in baseline.items()}
        validate(files)
        require(all(core.blob(files[n]) == v['blob'] for n, v in baseline.items()), 'INPUT_BLOB_MISMATCH')
        # Publish baseline marker only after every input is validated/refreshed.
        for n, raw in files.items():
            core.atomic(root/n, raw)
        value = {'version': VERSION, 'repository': origin(root), 'base': ref, 'checkout': head,
                 'code': code, 'baseline': baseline, 'inputs': encode(files),
                 'github_repository': os.environ.get('GITHUB_REPOSITORY', ''),
                 'run_id': os.environ.get('GITHUB_RUN_ID', ''),
                 'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '')}
        core.atomic(backup/'baseline.json', canonical(value))
        core.audit(backup, 'prepare', 'SUCCESS', base=ref)
    except Exception as exc:
        core.audit(backup, 'prepare', 'FAILED', error=type(exc).__name__)
        raise


def checkpoint(root, backup):
    core.external(root, backup)
    base = load(backup/'baseline.json')
    require(origin(root) == base['repository'], 'REPOSITORY_MISMATCH')
    require(local(root) == decode(base['inputs']), 'INPUT_CHANGED_BEFORE_COMPUTE')
    require(not (backup/'input-checkpoint.json').exists(), 'CHECKPOINT_EXISTS')
    scans = list(root.glob('mexc_scan_*.csv'))
    require(scans, 'SCANNER_SOURCE_MISSING')
    scan = max(scans, key=lambda p: p.stat().st_mtime_ns)
    require(not scan.is_symlink(), 'SCANNER_SYMLINK')
    raw = scan.read_bytes()
    value = {'version': VERSION, 'baseline_sha256': core.sha((backup/'baseline.json').read_bytes()),
             'scanner': scan.name, 'scanner_sha256': core.sha(raw),
             'scanner_data': base64.b64encode(raw).decode(), 'replay_allowed': False}
    core.atomic(backup/'input-checkpoint.json', canonical(value))
    core.audit(backup, 'checkpoint', 'SUCCESS', scanner=scan.name)


def archive(root, backup):
    core.external(root, backup)
    try:
        base = load(backup/'baseline.json')
        require(origin(root) == base['repository'], 'REPOSITORY_MISMATCH')
        require(not (backup/'recovery.json').exists(), 'RECOVERY_ALREADY_EXISTS')
        files = local(root)
        validate(files)
        completed = load(backup/'completed.json')
        require(completed['files'] == encode(files), 'OUTPUT_NOT_SUCCESSFULLY_COMPLETED')
        require(completed['baseline_sha256'] == core.sha((backup/'baseline.json').read_bytes()), 'COMPLETION_BASELINE_MISMATCH')
        original = decode(base['inputs'])
        require(not set(original)-set(files), 'OUTPUT_FILE_MISSING')
        if TRADES in original:
            require(files[TRADES].startswith(original[TRADES]), 'TRADE_PREFIX_CHANGED')
        bundle = {'version': VERSION, 'base': base, 'files': encode(files),
                  'evidence': {'checkpoint': load(backup/'input-checkpoint.json'), 'completion': completed}}
        validate_evidence(bundle)
        core.atomic(backup/'recovery.json', canonical(bundle))
        core.audit(backup, 'archive', 'SUCCESS', bundle_sha256=core.sha(canonical(bundle)))
    except Exception as exc:
        core.audit(backup, 'archive', 'FAILED', error=type(exc).__name__)
        raise


def execute(root, backup):
    """Run the unchanged engine once; interrupted/failed outputs are diagnostic only."""
    core.external(root, backup)
    require(not (backup/'started.json').exists(), 'COMPUTATION_ALREADY_STARTED')
    base = load(backup/'baseline.json')
    check = load(backup/'input-checkpoint.json')
    require(check['baseline_sha256'] == core.sha((backup/'baseline.json').read_bytes()), 'CHECKPOINT_BASELINE_MISMATCH')
    require(local(root) == decode(base['inputs']), 'INPUT_CHANGED_BEFORE_COMPUTE')
    scans = list(root.glob('mexc_scan_*.csv'))
    require(scans, 'SCANNER_SOURCE_MISSING')
    scan = max(scans, key=lambda p: p.stat().st_mtime_ns)
    require(scan.name == check['scanner'] and core.sha(scan.read_bytes()) == check['scanner_sha256'], 'SCANNER_CHANGED')
    core.atomic(backup/'started.json', canonical({'version': VERSION}))
    result = subprocess.run([sys.executable, 'mexc_hybrid_system.py'], cwd=root)
    require(result.returncode == 0, 'HYBRID_COMPUTATION_FAILED')
    files = local(root)
    validate(files)
    core.atomic(backup/'completed.json', canonical({'version': VERSION,
        'baseline_sha256': check['baseline_sha256'], 'files': encode(files)}))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def remote_artifact(repository, artifact_id):
    """Read only. Never forward the GitHub token to the signed storage URL."""
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'INVALID_REPOSITORY')
    require(str(artifact_id).isdigit(), 'INVALID_ARTIFACT_ID')
    token = os.environ.get('GITHUB_TOKEN', '')
    require(token, 'ARTIFACT_READ_TOKEN_REQUIRED')
    url = f'https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}'
    headers = {'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'hybrid-recovery-verification'}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            metadata = json.loads(r.read())
        try:
            urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url+'/zip', headers=headers), timeout=60)
        except urllib.error.HTTPError as exc:
            require(exc.code == 302, 'ARTIFACT_REDIRECT_REQUIRED')
            signed = exc.headers['Location']
            require(urllib.parse.urlsplit(signed).scheme == 'https', 'UNSAFE_ARTIFACT_URL')
        else:
            raise Error('ARTIFACT_REDIRECT_REQUIRED')
        with urllib.request.urlopen(signed, timeout=120) as r:
            chunks = []
            size = 0
            while True:
                part = r.read(65536)
                if not part:
                    break
                size += len(part)
                require(size <= MAX_ARTIFACT_BYTES, 'ARTIFACT_TOO_LARGE')
                chunks.append(part)
        return metadata, b''.join(chunks)
    except Exception:
        raise Error('ARTIFACT_VERIFICATION_TRANSPORT_FAILED') from None


def confirm(root, backup, artifact_id, digest):
    core.external(root, backup)
    raw = (backup/'recovery.json').read_bytes()
    bundle = load(backup/'recovery.json')
    validate_evidence(bundle)
    base = bundle['base']
    require(origin(root) == base['repository'], 'REPOSITORY_MISMATCH')
    decode(bundle['files'])
    metadata, zipped = remote_artifact(base['github_repository'], artifact_id)
    digest = digest.removeprefix('sha256:')
    require(re.fullmatch('[0-9a-f]{64}', digest), 'INVALID_ARTIFACT_DIGEST')
    require(not metadata['expired'] and str(metadata['id']) == str(artifact_id), 'ARTIFACT_UNAVAILABLE')
    require(str(metadata['workflow_run']['id']) == base['run_id'], 'ARTIFACT_RUN_MISMATCH')
    require(metadata['name'] == f"hybrid-recovery-{base['run_id']}-{base['run_attempt']}", 'ARTIFACT_ATTEMPT_MISMATCH')
    require(metadata['digest'] == 'sha256:'+digest and core.sha(zipped) == digest, 'ARTIFACT_DIGEST_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        require(z.namelist().count('recovery.json') == 1, 'AMBIGUOUS_RECOVERY_MEMBER')
        require(z.getinfo('recovery.json').file_size <= MAX_ARTIFACT_BYTES, 'RECOVERY_TOO_LARGE')
        require(z.read('recovery.json') == raw, 'REMOTE_RECOVERY_CONTENT_MISMATCH')
    receipt = {'version': VERSION, 'artifact_id': str(artifact_id), 'digest': digest,
               'bundle_sha256': core.sha(raw), 'expires_at': metadata['expires_at']}
    core.atomic(backup/'durable_receipt.json', canonical(receipt))
    core.audit(backup, 'durable_verify', 'SUCCESS', artifact_id=str(artifact_id), bundle_sha256=core.sha(raw))


def candidate(root, parent, base, files):
    with tempfile.TemporaryDirectory() as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp)/'index'),
                   GIT_AUTHOR_NAME='mexc-hybrid-bot', GIT_COMMITTER_NAME='mexc-hybrid-bot',
                   GIT_AUTHOR_EMAIL='mexc-hybrid-bot@users.noreply.github.com',
                   GIT_COMMITTER_EMAIL='mexc-hybrid-bot@users.noreply.github.com')
        git(root, 'read-tree', parent, env=env)
        for n, raw in files.items():
            oid = git(root, 'hash-object', '-w', '--stdin', data=raw).decode().strip()
            require(oid == core.blob(raw), 'GIT_OBJECT_MISMATCH')
            mode = base['baseline'].get(n, {}).get('mode', '100644')
            git(root, 'update-index', '--add', '--cacheinfo', mode+','+oid+','+n, env=env)
        oid = git(root, 'write-tree', env=env).decode().strip()
        return git(root, 'commit-tree', oid, '-p', parent, '-m', 'Update MEXC HYBRID state', env=env).decode().strip()


def save(root, backup, attempts=5, delay=3):
    core.external(root, backup)
    try:
        bundle = load(backup/'recovery.json')
        validate_evidence(bundle)
        receipt = load(backup/'durable_receipt.json')
        require(receipt['bundle_sha256'] == core.sha((backup/'recovery.json').read_bytes()), 'DURABILITY_BINDING_MISMATCH')
        require(datetime.fromisoformat(receipt['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc), 'DURABLE_ARTIFACT_EXPIRED')
        base = bundle['base']
        require(base['version'] == VERSION and base['repository'] == origin(root), 'REPOSITORY_OR_VERSION_MISMATCH')
        files = decode(bundle['files'])
        original = decode(base['inputs'])
        require(set(original) == set(base['baseline']) and all(core.blob(original[n]) == v['blob'] for n, v in base['baseline'].items()), 'BASELINE_HASH_MISMATCH')
        require(not set(original)-set(files), 'OUTPUT_FILE_MISSING')
        if TRADES in original:
            require(files[TRADES].startswith(original[TRADES]), 'TRADE_PREFIX_CHANGED')
        output = {n: {'blob': core.blob(raw), 'mode': base['baseline'].get(n, {}).get('mode', '100644')} for n, raw in files.items()}
        for attempt in range(attempts+1):
            try:
                ref = latest(root)
                remote = tree(root, ref)
                if remote == output:
                    core.audit(backup, 'save', 'ALREADY_SAVED', commit=ref)
                    return ref
                require(remote == base['baseline'], 'HYBRID_REMOTE_CONFLICT')
                require(tree(root, ref, code=True) == base['code'], 'CODE_CHANGED_DURING_SAVE')
                if attempt == attempts:
                    break
                commit = candidate(root, ref, base, files)
                core.audit(backup, 'push', 'ATTEMPT', parent=ref, candidate=commit, attempt=attempt+1)
                git(root, 'push', 'origin', commit+':refs/heads/main')
                core.audit(backup, 'save', 'SUCCESS', commit=commit)
                return commit
            except core.GitFailure as exc:
                core.audit(backup, 'transport', 'RETRY', category=exc.category, attempt=attempt+1)
                if attempt < attempts:
                    time.sleep(delay)
        raise Error('HYBRID_SAVE_RETRIES_EXHAUSTED')
    except Exception as exc:
        core.audit(backup, 'save', 'FAILED', error=type(exc).__name__,
                   reason=str(exc) if isinstance(exc, Error) else 'INVALID_OR_MISSING_RECOVERY')
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'checkpoint', 'execute', 'archive', 'confirm', 'save'))
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--artifact-id')
    parser.add_argument('--digest')
    args = parser.parse_args()
    core.external(Path.cwd(), args.backup)
    try:
        if args.action == 'confirm':
            require(args.artifact_id and args.digest, 'ARTIFACT_ID_AND_DIGEST_REQUIRED')
            confirm(Path.cwd(), args.backup, args.artifact_id, args.digest)
        else:
            globals()[args.action](Path.cwd(), args.backup)
    except Exception as exc:
        core.audit(args.backup, args.action, 'CLI_FAILED', error=type(exc).__name__,
                   reason=str(exc) if isinstance(exc, Error) else 'INVALID_OR_MISSING_INPUT')
        raise


if __name__ == '__main__':
    main()
