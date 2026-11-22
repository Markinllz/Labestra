"""Behavioral fixtures only: no network, credentials, gold or quality scores."""

from dataclasses import replace
import json
import threading
import time
import unittest
from unittest.mock import patch

from labestra.llm import Completion, CompletionFailure, FixtureModel, YandexModel
from labestra.schemas import Case, Corpus, Fragment, Limits, Source, ToolError, ToolFailure
from labestra.workflow import WorkflowServices, run_system, strict_json, validate_draft
from labestra.workflow_runtime import Runtime

FRAGMENT = Fragment('f1', 's1', 'The trial used a fixed corpus.', 'chars:0:30', 'a' * 64)
CORPUS = Corpus('corpus1', 'collection1', ['s1'])
PLAN = {'action': 'plan', 'text': 'Search the fixed collection.'}


def draft(fid='f1'):
    return {'answer': 'A fixed corpus was used.', 'claims': [{'claim_id': 'c1',
            'text': 'The trial used a fixed corpus.', 'evidence': [
                {'fragment_id': fid, 'relation': 'supports'}]}],
            'limitations': [], 'evidence_status': 'sufficient'}


def make_case(phase='J2', **kwargs):
    return replace(Case('dev-test', phase, 'dev', 'direct', 'group1',
                        'What corpus was used?', 'collection1', 'fixed corpus',
                        'corpus1' if phase == 'J2' else None), **kwargs)


class Tools:
    def __init__(self, fragments=None, errors=None):
        self.fragments = [FRAGMENT] if fragments is None else fragments
        self.calls = []
        self.errors = list(errors or [])

    def _record(self, name, argument):
        self.calls.append((name, argument))
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error

    def search_sources(self, query, filters, collection_id):
        self._record('sources', query)
        return [Source('s1', 'Example', 'test', 'full_text'),
                Source('s2', 'Distractor', 'test', 'abstract')]

    def search_fragments(self, query, corpus):
        self._record('fragments', query)
        return self.fragments.copy()

    def read_fragments(self, ids, corpus):
        self._record('read', ids)
        return self.fragments.copy()


def scripted_ui(event, payload):
    if event == 'confirm_plan':
        return {'action': 'confirm'}
    if event == 'select_sources':
        return {'action': 'select', 'source_ids': ['s1']}


