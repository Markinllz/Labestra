"""REST protocol checks against recorded synthetic responses; never contact API."""

import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from labestra.llm import CompletionFailure, YandexModel


class AdapterTests(unittest.TestCase):
    def response(self, *, status='ALTERNATIVE_STATUS_FINAL', usage=None):
        return io.BytesIO(json.dumps({'result': {
            'alternatives': [{'status': status, 'message': {'role': 'assistant', 'text': '{}'}}],
            'usage': usage if usage is not None else {'inputTextTokens': '12', 'completionTokens': '3'},
            'modelVersion': 'test-version',
        }}).encode())

    def test_request_options_and_measured_usage(self):
        opener = Mock()
        opener.open.return_value = self.response()
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-test-key'}), \
             patch('labestra.llm.build_opener', return_value=opener):
            result = YandexModel('gpt://folder/model/version').complete(
                [{'role': 'user', 'text': 'Return JSON'}], max_tokens=100, temperature=0, timeout=2)
        self.assertEqual((result.input_tokens, result.output_tokens, result.model_version),
                         (12, 3, 'test-version'))
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body['completionOptions'], {'stream': False, 'temperature': 0, 'maxTokens': '100'})
        self.assertTrue(body['jsonObject'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 2)
        opener.open.assert_called_once()

    def test_http_failure_does_not_retry_or_expose_response_text(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('https://example.invalid', 503,
                                           'sensitive response text', {}, io.BytesIO(b'private body'))
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-test-key'}), \
             patch('labestra.llm.build_opener', return_value=opener), \
             self.assertRaises(CompletionFailure) as caught:
            YandexModel('gpt://folder/model/version').complete([], max_tokens=10, temperature=0, timeout=1)
        self.assertEqual(str(caught.exception), 'http_503')
        self.assertTrue(caught.exception.error.retryable)
        self.assertFalse(caught.exception.usage_known_zero)
        opener.open.assert_called_once()

    def test_missing_usage_stays_unknown(self):
        opener = Mock()
        opener.open.return_value = self.response(usage={})
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-test-key'}), \
             patch('labestra.llm.build_opener', return_value=opener):
            result = YandexModel('gpt://folder/model/version').complete([], max_tokens=10, temperature=0, timeout=1)
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)

    def test_truncated_completion_is_not_a_success(self):
        opener = Mock()
        opener.open.return_value = self.response(status='ALTERNATIVE_STATUS_TRUNCATED_FINAL')
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-test-key'}), \
             patch('labestra.llm.build_opener', return_value=opener), \
             self.assertRaises(CompletionFailure) as caught:
            YandexModel('gpt://folder/model/version').complete([], max_tokens=10, temperature=0, timeout=1)
        self.assertEqual(caught.exception.error.code, 'incomplete_completion')
        self.assertEqual(caught.exception.completion.input_tokens, 12)
        self.assertEqual(caught.exception.completion.output_tokens, 3)

    def test_truncated_result_charges_known_usage_but_never_publishes_partial_text(self):
        from dataclasses import replace
        from labestra.llm import Completion
        from labestra.schemas import Limits, ToolFailure
        from labestra.workflow_runtime import Runtime
        runtime = Runtime('truncated', replace(Limits(), currency='RUB', max_cost=1),
                          mode='live', pricing={'currency': 'RUB', 'input_per_1000': 1,
                                               'output_per_1000': 2, 'verified_on': '2026-11-23'})
        model = Mock(mode='live', model_id='gpt://synthetic/model/version')
        model.complete.side_effect = CompletionFailure('incomplete_completion',
            completion=Completion('partial invalid JSON', 12, 3, 'synthetic-version'))
        with self.assertRaises(ToolFailure):
            runtime.llm(model, [{'role': 'user', 'text': 'JSON'}],
                        max_tokens=10, temperature=0, timeout=1)
        model.complete.assert_called_once()
        self.assertEqual(runtime.usage().input_tokens, 12)
        self.assertEqual(runtime.usage().output_tokens, 3)
        self.assertAlmostEqual(runtime.usage().cost, .018)
        self.assertTrue(runtime.measurement_complete)
