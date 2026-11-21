"""Explicit, bounded acquisition from ACL. Offline verification never calls this."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

from labestra.data import chunk_snapshot
from labestra.schemas import Source
from .identifiers import normalize_external_id


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in values), encoding="utf-8")


class ACLPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}
        self.abstract = []
        self.abstract_div = 0
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").startswith("citation_"):
            self.metadata.setdefault(attrs["name"], []).append(attrs.get("content", ""))
        if tag == "div":
            if self.abstract_div:
                self.abstract_div += 1
            elif "acl-abstract" in attrs.get("class", "").split():
                self.abstract_div = 1
        if self.abstract_div and tag == "h5":
            self.in_heading = True

    def handle_endtag(self, tag):
        if tag == "div" and self.abstract_div:
            self.abstract_div -= 1
        if tag == "h5":
            self.in_heading = False

    def handle_data(self, data):
        if self.abstract_div and not self.in_heading:
            self.abstract.append(data)


def acquire(catalog, root, cache):
    """Create a new source export. Never replace existing source identities."""
    if (root / "data/sources.jsonl").exists():
        raise ValueError("sources already exist; use a new output directory/version")
    sources, fragments, provenance = [], [], []
    cache.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()
    for item in catalog:
        acl_id = item["anthology_id"]
        if not re.fullmatch(r"(?:\d{4}\.[a-z0-9-]+\.\d+|[A-Z]\d{2}-\d{4})", acl_id):
            raise ValueError("invalid Anthology ID")
        url = f"https://aclanthology.org/{acl_id}/"

        def fetch(url, suffix):
            path = cache / f"{acl_id}.{suffix}"
            if not path.exists():
                with urlopen(Request(url, headers={"User-Agent": "Labestra-research-dataset/0.1"}), timeout=30) as response:
                    data = response.read(20_000_001)
                    if len(data) > 20_000_000:
                        raise ValueError("source exceeds 20 MB acquisition limit")
                path.write_bytes(data)
            return path

        page_path = fetch(url, "html")
        page = ACLPage()
        page.feed(page_path.read_text(encoding="utf-8"))
        meta = page.metadata
        title = meta["citation_title"][0]
        doi = normalize_external_id(meta["citation_doi"][0])
        source_id = "acl:" + acl_id
        abstract = "".join(page.abstract).strip()
        mode = item["availability"]
        original_hash = sha256(page_path.read_bytes())
        original_url = url
        extraction = "HTML acl-abstract text, stripped outer whitespace; UTF-8 + LF"
        if mode == "full_text":
            original_url = meta["citation_pdf_url"][0]
            if not original_url.startswith("https://aclanthology.org/"):
                raise ValueError("unexpected PDF host")
            pdf = fetch(original_url, "pdf")
            if not pdf.read_bytes().startswith(b"%PDF-"):
                raise ValueError("response is not a PDF")
            text_path = cache / f"{acl_id}.txt"
            subprocess.run(["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(pdf), str(text_path)], check=True)
            snapshot = text_path.read_bytes()
            original_hash = sha256(pdf.read_bytes())
            extraction = "pdftotext -enc UTF-8 -nopgbrk; unchanged output bytes"
        elif mode == "abstract":
            snapshot = (abstract + "\n").encode("utf-8") if abstract else None
            if snapshot is None:
                mode = "metadata"
                extraction = "No abstract on the landing page; metadata only"
        else:
            raise ValueError("catalog supports abstract/full_text")
        relative = f"data/snapshots/{acl_id}.txt" if snapshot is not None else None
        if snapshot is not None:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot)
        source = Source(source_id, title, "ACL Anthology", mode, doi, url, relative,
                        sha256(snapshot) if snapshot is not None else None)
        sources.append(asdict(source))
        if snapshot is not None:
            fragments.extend(asdict(f) for f in chunk_snapshot(source_id, snapshot))
        provenance.append({
            "source_id": source_id, "authors": meta.get("citation_author", []),
            "publication_date": meta["citation_publication_date"][0],
            "retrieved_at_utc": retrieved, "original_url": original_url,
            "original_sha256": original_hash, "snapshot_sha256": source.sha256,
            "metadata_sha256": sha256(page_path.read_bytes()),
            "license": item["license"], "license_basis": item["license_basis"],
            "transformation": extraction, "abstract": abstract,
        })
        print(f"{acl_id}: {title}", flush=True)
    write_jsonl(root / "data/sources.jsonl", sources)
    write_jsonl(root / "data/fragments.jsonl", fragments)
    write_jsonl(root / "data/provenance.jsonl", provenance)
    write_json(root / "data/aliases.json", {s["source_id"]: [] for s in sources})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    acquire(json.loads(args.catalog.read_text(encoding="utf-8")), args.output, args.cache)


if __name__ == "__main__":
    main()