class WorkflowTests(unittest.TestCase):
    def execute(self, case=None, replies=None, raw=None, ui=scripted_ui, config=None,
                cancelled=lambda: False, runtime=None, model=None):
        case = case or make_case()
        raw = raw or Tools()
        model = model or FixtureModel([draft()] if replies is None else replies)
        rt = runtime or Runtime('run-test', case.limits, cancelled=cancelled)
        svc = WorkflowServices(rt, model, {'corpus1': CORPUS}, ui)
        result = run_system(case, rt.wrap_search(raw), config or {}, 'run-test', services=svc)
        return result, rt, raw, model, svc

    def test_j2_reads_search_ids_and_validates_draft(self):
        result, rt, raw, model, _ = self.execute()
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.mode, 'fixture')
        self.assertEqual(result.usage.steps, 3)
        self.assertEqual(raw.calls, [('fragments', 'fixed corpus'), ('read', ['f1'])])
        self.assertEqual(json.loads(model.calls[0][1]['text'])['fragments'][0]['text'], FRAGMENT.text)
        self.assertEqual(result.claims[0].evidence[0].fragment_id, 'f1')
        starts = [e.step_id for e in rt.events if e.event_type == 'started']
        self.assertEqual(starts, [1, 2, 3])

    def test_j1_confirmation_then_ranking_then_selected_corpus(self):
        raw = Tools()
        def ui(event, payload):
            if event == 'confirm_plan':
                self.assertFalse(raw.calls)
            return scripted_ui(event, payload)
        r, _, _, _, svc = self.execute(make_case('J1'), [PLAN], raw, ui)
        self.assertEqual(r.retrieved_source_ids, ['s1', 's2'])
        self.assertEqual(svc.created_corpora[0].source_ids, ['s1'])
        self.assertEqual(r.corpus_revision_id, svc.created_corpora[0].corpus_revision_id)
        self.assertEqual(r.claims, [])

    def test_no_implicit_confirmation(self):
        r, _, raw, _, _ = self.execute(make_case('J1'), [PLAN], ui=lambda e, p: None)
        self.assertEqual(r.stop_reason, 'awaiting_confirmation')
        self.assertEqual(raw.calls, [])

    def test_ambiguous_query_needs_clarification_without_calls(self):
        r, _, raw, model, _ = self.execute(make_case('J1', search_query=''))
        self.assertEqual(r.status, 'needs_clarification')
        self.assertFalse(raw.calls or model.calls)

    def test_model_can_request_clarification(self):
        r, _, raw, _, _ = self.execute(make_case('J1'), [{'action': 'clarify', 'text': 'Which task?'}])
        self.assertEqual(r.status, 'needs_clarification')
        self.assertFalse(raw.calls)

    def test_unknown_selection_fails(self):
        def ui(event, payload):
            if event == 'select_sources':
                return {'action': 'select', 'source_ids': ['outside']}
            return scripted_ui(event, payload)
        r, _, _, _, svc = self.execute(make_case('J1'), [PLAN], ui=ui)
        self.assertEqual(r.stop_reason, 'invalid_source_selection')
        self.assertFalse(svc.created_corpora)

    def test_one_repair_for_bad_json(self):
        r, rt, _, model, _ = self.execute(replies=['invalid JSON', draft()])
        self.assertEqual(r.status, 'completed')
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(r.usage.steps, 4)
        self.assertEqual(sum(e.name == 'format_repair' for e in rt.events), 1)

    def test_bad_evidence_is_repaired_only_once(self):
        r, _, _, model, _ = self.execute(replies=[draft('invented'), draft('outside'), draft()])
        self.assertEqual(r.stop_reason, 'invalid_model_output')
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(r.claims, [])

    def test_duplicate_claims_are_rejected(self):
        bad = draft()
        bad['claims'] *= 2
        with self.assertRaises(ValueError):
            validate_draft(bad, [FRAGMENT])

    def test_strict_json_rejects_duplicate_keys_and_nan(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '[]', '```json\n{}\n```'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json(value)

    def test_temporary_tool_error_retried_and_counted(self):
        raw = Tools(errors=[ToolFailure(ToolError('timeout', 'timeout', True))])
        r, rt, _, _, _ = self.execute(raw=raw)
        self.assertEqual(r.status, 'completed')
        self.assertEqual(r.usage.search_calls, 2)
        self.assertEqual(r.usage.steps, 4)
        error = next(e.error for e in rt.events if e.error)
        self.assertEqual(error.tool_call_id, 'run-test:1')

    def test_retry_exhaustion_stops_after_three_attempts(self):
        raw = Tools(errors=[ToolFailure(ToolError('timeout', 'timeout', True))] * 4)
        r, _, _, model, _ = self.execute(raw=raw)
        self.assertEqual(r.stop_reason, 'timeout')
        self.assertEqual(len(raw.calls), 3)
        self.assertFalse(model.calls)

    def test_permanent_tool_failure_is_not_retried(self):
        raw = Tools(errors=[ToolFailure(ToolError('not_found', 'not_found', False))])
        r, _, _, _, _ = self.execute(raw=raw)
        self.assertEqual(r.stop_reason, 'not_found')
        self.assertEqual(len(raw.calls), 1)

    def test_known_zero_llm_retry_counted(self):
        r, _, _, model, _ = self.execute(replies=[
            CompletionFailure('temporary', True, usage_known_zero=True), draft()])
        self.assertEqual(r.status, 'completed')
        self.assertEqual(r.usage.llm_calls, 2)
        self.assertEqual(len(model.calls), 2)

    def test_unknown_billing_stops_without_a_retry_or_fake_zero(self):
        r, _, _, model, _ = self.execute(replies=[CompletionFailure('llm_timeout', True), draft()])
        self.assertEqual(r.stop_reason, 'usage_unknown')
        self.assertIsNone(r.usage.cost)
        self.assertIsNone(r.usage.input_tokens)
        self.assertEqual(len(model.calls), 1)

    def test_missing_usage_cannot_be_published(self):
        r, _, _, _, _ = self.execute(replies=[Completion(json.dumps(draft()), None, None, 'test')])
        self.assertEqual(r.status, 'failed')
        self.assertEqual(r.stop_reason, 'usage_unknown')

    def test_step_budget_stops_before_generation(self):
        r, _, raw, model, _ = self.execute(make_case(limits=Limits(max_steps=2)))
        self.assertEqual(r.status, 'budget_exceeded')
        self.assertEqual(r.stop_reason, 'max_steps')
        self.assertEqual(len(raw.calls), 2)
        self.assertFalse(model.calls)

    def test_preflight_enforces_individual_quotas(self):
        for name, value in [('max_llm_calls', 0), ('max_input_tokens', 1),
                            ('max_output_tokens', 1), ('max_search_calls', 0)]:
            with self.subTest(name=name):
                r, _, _, model, _ = self.execute(make_case(limits=replace(Limits(), **{name: value})))
                self.assertEqual(r.stop_reason, name)
                self.assertFalse(model.calls)

    def test_call_timeout_must_fit_remaining_run_budget(self):
        r, _, raw, model, _ = self.execute(make_case(limits=Limits(timeout_seconds=1)))
        self.assertEqual(r.stop_reason, 'timeout_seconds')
        self.assertFalse(raw.calls or model.calls)

    def test_cost_preflight_does_not_call_a_model(self):
        limits = Limits(max_cost=0, currency='RUB')
        rt = Runtime('run-test', limits, mode='live', pricing={
            'currency': 'RUB', 'input_per_1000': 1, 'output_per_1000': 1, 'verified_on': 'fixture'})
        model = FixtureModel([PLAN])
        model.mode = 'live'  # Preflight-only test; complete must never run.
        r, _, _, _, _ = self.execute(make_case('J1', limits=limits), runtime=rt, model=model,
                                    config={'workflow': {'token_bound_verified': True}})
        self.assertEqual(r.stop_reason, 'max_cost')
        self.assertFalse(model.calls)

    def test_currency_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'currency'):
            Runtime('test', Limits(), mode='live', pricing={'currency': 'RUB'})

    def test_cancel_before_start_has_no_calls(self):
        r, _, raw, model, _ = self.execute(cancelled=lambda: True)
        self.assertEqual(r.status, 'cancelled')
        self.assertFalse(raw.calls or model.calls)

    def test_late_completion_cannot_publish_or_mutate_events(self):
        cancelled = threading.Event()
        release = threading.Event()
        started = threading.Event()
        def delayed(messages):
            started.set()
            cancelled.set()
            release.wait(1)
            return draft()
        r, rt, _, _, _ = self.execute(replies=[delayed], cancelled=cancelled.is_set)
        self.assertTrue(started.is_set())
        self.assertEqual(r.status, 'cancelled')
        count = len(rt.events)
        release.set()
        time.sleep(.03)
        self.assertEqual(len(rt.events), count)
        self.assertFalse(any(e.output and isinstance(e.output, dict) and
                            e.output.get('status') == 'completed' for e in rt.events))

    def test_llm_deadline_is_not_just_checked_after_return(self):
        release = threading.Event()
        def delayed(messages):
            release.wait(1)
            return draft()
        start = time.monotonic()
        r, _, _, _, _ = self.execute(replies=[delayed], config={'workflow': {'llm_timeout_seconds': .04}})
        elapsed = time.monotonic() - start
        release.set()
        self.assertLess(elapsed, .5)
        self.assertEqual(r.stop_reason, 'usage_unknown')

    def test_revision_discards_old_answer_and_uses_new_query(self):
        changed = False
        def ui(event, payload):
            nonlocal changed
            if event == 'after_llm' and not changed:
                changed = True
                return {'action': 'revise', 'question': 'New question', 'search_query': 'new query'}
        new = draft()
        new['answer'] = 'New answer'
        r, rt, raw, _, _ = self.execute(replies=[draft(), new], ui=ui)
        self.assertEqual(r.answer, 'New answer')
        self.assertIn(('fragments', 'new query'), raw.calls)
        self.assertEqual(r.usage.steps, 6)
        self.assertEqual(sum(e.name == 'workflow_finished' for e in rt.events), 1)

    def test_cancel_after_selection_does_not_stage_corpus(self):
        def ui(event, payload):
            if event == 'before_publish':
                return {'action': 'cancel'}
            return scripted_ui(event, payload)
        r, _, _, _, svc = self.execute(make_case('J1'), [PLAN], ui=ui)
        self.assertEqual(r.status, 'cancelled')
        self.assertEqual(svc.created_corpora, [])

    def test_empty_search_is_explicit_insufficiency(self):
        r, _, _, model, _ = self.execute(raw=Tools(fragments=[]))
        self.assertEqual(r.evidence_status, 'insufficient_evidence')
        self.assertEqual(r.status, 'completed')
        self.assertFalse(model.calls)

    def test_oversize_evidence_is_not_silently_sliced(self):
        raw = Tools(fragments=[replace(FRAGMENT, text='x' * 20000)])
        r, rt, _, model, _ = self.execute(raw=raw)
        self.assertEqual(r.evidence_status, 'insufficient_evidence')
        self.assertTrue(r.limitations)
        self.assertFalse(model.calls)
        context = next(e for e in rt.events if e.name == 'context_selected')
        self.assertEqual(context.output['excluded'], ['f1'])

    def test_outside_corpus_fragments_fail_before_generation(self):
        r, _, _, model, _ = self.execute(raw=Tools(fragments=[replace(FRAGMENT, source_id='foreign')]))
        self.assertEqual(r.stop_reason, 'invalid_read_result')
        self.assertFalse(model.calls)

    def test_holdout_is_not_enabled_in_kt3(self):
        r, _, raw, model, _ = self.execute(make_case(split='holdout'))
        self.assertEqual(r.stop_reason, 'holdout_disabled_for_kt3')
        self.assertFalse(raw.calls or model.calls)

    def test_live_adapter_requires_key_and_does_not_leak_it(self):
        model = YandexModel('gpt://folder/model/version')
        with patch.dict('os.environ', {}, clear=True), patch('labestra.llm.build_opener') as opened:
            with self.assertRaises(CompletionFailure) as caught:
                model.complete([], max_tokens=10, temperature=0, timeout=1)
            self.assertEqual(caught.exception.error.code, 'credentials_missing')
            opened.assert_not_called()

    def test_accidental_double_wrapper_rejected(self):
        rt = Runtime('run', Limits())
        with self.assertRaises(ValueError):
            rt.wrap_search(rt.wrap_search(Tools()))


if __name__ == '__main__':
    unittest.main()
