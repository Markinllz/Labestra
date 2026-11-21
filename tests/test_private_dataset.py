"""Synthetic label fixtures only; these are not scientific annotations."""

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from dataset_tools.acquire import write_jsonl
from dataset_tools.freeze import seal_private
from dataset_tools.validation import private_report
from labestra.data import load_cases, load_sources

ROOT = Path(__file__).resolve().parents[1]


class PrivateDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.private = Path(self.temp.name)
        pool = [s.source_id for s in load_sources(ROOT / "data/sources.jsonl")]
        self.holdout = asdict(load_cases(ROOT / "eval/dev/tasks.jsonl")[0])
        self.holdout.update(case_id="holdout-fixture", split="holdout", group_id="independent-fixture")
        write_jsonl(self.private / "holdout/tasks.jsonl", [self.holdout])
        write_jsonl(self.private / "holdout/corpora.jsonl", [{"corpus_revision_id": "fixture-private-corpus",
                    "collection_id": self.holdout["collection_id"], "source_ids": []}])
        write_jsonl(self.private / "annotation_candidates.jsonl", [])
        for split in ("dev", "visible", "holdout"):
            path = self.private / "holdout/tasks.jsonl" if split == "holdout" else ROOT / f"eval/{split}/tasks.jsonl"
            rows = []
            for case in load_cases(path):
                row = dict(case_id=case.case_id, gold_version="fixture", rubric_version="1", status="needs_review",
                           prepared_by="test", reviewed_by=[], rationale="Technical fixture, no scientific meaning.")
                if case.phase == "J1":
                    row.update(expected_action="clarify", candidate_source_ids=pool, relevant_source_ids=[],
                               source_judgments={sid: {"relevant": False, "rationale": "fixture"} for sid in pool})
                else:
                    row.update(expected_action="insufficient_evidence", corpus_revision_id=case.corpus_revision_id,
                               required_claims=[], limitations=["fixture"])
                rows.append(row)
            write_jsonl(self.private / f"gold/{split}.jsonl", rows)

    def test_private_seal_and_tampering(self):
        seal_private(ROOT, self.private, "fixture-1")
        self.assertEqual(private_report(ROOT, self.private)["holdout"]["cases"], 1)
        with (self.private / "gold/visible.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            private_report(ROOT, self.private)
        with self.assertRaisesRegex(ValueError, "new private version"):
            seal_private(ROOT, self.private, "fixture-1")

    def test_group_overlap_with_holdout_fails_and_does_not_seal(self):
        self.holdout["group_id"] = load_cases(ROOT / "eval/dev/tasks.jsonl")[0].group_id
        write_jsonl(self.private / "holdout/tasks.jsonl", [self.holdout])
        with self.assertRaisesRegex(ValueError, "group crosses splits"):
            seal_private(ROOT, self.private, "fixture-1")
        self.assertFalse((self.private / "manifest.json").exists())

    def test_holdout_collection_must_match_public_inputs(self):
        self.holdout["collection_id"] = "different-collection"
        write_jsonl(self.private / "holdout/tasks.jsonl", [self.holdout])
        with self.assertRaisesRegex(ValueError, "collection mismatch"):
            seal_private(ROOT, self.private, "fixture-1")
