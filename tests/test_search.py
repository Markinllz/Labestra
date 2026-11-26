"""Source-search contract tests on technical fixtures, without an LLM."""
import hashlib
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from labestra.data import chunk_snapshot
from labestra.schemas import Fragment, Source, ToolFailure
from labestra.search import LocalSearch, _source_text


class SourceSearchTests(unittest.TestCase):
    def build(self, documents, *, top_k=20, reverse=False):
        sources, fragments = [], []
        for source_id, title, body in documents:
            blob = body.encode()
            sources.append(Source(source_id, title, 'fixture', 'full_text',
                                  sha256=hashlib.sha256(blob).hexdigest()))
            if body:
                fragments.extend(chunk_snapshot(source_id, blob, chunk_size=3, overlap=1))
        if reverse:
            sources.reverse()
            fragments.reverse()
        search = LocalSearch(sources=sources, fragments=fragments,
                             config={'top_k': top_k, 'timeout_seconds': 30}, collection_id='c')
        self.addCleanup(search._db.close)
        return search

    def ids(self, search, query, filters=None, collection='c'):
        return [s.source_id for s in search.search_sources(query, {} if filters is None else filters, collection)]

    def test_ranking_and_repeated_query(self):
        search = self.build([('a', 'Other', 'geometry geometry geometry'),
                             ('b', 'Sparse recovery', 'sparse sparse recovery'),
                             ('c', 'Other', 'sparse geometry geometry')])
        result = self.ids(search, 'sparse recovery')
        self.assertEqual(result[0], 'b')
        self.assertEqual(set(result), {'b', 'c'})
        self.assertEqual(self.ids(search, 'sparse recovery'), result)
        self.assertEqual(self.ids(search, 'SPARSE sparse recovery'), result)

    def test_ties_stable_independent_of_input_order(self):
        docs = [(key, 'same', 'identical terms') for key in ('c', 'a', 'b')]
        for reverse in (False, True):
            search = self.build(docs, reverse=reverse)
            self.assertEqual(self.ids(search, 'identical'), ['a', 'b', 'c'])

    def test_top_k_empty_and_small_result(self):
        docs = [(f's{i:02}', 'same', 'match') for i in range(30)]
        search = self.build(docs)
        self.assertEqual(len(self.ids(search, 'match')), 20)
        self.assertEqual(self.ids(search, 'absent'), [])
        small = self.build(docs[:2])
        self.assertEqual(len(self.ids(small, 'match')), 2)
        limited = self.build(docs, top_k=3)
        self.assertEqual(len(self.ids(limited, 'match')), 3)

    def test_literal_query_syntax_and_no_stemming(self):
        search = self.build([('a', 'Sparse', 'recovery'), ('b', 'Geometry', 'circle'),
                             ('c', 'Operators', 'OR')])
        self.assertEqual(set(self.ids(search, 'sparse AND geometry')), {'a', 'b'})
        self.assertEqual(self.ids(search, '"OR"'), ['c'])
        self.assertEqual(self.ids(search, 'recover'), [])
        self.assertEqual(self.ids(search, '"; DROP TABLE sources; --'), [])
        self.assertEqual(self.ids(search, 'sparse'), ['a'])

    def test_invalid_inputs(self):
        search = self.build([('a', 'Sparse', 'recovery')])
        for query in ('', '   ', '---?!', 'a'*1001, 'a '*101, None, 42):
            with self.subTest(query=query), self.assertRaises(ToolFailure) as ctx:
                search.search_sources(query, {}, 'c')
            self.assertEqual(ctx.exception.error.code, 'invalid_input')
            self.assertFalse(ctx.exception.error.retryable)
        for filters in ({'year': '2020'}, [], None):
            with self.assertRaises(ToolFailure) as ctx:
                search.search_sources('sparse', filters, 'c')
            self.assertEqual(ctx.exception.error.code, 'invalid_input')
        with self.assertRaises(ToolFailure) as ctx:
            self.ids(search, 'sparse', collection='unknown')
        self.assertEqual(ctx.exception.error.code, 'not_found')

    def test_sqlite_timeout_interrupts_and_next_call_recovers(self):
        search = self.build([(f's{i}', 'Sparse', 'recovery') for i in range(300)])
        # Expire only after SQLite starts the ranking SELECT. The exception
        # context below proves the VM was interrupted, not just a post-call check.
        ranking_started = False

        def trace(statement):
            nonlocal ranking_started
            if statement.startswith('SELECT source_id FROM sources'):
                ranking_started = True

        search._db.set_trace_callback(trace)
        try:
            with patch('labestra.search.time.monotonic', side_effect=lambda: 31 if ranking_started else 0):
                with self.assertRaises(ToolFailure) as ctx:
                    self.ids(search, 'sparse')
        finally:
            search._db.set_trace_callback(None)
        self.assertEqual(ctx.exception.error.code, 'timeout')
        self.assertEqual(ctx.exception.__context__.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertTrue(ctx.exception.error.retryable)
        self.assertEqual(ctx.exception.error.tool_call_id, '')
        self.assertEqual(len(self.ids(search, 'sparse')), 20)

    def test_overlapping_text_is_counted_once(self):
        text = 'one two three four five six seven'
        fragments = chunk_snapshot('s', text.encode(), chunk_size=3, overlap=1)
        self.assertEqual(_source_text(list(reversed(fragments))), text)
        self.assertEqual(_source_text(fragments + [fragments[0]]), text)
        repeated = 'one two one two'
        self.assertEqual(_source_text(chunk_snapshot('s', repeated.encode(), chunk_size=2, overlap=0)), repeated)

    def test_disjoint_ranges_and_conflicting_overlap(self):
        a = Fragment('a', 's', 'one', 'chars:0:3', 'hash')
        b = Fragment('b', 's', 'two', 'chars:10:13', 'hash')
        self.assertEqual(_source_text([b, a]), 'one two')
        for bad in (replace(a, text='bad'), replace(a, snapshot_sha256='other'),
                    replace(a, location='chars:0:99')):
            with self.assertRaises(ToolFailure) as ctx:
                _source_text([a, bad])
            self.assertEqual(ctx.exception.error.code, 'invalid_input')

    def test_metadata_title_only_and_empty_collection(self):
        for sources, expected in (([Source('s', 'Sparse recovery', 'fixture', 'metadata')], ['s']), ([], [])):
            search = LocalSearch(sources=sources, fragments=[], config={}, collection_id='c')
            self.addCleanup(search._db.close)
            self.assertEqual(self.ids(search, 'sparse'), expected)

    def test_invalid_config_and_source_references(self):
        for config in ({'top_k': 21}, {'top_k': True}, {'timeout_seconds': 0},
                       {'timeout_seconds': float('nan')}, {'timeout_seconds': 10**400}):
            with self.subTest(config=config), self.assertRaises(ToolFailure) as ctx:
                LocalSearch(sources=[], fragments=[], config=config, collection_id='c')
            self.assertEqual(ctx.exception.error.code, 'invalid_input')
        fragment = Fragment('f', 'missing', 'text', 'chars:0:4', 'hash')
        with self.assertRaises(ToolFailure):
            LocalSearch(sources=[], fragments=[fragment], config={}, collection_id='c')


if __name__ == '__main__':
    unittest.main()
