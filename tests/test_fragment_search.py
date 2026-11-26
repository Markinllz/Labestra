"""Corpus-limited fragment search/read contract tests on technical data."""
import sqlite3
import unittest
import warnings
from dataclasses import replace
from unittest.mock import patch

from labestra.schemas import Corpus, Fragment, Source, ToolFailure
from labestra.search import LocalSearch


class FragmentSearchTests(unittest.TestCase):
    def setUp(self):
        sources, fragments = [], []
        for source_id, count in [('a', 12), ('b', 4), ('c', 4), ('d', 4), ('0outside', 5)]:
            sources.append(Source(source_id, 'Title', 'fixture', 'full_text'))
            for i in range(count):
                start = i * 7
                fragments.append(Fragment(f'{source_id}{i:02}', source_id, 'target',
                                          f'chars:{start}:{start+6}', 'fixture-hash'))
        self.records = {f.fragment_id: f for f in fragments}
        self.tools = LocalSearch(sources=sources, fragments=list(reversed(fragments)),
                                 config={'timeout_seconds': 30}, collection_id='collection')
        self.addCleanup(self.tools._db.close)
        self.corpus = Corpus('revision', 'collection', ['a', 'b', 'c', 'd'])

    def assert_code(self, code, function, *args):
        with self.assertRaises(ToolFailure) as ctx:
            function(*args)
        self.assertEqual(ctx.exception.error.code, code)
        self.assertEqual(ctx.exception.error.tool_call_id, '')
        return ctx.exception

    def test_limits_scope_order_and_repeat(self):
        result = self.tools.search_fragments('target', self.corpus)
        self.assertEqual([f.fragment_id for f in result],
                         ['a00', 'a01', 'a02', 'b00', 'b01', 'b02', 'c00', 'c01', 'c02', 'd00'])
        self.assertEqual(result, self.tools.search_fragments('TARGET target', self.corpus))
        self.assertTrue(all(f.source_id in self.corpus.source_ids for f in result))
        self.assertEqual([f.fragment_id for f in self.tools.search_fragments(
            'target', replace(self.corpus, source_ids=['d']))], ['d00', 'd01', 'd02'])
        # Switching corpora must not retain membership from a previous call.
        self.assertEqual(self.tools.search_fragments('target', self.corpus), result)

    def test_empty_corpus_and_no_matches(self):
        empty = replace(self.corpus, source_ids=[])
        self.assertEqual(self.tools.search_fragments('target', empty), [])
        self.assertEqual(self.tools.search_fragments('absent', self.corpus), [])
        self.assertEqual(self.tools.read_fragments([], empty), [])
        self.assert_code('outside_corpus', self.tools.read_fragments, ['a00'], empty)

    def test_query_validation_and_literal_syntax(self):
        for query in ('', '???', 'x'*1001, 'x '*101, None, 123):
            self.assert_code('invalid_input', self.tools.search_fragments, query, self.corpus)
        self.assertEqual(self.tools.search_fragments('"; DROP TABLE fragments; --', self.corpus), [])
        self.assertEqual(len(self.tools.search_fragments('target OR missing', self.corpus)), 10)

    def test_corpus_validation_for_both_methods(self):
        for corpus, code in ((None, 'invalid_input'),
                             (replace(self.corpus, corpus_revision_id=''), 'invalid_input'),
                             (replace(self.corpus, collection_id='unknown'), 'not_found'),
                             (replace(self.corpus, source_ids=['unknown']), 'not_found'),
                             (replace(self.corpus, source_ids=['a', 12]), 'invalid_input'),
                             (replace(self.corpus, source_ids='a'), 'invalid_input')):
            with self.subTest(corpus=corpus):
                self.assert_code(code, self.tools.search_fragments, 'target', corpus)
                self.assert_code(code, self.tools.read_fragments, [], corpus)

    def test_read_requested_order_and_exact_records(self):
        ids = ['c02', 'a01', 'b00']
        result = self.tools.read_fragments(ids, self.corpus)
        self.assertEqual(result, [self.records[key] for key in ids])
        self.assertEqual(ids, ['c02', 'a01', 'b00'])

    def test_read_duplicates_and_limit_before_deduplication(self):
        ids = ['b00', 'a00', 'b00']
        with self.assertWarns(UserWarning):
            result = self.tools.read_fragments(ids, self.corpus)
        self.assertEqual([f.fragment_id for f in result], ['b00', 'a00'])
        self.assertEqual(ids, ['b00', 'a00', 'b00'])
        self.assert_code('invalid_input', self.tools.read_fragments, ['a00']*11, self.corpus)
        with self.assertWarns(UserWarning):
            self.assertEqual(len(self.tools.read_fragments(['a00']*10, self.corpus)), 1)

    def test_read_mixed_valid_invalid_requests_fail_atomically(self):
        self.assert_code('not_found', self.tools.read_fragments, ['a00', 'unknown'], self.corpus)
        self.assert_code('outside_corpus', self.tools.read_fragments, ['a00', '0outside00'], self.corpus)
        for ids in ('a00', None, [None], [''], [123]):
            self.assert_code('invalid_input', self.tools.read_fragments, ids, self.corpus)
        with warnings.catch_warnings(record=True) as caught:
            self.assert_code('not_found', self.tools.read_fragments, ['a00', 'a00', 'unknown'], self.corpus)
        self.assertEqual(caught, [])
        self.assertEqual(len(self.tools.read_fragments(['a00'], self.corpus)), 1)

    def test_fragment_sql_timeout_and_recovery(self):
        ranking_started = False

        def trace(statement):
            nonlocal ranking_started
            if statement.startswith('SELECT fragment_id, source_id FROM fragments'):
                ranking_started = True

        self.tools._db.set_trace_callback(trace)
        try:
            with patch('labestra.search.time.monotonic', side_effect=lambda: 31 if ranking_started else 0):
                error = self.assert_code('timeout', self.tools.search_fragments, 'target', self.corpus)
        finally:
            self.tools._db.set_trace_callback(None)
        self.assertEqual(error.__context__.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertTrue(error.error.retryable)
        self.assertEqual(len(self.tools.search_fragments('target', self.corpus)), 10)
        self.assertTrue(self.tools.search_sources('target', {}, 'collection'))

    def test_read_timeout_no_partial_return_and_recovery(self):
        corpus = replace(self.corpus, source_ids=['a'])
        # Expire before reading the second ID, after the first was accumulated.
        with patch('labestra.search.time.monotonic', side_effect=[0, 0, 0, 0, 31]):
            error = self.assert_code('timeout', self.tools.read_fragments, ['a00', 'a01'], corpus)
        self.assertTrue(error.error.retryable)
        self.assertEqual(len(self.tools.read_fragments(['a00', 'a01'], corpus)), 2)

    def test_read_preserves_unicode_and_line_endings(self):
        text = 'αβ\r\nSparse\t recovery 😀'
        source = Source('unicode', 'Unicode', 'fixture', 'full_text')
        fragment = Fragment('unicode-fragment', 'unicode', text, f'chars:0:{len(text)}', 'hash')
        tools = LocalSearch(sources=[source], fragments=[fragment], config={}, collection_id='collection')
        self.addCleanup(tools._db.close)
        corpus = Corpus('unicode-revision', 'collection', ['unicode'])
        result = tools.read_fragments(['unicode-fragment'], corpus)
        self.assertEqual(result, [fragment])
        self.assertEqual(result[0].text.encode(), text.encode())


if __name__ == '__main__':
    unittest.main()
