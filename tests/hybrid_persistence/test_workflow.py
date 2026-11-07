import unittest
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml


class Workflow(unittest.TestCase):
    def test_cli_failure_return_code_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp)/'recovery'
            result = subprocess.run([sys.executable, '-B', '-W', 'error', 'hybrid_persistence.py',
                'confirm', '--backup', str(backup)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            rows = [json.loads(line) for line in (backup/'save_audit.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['status'], 'CLI_FAILED')
            self.assertEqual(rows[-1]['reason'], 'ARTIFACT_ID_AND_DIGEST_REQUIRED')

    def test_storage_order_fail_closed_and_depth_unchanged(self):
        workflow = yaml.safe_load(Path('.github/workflows/hybrid.yml').read_text(encoding='utf-8-sig'))
        self.assertEqual(workflow['permissions'], {'contents': 'write', 'actions': 'read'})
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        steps = workflow['jobs']['scan']['steps']
        self.assertEqual(steps[0]['with']['fetch-depth'], 0)
        commands = [s.get('run', '') for s in steps]
        def locate(fragment):
            return next(i for i,c in enumerate(commands) if fragment in c)
        self.assertLess(locate(' prepare '), locate('python mexc_scanner.py'))
        self.assertLess(locate(' checkpoint '), locate(' execute '))
        self.assertLess(locate(' execute '), locate(' archive '))
        self.assertLess(locate(' archive '), locate(' confirm '))
        self.assertLess(locate(' confirm '), locate(' save '))
        uploads = [(i,s) for i,s in enumerate(steps) if s.get('uses') == 'actions/upload-artifact@v4']
        self.assertEqual(len(uploads), 3)
        self.assertLess(uploads[0][0], locate(' execute '))
        self.assertLess(locate(' archive '), uploads[1][0])
        self.assertLess(uploads[1][0], locate(' confirm '))
        self.assertEqual(uploads[2][1]['if'], 'always()')
        for step in steps[:-1]:
            self.assertNotIn('continue-on-error', step)
            self.assertNotIn('if', step)
        self.assertNotIn('reset --hard', '\n'.join(commands))
        self.assertNotIn('--force', '\n'.join(commands))
        self.assertNotIn('workflow_dispatch', Path('.github/workflows/hybrid_persistence_tests.yml').read_text(encoding='utf-8-sig'))
