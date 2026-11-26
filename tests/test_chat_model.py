"""Synthetic HTTP protocol tests for Qwen's Chat Completions endpoint."""

import io
import json
import unittest
from unittest.mock import Mock, patch

from labestra.llm import ChatModel, CompletionFailure


class ChatTests(unittest.TestCase):
    def response(self, **changes):
        value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"test"}'}}],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}, 'system_fingerprint': 'test-backend'}
        value.update(changes)
        return io.BytesIO(json.dumps(value).encode())

    def call(self, response):
        opener = Mock()
        opener.open.return_value = response
        with patch.dict('os.environ', {'LABESTRA_API_KEY': 'test-secret'}), patch('labestra.llm.build_opener', return_value=opener):
            result = ChatModel('gpt://test-folder/qwen3.6-35b-a3b').complete(
                [{'role': 'system', 'text': 'Return JSON'}, {'role': 'user', 'text': 'Test'}],
                max_tokens=100, temperature=0, timeout=2)
        return result, opener.open.call_args

    def test_yandex_endpoint_headers_json_and_usage(self):
        result, call = self.call(self.response())
        request = call.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://ai.api.cloud.yandex.net/v1/chat/completions')
        self.assertEqual(request.get_header('Openai-project'), 'test-folder')
        self.assertEqual(request.get_header('Authorization'), 'Api-Key test-secret')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['messages'][1], {'role': 'user', 'content': 'Test'})
        self.assertEqual(body['max_tokens'], 100)
        self.assertFalse(body['stream'])
        self.assertEqual((result.input_tokens, result.output_tokens, result.model_version), (10, 5, 'test-backend'))
        self.assertNotIn('test-secret', json.dumps(body))

    def test_length_finish_preserves_total_output_including_reasoning(self):
        with self.assertRaises(CompletionFailure) as caught:
            self.call(self.response(choices=[{'finish_reason': 'length', 'message': {'content': ''}}],
                usage={'prompt_tokens': 12, 'completion_tokens': 100, 'completion_tokens_details': {'reasoning_tokens': 90}}))
        self.assertEqual(caught.exception.completion.output_tokens, 100)
        self.assertEqual(caught.exception.error.code, 'incomplete_completion')

    def test_missing_usage_is_unknown_not_zero(self):
        result, _ = self.call(self.response(usage={}))
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)

    def test_no_fingerprint_is_recorded_as_unreported(self):
        result, _ = self.call(self.response(system_fingerprint=None))
        self.assertEqual(result.model_version, 'unreported')

    def test_rejects_unsafe_urls_and_folder_mismatch(self):
        for url in ['http://example.com/v1', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret']:
            with self.assertRaises(ValueError):
                ChatModel('model', url)
        with self.assertRaises(ValueError):
            ChatModel('gpt://one/model', folder_id='two')

    def test_missing_key_is_known_zero_without_network(self):
        with patch.dict('os.environ', {}, clear=True), patch('labestra.llm.build_opener') as opener:
            with self.assertRaises(CompletionFailure) as caught:
                ChatModel('gpt://folder/model').complete([], max_tokens=10, temperature=0, timeout=1)
            self.assertTrue(caught.exception.usage_known_zero)
            opener.assert_not_called()
