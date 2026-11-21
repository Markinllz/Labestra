"""Integration over the real public corpus; no scientific quality scores."""

import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from dataset_tools.freeze import freeze
from dataset_tools.validation import public_report
from labestra.data import load_cases, load_corpora, load_fragments, load_sources
from labestra.search import LocalSearch

ROOT = Path(__file__).resolve().parents[1]


class ScientificDatasetTests(unittest.TestCase):
    def test_extra_metadata_cannot_point_at_private_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "data", root / "data")
            for split in ("dev", "visible"):
                shutil.copytree(ROOT / "eval" / split, root / "eval" / split)
            target = root / "eval/private/secret.json"
            target.parent.mkdir(parents=True)
            target.write_text("must not be parsed", encoding="utf-8")
            alias = root / "data/aliases.json"
            alias.unlink()
            alias.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                public_report(root)
            with self.assertRaisesRegex(ValueError, "symlink"):
                freeze(root, "new-version", "labestra-retrieval-2026-11-v1")

    def test_public_export_works_without_any_private_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "data", root / "data")
            for split in ("dev", "visible"):
                shutil.copytree(ROOT / "eval" / split, root / "eval" / split)
            report = public_report(root)
            self.assertEqual(report["sources"], 30)
            self.assertEqual(report["cases"], {"dev": {"J1": 3, "J2": 3}, "visible": {"J1": 12, "J2": 12}})
            self.assertFalse((root / "eval/private").exists())
            snapshot = next((root / "data/snapshots").glob("*.txt"))
            snapshot.write_bytes(snapshot.read_bytes() + b"tampered")
            with self.assertRaises(ValueError):
                public_report(root)
            with self.assertRaisesRegex(ValueError, "new dataset version"):
                freeze(root, "0.1.0-draft", "labestra-retrieval-2026-11-v1")

    def test_real_data_search_and_exact_reading(self):
        sources = load_sources(ROOT / "data/sources.jsonl")
        fragments = load_fragments(ROOT / "data/fragments.jsonl")
        corpora = {c.corpus_revision_id: c for c in load_corpora(ROOT / "data/corpora.jsonl")}
        tools = LocalSearch(sources=sources, fragments=fragments, config={"top_k": 20},
                            collection_id="labestra-retrieval-2026-11-v1")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "data").rglob("*") if p.is_file()}
        for split in ("dev", "visible"):
            for case in load_cases(ROOT / f"eval/{split}/tasks.jsonl"):
                if not case.search_query:
                    continue
                with self.subTest(case_id=case.case_id):
                    if case.phase == "J1":
                        first = tools.search_sources(case.search_query, {}, case.collection_id)
                        self.assertEqual(first, tools.search_sources(case.search_query, {}, case.collection_id))
                        self.assertLessEqual(len(first), 20)
                    else:
                        corpus = corpora[case.corpus_revision_id]
                        hits = tools.search_fragments(case.search_query, corpus)
                        exact = tools.read_fragments([f.fragment_id for f in hits], corpus)
                        self.assertEqual(hits, exact)
                        self.assertTrue(all(f.source_id in corpus.source_ids for f in hits))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
