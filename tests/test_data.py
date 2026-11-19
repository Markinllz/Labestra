"""Technical fixtures only; these are not scientific evaluation data."""
import hashlib
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from labestra.data import load_cases, load_corpora, load_fragments, load_sources, validate_dataset


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = dict(source_id='s1', title='Example', origin='fixture',
                           availability='full_text', snapshot_path='data/snapshots/s1.txt')
        self.text = 'First line.\r\nSparse recovery is possible.\n'
        self.source['sha256'] = hashlib.sha256(self.text.encode()).hexdigest()
        path = self.root / self.source['snapshot_path']
        path.parent.mkdir(parents=True)
        path.write_bytes(self.text.encode())
        self.fragment = dict(fragment_id='f1', source_id='s1', text=self.text,
                             location=f'chars:0:{len(self.text)}',
                             snapshot_sha256=self.source['sha256'])
        self.corpus = dict(corpus_revision_id='c1', collection_id='collection', source_ids=['s1'])
        self.write('data/sources.jsonl', [self.source])
        self.write('data/fragments.jsonl', [self.fragment])
        self.write('data/corpora.jsonl', [self.corpus])
        for split in ('dev', 'visible'):
            self.write(f'eval/{split}/tasks.jsonl', [self.case(split)])

    def case(self, split):
        return dict(case_id=split, phase='J2', split=split, category='fixture',
                    group_id=split, question='Question?', collection_id='collection',
                    corpus_revision_id='c1', search_query='sparse recovery', history=[])

    def write(self, name, records):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(x) + '\n' for x in records), encoding='utf-8')
        self.refresh_manifest()
        return path

    def refresh_manifest(self):
        names = ['data/sources.jsonl', 'data/fragments.jsonl', 'data/corpora.jsonl',
                 'eval/dev/tasks.jsonl', 'eval/visible/tasks.jsonl', 'data/snapshots/s1.txt']
        if not all((self.root / name).is_file() for name in names):
            return
        manifest = dict(manifest_version='1', schema_version='1.1', dataset_version='fixture-v1',
                        collection_id='collection', files={
                            name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in names})
        (self.root / 'data/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_valid_dataset_and_private_files_ignored(self):
        self.write('eval/private/holdout.jsonl', [{'invalid': 'private'}])
        self.assertIsNone(validate_dataset(self.root))
        cases = load_cases(self.root / 'eval/dev/tasks.jsonl')
        self.assertEqual(cases[0].limits.max_steps, 20)
        self.assertIsNone(load_sources(self.root / 'data/sources.jsonl')[0].external_id)

    def test_safe_repairs_leave_file_unchanged(self):
        path = self.write('corpora.jsonl', [dict(self.corpus, source_ids=['s1', 's1']), self.corpus])
        path.write_bytes(b'\xef\xbb\xbf\n' + path.read_bytes() + b'\n')
        before = path.read_bytes()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            result = load_corpora(path)
        self.assertEqual(len(caught), 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_ids, ['s1'])
        self.assertEqual(path.read_bytes(), before)

    def test_multiple_schema_errors_without_record_contents(self):
        case = self.case('dev')
        case.update(case_id=123, phase='BAD', gold='SECRET', history=[{'role': 42}],
                    limits={'max_steps': True})
        path = self.write('bad.jsonl', [case, dict(self.case('visible'), expected='SECRET')])
        with self.assertRaises(ValueError) as ctx:
            load_cases(path)
        message = str(ctx.exception)
        for field in ('case_id', 'phase', 'gold', 'history', 'max_steps', 'expected'):
            self.assertIn(field, message)
        self.assertIn(':2', message)
        self.assertNotIn('SECRET', message)

    def test_conflicting_duplicate(self):
        path = self.write('conflict.jsonl', [self.source, dict(self.source, title='Different')])
        with self.assertRaisesRegex(ValueError, 'конфликт'):
            load_sources(path)

    def test_missing_empty_and_malformed_files(self):
        with self.assertRaises(ValueError):
            load_sources(self.root / 'missing.jsonl')
        path = self.write('empty.jsonl', [])
        with self.assertRaisesRegex(ValueError, 'пуст'):
            load_sources(path)
        path.write_text('{bad}\n[]\n', encoding='utf-8')
        with self.assertRaises(ValueError) as ctx:
            load_sources(path)
        self.assertIn(':1', str(ctx.exception))
        self.assertIn(':2', str(ctx.exception))

    def test_aggregate_dataset_errors(self):
        self.write('data/sources.jsonl', [dict(self.source, sha256='wrong'),
                   dict(self.source, source_id='missing', snapshot_path='data/missing.txt')])
        self.write('data/fragments.jsonl', [dict(self.fragment, text='not in snapshot',
                                               snapshot_sha256='wrong'),
                   dict(self.fragment, fragment_id='f2', source_id='unknown')])
        self.write('data/corpora.jsonl', [dict(self.corpus, source_ids=['unknown'])])
        self.write('eval/visible/tasks.jsonl', [dict(self.case('visible'), group_id='dev',
                                                  split='dev', collection_id='other')])
        with self.assertRaises(ValueError) as ctx:
            validate_dataset(self.root)
        message = str(ctx.exception)
        for term in ('sha256', 'snapshot_path', '.text', '.source_id', '.source_ids',
                     '.group_id', '.split', '.collection_id'):
            self.assertIn(term, message)

    def test_empty_corpus_allowed(self):
        self.write('data/corpora.jsonl', [dict(self.corpus, source_ids=[])])
        validate_dataset(self.root)

    def test_j2_missing_or_unknown_corpus(self):
        for value in (None, 'unknown'):
            with self.subTest(value=value):
                self.write('eval/dev/tasks.jsonl', [dict(self.case('dev'), corpus_revision_id=value)])
                with self.assertRaisesRegex(ValueError, 'corpus_revision_id'):
                    validate_dataset(self.root)

    def test_metadata_without_snapshot_allowed_but_no_fragments(self):
        metadata = dict(source_id='metadata', title='Title', origin='fixture', availability='metadata')
        self.write('data/sources.jsonl', [self.source, metadata])
        validate_dataset(self.root)
        self.write('data/fragments.jsonl', [dict(self.fragment, source_id='metadata')])
        with self.assertRaisesRegex(ValueError, 'metadata'):
            validate_dataset(self.root)

    def test_empty_snapshot_rejected(self):
        (self.root / self.source["snapshot_path"]).write_bytes(b" \n")
        with self.assertRaisesRegex(ValueError, "снимок пуст"):
            validate_dataset(self.root)

    def test_unicode_line_separators_in_json_string(self):
        path = self.root / 'unicode.jsonl'
        record = dict(self.source, title='A\u2028B\u2029C')
        path.write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
        self.assertEqual(load_sources(path)[0].title, record['title'])

    def test_huge_numeric_field_is_reported(self):
        path = self.write('huge.jsonl', [dict(self.case('dev'), limits={'timeout_seconds': 10**400})])
        with self.assertRaisesRegex(ValueError, 'timeout_seconds'):
            load_cases(path)

    def test_repeated_json_keys(self):
        path = self.root / 'keys.jsonl'
        base = json.dumps(self.source)
        for value in ('"other"', '123'):
            path.write_text(base[:-1] + ', "source_id":' + value + '}\n')
            with self.assertRaisesRegex(ValueError, 'конфликт'):
                load_sources(path)
        path.write_text(base[:-1] + ', "source_id":"s1"}\n')
        before = path.read_bytes()
        with self.assertWarns(UserWarning):
            self.assertEqual(load_sources(path)[0].source_id, 's1')
        self.assertEqual(path.read_bytes(), before)

    def test_duplicate_nested_key_types_not_coerced(self):
        case = json.dumps(self.case('dev'))
        path = self.root / 'nested.jsonl'
        path.write_text(case[:-1] + ', "limits":{"max_steps":1,"max_steps":true}}\n')
        with self.assertRaisesRegex(ValueError, 'конфликт'):
            load_cases(path)

    def test_coordinates_must_match_exact_slice(self):
        for location in ('chars:0:99999', 'chars:1:2', 'chars:2:1', 'page:1', 'chars:-1:3'):
            with self.subTest(location=location):
                self.write('data/fragments.jsonl', [dict(self.fragment, location=location)])
                with self.assertRaisesRegex(ValueError, 'location'):
                    validate_dataset(self.root)

    def test_generated_fragments_validate(self):
        from dataclasses import asdict
        from labestra.data import chunk_snapshot
        fragments = chunk_snapshot('s1', self.text.encode(), chunk_size=3, overlap=1)
        self.write('data/fragments.jsonl', [asdict(f) for f in fragments])
        validate_dataset(self.root)

    def test_blank_ids_rejected_by_loaders(self):
        examples = [
            (load_sources, self.source, ('source_id', 'external_id')),
            (load_fragments, self.fragment, ('fragment_id', 'source_id')),
            (load_corpora, self.corpus, ('corpus_revision_id', 'collection_id')),
            (load_cases, self.case('dev'), ('case_id', 'collection_id', 'group_id', 'corpus_revision_id')),
        ]
        for loader, record, fields in examples:
            for field in fields:
                for value in ('', ' \t'):
                    with self.subTest(field=field, value=value):
                        path = self.write('blank.jsonl', [dict(record, **{field: value})])
                        with self.assertRaisesRegex(ValueError, 'идентификатор'):
                            loader(path)
        path = self.write('blank.jsonl', [dict(self.corpus, source_ids=[' '])])
        with self.assertRaisesRegex(ValueError, 'source_ids'):
            load_corpora(path)

    def test_blank_source_id_rejected_by_full_validation(self):
        self.write('data/sources.jsonl', [dict(self.source, source_id=' ')])
        with self.assertRaisesRegex(ValueError, 'source_id: идентификатор'):
            validate_dataset(self.root)

    def test_manifest_required_and_schema_checked(self):
        path = self.root / 'data/manifest.json'
        original = json.loads(path.read_text())
        for field, value in [('manifest_version', '2'), ('schema_version', '1'),
                             ('dataset_version', ' '), ('collection_id', ''), ('files', [])]:
            with self.subTest(field=field):
                path.write_text(json.dumps(dict(original, **{field: value})))
                with self.assertRaisesRegex(ValueError, field):
                    validate_dataset(self.root)
        for field in original:
            changed = dict(original)
            del changed[field]
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, field):
                validate_dataset(self.root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'manifest.json'):
            validate_dataset(self.root)

    def test_manifest_hashes_and_required_files(self):
        path = self.root / 'data/manifest.json'
        original = json.loads(path.read_text())
        for name in original['files']:
            with self.subTest(name=name):
                changed = json.loads(json.dumps(original))
                del changed['files'][name]
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'manifest'):
                    validate_dataset(self.root)
        for digest in ('REPLACE_WITH_SHA256_OF_FILE', '0'*64):
            changed = json.loads(json.dumps(original))
            changed['files']['data/sources.jsonl'] = digest
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                validate_dataset(self.root)
        path.write_text(json.dumps(original))
        with (self.root / 'data/sources.jsonl').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            validate_dataset(self.root)

    def test_manifest_collection_checked_even_for_j1(self):
        self.write('eval/dev/tasks.jsonl', [dict(self.case('dev'), phase='J1',
                    corpus_revision_id=None, collection_id='wrong')])
        self.write('data/corpora.jsonl', [dict(self.corpus, collection_id='wrong')])
        with self.assertRaises(ValueError) as ctx:
            validate_dataset(self.root)
        message = str(ctx.exception)
        self.assertIn('eval/dev/tasks.jsonl[dev].collection_id: не совпадает с manifest', message)
        self.assertIn('data/corpora.jsonl[c1].collection_id: не совпадает с manifest', message)

    def private_guard(self):
        private = self.root / 'eval/private'
        private.mkdir(parents=True, exist_ok=True)
        target = private / 'secret.txt'
        target.write_text('private test sentinel')
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            self.assertFalse(path.resolve().is_relative_to(private.resolve()),
                             'validator attempted to open a private file')
            return original_open(path, *args, **kwargs)

        return target, guarded_open

    def test_snapshot_private_paths_rejected_before_open(self):
        target, guard = self.private_guard()
        link = self.root / 'data/snapshots/link.txt'
        link.symlink_to(target)
        candidates = ['eval/private/secret.txt', 'data/snapshots/../../eval/private/secret.txt',
                      'data/snapshots/link.txt', str(target)]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.write('data/sources.jsonl', [dict(self.source, snapshot_path=candidate)])
                with patch.object(Path, 'open', guard):
                    with self.assertRaisesRegex(ValueError, 'snapshot_path'):
                        validate_dataset(self.root)

    def test_snapshot_directory_symlink_cannot_redefine_boundary(self):
        target, guard = self.private_guard()
        (target.parent / 's1.txt').write_text(self.text)
        snapshots = self.root / 'data/snapshots'
        snapshots.rename(self.root / 'data/saved-snapshots')
        snapshots.symlink_to(target.parent, target_is_directory=True)
        with patch.object(Path, 'open', guard):
            with self.assertRaisesRegex(ValueError, 'snapshot_path'):
                validate_dataset(self.root)

    def test_manifest_cannot_hash_private_files_or_links(self):
        target, guard = self.private_guard()
        (self.root / 'data/snapshots/link.txt').symlink_to(target)
        path = self.root / 'data/manifest.json'
        original = json.loads(path.read_text())
        for name in ('eval/private/secret.txt', 'eval/gold/visible.jsonl',
                     'data/snapshots/link.txt', 'data/snapshots/../../eval/private/secret.txt',
                     str(target)):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(original))
                changed['files'][name] = '0'*64
                path.write_text(json.dumps(changed))
                with patch.object(Path, 'open', guard):
                    with self.assertRaisesRegex(ValueError, 'путь запрещён'):
                        validate_dataset(self.root)

    def test_public_jsonl_and_manifest_symlinks_to_private_not_opened(self):
        target, guard = self.private_guard()
        for name in ('data/sources.jsonl', 'data/manifest.json'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.unlink()
                path.symlink_to(target)
                try:
                    with patch.object(Path, 'open', guard):
                        with self.assertRaises(ValueError):
                            validate_dataset(self.root)
                finally:
                    path.unlink()
                    path.write_bytes(original)

    def test_snapshot_internal_symlink_allowed(self):
        link = self.root / 'data/snapshots/link.txt'
        link.symlink_to(self.root / self.source['snapshot_path'])
        self.write('data/sources.jsonl', [dict(self.source, snapshot_path='data/snapshots/link.txt')])
        path = self.root / 'data/manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files']['data/snapshots/link.txt'] = self.source['sha256']
        path.write_text(json.dumps(manifest))
        validate_dataset(self.root)

    def test_missing_required_snapshot(self):
        self.write('data/sources.jsonl', [dict(self.source, snapshot_path=None)])
        with self.assertRaisesRegex(ValueError, 'требуются'):
            validate_dataset(self.root)


if __name__ == '__main__':
    unittest.main()
