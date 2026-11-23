# Labestra

Часть Аркаши (участник 1), «Источники и эталоны»: [набор, проверка и независимая разметка](docs/DATASET.md).
В репозитории 30 реальных работ и 30 публичных заданий; приватные gold/holdout
остаются у хранителя. Статус эталонов: `needs_human_review`.

Учебный проект КТ3: поиск по сохранённым научным источникам и ответ с основаниями.
Реализованы загрузка и проверка данных, нарезка текстов, поиск статей и
поиск/чтение фрагментов, фиксированный workflow и проверки его поведения.
Публичный набор подготовлен. Аркаша выполнил реальный dev-пилот; visible начат
и остановлен при неизвестном usage. Макс сохранил подтверждённую им вторую
разметку. Проверка первичных эталонов, agreement и сравнение с B0 ещё не завершены.
Общий runner запускает baseline/system, сохраняет результаты и ресурсы.
Актуальные результаты работы участника 1 и команды B0:
[статус и передача](docs/DATASET.md#статус-части-аркаши-на-24-ноября-2026),
[общий конфиг без ключа](configs/arkady.deepseek-v4-flash-0731.json).

Часть Олега (участник 3): [инструкция, код и подключение](docs/WORKFLOW.md).
Локальный пример без API и ключа:

```bash
python3 -m labestra.workflow_demo
```

Это технический fixture с реальными публичными dev-данными и поиском;
он не заменяет научный ответ модели или оценку качества.

## Запуск для Аркаши и Сабухи

Python 3.11+, macOS/Linux. Сторонних пакетов и Docker не требуется. Из корня
репозитория выполните `./run.sh check`: скрипт создаст `.venv` и проверит данные
и тесты. При нескольких Python можно указать `PYTHON=python3.11 ./run.sh check`.
На Windows используйте WSL; интерактивный ввод использует POSIX select.

**Аркаша — модель.** Создайте локальный конфиг и отредактируйте его под свой API:

```bash
./run.sh configure
# Отредактировать configs/local.live.json
./run.sh system --participant arkady --case-id dev-01 --case-id dev-04 --session-id system-pilot
./run.sh pack --session runs/local/system-pilot --output runs/system-pilot.zip
```

`configure` ничего не спрашивает и не вызывает API. В `configs/local.live.json`
самостоятельно задайте:

- `model.base_url`: HTTPS-адрес OpenAI-совместимого API, например `https://provider.example/v1`, без `/chat/completions`;
- `model.model_id`: точный ID вашей модели; `model.provider`: название провайдера;
- `pricing.input_per_1000`, `output_per_1000`: тариф за 1000 токенов, `currency`, `verified_on` и `source`;
- `limits.currency`: та же валюта; `limits.max_cost`: предел на задание; `session_max_cost`: на сессию;
- `workflow.context_window_tokens` и пределы входа/выхода: под выбранную модель;
- `workflow.input_bound_policy`: обоснование безопасной оценки входа. Только после проверки токенизатора/обвязки выбранной модели выставить `token_bound_verified: true`.

Тарифы и подтверждение токенизатора намеренно не перенесены с Qwen в Яндексе:
они относятся к конкретной модели и провайдеру. Пока обязательные настройки пусты,
платный запуск остановится до запроса. При желании адрес/модель/бюджет можно передать
через `configure --base-url ... --model-id ... --budget ...`, остальное заполнить в файле.

Конфигурация исключена из Git, повторная настройка не перезаписывает её.
Ключ не вставляйте в JSON: программа запросит его скрыто перед запуском или
прочитает `LABESTRA_API_KEY`. Он живёт только в процессе. Для обычного
OpenAI-совместимого API используется Bearer, для Yandex автоматически Api-Key.
Если API имеет другой формат, меняется адаптер `LanguageModel` в `labestra/llm.py`;
workflow и runner остаются общими.

В J1 подтвердите план и выберите источники. В J2 проверьте ответ и завершите
авторскую проверку. Отправьте ZIP результатов. Лимит сессии не является лимитом
всего аккаунта: учитывайте расходы прошлых сессий. При неизвестном usage сессия
останавливается для сверки биллинга. Отмена прекращает ожидание, но не гарантирует
отмену обработки/списания у провайдера.

После успешного dev-пилота назначьте согласованный бюджет в локальном config и
запустите visible, не меняя модель/промпты/поиск в середине сравнения:

```bash
./run.sh system --participant arkady --cases eval/visible/tasks.jsonl --session-id system-visible
./run.sh pack --session runs/local/system-visible --output runs/system-visible.zip
```

**Сабухи — ручной B0.** Не открывайте gold, чужие метки и ответы системы до своей сессии.
Используйте опубликованный идентичный config без ключа; для B0 API не нужен.
`--blind-attestation` подтверждает отсутствие такого доступа. Если материалы уже
просмотрены, сначала согласуйте протокол; нельзя отмечать запуск как слепой.
Сначала dev-пилот, затем visible. Инструкция ввода ответа приведена ниже.

```bash
./run.sh baseline --config configs/arkady.deepseek-v4-flash-0731.json --participant sabukhi --blind-attestation --case-id dev-01 --case-id dev-04 --session-id b0-pilot
./run.sh baseline --config configs/arkady.deepseek-v4-flash-0731.json --participant sabukhi --blind-attestation --cases eval/visible/tasks.jsonl --session-id b0-visible
./run.sh pack --session runs/local/b0-visible --output runs/b0-visible.zip
```

Лимит человека — 240 с/задание; задайте другой после dev, если нужен, но до visible
и одинаково для обоих вариантов. Последний этап — человеческая проверка gold,
независимая разметка и оценивание сохранённых ответов. Инструкция хранителя:
[DATASET.md](docs/DATASET.md). Holdout до КТ4 не запускать.

## Структура

```text
labestra/
  schemas.py      # общие типы
  data.py         # загрузка и проверка данных
  search.py       # поиск и чтение фрагментов
  workflow.py     # фиксированная цепочка J1/J2
  llm.py          # тестовый и необязательный REST-адаптер
  workflow_runtime.py  # единый учёт, используемый runner
  workflow_demo.py # локальный пример без API
  runner.py       # baseline/system, сохранение, метрики
  __main__.py     # точка входа
configs/          # пример настроек
prompts/          # тексты плана/уточнения и ответа
data/             # публичные источники и фрагменты
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

Общий CLI использует `configs/default.json`: это явно обозначенный offline-fixture.
Для реального API предназначен `configs/workflow.live.example.json`; до настройки
ключа, модели, тарифа и контекста live-запуск блокируется. `.env` автоматически
не загружается. Ключ передаётся только через `LABESTRA_API_KEY`, не через аргументы CLI.

### Проверка общего runner без API

```bash
python -m labestra --variant baseline --cases eval/dev/tasks.jsonl --fixture-actions --session-id baseline-dev
python -m labestra --variant system --cases eval/dev/tasks.jsonl --fixture-actions --session-id system-dev
python -m labestra.scenarios --output runs/local/scenarios.json
```

Один вход и одни реальные данные; ответы модели и действия человека здесь тестовые.
Флаг `--fixture-actions` разрешён только на dev, научные оценки по таким ответам запрещены.
Повтор с тем же session-id завершается до API-вызовов и не перезаписывает результаты.
`runs/local/` исключён из Git. Для нового прогона выберите новый session-id.

### Ручной B0 и реальная система

Сначала выполните dev-пилот, затем зафиксируйте настройки. В конфигурации уже
выбран лимит человека 240 секунд на задание, общий deadline 300 секунд.
Это начальный регламент: после dev его можно изменить **до** visible-сравнения.
Одинаковую итоговую конфигурацию используйте для B0/system. Baseline не обращается
к модели и не требует ключа. До B0 нельзя читать gold и ответы системы на эти задания.

```bash
python -m labestra --variant baseline --cases eval/visible/tasks.jsonl --config configs/workflow.live.example.json --participant sabukhi --blind-attestation --session-id b0-visible
python -m labestra --variant system --cases eval/visible/tasks.jsonl --config configs/workflow.live.example.json --participant sabukhi --session-id system-visible
```

Эти команды готовы после заполнения live-конфига; сейчас он намеренно незаполнен.
Для пилота укажите dev-файл и `--case-id dev-01` либо `--case-id dev-04`.
B0: выберите номера источников, `read 1,2` показывает фрагменты выбранных работ.
В J2 прочитайте показанные фрагменты и введите собственный JSON ответа, затем
строку с точкой. `/cancel`, Ctrl-C, EOF и deadline завершают сессию с причиной.
JSON имеет ту же схему, что и ответ системы:

```json
{"answer":"Ваш ответ", "claims":[{"claim_id":"c1","text":"Проверяемый вывод","evidence":[{"fragment_id":"ID из показанного фрагмента","relation":"supports"}]}],"limitations":[],"evidence_status":"sufficient"}
```

При недостатке оснований: `claims: []`, `evidence_status: "insufficient_evidence"`,
явное объяснение в `answer` и непустой `limitations`. Смысловую корректность код
не определяет. После ответа системы автор может отметить accept/reject;
это **не независимая оценка** и не метрика Acceptance Rate.

В папке задания сохраняются input/config/result/manifest/corpora и events.jsonl.
Manifest содержит модель, полный config без ключа, хеши кода, данных и промптов,
Python/SQLite, режим, участника и предупреждения загрузки. Версия ответа модели
и фактический usage находятся также в событиях. Seed API не поддерживается,
поэтому фиксируются temperature и версия; побайтовое совпадение генераций не обещается.

### Оценка после сохранения B0

Gold остаётся у Аркаши. Сначала получите проверенные людьми эталоны и независимые
оценки ответов. Пустой шаблон оценок создаётся без автоматического заполнения:

```bash
python -m labestra.evaluate --session runs/local/system-visible --prepare-ratings --output runs/local/system-ratings.jsonl
python -m labestra.evaluate --session runs/local/system-visible --gold eval/private/gold/visible.jsonl --ratings runs/local/system-ratings.jsonl --output runs/local/system-report.json
```

Заполнить шаблон должен независимый проверяющий: корректность каждого claim,
отношения claim/evidence, покрытые gold_claim_id, accepted и объяснение. Для B0
команды аналогичны. Пустые метки, оценка собственного ответа и непроверенный gold
отклоняются. Для отчёта только о ресурсах не передавайте `--gold`/`--ratings`.
Ошибки и пропуски сохраняются в знаменателях; неизвестный расход не превращается в ноль.

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
внутри `data/snapshots/`. Публичный набор Аркаши уже включён в эту локальную ветку.

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

Это пример подключения к runner для J2, а не готовый CLI-прогон.
В текущем config задан ID коллекции из manifest.
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

Общие интерфейсы закреплены в разделе 3.5 подробного плана: один английский search_query для B0/system, без фильтров; загрузка и общий учёт — runner, поиск/чтение — LocalSearch. Инструменты подключены к общему runner/workflow; реальный API проверен на dev, полный visible и человеческая оценка ещё не завершены. Gold не передавать решающему варианту,
holdout хранить приватно до КТ4. Подробная расширенная архитектура не обязательна для КТ3.
