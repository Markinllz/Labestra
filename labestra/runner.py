"""Common local baseline/system runner. Gold is never opened here."""

import argparse
import codecs
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings

from .data import load_cases, load_corpora, load_fragments, load_sources, validate_dataset
from .llm import ChatModel, FixtureModel, YandexModel
from .schemas import Corpus, Limits, RunResult, ToolFailure
from .search import LocalSearch
from .workflow import WorkflowServices, run_system, strict_json, validate_draft
from .workflow_runtime import Runtime, StopRun

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', value):
        raise ValueError('ID must contain 1–120 ASCII letters, digits, dots, underscores or hyphens')
    return value


def check_public_config(value):
    """Configurations are saved with runs; credentials belong only in the environment."""
    secret_keys = {'api_key', 'apikey', 'authorization', 'password', 'secret', 'access_token', 'token'}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in secret_keys:
                raise ValueError('Credentials are not allowed in config; use LABESTRA_API_KEY')
            check_public_config(item)
    elif isinstance(value, list):
        for item in value:
            check_public_config(item)
    elif isinstance(value, str) and os.environ.get('LABESTRA_API_KEY') and os.environ['LABESTRA_API_KEY'] in value:
        raise ValueError('API key found in config; refusing to save it')


def prepare(root, config, cases_path):
    """ValueError stops preparation before an index or model is constructed."""
    root = Path(root).resolve()
    cases_path = Path(cases_path)
    if not cases_path.is_absolute():
        cases_path = root / cases_path
    allowed = [(root / f'eval/{split}/tasks.jsonl').resolve() for split in ('dev', 'visible')]
    if cases_path.resolve() not in allowed:
        raise ValueError('KT3 runner accepts only public dev/visible task files')
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter('always')
        validate_dataset(root)
        manifest = strict_json((root / 'data/manifest.json').read_text(encoding='utf-8-sig'))
        if config.get('collection_id') != manifest['collection_id']:
            raise ValueError('config.collection_id does not match data/manifest.json')
        sources = load_sources(root / 'data/sources.jsonl')
        fragments = load_fragments(root / 'data/fragments.jsonl')
        corpora = load_corpora(root / 'data/corpora.jsonl')
        cases = load_cases(cases_path)
    diagnostics = list(dict.fromkeys(str(w.message) for w in notices))
    tools = LocalSearch(sources=sources, fragments=fragments, config=config['search'],
                        collection_id=manifest['collection_id'])
    return tools, {c.corpus_revision_id: c for c in corpora}, cases, sources, manifest, diagnostics


