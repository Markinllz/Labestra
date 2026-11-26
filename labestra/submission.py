"""Recalculate submission reports offline from the preserved primary cohort."""

import argparse
from pathlib import Path
import tempfile
import zipfile

from dataset_tools.review import agreement
from dataset_tools.validation import validate_gold_rows
from .data import validate_dataset, load_cases, load_sources, load_fragments, load_corpora
from .evaluate import rows, score
from .runner import ROOT, digest, encoded
from .workflow import strict_json

ARCHIVE = 'runs/submission/live-20261124-v2/experiments-all-attempts-v2.zip'
PREFIX = 'experiments-v2/attempts/deepseek-visible-20261124-v2/'
BASELINE_ARCHIVE = 'runs/submission/baseline-20261126/extractive-visible.zip'


def extract_cohort(archive, output):
    """Extract only the declared cohort; reject archive traversal before writing."""
    output = Path(output)
    with zipfile.ZipFile(archive) as bundle:
        found = False
        seen = set()
        for entry in bundle.infolist():
            if not entry.filename.startswith(PREFIX) or entry.is_dir():
                continue
            relative = Path(entry.filename[len(PREFIX):])
            if relative.is_absolute() or '..' in relative.parts or str(relative) in seen:
                raise ValueError('Invalid or duplicate archive member')
            seen.add(str(relative))
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(entry))
            found = True
    if not found or not (output / 'summary.json').is_file():
        raise ValueError('Primary visible cohort missing from archive')


def build(root=ROOT, *, baseline_session=None, baseline_ratings=None):
    root = Path(root)
    validate_dataset(root)
    review = root / 'eval/review'
    archive = root / ARCHIVE
    validate_gold_rows(load_cases(root / 'eval/dev/tasks.jsonl'), rows(review / 'gold.dev.jsonl'),
                       {s.source_id: s for s in load_sources(root / 'data/sources.jsonl')},
                       {f.fragment_id: f for f in load_fragments(root / 'data/fragments.jsonl')},
                       {c.corpus_revision_id: c for c in load_corpora(root / 'data/corpora.jsonl')},
                       require_reviewed=True)
    packet = strict_json((review / 'packet.json').read_text(encoding='utf-8'))
    agreed = agreement(packet, rows(review / 'annotations.sabukhi.jsonl'),
                       rows(root / 'eval/annotations/chelush-20261123/annotations.max.jsonl'))
    agreed['interpretation'] = ('Descriptive agreement of confirmed labels before reconciliation; '
                                'full independence and blinding are not established.')
    with tempfile.TemporaryDirectory(prefix='labestra-submission-') as temporary:
        session = Path(temporary)
        extract_cohort(archive, session)
        report = score(session, gold_path=review / 'gold.visible.jsonl',
                       ratings_path=review / 'system-ratings.sabukhi.jsonl', root=root)
    report['assessment_basis'] = ('Reviewer confirmed proposed assessments without changes on 2026-11-26. '
                                  'No inference of human labor savings from operator interaction time.')
    report['reference_cost_only'] = True
    report['billed_proxy_cost'] = None
    report['baseline_comparison'] = 'pending: no completed B0' if not baseline_session else 'see baseline-quality.json'
    report['reproducibility'] = {'source_archive': ARCHIVE, 'source_archive_sha256': digest(archive),
                                 'cohort_prefix': PREFIX}
    reports = {'system-v2-quality.json': report, 'agreement.sabukhi-max.json': agreed}
    baseline = None
    if baseline_session:
        baseline = score(baseline_session, gold_path=review / 'gold.visible.jsonl',
                         ratings_path=baseline_ratings, root=root)
    elif (root / BASELINE_ARCHIVE).is_file():
        with tempfile.TemporaryDirectory(prefix='labestra-baseline-') as temporary:
            with zipfile.ZipFile(root / BASELINE_ARCHIVE) as bundle:
                for entry in bundle.infolist():
                    rel = Path(entry.filename)
                    if rel.is_absolute() or '..' in rel.parts:
                        raise ValueError('Invalid baseline archive member')
                    if not entry.is_dir():
                        target = Path(temporary) / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(bundle.read(entry))
            sessions = list(Path(temporary).glob('*/summary.json'))
            if len(sessions) != 1:
                raise ValueError('Expected one baseline session')
            baseline = score(sessions[0].parent, gold_path=review / 'gold.visible.jsonl',
                             ratings_path=baseline_ratings, root=root)
        baseline['reproducibility'] = {'source_archive': BASELINE_ARCHIVE,
                                       'source_archive_sha256': digest(root / BASELINE_ARCHIVE)}
    if baseline:
        from .comparison import compare
        reports['baseline-quality.json'] = baseline
        reports['comparison.json'] = compare(report, baseline)
        report['baseline_comparison'] = 'comparison.json'
    reports['readiness.json'] = {
        'public_dataset_valid': True, 'visible_system_cases': report['planned'],
        'reviewed_public_gold_cases': len(rows(review / 'gold.dev.jsonl')) + len(rows(review / 'gold.visible.jsonl')),
        'annotation_objects_per_reviewer': sum(v['total'] for v in agreed['by_kind'].values()),
        'baseline_measured': baseline is not None,
        'paired_comparison_available': baseline is not None,
        'baseline_semantic_ratings_complete': bool(baseline and baseline['quality']['accepted']['assessed_cases'] == 10),
        'fully_independent_blind_annotation_established': False,
        'coursework_complete': False,
        'remaining': (['Measure baseline'] if baseline is None else []) + [
            'Complete semantic review of baseline answers',
            'Resolve annotation independence limitation',
            'Teacher review request and submission form'],
    }
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/local/submission-check')
    parser.add_argument('--baseline-session', type=Path)
    parser.add_argument('--baseline-ratings', type=Path)
    args = parser.parse_args()
    try:
        reports = build(baseline_session=args.baseline_session, baseline_ratings=args.baseline_ratings)
        args.output.mkdir(parents=True, exist_ok=True)
        for name, report in reports.items():
            (args.output / name).write_text(encoded(report), encoding='utf-8')
        print(f'Пересчитано {len(reports)} отчёта: {args.output.resolve()}; запросов к модели нет.')
        print('Непроверенные смысловые оценки остаются null; статусы подготовки — в readiness.json.')
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(2, f'Проверка сдачи остановлена: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
