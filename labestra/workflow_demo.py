"""Offline integration demonstration, deliberately not the course eval runner.

Uses public dev inputs and real local search; scripted LLM and human interaction.
No network or credential access, no gold, no baseline or scientific scoring.
"""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import uuid

from .data import load_cases, load_corpora, load_fragments, load_sources, validate_dataset
from .llm import FixtureModel
from .schemas import Limits
from .search import LocalSearch
from .workflow import WorkflowServices, run_system
from .workflow_runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def technical_reply(messages):
    fragment = json.loads(messages[-1]['text'])['fragments'][0]
    return {
        'answer': 'Технический fixture: передача фрагмента и проверка ссылки выполнены. '
                  'Научный ответ модели в этом запуске не получен.',
        'claims': [{'claim_id': 'fixture-quote-1', 'text': fragment['text'][:180],
                    'evidence': [{'fragment_id': fragment['fragment_id'], 'relation': 'supports'}]}],
        'limitations': ['Ответ сгенерирован тестовым адаптером; научное качество не измерялось.'],
        'evidence_status': 'insufficient_evidence',
    }


def fixture_interaction(event, payload):
    if event == 'confirm_plan':
        return {'action': 'confirm', 'origin': 'scripted_fixture'}
    if event == 'select_sources':
        return {'action': 'select', 'source_ids': payload['source_ids'][:3],
                'origin': 'scripted_fixture'}


def run_demo(output: Path):
    validate_dataset(ROOT)
    config_path = ROOT / 'configs/workflow.fixture.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    manifest_path = ROOT / 'data/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if config['mode'] != 'fixture' or config['collection_id'] != manifest['collection_id']:
        raise ValueError('Fixture mode and matching collection are required')
    limits = Limits(**config['limits'])
    cases = load_cases(ROOT / 'eval/dev/tasks.jsonl')
    corpora = {c.corpus_revision_id: c for c in load_corpora(ROOT / 'data/corpora.jsonl')}
    sources = load_sources(ROOT / 'data/sources.jsonl')
    fragments = load_fragments(ROOT / 'data/fragments.jsonl')
    raw = LocalSearch(sources=sources, fragments=fragments, config=config['search'],
                      collection_id=config['collection_id'])
    files = sorted((ROOT / 'labestra').glob('*.py')) + [config_path]
    files += sorted((ROOT / 'prompts').glob('*.txt'))
    code_hashes = {str(p.relative_to(ROOT)): sha(p) for p in files}
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                  text=True, check=False).stdout.strip() or 'unknown'
    except FileNotFoundError:
        revision = 'unknown'  # File hashes still identify an exported source archive.
    # Each demonstration gets a new directory. Idempotent publication belongs to runner (S5).
    output.mkdir(parents=True, exist_ok=False)
    summary = []
    for original in cases:
        case = replace(original, limits=limits)
        run_id = 'fixture-' + uuid.uuid4().hex
        rt = Runtime(run_id, limits, mode='fixture')
        replies = [{'action': 'plan', 'text': 'Поиск по заданной английской фразе; затем выбор корпуса.'}]
        model = FixtureModel(replies if case.phase == 'J1' else [technical_reply])
        services = WorkflowServices(rt, model, corpora, fixture_interaction)
        result = run_system(case, rt.wrap_search(raw), config, run_id, services=services)
        directory = output / case.case_id
        directory.mkdir()
        write_json(directory / 'input.json', asdict(case))
        write_json(directory / 'config.json', config)
        write_json(directory / 'result.json', asdict(result))
        write_json(directory / 'corpora.json', [asdict(c) for c in services.created_corpora])
        (directory / 'events.jsonl').write_text(''.join(
            json.dumps(asdict(e), ensure_ascii=False) + '\n' for e in rt.events), encoding='utf-8')
        write_json(directory / 'manifest.json', {
            'run_id': run_id, 'mode': 'fixture', 'variant': 'system',
            'model_id': model.model_id, 'usage_kind': 'synthetic_fixture',
            'network_calls': 0, 'scientific_evaluation': False,
            'interaction_policy': 'scripted confirm; select first three ranked sources',
            'dataset_version': manifest['dataset_version'],
            'data_manifest_sha256': sha(manifest_path), 'code_base_revision': revision,
            'file_sha256': code_hashes, 'python': platform.python_version(),
            'sqlite': sqlite3.sqlite_version, 'parameters': config,
        })
        summary.append({'case_id': case.case_id, 'phase': case.phase, 'mode': 'fixture',
                        'status': result.status, 'stop_reason': result.stop_reason,
                        'steps': result.usage.steps, 'cost_rub': result.usage.cost})
    write_json(output / 'summary.json', summary)
    (output / 'README.md').write_text(
        '# Локальная проверка workflow\n\n'
        'Реальные публичные dev-входы и поиск; тестовый LLM и заранее заданные действия человека.\n'
        'Сети и оплаты нет. Токены синтетические. Это не B0, не научный eval и не live-прогон.\n'
        'result.json содержит технический итог; events.jsonl — порядок действий и точные промпты.\n',
        encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description='Offline workflow demonstration (fixture only)')
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()
    output = args.output or ROOT / 'runs/local' / (
        'workflow-fixture-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6])
    try:
        summary = run_demo(output)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps({'output': str(output.resolve()), 'mode': 'fixture', 'cases': summary},
                     ensure_ascii=False, indent=2))
    return int(any(c['status'] in ('failed', 'budget_exceeded', 'cancelled') for c in summary))


if __name__ == '__main__':
    raise SystemExit(main())