class Console:
    """Human input with a deadline, including time spent waiting at a prompt."""

    def __init__(self, runtime, sources, tools, case, human_limit):
        self.rt, self.tools, self.case = runtime, tools, case
        self.sources = {s.source_id: s for s in sources}
        self.human_limit = human_limit
        self.active_seconds = 0.
        self._pending = ''
        self._decoder = codecs.getincrementaldecoder('utf-8')()

    def read(self, prompt):
        print(prompt, end='', flush=True)
        start = time.monotonic()
        try:
            while True:
                self.rt.checkpoint()
                if self.active_seconds + time.monotonic() - start >= self.human_limit:
                    raise StopRun('budget_exceeded', 'human_time_limit_seconds')
                if '\n' not in self._pending and select.select([sys.stdin], [], [], .1)[0]:
                    chunk = os.read(sys.stdin.fileno(), 4096)
                    if not chunk:
                        raise StopRun('cancelled', 'input_closed')
                    self._pending += self._decoder.decode(chunk)
                if '\n' in self._pending:
                    value, self._pending = self._pending.split('\n', 1)
                    value = value.rstrip('\r')
                    if value == '/cancel':
                        raise StopRun('cancelled', 'user_cancelled')
                    return value
        finally:
            self.active_seconds += time.monotonic() - start

    def __call__(self, event, payload):
        if event == 'confirm_plan':
            print(payload['plan'])
            return {'action': 'confirm'} if self.read('Подтвердить план? [yes/no]: ') == 'yes' else None
        if event == 'select_sources':
            print('Задача: выбери статьи, которые отвечают вопросу. Для чтения введи read и номера; для завершения — только номера.')
            ids = payload['source_ids']
            for i, sid in enumerate(ids, 1):
                source = self.sources[sid]
                print(f'{i}. {sid}: {source.title} [{source.availability}]')
            while True:
                value = self.read('Номера через запятую; read 1,2 — прочитать; /cancel: ')
                reading = value.startswith('read ')
                try:
                    numbers = [int(n.strip()) for n in (value[5:] if reading else value).split(',')]
                    if not numbers or any(n < 1 or n > len(ids) for n in numbers):
                        raise ValueError
                    chosen = list(dict.fromkeys(ids[n - 1] for n in numbers))
                except ValueError:
                    print('Укажите номера из выдачи.')
                    continue
                if not reading:
                    return {'action': 'select', 'source_ids': chosen}
                corpus = Corpus('b0-preview', self.case.collection_id, chosen)
                hits = self.tools.search_fragments(self.case.search_query, corpus)
                self.show_fragments(self.tools.read_fragments([f.fragment_id for f in hits], corpus))
        if event == 'baseline_answer':
            return self.guided_answer(payload['fragments'])
        if event == 'review_answer':
            print(encoded(payload['result']))
            return {'decision': self.read('Проверка автором: accept/reject (не независимая оценка): ')}
        return None

    @staticmethod
    def show_fragments(fragments):
        for number, fragment in enumerate(fragments, 1):
            print(f'\nФРАГМЕНТ {number} [{fragment.fragment_id}] source={fragment.source_id} {fragment.location}\n{fragment.text}\n')

    def text(self, prompt, *, optional=False):
        print(prompt + ' Заверши текст отдельной строкой с точкой (.).')
        while True:
            lines = []
            while True:
                line = self.read('> ')
                if line == '.':
                    break
                lines.append(line)
            value = '\n'.join(lines).strip()
            if value or optional:
                return value
            print('Нужно написать текст или отменить задание командой /cancel.')

    def choice(self, prompt, allowed):
        while True:
            value = self.read(prompt).strip()
            if value in allowed:
                return value
            print('Допустимые варианты: ' + ', '.join(allowed))

    def guided_answer(self, fragments):
        print('Задача: прочитай вопрос и отрывки, затем ответь своими словами. JSON и длинные ID вводить не нужно.')
        self.show_fragments(fragments)
        state = self.choice('Есть ли основания для ответа? 1 — да; 2 — недостаточно; 3 — нужно уточнить вопрос: ', ('1', '2', '3'))
        if state == '3':
            return {'action': 'clarify', 'text': self.text('Какое уточнение нужно спросить?')}
        answer = self.text('Напиши ответ' if state == '1' else 'Объясни, почему материалов недостаточно')
        if state == '2':
            return {'answer': answer, 'claims': [], 'limitations': [answer], 'evidence_status': 'insufficient_evidence'}
        count = int(self.choice('Сколько отдельных проверяемых выводов в ответе? Введи число от 1 до 50: ', tuple(str(n) for n in range(1, 51))))
        claims = []
        for number in range(1, count + 1):
            statement = self.text(f'Вывод {number}: одна проверяемая мысль из твоего ответа')
            while True:
                value = self.read('Номера подтверждающих фрагментов через запятую (например 1,3): ')
                try:
                    indexes = list(dict.fromkeys(int(v.strip()) for v in value.split(',')))
                    if not indexes or any(n < 1 or n > len(fragments) for n in indexes):
                        raise ValueError
                    break
                except ValueError:
                    print(f'Нужны номера от 1 до {len(fragments)}.')
            evidence = []
            for index in indexes:
                relation = self.choice(f'Фрагмент {index}: 1 — подтверждает вывод; 2 — противоречит; 3 — связь неясна: ', ('1', '2', '3'))
                evidence.append({'fragment_id': fragments[index - 1].fragment_id,
                                 'relation': {'1': 'supports', '2': 'contradicts', '3': 'uncertain'}[relation]})
            claims.append({'claim_id': f'c{number}', 'text': statement, 'evidence': evidence})
        limitation = self.text('Какие ограничения есть у ответа? Если нечего добавить, введи только точку', optional=True)
        value = {'answer': answer, 'claims': claims, 'limitations': [limitation] if limitation else [], 'evidence_status': 'sufficient'}
        print('Ответ и ссылки собраны автоматически из твоего ввода. Сохраняем результат.')
        return value


