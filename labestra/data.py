"""Strict JSONL loading and public dataset validation (participant 2).

Safe repairs happen in memory and emit warnings. Source files are never changed.
"""

import hashlib
import json
import math
import re
import types
import warnings
from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Union, get_args, get_origin, get_type_hints

from .schemas import SCHEMA_VERSION, Case, Corpus, Fragment, Source


def _decode(value, annotation, location, errors):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            trial = []
            result = _decode(value, option, location, trial)
            if not trial:
                return result
        errors.append(f"{location}: неверный тип")
    elif origin is Literal:
        if not any(type(value) is type(x) and value == x for x in args):
            errors.append(f"{location}: недопустимое значение")
    elif is_dataclass(annotation):
        if type(value) is not dict:
            errors.append(f"{location}: ожидается объект")
            return value
        hints = get_type_hints(annotation)
        for key in value.keys() - hints.keys():
            errors.append(f"{location}.{key}: неизвестное поле")
        decoded = {}
        start = len(errors)
        for item in fields(annotation):
            child = f"{location}.{item.name}"
            if item.name in value:
                decoded[item.name] = _decode(value[item.name], hints[item.name], child, errors)
            elif item.default is MISSING and item.default_factory is MISSING:
                errors.append(f"{child}: отсутствует обязательное поле")
        if len(errors) == start and not (value.keys() - hints.keys()):
            return annotation(**decoded)
    elif origin is list:
        if type(value) is not list:
            errors.append(f"{location}: ожидается список")
        else:
            return [_decode(x, args[0], f"{location}[{i}]", errors) for i, x in enumerate(value)]
    elif origin is dict:
        if type(value) is not dict:
            errors.append(f"{location}: ожидается объект")
        else:
            for key, item in value.items():
                _decode(key, args[0], location, errors)
                _decode(item, args[1], f"{location}.{key}", errors)
    elif annotation is float:
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            errors.append(f"{location}: ожидается конечное число")
    elif type(value) is not annotation:
        errors.append(f"{location}: неверный тип")
    return value


_PUBLIC_FILES = frozenset({
    "data/sources.jsonl", "data/fragments.jsonl", "data/corpora.jsonl",
    "eval/dev/tasks.jsonl", "eval/visible/tasks.jsonl",
})


@dataclass(frozen=True)
class _Manifest:
    manifest_version: str
    schema_version: str
    dataset_version: str
    collection_id: str
    files: dict[str, str]


def _parse_json(text: str, location: str):
    duplicate_keys = []

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                # Preserve the distinction between bool/int, also when nested.
                before = json.dumps(result[key], sort_keys=True, ensure_ascii=True)
                after = json.dumps(value, sort_keys=True, ensure_ascii=True)
                if before != after:
                    raise ValueError("conflicting JSON keys")
                duplicate_keys.append(key)
            result[key] = value
        return result

    result = json.loads(text, object_pairs_hook=object_pairs)
    for key in duplicate_keys:
        warnings.warn(f"{location}.{key}: одинаковый повторный ключ объединён в памяти", stacklevel=3)
    return result


def _public_path(root: Path, name: str, *, snapshot: bool = False) -> Path:
    """Resolve and authorize before opening; private files are never targets.

    Paths are canonical project-relative POSIX paths. Snapshot aliases are
    allowed only if their resolved target remains in the actual permitted
    root/data/snapshots subtree; a symlink replacing that directory cannot
    redefine the boundary to eval/private or an external directory.
    """
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
        raise ValueError("Некорректный относительный путь")
    is_snapshot = relative.parts[:2] == ("data", "snapshots") and len(relative.parts) > 2
    if snapshot and not is_snapshot:
        raise ValueError("Снимки разрешены только в data/snapshots/")
    if not is_snapshot and name not in _PUBLIC_FILES | {"data/manifest.json"}:
        raise ValueError("Путь не относится к публичному набору")
    try:
        base = root.resolve(strict=True)
        target = (base / name).resolve(strict=True)
        if is_snapshot:
            if not target.is_relative_to(base / "data" / "snapshots"):
                raise ValueError("Снимок выходит за data/snapshots/")
        elif target not in {base / item for item in _PUBLIC_FILES | {"data/manifest.json"}}:
            raise ValueError("Публичный файл ссылается за разрешённые границы")
        if not target.is_file():
            raise ValueError("Ожидается файл")
        return target
    except (OSError, RuntimeError) as exc:
        raise ValueError("Путь недоступен") from exc


