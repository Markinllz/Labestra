"""Макс: поиск и чтение. Сабухи создаёт LocalSearch в runner."""

from typing import Protocol

from .schemas import Corpus, Fragment, Source


class SearchTools(Protocol):
    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]: ...
    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]: ...
    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]: ...


class LocalSearch:
    """Shared instance for both variants, one fixed collection.

    query is Case.search_query (English); filters must be {} in KT3.
    Source results keep ranking order. The runner logs calls and enforces budgets.
    """

    def __init__(
        self,
        *,
        sources: list[Source],
        fragments: list[Fragment],
        config: dict,
        collection_id: str,
    ) -> None:
        # config is config["search"]. The runner loads files; Max builds the index.
        raise NotImplementedError("Макс: создать индекс по переданным данным")

    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]:
        raise NotImplementedError("Participant 2: implement fixed-collection source search")

    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: implement corpus fragment search")

    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: validate IDs and read exact fragments")
