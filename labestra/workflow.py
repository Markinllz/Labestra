"""Participant 3: bounded J1/J2 using the runner's single execution budget."""

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
from typing import Callable

from .llm import LanguageModel, input_bound
from .schemas import Case, Claim, Corpus, Evidence, Fragment, RunResult, ToolFailure
from .search import SearchTools
from .workflow_runtime import Runtime, StopRun

PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'


@dataclass
class WorkflowServices:
    runtime: Runtime
    model: LanguageModel
    corpora: dict[str, Corpus]
    interact: Callable[[str, dict], dict | None] = lambda event, payload: None
    created_corpora: list[Corpus] = field(default_factory=list)


class Revision(Exception):
    def __init__(self, case):
        self.case = case


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError('non_finite_json')
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)
    except (json.JSONDecodeError, TypeError, RecursionError):
        raise ValueError('invalid_json') from None
    if type(value) is not dict:
        raise ValueError('json_object_required')
    return value


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_draft(value: dict, fragments: list[Fragment]):
    if set(value) != {'answer', 'claims', 'limitations', 'evidence_status'}:
        raise ValueError('draft_fields')
    if not nonempty(value['answer']):
        raise ValueError('empty_answer')
    if (type(value['limitations']) is not list or
        any(not nonempty(v) for v in value['limitations'])):
        raise ValueError('invalid_limitations')
    if value['evidence_status'] not in ('sufficient', 'insufficient_evidence'):
        raise ValueError('invalid_evidence_status')
    if type(value['claims']) is not list or len(value['claims']) > 50:
        raise ValueError('invalid_claims')
    allowed, seen, claims = {f.fragment_id for f in fragments}, set(), []
    for claim in value['claims']:
        if type(claim) is not dict or set(claim) != {'claim_id', 'text', 'evidence'}:
            raise ValueError('invalid_claim')
        cid = claim['claim_id']
        if not nonempty(cid) or cid in seen or not nonempty(claim['text']):
            raise ValueError('invalid_claim_id_or_text')
        seen.add(cid)
        if type(claim['evidence']) is not list or not claim['evidence']:
            raise ValueError('evidence_required')
        evidence, referenced = [], set()
        for item in claim['evidence']:
            if type(item) is not dict or set(item) != {'fragment_id', 'relation'}:
                raise ValueError('invalid_evidence')
            fid, relation = item['fragment_id'], item['relation']
            if not isinstance(fid, str) or fid not in allowed or fid in referenced:
                raise ValueError('evidence_not_in_context_or_duplicate')
            if relation not in ('supports', 'contradicts', 'uncertain'):
                raise ValueError('invalid_relation')
            referenced.add(fid)
            evidence.append(Evidence(fid, relation))
        claims.append(Claim(cid, claim['text'], evidence))
    if value['evidence_status'] == 'sufficient' and not claims:
        raise ValueError('sufficient_requires_claims')
    if value['evidence_status'] == 'insufficient_evidence' and not value['limitations']:
        raise ValueError('insufficiency_requires_limitation')
    return claims


