# Labestra

Часть Аркаши (участник 1), «Источники и эталоны»: [набор, проверка и независимая разметка](docs/DATASET.md).
В репозитории 30 реальных работ и 30 публичных заданий; приватные gold/holdout
остаются у хранителя. Статус эталонов: `needs_human_review`.

Учебный проект КТ3: поиск по сохранённым научным источникам и ответ с основаниями.
Реализованы загрузка и проверка данных, нарезка текстов, поиск статей и
поиск/чтение фрагментов. Runner и LLM-workflow пока содержат заглушки.
Научный набор, оценочные прогоны и измеренный baseline ещё не подготовлены.

## Структура

```text
labestra/
  schemas.py      # общие типы
  data.py         # загрузка и проверка данных
  search.py       # поиск и чтение фрагментов
  workflow.py     # фиксированная цепочка и LLM
  runner.py       # baseline/system, сохранение, метрики
  __main__.py     # точка входа
configs/          # пример настроек
prompts/          # будущие промпты
data/            # будущие источники и фрагменты
eval/            # dev, visible и многошаговые сценарии
tests/
runs/
adr/
docs/KT3_PLAN.md
design_doc.md
```

## Начало работы

Python 3.11+ со встроенным SQLite FTS5. Для готовой части внешние Python-зависимости не нужны.
Проверьте `python3 --version`: системный Python может быть старее требуемого.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m labestra --help
```

`runner.py` и `workflow.py` содержат `NotImplementedError`. CLI показывает предполагаемые параметры,
но ещё не выполняет прогон. Настройки модели — `.env.example` и `configs/default.json`;
загрузку настроек и вызовы API предстоит реализовать. Ключи в Git не добавлять.

## Проверка готовой части без модели и научных данных

Из корня проекта, в активированном окружении:

```bash
python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(text)'); c.close(); print('FTS5 OK')"
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_data_search_integration.py -v
```

Последняя команда проверяет цепочку manifest → загрузчики → поиск статей →
поиск в корпусе → дословное чтение. Она создаёт временный технический набор,
дважды строит индекс из файлов и сверяет результаты. Набор удаляется после
проверки. Это не научный eval, не запуск runner/LLM и не измерение B0.

## Данные и подключение инструментов

Аркаша готовит UTF-8-тексты из PDF в `data/snapshots/`. Нарезка принимает байты,
поэтому переносы строк не нормализуются:

```python
from pathlib import Path
from labestra.data import chunk_snapshot

fragments = chunk_snapshot("source-id", Path("data/snapshots/article.txt").read_bytes())
```

Пример требует настоящего файла. Функция возвращает `list[Fragment]`, ничего
не записывает. Правила: до 300 непробельных слов, перекрытие 50, точные срезы
текста; `chars:start:end` — Unicode-символы, правая граница исключена. ID зависит
от ID источника, SHA-256 исходных байтов и координат. После изменения текста
или нарезки нужно обновить ссылки и gold до заморозки набора.

Публичная сборка содержит:

- `data/sources.jsonl`, `data/fragments.jsonl`, `data/corpora.jsonl`;
- `eval/dev/tasks.jsonl`, `eval/visible/tasks.jsonl`;
- использованные текстовые снимки и `data/manifest.json`.

Manifest — JSON-объект с обязательными полями `manifest_version: "1"`,
`schema_version: "1.1"`, непустыми `dataset_version`, `collection_id` и `files`.
`files` — словарь «путь от корня проекта → SHA-256 байтов файла», включающий пять
JSONL-файлов и все снимки, указанные в источниках. Gold/holdout и сам manifest
в список не входят. Пути снимков, включая цели симлинков, должны оставаться
внутри `data/snapshots/`. Реальных файлов набора в ветке пока нет.

После подготовки данных runner использует следующую последовательность:

```python
import json
from pathlib import Path
from labestra.data import (
    validate_dataset, load_sources, load_fragments, load_corpora, load_cases,
)
from labestra.search import LocalSearch

root = Path(".")
config = json.loads((root / "configs/default.json").read_text(encoding="utf-8"))
validate_dataset(root)  # ValueError останавливает подготовку до поиска/LLM
manifest = json.loads((root / "data/manifest.json").read_text(encoding="utf-8-sig"))
if config["collection_id"] != manifest["collection_id"]:
    raise ValueError("Коллекция конфигурации не совпадает с manifest")

tools = LocalSearch(
    sources=load_sources(root / "data/sources.jsonl"),
    fragments=load_fragments(root / "data/fragments.jsonl"),
    config=config["search"],
    collection_id=config["collection_id"],
)
corpora = {c.corpus_revision_id: c for c in load_corpora(root / "data/corpora.jsonl")}
case = load_cases(root / "eval/dev/tasks.jsonl")[0]
# Для J2 выбирается заранее заданный корпус; исходный question не заменяет search_query.
corpus = corpora[case.corpus_revision_id]
found = tools.search_fragments(case.search_query, corpus)
evidence = tools.read_fragments([f.fragment_id for f in found], corpus)
```

Это пример подключения к будущему runner для J2, а не готовый CLI-прогон.
В текущем config `collection_id` ещё `null`: его задаёт команда вместе с набором.
`validate_dataset` принимает корень публичной сборки со структурой выше, не папку `data/`.
Предупреждения Python `warnings` нефатальны: runner сохраняет их в диагностике;
неоднозначные ошибки данных возвращаются в одном `ValueError`. Файлы при
исправлении одинаковых повторов не перезаписываются.

`search_sources(query, {}, collection_id)` возвращает до 20 статей; непустые
фильтры запрещены. `search_fragments(query, corpus)` возвращает до 10 фрагментов,
не более 3 на источник. `read_fragments(ids, corpus)` принимает до 10 ID до
удаления повторов, сохраняет порядок, возвращает точные записи. Неизвестный или
чужой ID завершает весь вызов ошибкой. Пустые корпус и список чтения допустимы.

Все методы возвращают ошибки через `ToolFailure.error` с полями
`code`, `message`, `retryable`, `tool_call_id`; последний назначает runner.
Ошибки входа/ссылок не повторяются внутри инструмента. Тайм-аут одного вызова
берётся из `config["search"]["timeout_seconds"]` (30 с), общие бюджеты и повторы
контролирует runner. SQLite-индексы находятся в памяти; один `LocalSearch`
используется последовательно в одном потоке.

## Распределение

| Участник | Часть |
|---|---|
| 1 — Аркаша | Источники, задания и первичный gold |
| 2 — Макс | Загрузка, поиск и независимая разметка |
| 3 — Олег | Workflow, модель и промпты |
| 4 — Сабухи | Runner, сохранение, сценарии и метрики |

[План работы](docs/KT3_PLAN.md) · [Подробный план](docs/KT3_PLAN_DETAILED.md) · [Дедлайны](docs/KT3_DEADLINES.md) · [Design doc](design_doc.md)

Общие интерфейсы закреплены в разделе 3.5 подробного плана: один английский search_query для B0/system, без фильтров; загрузка и общий учёт — runner, поиск/чтение — LocalSearch. Инструменты реализованы; интеграция с runner/workflow ещё предстоит. Gold не передавать решающему варианту,
holdout хранить приватно до КТ4. Подробная расширенная архитектура не обязательна для КТ3.