def _validate_manifest(root: Path, errors: list[str]) -> _Manifest | None:
    location = "data/manifest.json"
    try:
        path = _public_path(root, location)
        raw = _parse_json(path.read_text(encoding="utf-8-sig"), location)
    except (OSError, UnicodeError, ValueError, RecursionError):
        errors.append(f"{location}: файл отсутствует, недоступен или содержит некорректный JSON")
        return None
    schema_errors = []
    manifest = _decode(raw, _Manifest, location, schema_errors)
    if schema_errors:
        errors.extend(schema_errors)
        return None
    if manifest.manifest_version != "1":
        errors.append(f"{location}.manifest_version: поддерживается версия 1")
    if manifest.schema_version != SCHEMA_VERSION:
        errors.append(f"{location}.schema_version: не совпадает с версией схемы приложения")
    for name in ("dataset_version", "collection_id"):
        if not getattr(manifest, name).strip():
            errors.append(f"{location}.{name}: требуется непустая строка")
    for name in sorted(_PUBLIC_FILES - manifest.files.keys()):
        errors.append(f"{location}.files: отсутствует обязательный файл {name}")
    for name, expected in manifest.files.items():
        loc = f"{location}.files[{name}]"
        # manifest must not hash itself, private material or arbitrary files.
        if name == location:
            errors.append(f"{loc}: manifest не включается в собственный список файлов")
            continue
        if re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
            errors.append(f"{loc}: требуется SHA-256 из 64 шестнадцатеричных символов")
            continue
        try:
            target = _public_path(root, name)
        except ValueError:
            errors.append(f"{loc}: файл недоступен или путь запрещён")
            continue
        try:
            with target.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            errors.append(f"{loc}: не удалось прочитать файл")
            continue
        if actual != expected.lower():
            errors.append(f"{loc}: SHA-256 не совпадает с содержимым файла")
    return manifest


def _read(path, cls, id_field):
    path = Path(path)
    errors, records, seen = [], [], {}
    try:
        # utf-8-sig accepts ordinary UTF-8 and removes a leading BOM.
        lines = path.read_text(encoding="utf-8-sig").split("\n")
    except (OSError, UnicodeError):
        return [], [f"{path}: файл недоступен или не является UTF-8"]
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        location = f"{path}:{number}"
        try:
            raw = _parse_json(line, location)
        except (ValueError, RecursionError):
            errors.append(f"{location}: некорректный JSON или конфликт повторных ключей")
            continue
        row_errors = []
        record = _decode(raw, cls, location, row_errors)
        if not row_errors:
            for item in fields(record):
                value = getattr(record, item.name)
                if item.name.endswith("_id") and value is not None and not value.strip():
                    row_errors.append(f"{location}.{item.name}: идентификатор не может быть пустым")
                if item.name == "source_ids":
                    for index, source_id in enumerate(value):
                        if not source_id.strip():
                            row_errors.append(f"{location}.source_ids[{index}]: идентификатор не может быть пустым")
        if row_errors:
            errors.extend(row_errors)
            continue
        if cls is Corpus:
            unique = list(dict.fromkeys(record.source_ids))
            if unique != record.source_ids:
                warnings.warn(f"{location}.source_ids: повторы удалены в памяти", stacklevel=3)
                record = Corpus(record.corpus_revision_id, record.collection_id, unique)
        identifier = getattr(record, id_field)
        if identifier in seen:
            if record == seen[identifier]:
                warnings.warn(f"{location}.{id_field}: одинаковая запись пропущена", stacklevel=3)
            else:
                errors.append(f"{location}.{id_field}: конфликт записей с одним ID")
            continue
        seen[identifier] = record
        records.append(record)
    if not any(line.strip() for line in lines):
        errors.append(f"{path}: обязательный файл пуст")
    return records, errors


