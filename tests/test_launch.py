"""Local setup and result bundle checks without a model or credentials."""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from labestra.launch import configure, package


class LaunchTests(unittest.TestCase):
    def test_configuration_contains_no_key_and_requires_explicit_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'local.json'
            args = argparse.Namespace(model_id='my-model',
                base_url='https://provider.example/v1', budget=20, output=output)
            with patch('socket.socket.connect', side_effect=AssertionError('No network')), redirect_stdout(io.StringIO()):
                configure(args)
            config = json.loads(output.read_text())
            self.assertEqual(config['session_max_cost'], 20)
            self.assertEqual(config['model']['api_format'], 'chat_completions')
            self.assertEqual(config['model']['model_id'], 'my-model')
            self.assertEqual(config['model']['base_url'], 'https://provider.example/v1')
            self.assertIsNone(config['pricing']['input_per_1000'])
            self.assertIsNone(config['pricing']['output_per_1000'])
            self.assertFalse(config['workflow']['token_bound_verified'])
            self.assertNotIn('api_key', config)
            with self.assertRaises(FileExistsError):
                configure(args)

    def test_default_configuration_is_editable_without_prompts_or_yandex_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(model_id=None, base_url=None, budget=None, output=Path(tmp)/'local.json')
            with patch('builtins.input', side_effect=AssertionError('No prompts')), redirect_stdout(io.StringIO()):
                configure(args)
            config = json.loads(args.output.read_text())
            self.assertIsNone(config['model']['model_id'])
            self.assertIsNone(config['model']['base_url'])
            self.assertIsNone(config['session_max_cost'])

    def test_pack_only_includes_runner_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);session = root / 'session';case = session / 'case'
            case.mkdir(parents=True)
            (session / 'summary.json').write_text('{}')
            (session / '.env').write_text('SECRET=do-not-include')
            (case / 'result.json').write_text('{}')
            (case / 'private-gold.json').write_text('do-not-include')
            with redirect_stdout(io.StringIO()):
                package(session, root / 'results.zip')
            with zipfile.ZipFile(root / 'results.zip') as archive:
                self.assertEqual(set(archive.namelist()), {'session/summary.json', 'session/case/result.json'})

    def test_pack_refuses_secret_in_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);session = root / 'session';session.mkdir()
            (session / 'summary.json').write_text('{"bad":"synthetic-secret"}')
            with patch.dict('os.environ', {'LABESTRA_API_KEY': 'synthetic-secret'}), self.assertRaises(ValueError):
                package(session, root / 'results.zip')
            self.assertFalse((root / 'results.zip').exists())


if __name__ == '__main__':
    unittest.main()
