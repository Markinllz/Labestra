import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from labestra import submission
from labestra.runner import digest, ROOT


class SubmissionTests(unittest.TestCase):
    def test_reviewed_reports_reproduce_without_network_or_dataset_mutation(self):
        before = digest(ROOT / 'data/manifest.json')
        with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')):
            reports = submission.build()
        report = reports['system-v2-quality.json']
        self.assertEqual(report['planned'], 24)
        self.assertEqual(report['quality']['accepted']['assessed_cases'], 10)
        self.assertEqual(report['quality']['accepted']['mean'], .5)
        self.assertAlmostEqual(report['total_cost'], .049953)
        self.assertEqual(reports['agreement.sabukhi-max.json']['by_kind']['relevance']['matched'], 119)
        self.assertFalse(reports['readiness.json']['coursework_complete'])
        baseline=reports['baseline-quality.json']
        self.assertEqual(baseline['planned'],24)
        self.assertEqual(baseline['total_cost'],0.)
        self.assertEqual(baseline['quality']['accepted']['assessed_cases'],0)
        self.assertIsNone(baseline['quality']['accepted']['mean'])
        self.assertAlmostEqual(baseline['quality']['selected_source_recall']['mean'],.95)
        self.assertTrue(reports['comparison.json']['same_inputs_and_config'])
        self.assertEqual(reports['comparison.json']['paired_cases'],24)
        self.assertEqual(before, digest(ROOT / 'data/manifest.json'))

    def test_archive_path_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr(submission.PREFIX + '../outside.json', '{}')
            with self.assertRaises(ValueError):
                submission.extract_cohort(archive, directory / 'output')
            self.assertFalse((directory / 'outside.json').exists())
