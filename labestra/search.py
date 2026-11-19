"""Local FTS5/BM25 source search; the runner owns overall run budgets."""

import math
import sqlite3
import time
from contextlib import contextmanager
from typing import Protocol

from .data import _fragment_range
from .schemas import Corpus, Fragment, Source, ToolError, ToolFailure


class SearchTools(Protocol):
    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]: ...
    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]: ...
    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]: ...


def _fail(code: str, message: str, retryable: bool = False):
    raise ToolFailure(ToolError(code=code, message=message, retryable=retryable))


def _source_text(fragments: list[Fragment]) -> str:
    """Merge only coordinate overlaps; retain repeated text at other positions."""
    ranges = []
    hashes = set()
    for fragment in fragments:
        try:
            start, end = _fragment_range(fragment.location)
        except ValueError:
            _fail("invalid_input", "Неверные координаты фрагмента")
        if end - start != len(fragment.text):
            _fail("invalid_input", "Длина фрагмента не соответствует координатам")
        ranges.append((start, end, fragment.text))
        hashes.add(fragment.snapshot_sha256)
    if len(hashes) > 1:
        _fail("invalid_input", "Фрагменты источника относятся к разным снимкам")
    merged = []
    for start, end, text in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end, text))
            continue
        previous_start, previous_end, previous_text = merged[-1]
        overlap_end = min(end, previous_end)
        if previous_text[start - previous_start:overlap_end - previous_start] != text[:overlap_end - start]:
            _fail("invalid_input", "Тексты перекрывающихся фрагментов противоречат друг другу")
        if end > previous_end:
            merged[-1] = (previous_start, end, previous_text + text[previous_end - start:])
    # A gap is not reconstructed: separate available text spans with a space.
    return " ".join(text for _, _, text in merged)


