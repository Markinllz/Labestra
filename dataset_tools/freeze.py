"""Seal public inputs without publishing gold or claiming human approval."""

import argparse
from pathlib import Path

from labestra.data import load_sources, validate_dataset
from .acquire import sha256, write_json
from .validation import read_json

PUBLIC_INPUTS = ("data/sources.jsonl", "data/fragments.jsonl", "data/corpora.jsonl",
                 "eval/dev/tasks.jsonl", "eval/visible/tasks.jsonl")
METADATA = ("data/catalog.json", "data/provenance.jsonl", "data/aliases.json", "data/dataset.json")


def freeze(root, dataset_version, collection_id):
    root = Path(root)
    for name in (*METADATA, "data/manifest.json", "data/release.json"):
        if (root / name).resolve() != root.resolve() / name:
            raise ValueError("public metadata symlink is forbidden")
    paths = set(PUBLIC_INPUTS)
    paths.update(s.snapshot_path for s in load_sources(root / "data/sources.jsonl") if s.snapshot_path)
    manifest = {"manifest_version": "1", "schema_version": "1.1", "dataset_version": dataset_version,
                "collection_id": collection_id,
                "files": {name: sha256((root / name).read_bytes()) for name in sorted(paths)}}
    path = root / "data/manifest.json"
    if path.exists():
        previous = read_json(path)
        if previous["dataset_version"] == dataset_version and previous != manifest:
            raise ValueError("changed inputs need a new dataset version")
    previous = path.read_bytes() if path.exists() else None
    write_json(path, manifest)
    try:
        validate_dataset(root)
        release = {"dataset_version": dataset_version, "files": {
            name: sha256((root / name).read_bytes())
            for name in sorted(paths | set(METADATA) | {"data/manifest.json"})}}
        release_path = root / "data/release.json"
        if release_path.exists():
            old_release = read_json(release_path)
            if old_release["dataset_version"] == dataset_version and old_release != release:
                raise ValueError("changed provenance needs a new dataset version")
        write_json(release_path, release)
    except Exception:
        if previous is None:
            path.unlink()
        else:
            path.write_bytes(previous)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--version", required=True)
    parser.add_argument("--collection", required=True)
    args = parser.parse_args()
    freeze(args.root, args.version, args.collection)


if __name__ == "__main__":
    main()
