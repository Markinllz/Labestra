"""One run-owned budget for both search and LLM, reusable by the common runner.

The runner creates Runtime once, wraps raw SearchTools once, and passes the same
object to WorkflowServices. This is the reference integration implementation,
not a second counter inside workflow. No persistence or baseline UI lives here.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math
from queue import Empty, Queue
from threading import Thread
import time

from .llm import Completion, CompletionFailure, input_bound
from .schemas import Event, Limits, ToolError, ToolFailure, Usage


class StopRun(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


def serial(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [serial(v) for v in value]
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    return value


class Runtime:
    def __init__(self, run_id: str, limits: Limits, *, mode='fixture', pricing=None,
                 cancelled=lambda: False, clock=time.monotonic):
        for name in ('max_steps', 'max_llm_calls', 'max_search_calls',
                     'max_input_tokens', 'max_output_tokens'):
            value = getattr(limits, name)
            if type(value) is not int or value < 0:
                raise ValueError('Invalid limit: ' + name)
        for value in (limits.timeout_seconds, limits.max_cost):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid time/cost limit')
        if mode not in {'fixture', 'live'} or not limits.currency:
            raise ValueError('Invalid mode/currency')
        self.run_id, self.limits, self.mode = run_id, limits, mode
        self.clock, self.cancelled = clock, cancelled
        self.started = clock()
        self.events = []
        self.steps = self.llm_calls = self.search_calls = 0
        self.read_calls = 0
        self.input_tokens = self.output_tokens = 0
        self.cost = Decimal('0')
        self.measurement_complete = True
        self.pricing = pricing or {}
        self.rate_in = self.rate_out = Decimal('0')
        if mode == 'live':
            if self.pricing.get('currency') != limits.currency:
                raise ValueError('Pricing/limits currency mismatch')
            for name in ('input_per_1000', 'output_per_1000'):
                value = self.pricing.get(name)
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Live pricing must be explicitly known and positive')
            if not self.pricing.get('verified_on'):
                raise ValueError('Live tariff verification date is required')
            self.rate_in = Decimal(str(self.pricing['input_per_1000']))
            self.rate_out = Decimal(str(self.pricing['output_per_1000']))

    @property
    def duration(self):
        return max(0., self.clock() - self.started)

    def emit(self, name, inputs=None, output=None, *, kind='state', error=None, duration=None):
        event = Event(self.run_id, self.steps, kind, name,
                      datetime.now(timezone.utc).isoformat(), serial(inputs or {}),
                      serial(output), error, duration)
        self.events.append(event)

    def checkpoint(self):
        if self.cancelled():
            raise StopRun('cancelled', 'user_cancelled')
        if self.duration >= self.limits.timeout_seconds:
            raise StopRun('budget_exceeded', 'timeout_seconds')
        if not self.measurement_complete:
            raise StopRun('failed', 'usage_unknown')

    def _reserve(self, kind, timeout, input_tokens=0, output_tokens=0):
        self.checkpoint()
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise StopRun('failed', 'invalid_timeout')
        checks = [('max_steps', self.steps + 1),
                  ('max_llm_calls', self.llm_calls + (kind == 'llm')),
                  ('max_search_calls', self.search_calls + (kind == 'search')),
                  ('max_input_tokens', self.input_tokens + input_tokens),
                  ('max_output_tokens', self.output_tokens + output_tokens)]
        for name, proposed in checks:
            if proposed > getattr(self.limits, name):
                raise StopRun('budget_exceeded', name)
        if kind == 'read' and self.read_calls >= 5:
            raise StopRun('budget_exceeded', 'max_read_calls')
        reserve = (input_tokens * self.rate_in + output_tokens * self.rate_out) / 1000
        if self.cost + reserve > Decimal(str(self.limits.max_cost)):
            raise StopRun('budget_exceeded', 'max_cost')
        if timeout > self.limits.timeout_seconds - self.duration:
            raise StopRun('budget_exceeded', 'timeout_seconds')
        self.steps += 1
        self.llm_calls += kind == 'llm'
        self.search_calls += kind == 'search'
        self.read_calls += kind == 'read'

    def _error(self, name, code, retryable, start):
        error = ToolError(code, code, retryable, f'{self.run_id}:{self.steps}')
        self.emit(name, kind='error', error=error, duration=self.clock() - start)
        return error

    def _unknown(self):
        self.measurement_complete = False

    def usage(self):
        return Usage(self.input_tokens if self.measurement_complete else None,
                     self.output_tokens if self.measurement_complete else None,
                     self.llm_calls, self.search_calls, self.steps,
                     float(self.cost) if self.measurement_complete else None,
                     self.limits.currency)

    def wrap_search(self, tools, *, timeout=30.):
        if hasattr(tools, 'runtime'):
            raise ValueError('SearchTools must be wrapped exactly once')
        # LocalSearch enforces its own finite deadline on the calling thread.
        actual_timeout = getattr(tools, '_timeout', timeout)
        return TrackedSearch(self, tools, actual_timeout)

    def tool(self, name, operation, inputs, *, timeout, kind='search'):
        for attempt in range(3):
            self._reserve(kind, timeout)
            start = self.clock()
            self.emit(name, inputs, kind='started')
            try:
                value = operation()
            except ToolFailure as exc:
                error = self._error(name, exc.error.code, exc.error.retryable, start)
                self.checkpoint()
                if error.retryable and attempt < 2:
                    continue
                raise ToolFailure(error) from None
            except Exception:
                error = self._error(name, 'tool_exception', False, start)
                raise ToolFailure(error) from None
            self.checkpoint()
            if self.clock() - start > timeout:
                error = self._error(name, 'tool_timeout', True, start)
                if attempt < 2:
                    continue
                raise ToolFailure(error)
            self.emit(name, output=value, kind='succeeded', duration=self.clock() - start)
            return value

    def llm(self, model, messages, *, max_tokens, temperature, timeout):
        if model.mode != self.mode:
            raise StopRun('failed', 'model_mode_mismatch')
        bound = input_bound(messages)
        for attempt in range(3):
            self._reserve('llm', timeout, bound, max_tokens)
            start = self.clock()
            self.emit('llm', {'model_id': model.model_id, 'messages': messages,
                             'max_tokens': max_tokens, 'temperature': temperature,
                             'input_token_bound': bound, 'timeout': timeout}, kind='started')
            queue = Queue(maxsize=1)
            def worker():
                try:
                    queue.put(model.complete(messages, max_tokens=max_tokens,
                                             temperature=temperature, timeout=timeout))
                except Exception as exc:
                    queue.put(exc)
            # Only network/model work is threaded. SQLite stays on its own thread.
            Thread(target=worker, daemon=True).start()
            while True:
                try:
                    self.checkpoint()
                except StopRun:
                    self._unknown()
                    self._error('llm', 'interrupted_usage_unknown', False, start)
                    raise
                if self.clock() - start >= timeout:
                    reply = CompletionFailure('llm_timeout', True)
                    break
                try:
                    reply = queue.get(timeout=min(.02, timeout))
                    break
                except Empty:
                    continue
            if isinstance(reply, Exception):
                known = isinstance(reply, CompletionFailure) and reply.usage_known_zero
                retryable = isinstance(reply, CompletionFailure) and reply.error.retryable
                code = reply.error.code if isinstance(reply, CompletionFailure) else 'llm_exception'
                if not known:
                    self._unknown()
                error = self._error('llm', code, retryable, start)
                self.checkpoint()  # Unknown charges forbid another paid attempt.
                if retryable and attempt < 2:
                    continue
                raise ToolFailure(error)
            if (not isinstance(reply, Completion) or
                not isinstance(reply.text, str) or
                any(type(v) is not int or v < 0 for v in (reply.input_tokens, reply.output_tokens))):
                self._unknown()
                self._error('llm', 'usage_unknown', False, start)
                raise StopRun('failed', 'usage_unknown')
            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
            self.cost += (reply.input_tokens * self.rate_in + reply.output_tokens * self.rate_out) / 1000
            self.emit('llm', output=asdict(reply), kind='succeeded', duration=self.clock() - start)
            self.checkpoint()  # Late responses cannot publish after cancellation.
            if reply.input_tokens > bound or reply.output_tokens > max_tokens:
                raise StopRun('failed', 'provider_exceeded_reservation')
            return reply.text


class TrackedSearch:
    def __init__(self, runtime, raw, timeout):
        self.runtime, self.raw, self.timeout = runtime, raw, timeout

    def search_sources(self, query, filters, collection_id):
        return self.runtime.tool('search_sources',
            lambda: self.raw.search_sources(query, filters, collection_id),
            {'query': query, 'filters': filters, 'collection_id': collection_id}, timeout=self.timeout)

    def search_fragments(self, query, corpus):
        return self.runtime.tool('search_fragments', lambda: self.raw.search_fragments(query, corpus),
                                 {'query': query, 'corpus': corpus}, timeout=self.timeout)

    def read_fragments(self, fragment_ids, corpus):
        return self.runtime.tool('read_fragments', lambda: self.raw.read_fragments(fragment_ids, corpus),
                                 {'fragment_ids': fragment_ids, 'corpus': corpus},
                                 timeout=self.timeout, kind='read')
