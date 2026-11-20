"""Загрузка и проверка научных данных — Аркаша (данные) и Макс (загрузка)."""

from pathlib import Path

from .schemas import Case, Corpus, Fragment, Source


def load_sources(path: Path) -> list[Source]:
    raise NotImplementedError


def load_fragments(path: Path) -> list[Fragment]:
    raise NotImplementedError


def load_corpora(path: Path) -> list[Corpus]:
    raise NotImplementedError


def load_cases(path: Path) -> list[Case]:
    raise NotImplementedError


def validate_dataset(root: Path) -> None:
    raise NotImplementedError
