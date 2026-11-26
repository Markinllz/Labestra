from copy import deepcopy
import unittest

from dataset_tools.validation import validate_gold_rows
from labestra.schemas import Case, Corpus, Fragment, Source


class GoldValidationTests(unittest.TestCase):
    def setUp(self):
        self.case = Case("c1", "J2", "dev", "fixture", "g1", "Compare within dataset", "col", "method result", "corpus")
        self.sources = {"s1": Source("s1", "Fixture", "synthetic test", "abstract")}
        self.fragments = {"f1": Fragment("f1", "s1", "Method A improves on dataset X.", "chars:0:31", "hash")}
        self.corpora = {"corpus": Corpus("corpus", "col", ["s1"])}
        self.row = dict(case_id="c1", gold_version="1", rubric_version="1", status="needs_review", prepared_by="codex",
                        reviewed_by=[], expected_action="answer", rationale="Technical fixture, not science.",
                        corpus_revision_id="corpus", limitations=["Only dataset X"], required_claims=[{
                            "gold_claim_id": "gc1", "text": "Method A improves on dataset X.",
                            "conditions": {k: "fixture scope" for k in ("task", "dataset", "method", "comparison", "metric", "scope")},
                            "acceptable_paraphrases": [], "evidence": [{"fragment_id": "f1", "relation": "supports", "quote": "on dataset X"}]}])

    def validate(self, row=None, **kwargs):
        return validate_gold_rows([self.case], [row or self.row], self.sources, self.fragments, self.corpora, **kwargs)

    def test_draft_is_structurally_valid_but_not_reviewed(self):
        self.assertEqual(self.validate(), {"cases": 1, "reviewed": 0})
        with self.assertRaisesRegex(ValueError, "human review required"):
            self.validate(require_reviewed=True)

    def test_one_recorded_human_review_approves_gold_not_annotation_agreement(self):
        self.row.update(status="reviewed", reviewed_by=["person1"])
        self.assertEqual(self.validate(require_reviewed=True), {"cases": 1, "reviewed": 1})
        self.row["reviewed_by"] = []
        with self.assertRaisesRegex(ValueError, "human review required"):
            self.validate(require_reviewed=True)

    def test_bad_evidence(self):
        for field, value in (("fragment_id", "missing"), ("relation", "verified"), ("quote", "Method A always wins")):
            with self.subTest(field=field):
                row = deepcopy(self.row)
                row["required_claims"][0]["evidence"][0][field] = value
                with self.assertRaises(ValueError):
                    self.validate(row)

    def test_cross_corpus(self):
        self.corpora["corpus"] = Corpus("corpus", "col", [])
        with self.assertRaisesRegex(ValueError, "outside corpus"):
            self.validate()

    def test_conditions_cannot_disappear(self):
        del self.row["required_claims"][0]["conditions"]["dataset"]
        with self.assertRaisesRegex(ValueError, "experiment conditions"):
            self.validate()

    def test_empty_answer_and_missing_gold(self):
        self.row["required_claims"] = []
        with self.assertRaisesRegex(ValueError, "requires claims"):
            self.validate()
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_gold_rows([self.case], [], self.sources, self.fragments, self.corpora)

    def test_duplicate_claim_ids(self):
        self.row["required_claims"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate ID"):
            self.validate()

    def test_review_metadata_is_not_automatic_approval(self):
        self.row.update(status="reviewed", reviewed_by=["codex", "person2"])
        with self.assertRaisesRegex(ValueError, "independent"):
            self.validate()

    def test_j1_requires_full_pool_judgments_and_no_alias_duplicates(self):
        case = Case("search", "J1", "dev", "fixture", "g", "Find", "col", "query")
        pool = {f"s{i}": Source(f"s{i}", "Fixture", "test", "metadata") for i in range(21)}
        row = {k: self.row[k] for k in ("gold_version", "rubric_version", "status", "prepared_by", "reviewed_by", "rationale")}
        row.update(case_id="search", expected_action="search", candidate_source_ids=list(pool), relevant_source_ids=["s1"],
                   source_judgments={sid: {"relevant": sid == "s1", "rationale": "fixture"} for sid in pool})
        self.assertEqual(validate_gold_rows([case], [row], pool, {}, {})["cases"], 1)
        row["relevant_source_ids"].append("s1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_gold_rows([case], [row], pool, {}, {})
        row["relevant_source_ids"] = ["s1"]
        del row["source_judgments"]["s20"]
        with self.assertRaisesRegex(ValueError, "every candidate"):
            validate_gold_rows([case], [row], pool, {}, {})
