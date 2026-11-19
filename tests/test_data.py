"""Technical fixtures only; these are not scientific evaluation data."""
import hashlib
import json
import tempfile
import unittest
import warnings
from pathlib import Path

from labestra.data import load_cases, load_corpora, load_sources, validate_dataset


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
        return path

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

    def test_missing_required_snapshot(self):
        self.write('data/sources.jsonl', [dict(self.source, snapshot_path=None)])
        with self.assertRaisesRegex(ValueError, 'требуются'):
            validate_dataset(self.root)


if __name__ == '__main__':
    unittest.main()