class LocalSearch:
    """One in-memory index for the fixed collection, shared by B0 and system.

    Sources are indexed by title and merged fragment text, with BM25 weights
    1/1. Both documents and queries use SQLite unicode61 defaults, no stemming
    or stopword list. Query terms are OR-ed, deduplicated and quoted literally.
    Ties use source_id with SQLite BINARY ordering. This instance is used
    sequentially in the runner's thread; it is not a concurrent server.
    """

    def __init__(
        self,
        *,
        sources: list[Source],
        fragments: list[Fragment],
        config: dict,
        collection_id: str,
    ) -> None:
        if not isinstance(collection_id, str) or not collection_id.strip():
            _fail("invalid_input", "Требуется непустой collection_id")
        if type(config) is not dict:
            _fail("invalid_input", "Настройки поиска должны быть объектом")
        self._top_k = config.get("top_k", 20)
        self._timeout = config.get("timeout_seconds", 30)
        if type(self._top_k) is not int or not 1 <= self._top_k <= 20:
            _fail("invalid_input", "top_k должен быть целым числом от 1 до 20")
        try:
            valid_timeout = type(self._timeout) in (int, float) and math.isfinite(self._timeout) and self._timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            _fail("invalid_input", "timeout_seconds должен быть положительным конечным числом")
        if type(sources) is not list or type(fragments) is not list:
            _fail("invalid_input", "Источники и фрагменты должны быть списками")
        self._collection_id = collection_id
        self._sources = {}
        grouped = {}
        for source in sources:
            if not isinstance(source, Source) or not isinstance(source.source_id, str) or not source.source_id.strip():
                _fail("invalid_input", "Некорректный источник")
            if source.source_id in self._sources:
                _fail("invalid_input", "Повторный ID источника; используйте загрузчик данных")
            if not isinstance(source.title, str):
                _fail("invalid_input", "Название источника должно быть строкой")
            self._sources[source.source_id] = source
            grouped[source.source_id] = []
        seen = set()
        for fragment in fragments:
            if not isinstance(fragment, Fragment):
                _fail("invalid_input", "Некорректный фрагмент")
            if not all(isinstance(value, str) for value in (fragment.fragment_id, fragment.source_id,
                                                           fragment.text, fragment.location, fragment.snapshot_sha256)):
                _fail("invalid_input", "Поля фрагмента должны быть строками")
            if fragment.fragment_id in seen:
                _fail("invalid_input", "Повторный ID фрагмента; используйте загрузчик данных")
            seen.add(fragment.fragment_id)
            source = self._sources.get(fragment.source_id)
            if source is None:
                _fail("invalid_input", "Фрагмент ссылается на отсутствующий источник")
            if source.availability == "metadata":
                _fail("invalid_input", "У metadata-источника не должно быть фрагментов")
            if source.sha256 is not None and source.sha256 != fragment.snapshot_sha256:
                _fail("invalid_input", "Хеш фрагмента не совпадает с хешем источника")
            grouped[fragment.source_id].append(fragment)
        rows = [(key, self._sources[key].title, _source_text(grouped[key])) for key in sorted(self._sources)]
        self._db = sqlite3.connect(":memory:")
        try:
            self._db.execute("CREATE VIRTUAL TABLE sources USING fts5(source_id UNINDEXED, title, body, tokenize='unicode61')")
            self._db.executemany("INSERT INTO sources VALUES (?, ?, ?)", rows)
            # Use the actual SQLite tokenizer for the query, rather than a
            # Python approximation that could disagree on Unicode punctuation.
            self._db.execute("CREATE VIRTUAL TABLE query_text USING fts5(text, tokenize='unicode61')")
            self._db.execute("CREATE VIRTUAL TABLE query_terms USING fts5vocab(query_text, 'instance')")
            self._db.commit()
        except sqlite3.Error:
            self._db.close()
            _fail("io_error", "Не удалось создать индекс SQLite FTS5")

    @contextmanager
    def _deadline(self):
        deadline = time.monotonic() + self._timeout

        def expired():
            return time.monotonic() >= deadline

        def check():
            if expired():
                _fail("timeout", "Истёк тайм-аут поиска", retryable=True)

        self._db.set_progress_handler(expired, 100)
        try:
            check()
            yield check
            check()
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT or expired():
                _fail("timeout", "Истёк тайм-аут поиска", retryable=True)
            _fail("io_error", "Ошибка SQLite при поиске")
        finally:
            self._db.set_progress_handler(None, 0)

    def search_sources(self, query: str, filters: dict[str, str], collection_id: str) -> list[Source]:
        """Return up to top_k matches; no matches is an ordinary empty list.

        Inputs are limited to 1000 Unicode characters and 100 unicode61 tokens
        before deduplication. Empty input/punctuation-only input is invalid.
        No SQL or MATCH syntax supplied by the caller is executed.
        """
        with self._deadline() as check:
            if type(query) is not str or not query.strip() or len(query) > 1000:
                _fail("invalid_input", "Запрос должен содержать от 1 до 1000 символов")
            if type(filters) is not dict or filters:
                _fail("invalid_input", "Поддерживается только filters={}")
            if type(collection_id) is not str or not collection_id.strip():
                _fail("invalid_input", "Требуется непустой collection_id")
            if collection_id != self._collection_id:
                _fail("not_found", "Коллекция не найдена")
            self._db.execute("DELETE FROM query_text")
            self._db.execute("INSERT INTO query_text(text) VALUES (?)", (query,))
            terms = [row[0] for row in self._db.execute("SELECT term FROM query_terms ORDER BY offset LIMIT 101")]
            if not terms or len(terms) > 100:
                _fail("invalid_input", "Запрос должен содержать от 1 до 100 слов")
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in dict.fromkeys(terms))
            check()
            rows = self._db.execute(
                "SELECT source_id FROM sources WHERE sources MATCH ? "
                "ORDER BY bm25(sources, 0.0, 1.0, 1.0), source_id COLLATE BINARY LIMIT ?",
                (expression, self._top_k),
            ).fetchall()
            return [self._sources[row[0]] for row in rows]

    def search_fragments(self, query: str, corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: implement corpus fragment search")

    def read_fragments(self, fragment_ids: list[str], corpus: Corpus) -> list[Fragment]:
        raise NotImplementedError("Participant 2: validate IDs and read exact fragments")
