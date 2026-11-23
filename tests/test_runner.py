"""Runner boundary checks: no API, no private gold, no scientific scores."""

import argparse
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch

from labestra import runner
from labestra.workflow import WorkflowServices
from labestra.workflow_runtime import Runtime, StopRun
from test_workflow import Tools, CORPUS, draft, make_case


class RunnerTests(unittest.TestCase):
    def test_saved_config_must_not_contain_credentials(self):
        with self.assertRaises(ValueError):
            runner.check_public_config({'model': {'api_key': 'synthetic-secret'}})
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-secret'}), self.assertRaises(ValueError):
            runner.check_public_config({'custom': 'synthetic-secret'})

    def args(self, output, variant='system', **kw):
        return argparse.Namespace(variant=variant, config='configs/default.json',
            cases='eval/dev/tasks.jsonl', output=str(output), session_id='test-session',
            case_id=None, participant=None, blind_attestation=False,
            fixture_actions=True, **kw)

    def test_same_entry_runs_both_variants_without_network_and_preserves_dataset(self):
        before = runner.digest(runner.ROOT / 'data/manifest.json')
        with tempfile.TemporaryDirectory() as tmp, patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')):
            for variant in ('baseline', 'system'):
                out = Path(tmp) / variant
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.execute(self.args(out, variant)), 0)
                summary = json.loads((out / 'test-session/summary.json').read_text())
                self.assertEqual(summary['recorded'], 6)
                for row in summary['results']:
                    directory = out / 'test-session' / row['case_id']
                    manifest = json.loads((directory / 'manifest.json').read_text())
                    self.assertFalse(manifest['scientific_evaluation'])
                    self.assertEqual(manifest['usage_kind'], 'synthetic_fixture')
                    self.assertTrue(manifest['file_sha256'])
                    self.assertEqual(row['variant'], variant)
                    self.assertIsNone(row['human_active_seconds'])
        self.assertEqual(before, runner.digest(runner.ROOT / 'data/manifest.json'))

    def test_existing_session_stops_before_model_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'test-session').mkdir()
            with patch.object(runner, 'make_model') as model:
                with self.assertRaisesRegex(ValueError, 'Session already exists'):
                    runner.execute(self.args(tmp))
                model.assert_not_called()

    def test_bad_collection_and_validation_error_stop_before_index(self):
        config = json.loads((runner.ROOT / 'configs/default.json').read_text())
        config['collection_id'] = 'wrong'
        with patch.object(runner, 'LocalSearch') as index:
            with self.assertRaisesRegex(ValueError, 'collection_id'):
                runner.prepare(runner.ROOT, config, 'eval/dev/tasks.jsonl')
            index.assert_not_called()
        with patch.object(runner, 'validate_dataset', side_effect=ValueError('broken data')), patch.object(runner, 'LocalSearch') as index:
            with self.assertRaisesRegex(ValueError, 'broken data'):
                runner.prepare(runner.ROOT, config, 'eval/dev/tasks.jsonl')
            index.assert_not_called()

    def test_warnings_are_preserved_once(self):
        config = json.loads((runner.ROOT / 'configs/default.json').read_text())
        def validate(_):
            warnings.warn('duplicate record')
            warnings.warn('duplicate record')
        with patch.object(runner, 'validate_dataset', side_effect=validate):
            *_, diagnostics = runner.prepare(runner.ROOT, config, 'eval/dev/tasks.jsonl')
        self.assertEqual(diagnostics, ['duplicate record'])

    def test_private_and_holdout_inputs_are_rejected_before_reading(self):
        for path in ('eval/private/gold/visible.jsonl', 'eval/holdout/tasks.jsonl'):
            with patch.object(runner, 'validate_dataset') as validate:
                with self.assertRaisesRegex(ValueError, 'public dev/visible'):
                    runner.prepare(runner.ROOT, {}, path)
                validate.assert_not_called()

    def test_fixture_cannot_evaluate_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.cases = 'eval/visible/tasks.jsonl'
            with self.assertRaisesRegex(ValueError, 'limited to dev'):
                runner.execute(args)

    def test_baseline_never_calls_model_and_keeps_real_claims(self):
        case = make_case()
        runtime = Runtime('baseline-test', case.limits)
        model = unittest.mock.Mock()
        svc = WorkflowServices(runtime, model, {CORPUS.corpus_revision_id: CORPUS},
                               lambda event, payload: draft())
        result = runner.run_case(case, runtime.wrap_search(Tools()), {}, runtime.run_id,
                                 'baseline', services=svc)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.usage.llm_calls, 0)
        self.assertEqual(result.claims[0].text, draft()['claims'][0]['text'])
        model.complete.assert_not_called()

    def test_publication_is_idempotent_and_refuses_changed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'case'
            self.assertTrue(runner.publish(path, {'result.json': '{}\n'}))
            self.assertFalse(runner.publish(path, {'result.json': '{}\n'}))
            with self.assertRaises(ValueError):
                runner.publish(path, {'result.json': '{"changed":true}\n'})
            self.assertEqual((path / 'result.json').read_text(), '{}\n')

    def test_cancellation_before_publication_leaves_no_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'case'
            with self.assertRaises(StopRun):
                runner.publish(path, {'result.json': '{"status":"completed"}\n'}, lambda: True)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_session_path_cannot_escape_output(self):
        for value in ('../escape', '/tmp/escape', '.', '', 'a/b'):
            with self.assertRaises(ValueError):
                runner.safe_id(value)

    def test_console_checks_deadline_while_waiting_for_input(self):
        runtime = Runtime('deadline', make_case().limits)
        console = runner.Console(runtime, [], None, make_case(), .01)
        with patch.object(runner.select, 'select', return_value=([], [], [])), redirect_stdout(io.StringIO()):
            with self.assertRaises(StopRun) as exc:
                console.read('> ')
        self.assertEqual(exc.exception.reason, 'human_time_limit_seconds')

    def test_pasted_multiline_input_is_not_lost_in_stdio_buffer(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, 'первая\nвторая\n'.encode('utf-8'))
        os.close(write_fd)
        runtime = Runtime('paste', make_case().limits)
        console = runner.Console(runtime, [], None, make_case(), 1)
        with os.fdopen(read_fd) as stream, patch.object(runner.sys, 'stdin', stream), redirect_stdout(io.StringIO()):
            self.assertEqual(console.read('> '), 'первая')
            self.assertEqual(console.read('> '), 'вторая')

    def test_unknown_live_billing_stops_whole_session_before_next_case(self):
        from labestra.llm import CompletionFailure
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'live.json'
            config = json.loads((runner.ROOT / 'configs/workflow.live.example.json').read_text())
            config['model']['model_id'] = 'gpt://test-folder/qwen3.6-35b-a3b'
            config['session_max_cost'] = 20
            config['limits']['max_cost'] = 20
            path.write_text(runner.encoded(config))
            args = self.args(Path(tmp) / 'output')
            args.config = str(path)
            args.fixture_actions = False
            args.participant = 'synthetic-operator'
            args.case_id = ['dev-01', 'dev-02']
            model = unittest.mock.Mock(mode='live', model_id=config['model']['model_id'])
            model.complete.side_effect = CompletionFailure('http_503', True)
            with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-key'}), \
                 patch.object(runner, 'make_model', return_value=model), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.execute(args), 1)
            summary = json.loads((Path(args.output) / 'test-session/summary.json').read_text())
            self.assertEqual(summary['planned'], 2)
            self.assertEqual(summary['recorded'], 1)
            self.assertIn('usage_unknown', summary['halt_reason'])
            self.assertIsNone(summary['results'][0]['usage']['cost'])
            model.complete.assert_called_once()


if __name__ == '__main__':
    unittest.main()
