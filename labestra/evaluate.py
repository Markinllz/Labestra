"""Offline scoring after blind sessions; never imported by the solving runner.

Retrieval and structural checks are deterministic. Scientific claim correctness,
entailment, gold coverage and acceptance require explicit human assessments.
"""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

from .data import load_cases, load_corpora, load_fragments, load_sources
from .runner import ROOT, digest, encoded
from .workflow import strict_json


def rows(path):
    return [strict_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def fraction(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def distribution(values):
    if not values:
        return {'n': 0, 'median': None, 'p95': None}
    values = sorted(values)
    return {'n': len(values), 'median': statistics.median(values),
            'p95': values[max(0, math.ceil(.95 * len(values)) - 1)]}


def score(session, *, gold_path=None, ratings_path=None, root=ROOT):
    session, root = Path(session), Path(root)
    summary = strict_json((session / 'summary.json').read_text(encoding='utf-8'))
    expected = summary['planned_case_ids']
    if len(expected) != len(set(expected)) or len(expected) != summary['planned']:
        raise ValueError('Invalid planned task list')
    cases = {c.case_id: c for split in ('dev', 'visible') for c in load_cases(root / f'eval/{split}/tasks.jsonl')}
    if not set(expected) <= set(cases):
        raise ValueError('Unknown public case')
    gold = None
    if gold_path:
        from dataset_tools.validation import validate_gold_rows
        gold_rows = rows(gold_path)
        if any(r['case_id'] not in cases for r in gold_rows):
            raise ValueError('Gold includes unknown/private holdout cases')
        sources = {s.source_id: s for s in load_sources(root / 'data/sources.jsonl')}
        fragments = {f.fragment_id: f for f in load_fragments(root / 'data/fragments.jsonl')}
        corpora = {c.corpus_revision_id: c for c in load_corpora(root / 'data/corpora.jsonl')}
        validate_gold_rows([cases[r['case_id']] for r in gold_rows], gold_rows, sources, fragments,
                           corpora, require_reviewed=True)
        gold = {r['case_id']: r for r in gold_rows}
        if not set(expected) <= set(gold):
            raise ValueError('Gold must cover every planned task, including failures')
    ratings_rows = rows(ratings_path) if ratings_path else []
    ratings = {r['run_id']: r for r in ratings_rows}
    if len(ratings) != len(ratings_rows):
        raise ValueError('Duplicate human assessment')
    used_ratings, records = set(), []
    public_fragments = {f.fragment_id: f for f in load_fragments(root / 'data/fragments.jsonl')}
    public_corpora = {c.corpus_revision_id: c for c in load_corpora(root / 'data/corpora.jsonl')}
    all_real = True
    for cid in expected:
        directory = session / cid
        if not directory.exists():
            record = {'case_id': cid, 'phase': cases[cid].phase, 'category': cases[cid].category,
                      'status': 'missing', 'completion': False}
            if gold:
                record['action_correct'] = False
                if gold[cid]['expected_action'] == 'answer':
                    record.update(claim_correctness=0., gold_coverage=0., accepted=False)
                if gold[cid]['expected_action'] == 'search':
                    record['recall_at_20'] = fraction(0, len(gold[cid]['relevant_source_ids']))
                    record['precision_at_k'] = {**fraction(0, 0), 'value': 0., 'k': 0, 'empty_result': True}
            records.append(record)
            continue
        result = strict_json((directory / 'result.json').read_text(encoding='utf-8'))
        manifest = strict_json((directory / 'manifest.json').read_text(encoding='utf-8'))
        case_input = strict_json((directory / 'input.json').read_text(encoding='utf-8'))
        if result['case_id'] != cid or case_input['case_id'] != cid:
            raise ValueError('Result/input case mismatch')
        for field in ('question', 'search_query', 'collection_id', 'corpus_revision_id'):
            if case_input[field] != getattr(cases[cid], field):
                raise ValueError('Run input differs from the frozen task: ' + field)
        if manifest['data_manifest_sha256'] != digest(root / 'data/manifest.json'):
            raise ValueError('Current dataset differs from the frozen run')
        real = manifest['scientific_evaluation'] and manifest['usage_kind'] == 'measured'
        all_real = all_real and real
        if (gold_path or ratings_path) and not real:
            raise ValueError('Fixture runs cannot receive scientific quality scores')
        if gold_path and any(e['name'] == 'input_revised' for e in rows(directory / 'events.jsonl')):
            raise ValueError('Revised inputs need a separately frozen task and gold')
        record = {'case_id': cid, 'phase': cases[cid].phase, 'category': cases[cid].category,
                  'status': result['status'], 'stop_reason': result['stop_reason'],
                  'variant': result['variant'],
                  'blind_attestation': manifest.get('blind_attestation', False),
                  'prior_reference_access': manifest.get('prior_reference_access', False),
                  'comparison_limitation': manifest.get('comparison_limitation'),
                  'completion': result['status'] == 'completed',
                  'duration_seconds': result['duration_seconds'],
                  'human_active_seconds': result['human_active_seconds'], 'usage': result['usage'],
                  'config_sha256': manifest['config_sha256'],
                  'input_question': case_input['question'], 'search_query': case_input['search_query'],
                  'input_corpus_revision_id': case_input['corpus_revision_id']}
        if cases[cid].phase == 'J2':
            claims = result['claims']
            corpus = public_corpora.get(case_input['corpus_revision_id'])
            allowed_sources = set(corpus.source_ids) if corpus else set()
            links = [e for c in claims for e in c['evidence']]
            valid = [e for e in links if e['fragment_id'] in public_fragments and
                     public_fragments[e['fragment_id']].source_id in allowed_sources]
            record['emitted_claim_count'] = len(claims)
            record['valid_reference_fraction'] = fraction(len(valid), len(links))
            record['structural_evidence_coverage'] = fraction(sum(bool(c['evidence']) and all(
                e['fragment_id'] in public_fragments and public_fragments[e['fragment_id']].source_id in allowed_sources
                for e in c['evidence']) for c in claims), len(claims))
            if manifest.get('baseline_policy') == 'extractive-v1':
                record['literal_quote_validity'] = fraction(sum(bool(c['text']) and any(
                    e['fragment_id'] in public_fragments and c['text'] in public_fragments[e['fragment_id']].text
                    for e in c['evidence']) for c in claims), len(claims))
        if gold:
            reference = gold[cid]
            action = ('clarify' if result['status'] == 'needs_clarification' else
                      'refuse' if result['stop_reason'] == 'outside_scope' else
                      'search' if cases[cid].phase == 'J1' and result['status'] == 'completed' else
                      'answer' if result['stop_reason'] == 'draft_ready' else
                      'insufficient_evidence' if result['stop_reason'] in ('insufficient_evidence', 'empty_search') else 'failed')
            record['action_correct'] = action == reference['expected_action']
            if cases[cid].phase == 'J1' and reference['expected_action'] == 'search':
                ranked = result['retrieved_source_ids'][:20]
                if len(ranked) != len(set(ranked)):
                    raise ValueError('Repeated source IDs in initial ranking')
                relevant = set(reference['relevant_source_ids'])
                matches = len(set(ranked) & relevant)
                record['recall_at_20'] = fraction(matches, len(relevant))
                record['precision_at_k'] = {**fraction(matches, len(ranked)), 'k': len(ranked),
                                            'empty_result': not ranked}
                if not ranked:
                    record['precision_at_k']['value'] = 0.
                selected_corpora = json.loads((directory / 'corpora.json').read_text(encoding='utf-8'))
                if not isinstance(selected_corpora, list):
                    raise ValueError('Corpora artifact must be an array')
                selected = next((c['source_ids'] for c in selected_corpora
                                 if c['corpus_revision_id'] == result['corpus_revision_id']), [])
                if len(selected) != len(set(selected)) or not set(selected) <= set(ranked):
                    raise ValueError('Selected corpus contains repeated or unranked sources')
                selected_matches = len(set(selected) & relevant)
                record['selected_source_recall'] = fraction(selected_matches, len(relevant))
                record['selected_source_precision'] = fraction(selected_matches, len(selected))
                if not selected:
                    record['selected_source_precision']['value'] = 0.

            if cases[cid].phase == 'J2':
                answerable = reference['expected_action'] == 'answer'
                claims = result['claims']
                if answerable and (result['status'] != 'completed' or not claims):
                    record.update(claim_correctness=0., gold_coverage=0., accepted=False)
                assessment = ratings.get(result['run_id'])
                if assessment:
                    used_ratings.add(result['run_id'])
                    reviewer = assessment.get('reviewer_id')
                    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer in {manifest['participant'], 'codex', 'assistant'}:
                        raise ValueError('Assessment needs an independent human reviewer')
                    if not isinstance(assessment.get('rationale'), str) or not assessment['rationale'].strip():
                        raise ValueError('Assessment rationale is required')
                    correct = assessment['claim_correct']
                    if set(correct) != {c['claim_id'] for c in claims} or any(type(v) is not bool for v in correct.values()):
                        raise ValueError('Human correctness labels must cover all emitted claims')
                    covered = assessment['covered_gold_claim_ids']
                    allowed = {c['gold_claim_id'] for c in reference['required_claims']}
                    if len(covered) != len(set(covered)) or not set(covered) <= allowed:
                        raise ValueError('Unknown or repeated covered gold claim')
                    entailment = assessment['evidence_entailed']
                    evidence_keys = {c['claim_id'] + '/' + e['fragment_id'] for c in claims for e in c['evidence']}
                    if set(entailment) != evidence_keys or any(type(v) is not bool for v in entailment.values()):
                        raise ValueError('Human entailment labels must cover every claim/evidence pair')
                    if type(assessment['accepted']) is not bool:
                        raise ValueError('accepted must be boolean')
                    if assessment['accepted'] and (not answerable or not claims or not all(correct.values()) or
                            not all(entailment.values()) or set(covered) != allowed or not record['completion']):
                        raise ValueError('Acceptance requires correct claims, evidence and full required coverage')
                    if answerable and claims and record['completion']:
                        record['claim_correctness'] = sum(correct.values()) / len(correct)
                        record['gold_coverage'] = len(covered) / len(allowed)
                        record['accepted'] = assessment['accepted']
                    record['evidence_entailment'] = fraction(sum(entailment.values()), len(entailment))
                    record['reviewer_id'] = reviewer
        records.append(record)
    if set(ratings) != used_ratings:
        raise ValueError('Ratings contain unknown or non-J2 run IDs')
    metrics = {}
    for key in ('claim_correctness', 'gold_coverage', 'accepted', 'action_correct', 'emitted_claim_count'):
        values = [r[key] for r in records if r.get(key) is not None]
        metrics[key] = {'assessed_cases': len(values), 'mean': statistics.mean(values) if values else None}
    for key in ('recall_at_20', 'precision_at_k', 'evidence_entailment', 'selected_source_recall',
                'selected_source_precision', 'valid_reference_fraction', 'structural_evidence_coverage',
                'literal_quote_validity'):
        values = [r[key]['value'] for r in records if key in r and r[key]['value'] is not None]
        metrics[key] = {'assessed_cases': len(values), 'mean': statistics.mean(values) if values else None}
    costs = [r['usage']['cost'] for r in records if 'usage' in r]
    unknown_costs = sum(v is None for v in costs)
    return {'session_id': summary['session_id'], 'planned': len(expected),
            'statuses': dict(Counter(r['status'] for r in records)),
            'completion': fraction(sum(r['completion'] for r in records), len(expected)),
            'scientific_evaluation': all_real and gold is not None,
            'gold_sha256': digest(gold_path) if gold_path else None,
            'ratings_sha256': digest(ratings_path) if ratings_path else None,
            'quality': metrics,
            'duration_seconds': distribution([r['duration_seconds'] for r in records if 'duration_seconds' in r]),
            'human_active_seconds': distribution([r['human_active_seconds'] for r in records if r.get('human_active_seconds') is not None]),
            'total_cost': None if unknown_costs else sum(costs),
            'known_cost_subtotal': sum(v for v in costs if v is not None),
            'unknown_cost_runs': unknown_costs,
            'currency': sorted({r['usage']['currency'] for r in records if 'usage' in r}),
            'limitations': ['Unassessed semantic metrics remain null; missing tasks and failures remain in the task list.',
                            'Inspect per-case and per-category results; aggregates do not establish statistical independence.'],
            'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--gold', type=Path)
    parser.add_argument('--ratings', type=Path)
    parser.add_argument('--prepare-ratings', action='store_true', help='Write empty human assessment JSONL; no automatic labels')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.prepare_ratings:
            template = []
            for path in sorted(args.session.glob('*/result.json')):
                case = strict_json((path.parent / 'input.json').read_text(encoding='utf-8'))
                if case['phase'] != 'J2':
                    continue
                result = strict_json(path.read_text(encoding='utf-8'))
                template.append({'run_id': result['run_id'], 'reviewer_id': None, 'rationale': None,
                    'claim_correct': {c['claim_id']: None for c in result['claims']},
                    'evidence_entailed': {c['claim_id'] + '/' + e['fragment_id']: None
                                         for c in result['claims'] for e in c['evidence']},
                    'covered_gold_claim_ids': [], 'accepted': None})
            text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in template)
        else:
            text = encoded(score(args.session, gold_path=args.gold, ratings_path=args.ratings))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
        print(args.output)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Evaluation stopped: {exc}\n')


if __name__ == '__main__':
    main()
