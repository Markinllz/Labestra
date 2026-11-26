# Labestra

Поиск научных статей и подготовка ответов с проверяемыми основаниями.
Локальное приложение на Python: FTS5/BM25, фиксированный workflow и один LLM API.

- **J1:** вопрос → план → поиск → выбор источников → сохранённый корпус.
- **J2:** вопрос и корпус → поиск фрагментов → ответ с утверждениями и ссылками.
- **Baseline:** тот же поиск без LLM, выбор первых статей и извлечение дословных цитат.

[Design doc](design_doc.md) · [Оценка и результаты](docs/EVALUATION.md) ·
[Данные](docs/DATASET.md) · [Workflow и API](docs/WORKFLOW.md) · [ADR](adr/)

## Установка и проверка

Python 3.11+ со встроенным SQLite FTS5, macOS/Linux; Windows — через WSL.
Внешние Python-зависимости не требуются.

```bash
./run.sh check
./run.sh scenarios --output runs/local/scenarios.json
.venv/bin/python -m labestra.submission
```

`check` создаёт `.venv`, проверяет данные и запускает тесты. Остальные команды
выполняют шесть многошаговых сценариев и пересчитывают сохранённые результаты.
API-ключ и доступ к модели для этих проверок не нужны.

## Запуск

Baseline на публичном наборе:

```bash
./run.sh baseline \
  --config configs/arkady.deepseek-v4-flash-0731.v2.json \
  --cases eval/visible/tasks.jsonl --session-id baseline-visible
```

Новый запуск системы:

```bash
./run.sh configure
# Указать API, модель, тариф и лимиты в configs/local.live.json
./run.sh system --participant operator --case-id dev-01 --case-id dev-04 --session-id system-dev
```

Ключ вводится скрыто или читается из `LABESTRA_API_KEY`. Локальный конфиг и ключи
не сохраняются в Git. `.env` автоматически не загружается. Повторный session-id
отклоняется; результаты сохраняются в `runs/local/<session-id>/`.

## Результаты

Оба варианта измерены на одинаковых 24 visible-заданиях с одним config.

| Показатель | Baseline | System v2 |
|---|---:|---:|
| Completed / уточнение | 22 / 2 | 22 / 2 |
| Правильное действие | 22/24 | 22/24 |
| Recall@20 по 10 поисковым J1 | 100% | 100% |
| Recall выбранного корпуса | 95% | 100% |
| Precision выбранного корпуса | 33,33% | 100% |
| Принятые ответы на 10 ответимых J2 | 4/10 | 5/10 |
| Покрытие обязательных выводов J2 | 58,33% | 66,67% |
| API-стоимость, справочно | 0 USD | 0,049953 USD |

Подробные метрики и условия сравнения — [EVALUATION.md](docs/EVALUATION.md).

## Структура

```text
labestra/         runner, workflow, baseline, поиск, API, Runtime и оценивание
dataset_tools/    подготовка, проверка и заморозка данных
data/             источники, снимки, фрагменты, корпуса и manifest
eval/             dev/visible, эталоны и разметка
prompts/          версионируемые инструкции модели
configs/          параметры запусков и пример live-конфигурации
runs/submission/  сохранённые эксперименты и отчёты
tests/            проверки компонентов и интеграции
adr/              архитектурные решения
docs/             данные, интерфейсы и методика оценки
```

Отложенная часть набора хранится отдельно. Архитектура описана в [design doc](design_doc.md).
