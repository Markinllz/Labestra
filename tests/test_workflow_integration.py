"""Actual public dev data + actual FTS5 + scripted model. No scientific scoring."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labestra.workflow_demo import ROOT, run_demo


class WorkflowIntegrationTests(unittest.TestCase):
    def test_all_public_dev_inputs_run_offline_and_preserve_sources(self):
        public = list((ROOT / 'data').rglob('*.txt')) + list((ROOT / 'data').glob('*.json*'))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in public}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'demo'
            with patch('socket.socket.connect', side_effect=AssertionError('No network in fixture')), \
                 patch.dict('os.environ', {}, clear=True):
                summary = run_demo(output)
            self.assertEqual(len(summary), 6)
            self.assertEqual([r['status'] for r in summary],
                             ['completed', 'completed', 'needs_clarification',
                              'completed', 'completed', 'completed'])
            for row in summary:
                directory = output / row['case_id']
                result = json.loads((directory / 'result.json').read_text())
                manifest = json.loads((directory / 'manifest.json').read_text())
                events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
                self.assertEqual(manifest['mode'], 'fixture')
                self.assertEqual(manifest['network_calls'], 0)
                self.assertFalse(manifest['scientific_evaluation'])
                self.assertEqual(result['usage']['currency'], 'RUB')
                self.assertEqual(result['usage']['cost'], 0)
                contexts = [e['output']['included'] for e in events if e['name'] == 'context_selected']
                for claim in result['claims']:
                    self.assertTrue(contexts)
                    for evidence in claim['evidence']:
                        self.assertIn(evidence['fragment_id'], contexts[-1])
                self.assertEqual(events[-1]['name'], 'workflow_finished')
                for filename, expected in manifest['file_sha256'].items():
                    self.assertEqual(hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(), expected)
            with self.assertRaises(FileExistsError):
                run_demo(output)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in public})
