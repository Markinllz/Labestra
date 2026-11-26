"""Scoring boundary tests use only synthetic claims, never private gold."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labestra import evaluate, runner
import test_runner


class EvaluationTests(unittest.TestCase):
    def create_session(self, tmp):
        args = test_runner.RunnerTests().args(tmp)
        args.case_id = ['dev-04']
        with redirect_stdout(io.StringIO()):
            runner.execute(args)
        return Path(tmp) / 'test-session'

    def test_fixture_reports_resources_but_no_scientific_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate.score(self.create_session(tmp))
        self.assertFalse(report['scientific_evaluation'])
        self.assertEqual(report['duration_seconds']['n'], 1)
        self.assertEqual(report['quality']['gold_coverage']['mean'], None)
        self.assertEqual(report['total_cost'], 0.)

    def test_fixture_cannot_be_scored_against_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.create_session(tmp)
            gold = Path(tmp) / 'synthetic-gold.jsonl'
            gold.write_text(json.dumps({'case_id': 'dev-04', 'expected_action': 'answer'}) + '\n')
            with patch('dataset_tools.validation.validate_gold_rows'):
                with self.assertRaisesRegex(ValueError, 'Fixture runs'):
                    evaluate.score(session, gold_path=gold)

    def test_missing_answerable_task_is_zero_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / 'empty-session'
            session.mkdir()
            (session / 'summary.json').write_text(runner.encoded(
                {'session_id': 'empty', 'planned': 1, 'planned_case_ids': ['dev-04']}))
            gold = Path(tmp) / 'synthetic-gold.jsonl'
            gold.write_text(json.dumps({'case_id': 'dev-04', 'expected_action': 'answer'}) + '\n')
            with patch('dataset_tools.validation.validate_gold_rows'):
                report = evaluate.score(session, gold_path=gold)
            self.assertEqual(report['completion']['value'], 0.)
            self.assertEqual(report['quality']['gold_coverage']['mean'], 0.)
            self.assertEqual(report['quality']['accepted']['mean'], 0.)
            self.assertEqual(report['quality']['action_correct']['mean'], 0.)

    def test_changed_task_question_cannot_reuse_old_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.create_session(tmp)
            path = session / 'dev-04/input.json'
            value = json.loads(path.read_text())
            value['question'] = 'An unrelated, synthetic question'
            path.write_text(runner.encoded(value))
            with self.assertRaisesRegex(ValueError, 'frozen task'):
                evaluate.score(session)

    def test_unknown_cost_is_not_reported_as_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.create_session(tmp)
            path = session / 'dev-04/result.json'
            value = json.loads(path.read_text())
            value['usage']['cost'] = None
            path.write_text(runner.encoded(value))
            report = evaluate.score(session)
            self.assertIsNone(report['total_cost'])
            self.assertEqual(report['unknown_cost_runs'], 1)


if __name__ == '__main__':
    unittest.main()
