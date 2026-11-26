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
        self.assertEqual(baseline['quality']['accepted']['assessed_cases'],10)
        self.assertEqual(baseline['quality']['accepted']['mean'],.4)
        self.assertAlmostEqual(baseline['quality']['selected_source_recall']['mean'],.95)
        self.assertTrue(reports['comparison.json']['same_inputs_and_config'])
        self.assertEqual(reports['comparison.json']['paired_cases'],24)
        self.assertTrue(reports['readiness.json']['supplementary_annotation_audit_complete'])
        self.assertEqual(reports['readiness.json']['supplementary_annotation_objects'],24)
        self.assertTrue(all(v['matched']==v['total'] for v in reports['agreement.audit-24.json']['by_kind'].values()))
        self.assertEqual(before, digest(ROOT / 'data/manifest.json'))

    def test_completed_semantic_review_updates_readiness_without_creating_human_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            with zipfile.ZipFile(ROOT/submission.BASELINE_ARCHIVE) as archive:
                archive.extractall(directory)
            session=next(directory.glob('*/summary.json')).parent
            ratings=[]
            for path in sorted(session.glob('*/result.json')):
                case=json.loads((path.parent/'input.json').read_text())
                if case['phase']!='J2':continue
                result=json.loads(path.read_text())
                ratings.append({'run_id':result['run_id'],'reviewer_id':'synthetic-test',
                    'rationale':'Synthetic test labels, never published as human assessment.',
                    'claim_correct':{c['claim_id']:True for c in result['claims']},
                    'evidence_entailed':{c['claim_id']+'/'+e['fragment_id']:True for c in result['claims'] for e in c['evidence']},
                    'covered_gold_claim_ids':[],'accepted':False})
            path=directory/'synthetic-ratings.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in ratings))
            with patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')):
                reports=submission.build(baseline_ratings=path)
            self.assertTrue(reports['readiness.json']['baseline_semantic_ratings_complete'])
            self.assertNotIn('Complete semantic review of baseline answers',reports['readiness.json']['remaining'])
            self.assertFalse(reports['readiness.json']['coursework_complete'])

    def test_audit_rejects_modified_primary_labels(self):
        from shutil import copytree
        from labestra.runner import encoded
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            copytree(ROOT/'eval/review',root/'eval/review')
            copytree(ROOT/'eval/annotations',root/'eval/annotations')
            path=root/'eval/review/audit-24/annotations.primary.jsonl'
            values=[json.loads(line) for line in path.read_text().splitlines()]
            values[0]['label']='clarify' if values[0]['label']!='clarify' else 'search'
            path.write_text(''.join(json.dumps(r)+'\n' for r in values))
            provenance=root/'eval/review/audit-24/provenance.json'
            value=json.loads(provenance.read_text());value['files'][path.name]=digest(path)
            provenance.write_text(encoded(value))
            with self.assertRaisesRegex(ValueError,'primary annotation'):
                submission.supplementary_audit(root)

    def test_archive_path_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr(submission.PREFIX + '../outside.json', '{}')
            with self.assertRaises(ValueError):
                submission.extract_cohort(archive, directory / 'output')
            self.assertFalse((directory / 'outside.json').exists())
