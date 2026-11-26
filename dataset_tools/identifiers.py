"""Canonical scholarly identities, without guessing missing identifiers."""

import re
from urllib.parse import unquote


def normalize_external_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("external ID must be a string")
    value = unquote(value.strip())
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/|^doi:\s*", "", value, flags=re.I)
    if re.fullmatch(r"10\.\d{4,9}/[^\s]+", value, re.I):
        return value.lower()
    value = re.sub(r"^https?://(?:www\.)?openalex\.org/", "", value, flags=re.I)
    if re.fullmatch(r"W[1-9]\d*", value, re.I):
        return value.upper()
    raise ValueError("expected a DOI or OpenAlex work ID")


def validate_aliases(sources, aliases):
    """Reject two records for one work, including cross-provider aliases."""
    owners = {}
    ids = {source.source_id for source in sources}
    if set(aliases) - ids:
        raise ValueError("alias refers to an unknown source")
    for source in sources:
        values = aliases.get(source.source_id, [])
        if not isinstance(values, list):
            raise ValueError("aliases must be lists")
        for value in ([source.external_id] if source.external_id else []) + values:
            canonical = normalize_external_id(value)
            if canonical in owners and owners[canonical] != source.source_id:
                raise ValueError("one scholarly work has multiple source IDs")
            owners[canonical] = source.source_id
    return owners