def run_baseline(case, tools, config, run_id, *, services):
    """Search/read + human answer. No model construction or completion."""
    rt = services.runtime
    ranked, corpus_id, claims, limitations, answer = [], None, [], [], ''
    evidence = 'not_assessed'
    def finish(status, reason):
        return RunResult(run_id, case.case_id, 'baseline', rt.mode, status, answer, claims,
                         limitations, evidence, reason, rt.usage(), rt.duration,
                         retrieved_source_ids=ranked, corpus_revision_id=corpus_id)
    try:
        rt.checkpoint()
        if not case.search_query.strip():
            answer = 'Уточните задачу и задайте английскую поисковую фразу.'
            return finish('needs_clarification', 'search_query_required')
        if case.phase == 'J1':
            hits = tools.search_sources(case.search_query, {}, case.collection_id)
            ranked = [s.source_id for s in hits]
            rt.emit('initial_ranking', output={'source_ids': ranked})
            if not ranked:
                limitations = ['Поиск не вернул источников.']
                evidence = 'insufficient_evidence'
                return finish('completed', 'empty_search')
            reply = services.interact('select_sources', {'source_ids': ranked}) or {}
            selected = reply.get('source_ids')
            if (reply.get('action') != 'select' or type(selected) is not list or not selected or
                    len(set(selected)) != len(selected) or not set(selected) <= set(ranked)):
                raise StopRun('failed', 'invalid_source_selection')
            corpus_id = 'b0-' + hashlib.sha256(encoded([run_id, selected]).encode()).hexdigest()[:24]
            rt.checkpoint()
            corpus = Corpus(corpus_id, case.collection_id, selected)
            services.created_corpora.append(corpus)
            rt.emit('corpus_selected', output=corpus)
            answer = f'Выбрано источников: {len(selected)}.'
            return finish('completed', 'corpus_selected')
        corpus = services.corpora.get(case.corpus_revision_id)
        if corpus is None or corpus.collection_id != case.collection_id:
            raise StopRun('failed', 'invalid_corpus')
        corpus_id = corpus.corpus_revision_id
        hits = tools.search_fragments(case.search_query, corpus)
        exact = tools.read_fragments([f.fragment_id for f in hits], corpus)
        if not exact:
            answer, limitations, evidence = 'Недостаточно оснований.', ['Фрагменты не найдены.'], 'insufficient_evidence'
            return finish('completed', 'insufficient_evidence')
        value = services.interact('baseline_answer', {'fragments': exact})
        rt.checkpoint()
        if isinstance(value, dict) and value.get('action') == 'clarify':
            answer = value.get('text', '')
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('Clarification text is required')
            rt.emit('human_clarification', output={'text': answer})
            return finish('needs_clarification', 'human_requested_clarification')
        claims = validate_draft(value, exact)
        answer, limitations, evidence = value['answer'], value['limitations'], value['evidence_status']
        rt.emit('human_answer', output=value)
        return finish('completed', 'draft_ready' if evidence == 'sufficient' else 'insufficient_evidence')
    except StopRun as stop:
        return finish(stop.status, stop.reason)
    except ToolFailure as exc:
        return finish('failed', exc.error.code)
    except (ValueError, TypeError, KeyError):
        return finish('failed', 'invalid_human_answer')


def run_case(case, tools, config, run_id, variant, *, services):
    if variant == 'baseline':
        return run_baseline(case, tools, config, run_id, services=services)
    if variant == 'system':
        return run_system(case, tools, config, run_id, services=services)
    raise ValueError('Unknown variant')


def publish(directory, payload, cancelled=lambda: False):
    """Atomic local publication; identical retry has no side effects."""
    directory = Path(directory)
    if directory.exists():
        if (set(p.name for p in directory.iterdir()) == set(payload) and
                all((directory / name).read_text(encoding='utf-8') == body for name, body in payload.items())):
            return False
        raise ValueError('Result already exists with different content; choose another session ID')
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pending-', dir=directory.parent) as pending:
        stage = Path(pending) / 'result'
        stage.mkdir()
        for name, body in payload.items():
            if name != Path(name).name:
                raise ValueError('Invalid artifact name')
            (stage / name).write_text(body, encoding='utf-8')
        if cancelled():
            raise StopRun('cancelled', 'cancelled_before_publication')
        stage.rename(directory)
    return True


def make_model(config, phase):
    if config['mode'] == 'fixture':
        from .workflow_demo import technical_reply
        return FixtureModel([{'action': 'plan', 'text': 'Технический fixture: поиск и выбор корпуса.'}]
                            if phase == 'J1' else [technical_reply])
    settings = config['model']
    if settings.get('api_format') == 'chat_completions':
        return ChatModel(settings['model_id'], settings.get('base_url', 'https://ai.api.cloud.yandex.net/v1'),
                         folder_id=settings.get('folder_id'))
    return YandexModel(settings['model_id'])