def run_system(case: Case, tools: SearchTools, config: dict, run_id: str, *,
               services: WorkflowServices | None = None) -> RunResult:
    """Four original inputs plus explicit in-process services from the runner.

    Config remains serializable. No key, callback or mutable runtime is put in it.
    """
    if services is None:
        raise ValueError('Runner must supply WorkflowServices; see docs/WORKFLOW.md')
    rt = services.runtime
    if (rt.run_id != run_id or rt.limits != case.limits or
        getattr(tools, 'runtime', None) is not rt):
        raise ValueError('Use one run_id, resolved Limits and Runtime for search and LLM')
    ranked, selected_id, limitations = [], None, []
    repairs_left = 1
    settings = config.get('workflow', {}) if type(config) is dict else None

    def finish(status, reason, answer='', claims=None, evidence='not_assessed'):
        result = RunResult(run_id, case.case_id, 'system', rt.mode, status, answer,
                           claims or [], limitations.copy(), evidence, reason, rt.usage(),
                           rt.duration, retrieved_source_ids=ranked.copy(),
                           corpus_revision_id=selected_id)
        rt.emit('workflow_finished', output={'status': status, 'stop_reason': reason,
                                           'measurement_complete': rt.measurement_complete})
        return result

    def interact(event, payload=None):
        rt.checkpoint()
        reply = services.interact(event, {'case_id': case.case_id, 'question': case.question,
                                         'search_query': case.search_query, **(payload or {})})
        rt.emit(event, output=reply)
        rt.checkpoint()
        if reply is not None and type(reply) is not dict:
            raise StopRun('failed', 'invalid_interaction')
        if reply and reply.get('action') == 'cancel':
            raise StopRun('cancelled', 'user_cancelled')
        if reply and reply.get('action') == 'revise':
            if not nonempty(reply.get('question')) or not isinstance(reply.get('search_query'), str):
                raise StopRun('failed', 'revision_requires_question_and_query')
            revised = replace(case, question=reply['question'], search_query=reply['search_query'],
                              corpus_revision_id=reply.get('corpus_revision_id', case.corpus_revision_id),
                              history=[])
            rt.emit('input_revised', output={'question': revised.question,
                    'search_query': revised.search_query, 'corpus_revision_id': revised.corpus_revision_id})
            raise Revision(revised)
        return reply or {}

    def messages_for(name, payload):
        return [{'role': 'system', 'text': prompts[name]},
                {'role': 'user', 'text': json.dumps(payload, ensure_ascii=False)}]

    def generate(name, payload, validator):
        nonlocal repairs_left
        messages = messages_for(name, payload)
        if input_bound(messages) > context_limit:
            raise StopRun('failed', 'context_overflow')
        while True:
            interact('before_llm')
            text = rt.llm(services.model, messages, max_tokens=output_limit,
                          temperature=temperature, timeout=llm_timeout)
            interact('after_llm')
            try:
                value = strict_json(text)
                validator(value)
                return value
            except (ValueError, TypeError, KeyError):
                if not repairs_left:
                    raise StopRun('failed', 'invalid_model_output') from None
                repairs_left -= 1
                rt.emit('format_repair', output={'reason': 'invalid_json_or_evidence'})
                repair_payload = {**payload, 'repair': 'Regenerate JSON matching the schema and evidence IDs.'}
                messages = messages_for(name, repair_payload)
                if input_bound(messages) > context_limit:
                    raise StopRun('failed', 'context_overflow')

    def valid_plan(value):
        if (set(value) != {'action', 'text'} or
            value['action'] not in ('plan', 'clarify', 'refuse') or not nonempty(value['text'])):
            raise ValueError('invalid_plan')

    try:
        if type(settings) is not dict:
            raise StopRun('failed', 'invalid_workflow_config')
        context_limit = settings.get('max_input_tokens_per_call', 8000)
        output_limit = settings.get('max_output_tokens_per_call', 1500)
        window = settings.get('context_window_tokens', 16000)
        llm_timeout = settings.get('llm_timeout_seconds', 30.)
        temperature = settings.get('temperature', 0.)
        if (any(type(v) is not int or v <= 0 for v in (context_limit, output_limit, window)) or
            context_limit + output_limit > window or
            type(temperature) not in (int, float) or not math.isfinite(temperature) or
            not 0 <= temperature <= 1):
            raise StopRun('failed', 'invalid_workflow_config')
        if rt.mode == 'live' and settings.get('token_bound_verified') is not True:
            raise StopRun('failed', 'token_bound_not_verified')
        prompts = {name: (PROMPTS / (name + '.txt')).read_text(encoding='utf-8')
                   for name in ('plan', 'answer')}
        rt.emit('prompt_manifest', output={name: {'text': text, 'sha256':
            hashlib.sha256(text.encode('utf-8')).hexdigest()} for name, text in prompts.items()})
        for revision in range(21):
            ranked, selected_id, limitations = [], None, []
            try:
                interact('start_revision', {'revision': revision})
                if (not nonempty(case.question) or len(case.question) > 10000 or
                    not isinstance(case.search_query, str) or case.phase not in ('J1', 'J2')):
                    raise StopRun('failed', 'invalid_case')
                if case.split == 'holdout':
                    raise StopRun('failed', 'holdout_disabled_for_kt3')
                if not case.search_query.strip():
                    return finish('needs_clarification', 'search_query_required',
                                  'Уточните задачу и задайте английскую поисковую фразу.')
                payload = {'question': case.question, 'search_query': case.search_query,
                           'history': case.history}
                if input_bound(messages_for('plan', payload)) > context_limit:
                    payload['history'] = []
                    limitations.append('История исключена из контекста из-за лимита.')
                if case.phase == 'J1':
                    plan = generate('plan', payload, valid_plan)
                    if plan['action'] == 'clarify':
                        return finish('needs_clarification', 'model_requested_clarification', plan['text'])
                    if plan['action'] == 'refuse':
                        limitations.append(plan['text'])
                        return finish('completed', 'outside_scope', plan['text'], evidence='insufficient_evidence')
                    reply = interact('confirm_plan', {'plan': plan['text']})
                    if reply.get('action') != 'confirm':
                        return finish('needs_clarification', 'awaiting_confirmation', plan['text'])
                    interact('before_search')
                    sources = tools.search_sources(case.search_query, {}, case.collection_id)
                    ranked = [s.source_id for s in sources]
                    if len(ranked) > 20 or len(set(ranked)) != len(ranked):
                        raise StopRun('failed', 'invalid_source_search_result')
                    rt.emit('initial_ranking', output={'source_ids': ranked})
                    if not ranked:
                        limitations.append('Поиск не вернул источников по заданной фразе.')
                        interact('before_publish')
                        return finish('completed', 'empty_search', 'Источники не найдены.',
                                      evidence='insufficient_evidence')
                    reply = interact('select_sources', {'source_ids': ranked})
                    ids = reply.get('source_ids')
                    if reply.get('action') != 'select':
                        return finish('needs_clarification', 'awaiting_selection', 'Выберите источники корпуса.')
                    if (type(ids) is not list or not ids or any(not nonempty(i) for i in ids) or
                        len(set(ids)) != len(ids) or not set(ids) <= set(ranked)):
                        raise StopRun('failed', 'invalid_source_selection')
                    digest = hashlib.sha256(json.dumps([run_id, revision, case.question, ids],
                                                       ensure_ascii=False).encode()).hexdigest()
                    corpus = Corpus('corpus-' + digest[:24], case.collection_id, ids.copy())
                    interact('before_publish')
                    selected_id = corpus.corpus_revision_id
                    services.created_corpora.append(corpus)
                    rt.emit('corpus_selected', output=corpus)
                    return finish('completed', 'corpus_selected', f'Выбрано источников: {len(ids)}.')
                corpus = services.corpora.get(case.corpus_revision_id)
                if (corpus is None or corpus.corpus_revision_id != case.corpus_revision_id or
                    corpus.collection_id != case.collection_id):
                    raise StopRun('failed', 'invalid_corpus')
                selected_id = corpus.corpus_revision_id
                interact('before_search')
                hits = tools.search_fragments(case.search_query, corpus)
                ids = [f.fragment_id for f in hits]
                if len(ids) > 10 or len(set(ids)) != len(ids):
                    raise StopRun('failed', 'invalid_fragment_search_result')
                if not hits:
                    limitations.append('Поиск не нашёл подходящих фрагментов в заданном корпусе.')
                    interact('before_publish')
                    return finish('completed', 'insufficient_evidence', 'Недостаточно оснований.',
                                  evidence='insufficient_evidence')
                interact('before_read')
                exact = tools.read_fragments(ids, corpus)
                if exact != hits or any(f.source_id not in corpus.source_ids for f in exact):
                    raise StopRun('failed', 'invalid_read_result')
                payload.update({'corpus_revision_id': selected_id, 'fragments': []})
                payload['history'] = []
                included = []
                for fragment in exact:
                    entry = {'fragment_id': fragment.fragment_id, 'source_id': fragment.source_id,
                             'location': fragment.location, 'snapshot_sha256': fragment.snapshot_sha256,
                             'text': fragment.text}
                    candidate = {**payload, 'fragments': payload['fragments'] + [entry]}
                    if input_bound(messages_for('answer', candidate)) + 128 <= context_limit:
                        payload = candidate
                        included.append(fragment)
                if len(included) < len(exact):
                    limitations.append('Часть целых фрагментов исключена по лимиту контекста.')
                rt.emit('context_selected', output={'included': [f.fragment_id for f in included],
                        'excluded': [f.fragment_id for f in exact if f not in included]})
                if not included:
                    limitations.append('Основания не помещаются в контекст; требуется сузить вопрос.')
                    interact('before_publish')
                    return finish('completed', 'insufficient_evidence', 'Недостаточно доступного контекста.',
                                  evidence='insufficient_evidence')
                value = generate('answer', payload, lambda v: validate_draft(v, included))
                claims = validate_draft(value, included)
                limitations.extend(value['limitations'])
                interact('before_publish')
                return finish('completed', 'draft_ready' if value['evidence_status'] == 'sufficient'
                              else 'insufficient_evidence', value['answer'], claims, value['evidence_status'])
            except Revision as changed:
                case = changed.case
                continue
        raise StopRun('budget_exceeded', 'max_revisions')
    except StopRun as stop:
        return finish(stop.status, stop.reason)
    except ToolFailure as exc:
        return finish('failed', exc.error.code)
    except (OSError, ValueError, TypeError, KeyError):
        return finish('failed', 'invalid_configuration_or_input')
