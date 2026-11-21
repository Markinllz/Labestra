"""Maintainer-only checks. Structural validity never means scientific approval."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from labestra.data import load_cases, load_corpora, load_fragments, load_sources, validate_dataset
from .identifiers import validate_aliases


def check(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: check(False, "non-finite JSON number"))


def read_jsonl(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        if line.strip():
            # Use the same duplicate-key rejection as JSON files.
            def pairs(items):
                result = {}
                for k, v in items:
                    check(k not in result, "duplicate JSON key")
                    result[k] = v
                return result
            row = json.loads(line, object_pairs_hook=pairs,
                             parse_constant=lambda _: check(False, "non-finite JSON number"))
            check(type(row) is dict, "JSONL row must be an object")
            rows.append(row)
    return rows


def unique(values, name):
    check(type(values) is list and all(type(x) is str and x.strip() for x in values), f"{name}: expected IDs")
    check(len(values) == len(set(values)), f"{name}: duplicate ID")
    return set(values)


def text(value, name):
    check(type(value) is str and bool(value.strip()), f"{name}: required text")


def public_report(root):
    root = Path(root)
    validate_dataset(root)
    from .freeze import METADATA
    # Extra maintainer metadata must not redirect the public validator to gold.
    for name in (*METADATA, "data/release.json"):
        check((root / name).resolve() == root.resolve() / name, "public metadata symlink is forbidden")
    manifest = read_json(root / "data/manifest.json")
    release = read_json(root / "data/release.json")
    expected_paths = set(manifest["files"]) | set(METADATA) | {"data/manifest.json"}
    check(set(release["files"]) == expected_paths, "release file coverage mismatch")
    check(release["dataset_version"] == manifest["dataset_version"], "release version mismatch")
    for name, expected in release["files"].items():
        target = root / name
        check(target.resolve().is_relative_to(root.resolve()), "release path escapes root")
        check(hashlib.sha256(target.read_bytes()).hexdigest() == expected, "release hash mismatch")
    sources = load_sources(root / "data/sources.jsonl")
    fragments = load_fragments(root / "data/fragments.jsonl")
    check(bool(sources) and bool(fragments), "empty scientific dataset")
    validate_aliases(sources, read_json(root / "data/aliases.json"))
    provenance = read_jsonl(root / "data/provenance.jsonl")
    unique([r["source_id"] for r in provenance], "provenance")
    by_id = {s.source_id: s for s in sources}
    check({r["source_id"] for r in provenance} == set(by_id), "provenance coverage mismatch")
    for row in provenance:
        check(row["snapshot_sha256"] == by_id[row["source_id"]].sha256, "provenance snapshot mismatch")
        for name in ("original_url", "license", "license_basis", "transformation", "retrieved_at_utc"):
            text(row.get(name), name)
        check(re.fullmatch(r"[0-9a-f]{64}", row.get("original_sha256", "")) is not None, "invalid original hash")
        check(bool(row.get("authors")), "missing authors")
    counts = {}
    for split in ("dev", "visible"):
        cases = load_cases(root / f"eval/{split}/tasks.jsonl")
        check(bool(cases), f"empty {split}")
        for case in cases:
            text(case.question, "question")
            check(len(case.search_query) <= 1000, "search query too long")
        counts[split] = dict(Counter(c.phase for c in cases))
    return {"sources": len(sources), "availability": dict(Counter(s.availability for s in sources)),
            "fragments": len(fragments), "cases": counts, "scientific_status": "needs_human_review"}


def validate_gold_rows(cases, rows, sources, fragments, corpora, require_reviewed=False):
    """Check all referenced objects and scoped quotations; no semantic LLM judge."""
    check(len({c.case_id for c in cases}) == len(cases), "duplicate task ID")
    by_case = {c.case_id: c for c in cases}
    unique([row.get("case_id") for row in rows], "gold case IDs")
    check({r["case_id"] for r in rows} == set(by_case), "gold/task coverage mismatch")
    for row in rows:
        case = by_case[row["case_id"]]
        base = {"case_id", "gold_version", "rubric_version", "status", "prepared_by", "reviewed_by",
                "expected_action", "rationale"}
        fields = {"candidate_source_ids", "relevant_source_ids", "source_judgments"} if case.phase == "J1" else {
            "corpus_revision_id", "required_claims", "limitations"}
        check(set(row) == base | fields, "unexpected or missing gold fields")
        for name in ("gold_version", "rubric_version", "prepared_by", "rationale"):
            text(row[name], name)
        check(row["status"] in ("needs_review", "reviewed"), "invalid review status")
        reviewers = unique(row["reviewed_by"], "reviewers")
        if row["status"] == "reviewed" or require_reviewed:
            check(row["status"] == "reviewed" and len(reviewers) >= 2, "two human reviews required")
            check(not reviewers.intersection({row["prepared_by"], "codex", "assistant"}), "review must be independent")
        action = row["expected_action"]
        if case.phase == "J1":
            check(action in ("search", "clarify", "refuse"), "invalid J1 action")
            pool = unique(row["candidate_source_ids"], "candidate pool")
            relevant = unique(row["relevant_source_ids"], "relevant IDs")
            check(pool == set(sources), "J1 pool must equal the fixed collection")
            check(relevant <= pool, "relevant source outside pool")
            judgments = row["source_judgments"]
            check(type(judgments) is dict and set(judgments) == pool, "every candidate needs a judgment")
            for sid, judgment in judgments.items():
                check(type(judgment) is dict and set(judgment) == {"relevant", "rationale"}, "invalid source judgment")
                check(type(judgment["relevant"]) is bool and judgment["relevant"] == (sid in relevant), "inconsistent relevance")
                text(judgment["rationale"], "source rationale")
            if action == "search":
                check(len(pool) > 20, "search evaluation needs >20 candidates")
                text(case.search_query, "search_query")
            else:
                check(not relevant, "non-search action cannot have relevant results")
        else:
            check(action in ("answer", "insufficient_evidence", "clarify"), "invalid J2 action")
            check(row["corpus_revision_id"] == case.corpus_revision_id, "gold corpus mismatch")
            check(case.corpus_revision_id in corpora, "unknown J2 corpus")
            corpus = corpora[case.corpus_revision_id]
            check(corpus.collection_id == case.collection_id, "corpus collection mismatch")
            check(set(corpus.source_ids) <= set(sources), "unknown corpus source")
            claims = row["required_claims"]
            check(type(claims) is list, "required_claims must be a list")
            unique([c.get("gold_claim_id") for c in claims], "gold claim IDs")
            check(type(row["limitations"]) is list, "limitations must be a list")
            for limitation in row["limitations"]:
                text(limitation, "limitation")
            if action == "answer":
                check(bool(claims), "answer requires claims")
                text(case.search_query, "search_query")
            if action == "insufficient_evidence":
                check(bool(row["limitations"]), "abstention needs an explicit limitation")
            for claim in claims:
                check(set(claim) == {"gold_claim_id", "text", "conditions", "acceptable_paraphrases", "evidence"}, "invalid claim fields")
                text(claim["text"], "claim text")
                conditions = claim["conditions"]
                check(type(conditions) is dict and set(conditions) == {"task", "dataset", "method", "comparison", "metric", "scope"}, "missing experiment conditions")
                for key, value in conditions.items():
                    text(value, key)
                check(type(claim["acceptable_paraphrases"]) is list, "paraphrases must be a list")
                for value in claim["acceptable_paraphrases"]:
                    text(value, "paraphrase")
                check(type(claim["evidence"]) is list and bool(claim["evidence"]), "claim needs evidence")
                for evidence in claim["evidence"]:
                    check(set(evidence) == {"fragment_id", "relation", "quote"}, "invalid evidence fields")
                    check(evidence["relation"] in ("supports", "contradicts", "uncertain"), "invalid relation")
                    fragment = fragments.get(evidence["fragment_id"])
                    check(fragment is not None, "missing evidence fragment")
                    check(fragment.source_id in corpus.source_ids, "evidence outside corpus")
                    text(evidence["quote"], "quote")
                    check(evidence["quote"] in fragment.text, "quote must be an exact fragment substring")
    return {"cases": len(cases), "reviewed": sum(r["status"] == "reviewed" for r in rows)}


def private_report(root, private, require_reviewed=False):
    root, private = Path(root), Path(private)
    public_report(root)
    from .freeze import PRIVATE_INPUTS
    seal = read_json(private / "manifest.json")
    text(seal.get("private_version"), "private_version")
    check(set(seal["files"]) == set(PRIVATE_INPUTS), "private manifest coverage mismatch")
    check(seal["public_manifest_sha256"] == hashlib.sha256((root / "data/manifest.json").read_bytes()).hexdigest(),
          "private gold is bound to different public inputs")
    for name, expected in seal["files"].items():
        check(hashlib.sha256((private / name).read_bytes()).hexdigest() == expected, "private file hash mismatch")
    collection_id = read_json(root / "data/manifest.json")["collection_id"]
    sources = {s.source_id: s for s in load_sources(root / "data/sources.jsonl")}
    fragments = {f.fragment_id: f for f in load_fragments(root / "data/fragments.jsonl")}
    corpora = {c.corpus_revision_id: c for c in load_corpora(root / "data/corpora.jsonl")}
    private_corpora = load_corpora(private / "holdout/corpora.jsonl")
    for corpus in private_corpora:
        check(corpus.collection_id == collection_id and set(corpus.source_ids) <= set(sources), "invalid private corpus")
    check(not set(corpora).intersection(c.corpus_revision_id for c in private_corpora), "duplicate private corpus")
    corpora.update({c.corpus_revision_id: c for c in private_corpora})
    groups, ids, result = {}, set(), {}
    for split in ("dev", "visible", "holdout"):
        path = private / "holdout/tasks.jsonl" if split == "holdout" else root / f"eval/{split}/tasks.jsonl"
        cases = load_cases(path)
        check(bool(cases), "empty split")
        for case in cases:
            check(case.split == split, "task split mismatch")
            check(case.collection_id == collection_id, "private task collection mismatch")
            check(case.case_id not in ids, "case ID crosses splits")
            ids.add(case.case_id)
            check(case.group_id not in groups or groups[case.group_id] == split, "group crosses splits")
            groups[case.group_id] = split
        result[split] = validate_gold_rows(cases, read_jsonl(private / f"gold/{split}.jsonl"),
                                           sources, fragments, corpora, require_reviewed)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--private", type=Path, help="explicit maintainer-only directory; never auto-discovered")
    parser.add_argument("--require-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        check(not args.require_reviewed or args.private is not None, "review gate requires --private")
        result = private_report(args.root, args.private, args.require_reviewed) if args.private else public_report(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"dataset validation failed: {exc}\n")


if __name__ == "__main__":
    main()
