"""Technical integration of participant 2 modules, not runner/LLM evaluation."""
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from labestra.data import (
    chunk_snapshot, load_cases, load_corpora, load_fragments, load_sources, validate_dataset,
)
from labestra.schemas import Case, Corpus, Source
from labestra.search import LocalSearch


class DataSearchIntegrationTests(unittest.TestCase):
    def test_manifest_to_exact_evidence_from_reloaded_files(self):
        with tempfile.TemporaryDirectory(prefix='labestra-fixture-') as directory:
            root = Path(directory)
            sources, fragments = [], []
            for source_id, title, text in (
                ('s1', 'Sparse recovery', 'Sparse recovery uses linear measurements.\r\n'),
                ('s2', 'Noisy recovery', 'Recovery from noisy measurements requires error bounds.'),
            ):
                blob = text.encode('utf-8')
                relative = f'data/snapshots/{source_id}.txt'
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)
                sources.append(Source(source_id, title, 'technical-fixture', 'full_text',
                                      snapshot_path=relative, sha256=hashlib.sha256(blob).hexdigest()))
                fragments.extend(chunk_snapshot(source_id, blob))
            corpus = Corpus('fixture-corpus', 'fixture-collection', ['s1'])
            entries = {
                'data/sources.jsonl': sources,
                'data/fragments.jsonl': fragments,
                'data/corpora.jsonl': [corpus],
            }
            for split in ('dev', 'visible'):
                entries[f'eval/{split}/tasks.jsonl'] = [Case(
                    f'{split}-case', 'J2', split, 'technical-fixture', split,
                    'What is used for recovery?', 'fixture-collection',
                    search_query='recovery', corpus_revision_id=corpus.corpus_revision_id,
                )]
            for name, records in entries.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(''.join(json.dumps(asdict(row)) + '\n' for row in records), encoding='utf-8')
            names = list(entries) + [source.snapshot_path for source in sources]
            manifest = {
                'manifest_version': '1', 'schema_version': '1.1',
                'dataset_version': 'technical-fixture-v1', 'collection_id': 'fixture-collection',
                'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
            }
            (root / 'data/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            before = {name: (root / name).read_bytes() for name in names}
            validate_dataset(root)
            case = load_cases(root / 'eval/dev/tasks.jsonl')[0]
            corpora = {c.corpus_revision_id: c for c in load_corpora(root / 'data/corpora.jsonl')}
            results = []
            # Rebuild from persisted files twice, without reusing dataclass objects.
            for _ in range(2):
                tools = LocalSearch(
                    sources=load_sources(root / 'data/sources.jsonl'),
                    fragments=load_fragments(root / 'data/fragments.jsonl'),
                    config={'top_k': 20, 'timeout_seconds': 30},
                    collection_id=manifest['collection_id'],
                )
                try:
                    retrieved = tools.search_sources(case.search_query, {}, case.collection_id)
                    self.assertEqual({s.source_id for s in retrieved}, {'s1', 's2'})
                    found = tools.search_fragments(case.search_query, corpora[case.corpus_revision_id])
                    evidence = tools.read_fragments([f.fragment_id for f in found], corpora[case.corpus_revision_id])
                    self.assertEqual(len(evidence), 1)
                    self.assertEqual(evidence[0].source_id, 's1')
                    _, start, end = evidence[0].location.split(':')
                    snapshot = (root / sources[0].snapshot_path).read_bytes().decode('utf-8')
                    self.assertEqual(snapshot[int(start):int(end)], evidence[0].text)
                    results.append(([s.source_id for s in retrieved], evidence))
                finally:
                    tools._db.close()
            self.assertEqual(results[0], results[1])
            self.assertEqual(before, {name: (root / name).read_bytes() for name in names})


if __name__ == '__main__':
    unittest.main()
