"""Blind annotation packets and agreement over independent, completed labels."""

import argparse
from collections import Counter
import json
from pathlib import Path

from labestra.data import load_cases, load_corpora, load_fragments, load_sources
from .acquire import sha256, write_json, write_jsonl
from .validation import check, private_report, read_json, read_jsonl, text

LABELS = {"action": ["search", "clarify", "refuse", "answer", "insufficient_evidence"],
          "relevance": ["relevant", "not_relevant", "uncertain"],
          "entailment": ["supports", "contradicts", "uncertain"]}


def packet_digest(objects):
    return sha256(json.dumps(objects, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def create_packet(root, private, case_ids, output):
    """Never include primary labels, rationales, gold IDs or review decisions."""
    root, private, output = Path(root), Path(private), Path(output)
    check(not output.exists(), "packet output exists; independent labels must not be overwritten")
    private_report(root, private)
    cases = {c.case_id: c for split in ("dev", "visible") for c in load_cases(root / f"eval/{split}/tasks.jsonl")}
    check(len(case_ids) == len(set(case_ids)) and bool(case_ids), "duplicate or empty selected case IDs")
    check(set(case_ids) <= set(cases), "packet selection must use public cases")
    gold = {r["case_id"]: r for split in ("dev", "visible") for r in read_jsonl(private / f"gold/{split}.jsonl")}
    sources = {s.source_id: s for s in load_sources(root / "data/sources.jsonl")}
    fragments = {f.fragment_id: f for f in load_fragments(root / "data/fragments.jsonl")}
    corpora = {c.corpus_revision_id: c for c in load_corpora(root / "data/corpora.jsonl")}
    objects = []
    for cid in case_ids:
        case, row = cases[cid], gold[cid]
        corpus = corpora.get(case.corpus_revision_id)
        objects.append({"case_id": cid, "object_id": "action", "kind": "action", "question": case.question,
                        "source_ids": corpus.source_ids if corpus else list(sources),
                        "label_options": ["search", "clarify", "refuse"] if case.phase == "J1" else ["answer", "insufficient_evidence", "clarify"]})
        if case.phase == "J1":
            for source in sources.values():
                objects.append({"case_id": cid, "object_id": source.source_id, "kind": "relevance",
                                "question": case.question, "title": source.title, "url": source.url,
                                "availability": source.availability, "snapshot_path": source.snapshot_path,
                                "label_options": LABELS["relevance"]})
        else:
            for i, claim in enumerate(row["required_claims"]):
                for evidence in claim["evidence"]:
                    fragment = fragments[evidence["fragment_id"]]
                    objects.append({"case_id": cid, "object_id": f"claim-{i + 1}/{fragment.fragment_id}",
                                    "kind": "entailment", "question": case.question, "claim": claim["text"],
                                    "conditions": claim["conditions"], "fragment_id": fragment.fragment_id,
                                    "source_id": fragment.source_id, "fragment_text": fragment.text,
                                    "location": fragment.location, "label_options": LABELS["entailment"]})
    # Optional deliberately testable claims supplied by the curator, without labels.
    # They let both humans judge the same counterclaims/uncertain assertions.
    candidates_path = private / "annotation_candidates.jsonl"
    if candidates_path.exists():
        for candidate in read_jsonl(candidates_path):
            check(set(candidate) == {"case_id", "object_id", "claim", "fragment_id"}, "invalid annotation candidate")
            if candidate["case_id"] not in case_ids:
                continue
            case = cases[candidate["case_id"]]
            check(case.phase == "J2", "candidate claim requires J2")
            fragment = fragments.get(candidate["fragment_id"])
            check(fragment is not None and fragment.source_id in corpora[case.corpus_revision_id].source_ids,
                  "candidate evidence outside corpus")
            text(candidate["claim"], "candidate claim")
            objects.append({**candidate, "kind": "entailment", "question": case.question,
                            "source_id": fragment.source_id, "fragment_text": fragment.text,
                            "location": fragment.location, "label_options": LABELS["entailment"]})
    check(len({(o["case_id"], o["object_id"]) for o in objects}) == len(objects), "duplicate packet objects")
    digest = packet_digest(objects)
    packet = {"rubric_version": "1", "packet_sha256": digest, "case_ids": case_ids, "objects": objects,
              "notice": "Unlabelled candidate claims, not approved facts. Assess independently using the whole cited source."}
    output.mkdir(parents=True, mode=0o700)
    write_json(output / "packet.json", packet)
    template = [{"annotator_id": None, "case_id": obj["case_id"], "object_id": obj["object_id"],
                 "kind": obj["kind"], "label": None, "rationale": None, "rubric_version": "1",
                 "packet_sha256": digest} for obj in objects]
    for name in ("ann_1.jsonl", "ann_2.jsonl"):
        write_jsonl(output / name, template)
    return {"cases": len(case_ids), "objects": len(objects), "packet_sha256": digest}


def agreement(packet, first, second):
    check(packet["packet_sha256"] == packet_digest(packet["objects"]), "packet hash mismatch")
    units = {(o["case_id"], o["object_id"]): o for o in packet["objects"]}
    check(bool(units) and len(units) == len(packet["objects"]), "empty or duplicate packet objects")
    def validate(rows):
        check(type(rows) is list and len(rows) == len(units), "incomplete annotation file")
        annotators, mapping = set(), {}
        for row in rows:
            check(set(row) == {"annotator_id", "case_id", "object_id", "kind", "label", "rationale", "rubric_version", "packet_sha256"}, "invalid annotation fields")
            text(row["annotator_id"], "annotator_id")
            text(row["rationale"], "rationale")
            annotators.add(row["annotator_id"])
            key = row["case_id"], row["object_id"]
            check(key in units and key not in mapping, "unknown or duplicate annotation object")
            check(row["packet_sha256"] == packet["packet_sha256"] and row["rubric_version"] == packet["rubric_version"], "annotation version mismatch")
            check(row["kind"] == units[key]["kind"], "annotation kind mismatch")
            check(row["label"] in units[key]["label_options"], "missing or invalid annotation label")
            mapping[key] = row
        check(len(annotators) == 1, "one author required per annotation file")
        return annotators.pop(), mapping
    a_id, a = validate(first)
    b_id, b = validate(second)
    check(a_id != b_id, "independent annotators required")
    result = {}
    for kind in sorted({o["kind"] for o in units.values()}):
        keys = [k for k in units if units[k]["kind"] == kind]
        n = len(keys)
        matched = sum(a[k]["label"] == b[k]["label"] for k in keys)
        ac, bc = Counter(a[k]["label"] for k in keys), Counter(b[k]["label"] for k in keys)
        expected_numerator = sum(ac[label] * bc[label] for label in set(ac) | set(bc))
        denominator = n * n - expected_numerator
        result[kind] = {"matched": matched, "total": n, "agreement": matched / n,
                        "cohen_kappa": (matched * n - expected_numerator) / denominator if denominator else None,
                        "kappa_limitation": "undefined: constant marginals" if not denominator else None,
                        "observed_labels": sorted(set(ac) | set(bc)),
                        "disagreements": [{"case_id": k[0], "object_id": k[1], "first": a[k]["label"], "second": b[k]["label"]}
                                          for k in keys if a[k]["label"] != b[k]["label"]]}
    return {"annotators": [a_id, b_id], "packet_sha256": packet["packet_sha256"], "by_kind": result,
            "note": "Compute before reconciliation; no gold approval or answer-quality score is implied."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("packet")
    create.add_argument("--root", type=Path, default=Path("."))
    create.add_argument("--private", type=Path, required=True)
    create.add_argument("--cases", nargs="+", required=True)
    create.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("agreement")
    compare.add_argument("--packet", type=Path, required=True)
    compare.add_argument("--first", type=Path, required=True)
    compare.add_argument("--second", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (create_packet(args.root, args.private, args.cases, args.output) if args.command == "packet" else
                  agreement(read_json(args.packet), read_jsonl(args.first), read_jsonl(args.second)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"review validation failed: {exc}\n")


if __name__ == "__main__":
    main()
