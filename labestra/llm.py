"""One completion adapter, no implicit retries. Offline fixtures never use a key."""

from dataclasses import dataclass
import json
import os
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .schemas import ToolError, ToolFailure


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model_version: str


class CompletionFailure(ToolFailure):
    """Unknown billing is the conservative default after a request was sent."""

    def __init__(self, code: str, retryable: bool = False, *, usage_known_zero=False):
        super().__init__(ToolError(code, code, retryable))
        self.usage_known_zero = usage_known_zero


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
            if alternative.get('status') != 'ALTERNATIVE_STATUS_FINAL':
                raise CompletionFailure('incomplete_completion')
            usage = result.get('usage', {})
            def tokens(name):
                value = usage.get(name)
                if value is None:
                    return None
                if isinstance(value, bool) or not str(value).isdigit():
                    raise ValueError('Invalid usage')
                return int(value)
            text = alternative['message']['text']
            if not isinstance(text, str):
                raise ValueError('Invalid completion')
            return Completion(text, tokens('inputTextTokens'), tokens('completionTokens'),
                              str(result.get('modelVersion', 'unreported')))
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
