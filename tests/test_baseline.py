import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from labestra.baseline import extract_quotes, run_extractive, MAX_QUOTE_CHARS
from labestra import runner
from labestra.schemas import Fragment
from labestra.workflow import WorkflowServices
from labestra.workflow_runtime import Runtime
from test_workflow import Tools, CORPUS, FRAGMENT, make_case


class ExtractiveBaselineTests(unittest.TestCase):
    def test_quotes_are_verbatim_bounded_deduplicated_and_source_diverse(self):
        a = replace(FRAGMENT, text='Retrieval supports questions. Retrieval supports questions. ' + 'retrieval ' * 150)
        b = replace(a, fragment_id='other', source_id='other-source', text='Retrieval is another result.')
        quotes = extract_quotes('retrieval', [a, b])
        self.assertEqual(quotes, extract_quotes('retrieval', [a, b]))
        self.assertEqual({q['fragment'].source_id for q in quotes}, {a.source_id, b.source_id})
        self.assertEqual(len({q['text'] for q in quotes}), len(quotes))
        for q in quotes:
            self.assertEqual(q['text'], q['fragment'].text[q['start']:q['end']])
            self.assertLessEqual(len(q['text']), MAX_QUOTE_CHARS)
        self.assertEqual(extract_quotes('nonmatchingtoken', [a]), [])

    def test_tool_failure_and_step_limit_produce_normal_results(self):
        case = make_case()
        rt = Runtime('extract', replace(case.limits, max_steps=0))
        services = WorkflowServices(rt, None, {CORPUS.corpus_revision_id: CORPUS},
                                    lambda *_: self.fail('No interaction expected'))
        result = run_extractive(case, rt.wrap_search(Tools()), {}, rt.run_id, services=services)
        self.assertEqual(result.status, 'budget_exceeded')
        self.assertEqual(result.usage.llm_calls, 0)

    def test_visible_runs_without_model_network_interaction_or_gold_reads(self):
        import argparse
        before = runner.digest(runner.ROOT / 'data/manifest.json')
        original = Path.read_text
        def read(path, *args, **kwargs):
            if 'eval/review' in str(path) or 'eval/private' in str(path):
                self.fail('Solving baseline accessed reference labels')
            return original(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(variant='baseline', baseline_policy='extractive-v1',
                config='configs/arkady.deepseek-v4-flash-0731.v2.json',
                cases='eval/visible/tasks.jsonl', output=temporary, session_id='extractive-test',
                participant=None, blind_attestation=False, fixture_actions=False,
                prior_reference_access=False, case_id=None)
            with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
                 patch.object(runner, 'make_model', side_effect=AssertionError('No model')), \
                 patch.object(runner.Console, '__call__', side_effect=AssertionError('No human input')), \
                 patch.object(Path, 'read_text', read), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.execute(args), 0)
            summary = json.loads((Path(temporary) / 'extractive-test/summary.json').read_text())
            self.assertEqual(summary['recorded'], 24)
            for row in summary['results']:
                self.assertEqual(row['usage']['llm_calls'], 0)
                self.assertEqual(row['usage']['cost'], 0)
                self.assertIsNone(row['human_active_seconds'])
                m=json.loads((Path(temporary)/'extractive-test'/row['case_id']/'manifest.json').read_text())
                self.assertEqual(m['baseline_policy'], 'extractive-v1')
                self.assertTrue(m['scientific_evaluation'])
                self.assertFalse(m['blind_attestation'])
            args.blind_attestation=True
            with self.assertRaisesRegex(ValueError, 'attestations'):
                runner.execute(args)
        self.assertEqual(before, runner.digest(runner.ROOT / 'data/manifest.json'))