def _raise_errors(errors):
    if errors:
        raise ValueError("Ошибки данных:\n" + "\n".join(errors))


def _load(path, cls, id_field):
    records, errors = _read(path, cls, id_field)
    _raise_errors(errors)
    return records


def load_sources(path: Path) -> list[Source]:
    return _load(path, Source, "source_id")


def load_fragments(path: Path) -> list[Fragment]:
    return _load(path, Fragment, "fragment_id")


def load_corpora(path: Path) -> list[Corpus]:
    return _load(path, Corpus, "corpus_revision_id")


def load_cases(path: Path) -> list[Case]:
    return _load(path, Case, "case_id")


def validate_dataset(root: Path) -> None:
    """Validate data and public dev/visible inputs under the project root.

    Collect recoverable diagnostics; never read private gold or holdout files.
    Validates manifest v1 and schema version against SCHEMA_VERSION.
    The runner separately checks manifest.collection_id against active config.
    """
    root = Path(root)
    errors = []
    loaded = {}
    manifest = _validate_manifest(root, errors)
    specs = [
        ("data/sources.jsonl", Source, "source_id"),
        ("data/fragments.jsonl", Fragment, "fragment_id"),
        ("data/corpora.jsonl", Corpus, "corpus_revision_id"),
        ("eval/dev/tasks.jsonl", Case, "case_id"),
        ("eval/visible/tasks.jsonl", Case, "case_id"),
    ]
    for name, cls, key in specs:
        try:
            path = _public_path(root, name)
        except ValueError:
            loaded[name] = []
            errors.append(f"{name}: недоступный или запрещённый путь")
            continue
        records, problems = _read(path, cls, key)
        loaded[name] = records
        errors.extend(problems)
    sources = {s.source_id: s for s in loaded["data/sources.jsonl"]}
    corpora = {c.corpus_revision_id: c for c in loaded["data/corpora.jsonl"]}
    snapshots = {}
    for source in sources.values():
        loc = f"data/sources.jsonl[{source.source_id}]"
        if source.availability != "metadata" and (source.snapshot_path is None or source.sha256 is None):
            errors.append(f"{loc}: требуются snapshot_path и sha256")
        if source.snapshot_path is None:
            continue
        if manifest is not None and source.snapshot_path not in manifest.files:
            errors.append(f"{loc}.snapshot_path: снимок отсутствует в manifest.files")
        try:
            path = _public_path(root, source.snapshot_path, snapshot=True)
        except ValueError:
            errors.append(f"{loc}.snapshot_path: снимок недоступен или выходит за data/snapshots/")
            continue
        try:
            blob = path.read_bytes()
            text = blob.decode("utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{loc}.snapshot_path: снимок недоступен или не является UTF-8")
            continue
        if not text.strip():
            errors.append(f"{loc}.snapshot_path: текстовый снимок пуст")
        digest = hashlib.sha256(blob).hexdigest()
        snapshots[source.source_id] = (text, digest)
        if source.sha256 != digest:
            errors.append(f"{loc}.sha256: хеш не совпадает со снимком")
    for fragment in loaded["data/fragments.jsonl"]:
        loc = f"data/fragments.jsonl[{fragment.fragment_id}]"
        source = sources.get(fragment.source_id)
        if source is None:
            errors.append(f"{loc}.source_id: источник отсутствует")
            continue
        if source.availability == "metadata":
            errors.append(f"{loc}: у metadata-источника не должно быть фрагментов")
        if fragment.source_id in snapshots:
            text, digest = snapshots[fragment.source_id]
            if not fragment.text or fragment.text not in text:
                errors.append(f"{loc}.text: фрагмент пуст или не входит дословно в снимок")
            if fragment.snapshot_sha256 != digest:
                errors.append(f"{loc}.snapshot_sha256: хеш не совпадает со снимком")
            try:
                start, end = _fragment_range(fragment.location)
            except ValueError:
                errors.append(f"{loc}.location: неверные координаты chars:start:end")
            else:
                if end > len(text) or text[start:end] != fragment.text:
                    errors.append(f"{loc}.location: координаты не соответствуют тексту фрагмента")
    for corpus in corpora.values():
        if manifest is not None and corpus.collection_id != manifest.collection_id:
            errors.append(f"data/corpora.jsonl[{corpus.corpus_revision_id}].collection_id: не совпадает с manifest")
        for source_id in corpus.source_ids:
            if source_id not in sources:
                errors.append(f"data/corpora.jsonl[{corpus.corpus_revision_id}].source_ids: источник отсутствует")
    groups, case_ids = {}, set()
    for split in ("dev", "visible"):
        name = f"eval/{split}/tasks.jsonl"
        for case in loaded[name]:
            loc = f"{name}[{case.case_id}]"
            if manifest is not None and case.collection_id != manifest.collection_id:
                errors.append(f"{loc}.collection_id: не совпадает с manifest")
            if case.case_id in case_ids:
                errors.append(f"{loc}.case_id: ID повторяется в публичных частях")
            case_ids.add(case.case_id)
            if case.split != split:
                errors.append(f"{loc}.split: не соответствует папке")
            if case.group_id in groups and groups[case.group_id] != split:
                errors.append(f"{loc}.group_id: группа пересекает dev и visible")
            groups[case.group_id] = split
            if case.phase == "J2" and case.corpus_revision_id is None:
                errors.append(f"{loc}.corpus_revision_id: для J2 требуется корпус")
            if case.corpus_revision_id is not None:
                corpus = corpora.get(case.corpus_revision_id)
                if corpus is None:
                    errors.append(f"{loc}.corpus_revision_id: корпус отсутствует")
                elif case.collection_id != corpus.collection_id:
                    errors.append(f"{loc}.collection_id: не совпадает с коллекцией корпуса")
    _raise_errors(errors)


