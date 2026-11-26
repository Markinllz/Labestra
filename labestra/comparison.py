"""Paired, descriptive comparison on identical frozen inputs."""

from .evaluate import distribution


def compare(system, baseline):
    a = {r['case_id']: r for r in system['records']}
    b = {r['case_id']: r for r in baseline['records']}
    if not a or set(a) != set(b):
        raise ValueError('Paired comparison requires the same planned cases')
    if system['gold_sha256'] != baseline['gold_sha256']:
        raise ValueError('Paired comparison requires the same reference version')
    pairs = []
    for cid in sorted(a):
        left, right = a[cid], b[cid]
        for field in ('input_question', 'search_query', 'input_corpus_revision_id', 'config_sha256'):
            if field not in left or field not in right or left[field] != right[field]:
                raise ValueError('Paired input/config mismatch: ' + cid + '/' + field)
        pairs.append({'case_id': cid, 'phase': left['phase'], 'category': left['category'],
                      'system_status': left['status'], 'baseline_status': right['status'],
                      'system_action_correct': left.get('action_correct'),
                      'baseline_action_correct': right.get('action_correct'),
                      'system_seconds': left['duration_seconds'], 'baseline_seconds': right['duration_seconds'],
                      'duration_delta_system_minus_baseline': left['duration_seconds'] - right['duration_seconds']})
    return {'system_session_id': system['session_id'], 'baseline_session_id': baseline['session_id'],
            'paired_cases': len(pairs), 'same_inputs_and_config': True,
            'gold_sha256': system['gold_sha256'],
            'quality': {key: {'baseline': baseline['quality'][key], 'system': value}
                        for key, value in system['quality'].items()},
            'duration_seconds': {'baseline': baseline['duration_seconds'], 'system': system['duration_seconds'],
                                 'paired_delta': distribution([p['duration_delta_system_minus_baseline'] for p in pairs])},
            'reference_cost': {'baseline': baseline['total_cost'], 'system': system['total_cost'],
                               'currency': system['currency'], 'provider_invoice_confirmed': False},
            'semantic_baseline_assessments': baseline['quality']['accepted']['assessed_cases'],
            'limitations': [
                'Baseline method selected after visible system results; descriptive comparison, not untouched evaluation.',
                'Technical completion, valid citations and literal quotation do not establish answer correctness or coverage.',
                ('Baseline semantic ratings are not supplied; null is not zero.'
                 if baseline['quality']['accepted']['assessed_cases'] == 0 else
                 'Semantic ratings are supplied; review procedures and reading aids are recorded with the ratings.'),
                'System duration includes operator interaction and is not human labor; no labor-saving claim is made.',
                'Recorded durations are from separate sessions; timing differences are not controlled causal estimates.',
                'Cost uses reference token prices, not a verified proxy invoice; old unknown-usage attempts remain archived.',
            ], 'pairs': pairs}
