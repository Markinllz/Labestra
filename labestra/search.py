"""Participant 2. Implement search; this module does not return simulated scientific results."""

from typing import Protocol

from .schemas import Corpus, Fragment, Source


class SearchTools(Protocol):
    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]: ...
    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]: ...
    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]: ...


class LocalSearch:
    """Implement these stubs using schemas.py and docs/KT3_PLAN_DETAILED.md."""

    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]:
        raise NotImplementedError("Participant 2: implement fixed-collection source search")

    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: implement corpus fragment search")

    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: validate IDs and read exact fragments")
