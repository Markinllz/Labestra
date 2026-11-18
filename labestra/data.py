"""Загрузка и проверка научных данных — участники 1 и 2."""

from pathlib import Path

from .schemas import Case, Fragment, Source


def load_sources(path: Path) -> list[Source]:
    raise NotImplementedError


def load_fragments(path: Path) -> list[Fragment]:
    raise NotImplementedError


def load_cases(path: Path) -> list[Case]:
    raise NotImplementedError


def validate_dataset(root: Path) -> None:
    raise NotImplementedError
