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


def supplementary_audit(root):
    """Validate the derived primary subset and compare the new manual labels."""
    root = Path(root)
    directory = root / 'eval/review/audit-24'
    if not directory.exists():
        return None
    packet = strict_json((directory / 'packet.json').read_text(encoding='utf-8'))
    provenance = strict_json((directory / 'provenance.json').read_text(encoding='utf-8'))
    source_packet = strict_json((root / 'eval/review/packet.json').read_text(encoding='utf-8'))
    primary_path = root / 'eval/annotations/chelush-20261123/annotations.max.jsonl'
    if (provenance['source_annotations_sha256'] != digest(primary_path) or
            provenance['source_packet_sha256'] != source_packet['packet_sha256'] or
            packet['source_packet_sha256'] != source_packet['packet_sha256'] or
            provenance['derived_packet_sha256'] != packet['packet_sha256']):
        raise ValueError('Supplementary audit source/version mismatch')
    for name, expected in provenance['files'].items():
        if Path(name).name != name or digest(directory / name) != expected:
            raise ValueError('Supplementary audit file hash mismatch: ' + name)
    original_objects = {(o['case_id'], o['object_id']): o for o in source_packet['objects']}
    for obj in packet['objects']:
        if original_objects.get((obj['case_id'], obj['object_id'])) != obj:
            raise ValueError('Supplementary audit object differs from the original packet')
    source_labels = {(r['case_id'], r['object_id']): r for r in rows(primary_path)}
    primary = rows(directory / 'annotations.primary.jsonl')
    for row in primary:
        original = source_labels.get((row['case_id'], row['object_id']))
        if original is None or row != {**original, 'packet_sha256': packet['packet_sha256']}:
            raise ValueError('Derived primary annotation was changed')
    result = agreement(packet, primary, rows(directory / 'annotations.secondary.jsonl'))
    result['review_method'] = {
        'primary': provenance['primary_method'], 'secondary': provenance['secondary_method'],
        'blinding': provenance['blinding'], 'reading_aid': provenance['reading_aid'],
        'selection': provenance['selection']}
    result['provenance_sha256'] = digest(directory / 'provenance.json')
    return result


def build(root=ROOT, *, baseline_session=None, baseline_ratings=None):
    root = Path(root)
    validate_dataset(root)
    review = root / 'eval/review'
    if baseline_ratings is None and (review / 'baseline-ratings.sabukhi.jsonl').is_file():
        baseline_ratings = review / 'baseline-ratings.sabukhi.jsonl'
        provenance_path = review / 'baseline-ratings.provenance.json'
        provenance = strict_json(provenance_path.read_text(encoding='utf-8'))
        if digest(baseline_ratings) != provenance['ratings_sha256']:
            raise ValueError('Baseline rating hash mismatch')
    audit_report = supplementary_audit(root)
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
        if baseline_ratings is not None:
            baseline['assessment_basis'] = 'Supplied J2 ratings; the recorded review method is described in the associated provenance.'
            local_provenance = review / 'baseline-ratings.provenance.json'
            if (Path(baseline_ratings).resolve() == (review / 'baseline-ratings.sabukhi.jsonl').resolve()
                    and local_provenance.is_file()):
                baseline['assessment_provenance_sha256'] = digest(local_provenance)
        from .comparison import compare
        reports['baseline-quality.json'] = baseline
        reports['comparison.json'] = compare(report, baseline)
        report['baseline_comparison'] = 'comparison.json'
    if audit_report is not None:
        reports['agreement.audit-24.json'] = audit_report
    reports['readiness.json'] = {
        'public_dataset_valid': True, 'visible_system_cases': report['planned'],
        'reviewed_public_gold_cases': len(rows(review / 'gold.dev.jsonl')) + len(rows(review / 'gold.visible.jsonl')),
        'annotation_objects_per_reviewer': sum(v['total'] for v in agreed['by_kind'].values()),
        'baseline_measured': baseline is not None,
        'paired_comparison_available': baseline is not None,
        'baseline_semantic_ratings_complete': bool(baseline and baseline['quality']['accepted']['assessed_cases'] == 10),
        'supplementary_annotation_audit_complete': audit_report is not None,
        'supplementary_annotation_objects': sum(v['total'] for v in audit_report['by_kind'].values()) if audit_report else 0,
        'annotation_blinding_basis': 'self-reported' if audit_report else 'not established',
        'coursework_complete': False,
        'remaining': (['Measure baseline'] if baseline is None else []) +
            ([] if baseline and baseline['quality']['accepted']['assessed_cases'] == 10 else
             ['Complete semantic review of baseline answers']) +
            ([] if audit_report is not None else ['Resolve annotation independence limitation']) +
            ['Teacher review request and submission form'],
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
