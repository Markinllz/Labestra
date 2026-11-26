"""Small launcher: local configuration, hidden key input and result packaging."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import zipfile

from .runner import ROOT, encoded, execute


def configure(args):
    config = json.loads((ROOT / 'configs/workflow.live.example.json').read_text(encoding='utf-8'))
    if args.model_id is not None:
        config['model']['model_id'] = args.model_id
    if args.base_url is not None:
        config['model']['base_url'] = args.base_url
    if args.budget is not None:
        if not 0 < args.budget <= 100000:
            raise ValueError('Нужен положительный конечный бюджет в валюте pricing.currency')
        config['session_max_cost'] = args.budget
        config['limits']['max_cost'] = args.budget
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(encoded(config))
    print(f'Создан {output}. Отредактируйте модель, адрес API, тариф, валюту и лимиты под своего провайдера. Запросов к API не было.')


def package(session, output):
    """Only runner artifacts; no environment, source snapshots, gold or other files."""
    allowed = {'input.json', 'config.json', 'result.json', 'manifest.json', 'corpora.json', 'events.jsonl'}
    if not (session / 'summary.json').is_file():
        raise ValueError('Укажите папку сессии с summary.json')
    files = [session / 'summary.json']
    for child in sorted(session.iterdir()):
        if child.is_dir() and not child.is_symlink():
            files.extend(p for p in child.iterdir() if p.name in allowed and p.is_file() and not p.is_symlink())
    key = os.environ.get('LABESTRA_API_KEY')
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(session.resolve()):
            raise ValueError('Симлинки в архиве запрещены')
        text = path.read_text(encoding='utf-8')
        if key and key in text:
            raise ValueError('Обнаружен ключ в артефакте; архив не создан')
        if path.name == 'config.json' and any(k in json.loads(text) for k in ('api_key', 'token', 'secret')):
            raise ValueError('Секреты не должны находиться в config')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=str(Path(session.name) / path.relative_to(session)))
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    conf = sub.add_parser('configure', help='Create editable local API config, no prompts or API calls')
    conf.add_argument('--model-id')
    conf.add_argument('--base-url')
    conf.add_argument('--budget', type=float)
    conf.add_argument('--output', type=Path, default=ROOT / 'configs/local.live.json')
    pack = sub.add_parser('pack', help='ZIP with runner results only')
    pack.add_argument('--session', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run', help='Prompt for key privately, then invoke common runner')
    run.add_argument('--variant', choices=('baseline', 'system'), required=True)
    run.add_argument('--cases', default='eval/dev/tasks.jsonl')
    run.add_argument('--config', default='configs/local.live.json')
    run.add_argument('--output', default='runs/local')
    run.add_argument('--session-id')
    run.add_argument('--case-id', action='append')
    run.add_argument('--participant', required=True)
    run.add_argument('--blind-attestation', action='store_true')
    run.add_argument('--prior-reference-access', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            configure(args)
        elif args.command == 'pack':
            package(args.session, args.output)
        else:
            if args.variant == 'system' and not os.environ.get('LABESTRA_API_KEY'):
                if not sys.stdin.isatty():
                    raise ValueError('Нет LABESTRA_API_KEY; скрытый ввод ключа требует интерактивного терминала')
                os.environ['LABESTRA_API_KEY'] = getpass.getpass('API-ключ (ввод скрыт, на диск не сохраняется): ')
            args.fixture_actions = False
            return execute(args)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Остановлено: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
