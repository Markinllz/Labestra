from copy import deepcopy
import unittest
from labestra.comparison import compare


class ComparisonTests(unittest.TestCase):
    def report(self):
        return {'session_id':'s', 'gold_sha256':'gold', 'quality':{'accepted':{'assessed_cases':0,'mean':None}},
                'duration_seconds':{'n':1,'median':1,'p95':1}, 'total_cost':0., 'currency':['USD'],
                'records':[{'case_id':'case', 'phase':'J2', 'category':'direct', 'status':'completed',
                            'input_question':'q','search_query':'query','input_corpus_revision_id':'c',
                            'config_sha256':'same','duration_seconds':1,'action_correct':True}]}

    def test_unrated_baseline_semantics_are_not_fabricated(self):
        system=self.report();baseline=deepcopy(system)
        result=compare(system,baseline)
        self.assertEqual(result['paired_cases'],1)
        self.assertIsNone(result['quality']['accepted']['baseline']['mean'])
        self.assertEqual(result['semantic_baseline_assessments'],0)

    def test_input_and_config_mismatches_block_comparison(self):
        for field in ['input_question','search_query','config_sha256','input_corpus_revision_id']:
            left=self.report();right=deepcopy(left);right['records'][0][field]='changed'
            with self.assertRaises(ValueError):compare(left,right)
        left=self.report();right=deepcopy(left);right['records'][0]['case_id']='another'
        with self.assertRaises(ValueError):compare(left,right)
