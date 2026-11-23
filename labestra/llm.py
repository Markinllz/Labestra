"""One completion adapter, no implicit retries. Offline fixtures never use a key."""

from dataclasses import dataclass
import json
import os
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

from .schemas import ToolError, ToolFailure


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model_version: str


class CompletionFailure(ToolFailure):
    """Unknown billing is the conservative default after a request was sent."""

    def __init__(self, code: str, retryable: bool = False, *, usage_known_zero=False,
                 completion: Completion | None = None):
        super().__init__(ToolError(code, code, retryable))
        self.usage_known_zero = usage_known_zero
        self.completion = completion


class LanguageModel(Protocol):
    mode: str
    model_id: str

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int,
                 temperature: float, timeout: float) -> Completion: ...


def input_bound(messages: list[dict[str, str]]) -> int:
    """Conservative UTF-8 byte bound + framing reserve, not measured model usage.

    The live runner must verify this policy against the selected model tokenizer.
    No tokenizer HTTP calls are hidden here.
    """
    return 128 + sum(len(m['text'].encode('utf-8')) + 32 for m in messages)


class FixtureModel:
    mode = 'fixture'
    model_id = 'fixture/scripted-v1'

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages, *, max_tokens, temperature, timeout):
        self.calls.append(messages)
        try:
            reply = next(self.replies)
        except StopIteration:
            raise CompletionFailure('fixture_exhausted', usage_known_zero=True) from None
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, Completion):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        # Explicit synthetic usage; never presented as a measurement of a real LLM.
        return Completion(text, input_bound(messages), min(max_tokens, len(text.encode('utf-8'))),
                          self.model_id)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class YandexModel:
    """Optional live transport. Merely importing/constructing it sends nothing.

    REST completion contract checked 2026-11-22; live access is not verified.
    Network deadline and cancellation are enforced by the shared Runtime.
    """

    mode = 'live'
    endpoint = 'https://ai.api.cloud.yandex.net/foundationModels/v1/completion'

    def __init__(self, model_id: str):
        if not isinstance(model_id, str) or not model_id.startswith('gpt://'):
            raise ValueError('An explicit Yandex model URI is required')
        self.model_id = model_id

    def complete(self, messages, *, max_tokens, temperature, timeout):
        key = os.environ.get('LABESTRA_API_KEY')
        if not key:
            raise CompletionFailure('credentials_missing', usage_known_zero=True)
        payload = json.dumps({
            'modelUri': self.model_id,
            'completionOptions': {'stream': False, 'temperature': temperature,
                                  'maxTokens': str(max_tokens)},
            'messages': messages, 'jsonObject': True,
        }, ensure_ascii=False).encode('utf-8')
        request = Request(self.endpoint, data=payload, method='POST', headers={
            'Authorization': 'Api-Key ' + key, 'Content-Type': 'application/json',
            'x-data-logging-enabled': 'false',
        })
        try:
            # No SDK retries, no redirect carrying an Authorization header elsewhere.
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CompletionFailure('response_too_large')
            result = json.loads(raw)['result']
            alternative = result['alternatives'][0]
            usage = result.get('usage', {})
            def tokens(name):
                value = usage.get(name)
                if value is None:
                    return None
                if isinstance(value, bool) or not str(value).isdigit():
                    raise ValueError('Invalid usage')
                return int(value)
            text = alternative.get('message', {}).get('text', '')
            if not isinstance(text, str):
                raise ValueError('Invalid completion')
            completion = Completion(text, tokens('inputTextTokens'), tokens('completionTokens'),
                                    str(result.get('modelVersion', 'unreported')))
            if alternative.get('status') != 'ALTERNATIVE_STATUS_FINAL':
                # A paid but truncated/filtered result is not a successful answer.
                # Preserve returned usage for billing; never regenerate implicitly.
                raise CompletionFailure('incomplete_completion', completion=completion)
            return completion
        except HTTPError as exc:
            # Do not log the body, request, headers or exception text.
            raise CompletionFailure('http_' + str(exc.code),
                                    exc.code in {429, 500, 502, 503, 504}) from None
        except (TimeoutError, socket.timeout):
            raise CompletionFailure('llm_timeout', True) from None
        except URLError:
            raise CompletionFailure('transport_error', True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise CompletionFailure('invalid_provider_response') from None


class ChatModel:
    """OpenAI-compatible Chat Completions, including Qwen in Yandex AI Studio.

    No SDK, redirects, implicit retries or credential persistence. max_tokens
    includes provider reasoning tokens when they are included in completion usage.
    Provider-specific reasoning switches are not assumed to be supported.
    """

    mode = 'live'

    def __init__(self, model_id, base_url='https://ai.api.cloud.yandex.net/v1',
                 *, folder_id=None):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError('Set an explicit model_id or LABESTRA_MODEL_ID')
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError('API base URL must be HTTPS without credentials/query/fragment')
        self.model_id, self.base_url = model_id, base_url.rstrip('/')
        self.is_yandex = parsed.hostname == 'ai.api.cloud.yandex.net'
        self.folder_id = folder_id
        if self.is_yandex:
            model_uri = urlsplit(model_id)
            if model_uri.scheme != 'gpt' or not model_uri.netloc or not model_uri.path.strip('/'):
                raise ValueError('Yandex requires gpt://<folder-id>/<model>')
            if folder_id and folder_id != model_uri.netloc:
                raise ValueError('Folder ID differs from model URI')
            self.folder_id = model_uri.netloc

    def complete(self, messages, *, max_tokens, temperature, timeout):
        key = os.environ.get('LABESTRA_API_KEY')
        if not key:
            raise CompletionFailure('credentials_missing', usage_known_zero=True)
        body = {'model': self.model_id,
                'messages': [{'role': m['role'], 'content': m['text']} for m in messages],
                'max_tokens': max_tokens, 'temperature': temperature, 'stream': False,
                'response_format': {'type': 'json_object'}}
        headers = {'Authorization': ('Api-Key ' if self.is_yandex else 'Bearer ') + key,
                   'Content-Type': 'application/json'}
        if self.is_yandex:
            headers.update({'OpenAI-Project': self.folder_id, 'x-data-logging-enabled': 'false'})
        request = Request(self.base_url + '/chat/completions',
                          data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                          method='POST', headers=headers)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CompletionFailure('response_too_large')
            value = json.loads(raw)
            choice = value['choices'][0]
            usage = value.get('usage') or {}
            def tokens(name):
                result = usage.get(name)
                if result is not None and (type(result) is not int or result < 0):
                    raise ValueError('Invalid usage')
                return result
            content = choice.get('message', {}).get('content')
            completion = Completion(content if isinstance(content, str) else '',
                                    tokens('prompt_tokens'), tokens('completion_tokens'),
                                    str(value.get('system_fingerprint') or 'unreported'))
            if choice.get('finish_reason') != 'stop':
                raise CompletionFailure('incomplete_completion', completion=completion)
            if not isinstance(content, str) or not content.strip():
                raise CompletionFailure('empty_completion', completion=completion)
            return completion
        except HTTPError as exc:
            raise CompletionFailure('http_' + str(exc.code),
                                    exc.code in {429, 500, 502, 503, 504}) from None
        except (TimeoutError, socket.timeout):
            raise CompletionFailure('llm_timeout', True) from None
        except URLError:
            raise CompletionFailure('transport_error', True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise CompletionFailure('invalid_provider_response') from None