def chunk_snapshot(
    source_id: str,
    snapshot: bytes,
    *,
    chunk_size: int = 300,
    overlap: int = 50,
) -> list[Fragment]:
    """Split UTF-8 snapshot bytes into exact, overlapping text slices.

    A word is a maximal non-whitespace sequence. Offsets are Python Unicode
    character indices, zero-based and end-exclusive, not byte or PDF offsets.
    Internal whitespace and CRLF are preserved; surrounding whitespace outside
    the first/last word is not included in a fragment. No PDF extraction occurs.

    IDs are full SHA-256 hex digests of the UTF-8 compact JSON array
    [source_id, snapshot_sha256, start, end] (ensure_ascii=False). This explicit
    encoding avoids ambiguous concatenation and changes IDs when a snapshot,
    source identity or coordinate range changes. No files are written.
    """
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id: требуется непустая строка")
    if type(snapshot) is not bytes:
        raise ValueError("snapshot: требуются исходные байты UTF-8")
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size: требуется положительное целое число")
    if type(overlap) is not int or not 0 <= overlap < chunk_size:
        raise ValueError("overlap: требуется целое число от 0 до chunk_size - 1")
    try:
        text = snapshot.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("snapshot: требуется текст UTF-8") from exc
    words = list(re.finditer(r"\S+", text))
    if not words:
        raise ValueError("snapshot: текстовый снимок пуст")
    digest = hashlib.sha256(snapshot).hexdigest()
    fragments = []
    first = 0
    while first < len(words):
        last = min(first + chunk_size, len(words))
        start, end = words[first].start(), words[last - 1].end()
        identity = json.dumps(
            [source_id, digest, start, end], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        fragments.append(Fragment(
            fragment_id=hashlib.sha256(identity).hexdigest(),
            source_id=source_id,
            text=text[start:end],
            location=f"chars:{start}:{end}",
            snapshot_sha256=digest,
        ))
        if last == len(words):
            break
        first += chunk_size - overlap
    return fragments


def _fragment_range(location: str) -> tuple[int, int]:
    """Parse the agreed character-coordinate format without normalizing it."""
    match = re.fullmatch(r"chars:([0-9]+):([0-9]+)", location)
    if match is None:
        raise ValueError("location: ожидается chars:start:end")
    try:
        start, end = (int(part) for part in match.groups())
    except ValueError as exc:
        raise ValueError("location: некорректные координаты") from exc
    if start >= end:
        raise ValueError("location: требуется start < end")
    return start, end
