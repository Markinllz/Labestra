"""Chunking contract tests; no scientific evaluation or PDF extraction."""
import hashlib
import re
import unittest

from labestra.data import chunk_snapshot


class ChunkingTests(unittest.TestCase):
    def test_default_windows_and_no_overlap_only_tail(self):
        for count, expected in ((1, [1]), (50, [50]), (299, [299]),
                                (300, [300]), (301, [300, 51]),
                                (550, [300, 300]), (551, [300, 300, 51])):
            with self.subTest(count=count):
                text = ' '.join(f'word{i}' for i in range(count))
                fragments = chunk_snapshot('source', text.encode())
                self.assertEqual([len(f.text.split()) for f in fragments], expected)
                self.assertEqual(fragments[0].text.split()[0], 'word0')
                self.assertEqual(fragments[-1].text.split()[-1], f'word{count-1}')
                for left, right in zip(fragments, fragments[1:]):
                    self.assertEqual(left.text.split()[-50:], right.text.split()[:50])

    def test_unicode_crlf_tabs_and_exact_coordinates(self):
        text = '  αβ\tγδ\r\n😀\u2028εζ  ηθ\n'
        blob = text.encode('utf-8')
        fragments = chunk_snapshot('source', blob, chunk_size=3, overlap=1)
        self.assertEqual(fragments[0].text, 'αβ\tγδ\r\n😀')
        self.assertEqual(fragments[1].text, '😀\u2028εζ  ηθ')
        for fragment in fragments:
            _, start, end = fragment.location.split(':')
            self.assertEqual(text[int(start):int(end)], fragment.text)
            self.assertEqual(fragment.snapshot_sha256, hashlib.sha256(blob).hexdigest())

    def test_ids_are_stable_and_bind_snapshot_source_and_range(self):
        blob = b'one two three four five'
        first = chunk_snapshot('source', blob, chunk_size=3, overlap=1)
        self.assertEqual(first, chunk_snapshot('source', blob, chunk_size=3, overlap=1))
        self.assertEqual(len({f.fragment_id for f in first}), len(first))
        for f in first:
            self.assertRegex(f.fragment_id, r'^[0-9a-f]{64}$')
        other_source = chunk_snapshot('other', blob, chunk_size=3, overlap=1)
        other_snapshot = chunk_snapshot('source', blob + b' ', chunk_size=3, overlap=1)
        other_range = chunk_snapshot('source', blob, chunk_size=2, overlap=1)
        for other in (other_source, other_snapshot, other_range):
            self.assertNotEqual(first[0].fragment_id, other[0].fragment_id)

    def test_no_overlap_and_single_word_chunks(self):
        fragments = chunk_snapshot('s', b'one two three', chunk_size=1, overlap=0)
        self.assertEqual([f.text for f in fragments], ['one', 'two', 'three'])

    def test_every_word_covered_without_exceeding_size(self):
        text = ' '.join(str(i) for i in range(1000))
        fragments = chunk_snapshot('s', text.encode())
        covered = set()
        for f in fragments:
            tokens = f.text.split()
            self.assertLessEqual(len(tokens), 300)
            covered.update(tokens)
        self.assertEqual(covered, set(text.split()))

    def test_bad_inputs(self):
        for blob in (b'', b' \t\r\n', b'\xff', 'not bytes'):
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                chunk_snapshot('s', blob)
        for size, overlap in ((0, 0), (-1, 0), (3, 3), (3, -1), (True, 0), (3, True), (3.5, 1)):
            with self.subTest(size=size, overlap=overlap), self.assertRaises(ValueError):
                chunk_snapshot('s', b'one two', chunk_size=size, overlap=overlap)
        with self.assertRaises(ValueError):
            chunk_snapshot('', b'one')


if __name__ == '__main__':
    unittest.main()
