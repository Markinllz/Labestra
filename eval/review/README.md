# Эталоны и оценки

Здесь сохранены подтверждённые эталоны 6 dev и 24 visible, 139 меток для 8 кейсов
и оценки 12 ответов system. Эталоны и оценки system подтверждены 26.11.2026;
оценки baseline и дополнительная разметка получены 27.11.2026.

- packet.json — одинаковые объекты и rubric для обоих наборов меток.
- annotations.sabukhi.jsonl и ../annotations/chelush-20261123/annotations.max.jsonl — исходные метки.
- gold.dev.jsonl / gold.visible.jsonl — эталоны, не вход решателя.
- system-ratings.sabukhi.jsonl — оценки system v2.
- baseline-ratings.sabukhi.jsonl — ручные оценки baseline; происхождение — baseline-ratings.provenance.json.
- audit-24/ — подвыборка, исходные и вторичные метки, provenance.

Согласие первичных 139 меток описательное. Для отдельного аудита 24 объектов
получена вторичная ручная разметка; слепота заявлена проверяющим, применялись
русские переводы источников.
Первичные метки сохранены до согласования; единственное расхождение —
visible-07, acl:D16-1264: uncertain / not_relevant. Методика — [EVALUATION.md](../../docs/EVALUATION.md).