def execute(args, *, root=ROOT):
    root = Path(root)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = strict_json(config_path.read_text(encoding='utf-8'))
    check_public_config(config)
    if config.get('mode') not in ('live', 'fixture'):
        raise ValueError('Explicit mode live|fixture is required')
    if args.fixture_actions and config['mode'] != 'fixture':
        raise ValueError('Scripted actions are only allowed in fixture mode')
    if not args.fixture_actions and not args.participant:
        raise ValueError('Human sessions require --participant')
    if args.variant == 'baseline' and not args.fixture_actions and not args.blind_attestation:
        raise ValueError('B0 requires --blind-attestation: confirm no prior gold access')
    human_limit = config.get('baseline', {}).get('human_time_limit_seconds')
    if type(human_limit) not in (int, float) or not math.isfinite(human_limit) or human_limit <= 0:
        raise ValueError('Set a positive baseline.human_time_limit_seconds before comparison')
    raw, corpora, cases, sources, manifest, diagnostics = prepare(root, config, args.cases)
    if args.case_id:
        wanted = set(args.case_id)
        if not wanted <= {c.case_id for c in cases}:
            raise ValueError('Unknown --case-id')
        cases = [c for c in cases if c.case_id in wanted]
    if args.fixture_actions and any(c.split != 'dev' for c in cases):
        raise ValueError('Scripted fixtures are limited to dev; no synthetic scientific evaluation')
    limits = Limits(**config['limits'])
    session_id = safe_id(args.session_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output = Path(args.output) / session_id
    if output.exists():
        raise ValueError('Session already exists; no model calls or overwrites were made')
    if args.variant == 'system' and config['mode'] == 'live':
        if not os.environ.get('LABESTRA_API_KEY'):
            raise ValueError('LABESTRA_API_KEY is missing; no API call made')
        if config.get('workflow', {}).get('token_bound_verified') is not True:
            raise ValueError('Verify tokenizer/context policy before setting token_bound_verified')
        make_model(config, cases[0].phase)
        session_budget = config.get('session_max_cost')
        if type(session_budget) not in (int, float) or not math.isfinite(session_budget) or session_budget <= 0:
            raise ValueError('Set positive session_max_cost before paid calls')
    files = sorted((root / 'labestra').glob('*.py')) + sorted((root / 'prompts').glob('*.txt'))
    code_hashes = {str(p.relative_to(root)): digest(p) for p in files}
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
                              text=True, check=False).stdout.strip() or 'unknown'
    stopped = [False]
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
    results = []
    known_session_cost = 0.
    halt_reason = None
    try:
        for original in cases:
            if stopped[0]:
                break
            case_limits = limits
            if args.variant == 'system' and config['mode'] == 'live':
                remaining = max(0., config['session_max_cost'] - known_session_cost)
                if remaining <= 0:
                    halt_reason = 'session_max_cost'
                    break
                case_limits = replace(limits, max_cost=min(limits.max_cost, remaining))
            case = replace(original, limits=case_limits)
            run_id = safe_id(session_id + '-' + case.case_id)
            # B0 has no paid model calls; fixture Runtime supplies measured zero cost.
            runtime_mode = config['mode'] if args.variant == 'system' else 'fixture'
            rt = Runtime(run_id, case_limits, mode=runtime_mode, pricing=config.get('pricing'),
                         cancelled=lambda: stopped[0])
            tools = rt.wrap_search(raw)
            console = Console(rt, sources, tools, case, human_limit)
            if args.fixture_actions:
                from .workflow_demo import fixture_interaction, technical_reply
                def interact(event, payload):
                    if event == 'baseline_answer':
                        return technical_reply([{'text': json.dumps({'fragments': [asdict(f) for f in payload['fragments']]})}])
                    return fixture_interaction(event, payload)
            else:
                interact = console
            model = make_model(config, case.phase) if args.variant == 'system' else None
            services = WorkflowServices(rt, model, corpora, interact)
            print(f'\n{case.case_id} [{case.phase}] {case.question}\nsearch_query: {case.search_query}', flush=True)
            result = run_case(case, tools, config, run_id, args.variant, services=services)
            if args.variant == 'system' and not args.fixture_actions and result.status == 'completed':
                try:
                    review = console('review_answer', {'result': asdict(result)})
                    rt.emit('author_review', output=review)
                except StopRun as stop:
                    result = replace(result, status=stop.status, stop_reason=stop.reason)
            result_mode = 'fixture' if args.fixture_actions else ('live' if args.variant == 'baseline' else config['mode'])
            result = replace(result, mode=result_mode, duration_seconds=rt.duration,
                             human_active_seconds=None if args.fixture_actions else console.active_seconds)
            run_manifest = {
                'run_id': run_id, 'session_id': session_id, 'variant': args.variant, 'mode': result_mode,
                'participant': args.participant, 'blind_attestation': bool(args.blind_attestation),
                'interaction_interface': 'scripted-fixture' if args.fixture_actions else 'guided-console-v1',
                'scientific_evaluation': not args.fixture_actions and (args.variant == 'baseline' or config['mode'] == 'live'),
                'usage_kind': 'synthetic_fixture' if args.fixture_actions or (args.variant == 'system' and config['mode'] == 'fixture') else 'measured',
                'dataset_version': manifest['dataset_version'], 'data_manifest_sha256': digest(root / 'data/manifest.json'),
                'code_revision': revision, 'file_sha256': code_hashes, 'config_sha256': digest(config_path),
                'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                'model_id': model.model_id if model else None, 'temperature': config.get('workflow', {}).get('temperature'),
                'seed': None, 'seed_note': 'Provider adapter has no seed parameter; exact repeated text is not guaranteed.',
                'parameters': config, 'diagnostics': diagnostics,
            }
            payload = {'input.json': encoded(asdict(case)), 'config.json': encoded(config),
                       'result.json': encoded(asdict(result)), 'manifest.json': encoded(run_manifest),
                       'corpora.json': encoded([asdict(c) for c in services.created_corpora]),
                       'events.jsonl': ''.join(json.dumps(asdict(e), ensure_ascii=False, allow_nan=False) + '\n' for e in rt.events)}
            if stopped[0] or rt.duration >= limits.timeout_seconds:
                result = replace(result, status='cancelled' if stopped[0] else 'budget_exceeded',
                                 stop_reason='user_cancelled' if stopped[0] else 'timeout_seconds',
                                 claims=[], corpus_revision_id=None)
                payload['result.json'], payload['corpora.json'] = encoded(asdict(result)), encoded([])
            try:
                publish(output / case.case_id, payload,
                        cancelled=lambda: stopped[0] and result.status == 'completed')
            except StopRun:
                result = replace(result, status='cancelled', stop_reason='cancelled_before_publication',
                                 claims=[], corpus_revision_id=None)
                payload['result.json'], payload['corpora.json'] = encoded(asdict(result)), encoded([])
                publish(output / case.case_id, payload)
            results.append(asdict(result))
            print(f'{result.status}: {result.stop_reason}', flush=True)
            if args.variant == 'system' and config['mode'] == 'live':
                if result.usage.cost is None:
                    halt_reason = 'usage_unknown: reconcile provider billing before another paid session'
                    break
                known_session_cost += result.usage.cost
        summary = {'session_id': session_id, 'planned': len(cases),
                   'planned_case_ids': [c.case_id for c in cases], 'recorded': len(results),
                   'interrupted': stopped[0], 'halt_reason': halt_reason, 'results': results,
                   'note': 'Semantic quality and acceptance require independent human evaluation.'}
        output.mkdir(parents=True, exist_ok=True)
        (output / 'summary.json').write_text(encoded(summary), encoding='utf-8')
        print(f'Результаты: {output.resolve()}')
        return int(stopped[0] or halt_reason is not None or any(r['status'] in ('failed', 'cancelled', 'budget_exceeded') for r in results))
    finally:
        signal.signal(signal.SIGINT, previous)


def main():
    parser = argparse.ArgumentParser(description='Labestra: common baseline/system runner; /cancel or Ctrl-C stops a session')
    parser.add_argument('--variant', choices=('baseline', 'system'), required=True)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--config', default='configs/default.json')
    parser.add_argument('--output', default='runs/local')
    parser.add_argument('--session-id')
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--participant')
    parser.add_argument('--blind-attestation', action='store_true')
    parser.add_argument('--fixture-actions', action='store_true', help='Technical dev-only scripts; no scientific scores')
    args = parser.parse_args()
    try:
        return execute(args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Подготовка/запись остановлена: {exc}\n')
