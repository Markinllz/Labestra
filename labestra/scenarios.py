"""Reproducible course scenario report; technical fixtures, never quality eval.

Execute selected behavioral tests with their existing assertions and save their
IDs, outcomes and failure details. This does not invoke a real language model.
"""

import argparse
from dataclasses import asdict
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from .runner import ROOT, encoded
from .workflow_runtime import Runtime

SCENARIOS = [
    ('S1', 'Поиск → чтение найденных ID → ответ с evidence',
     ['test_workflow.WorkflowTests.test_j2_reads_search_ids_and_validates_draft']),
    ('S2', 'Временная ошибка → повтор; постоянная ошибка → остановка',
     ['test_workflow.WorkflowTests.test_temporary_tool_error_retried_and_counted',
      'test_workflow.WorkflowTests.test_retry_exhaustion_stops_after_three_attempts',
      'test_workflow.WorkflowTests.test_permanent_tool_failure_is_not_retried']),
    ('S3', 'Исчерпание шагов → остановка до следующей генерации',
     ['test_workflow.WorkflowTests.test_step_budget_stops_before_generation']),
    ('S4', 'Изменение вводных → старый ответ отброшен, поиск заново',
     ['test_workflow.WorkflowTests.test_revision_discards_old_answer_and_uses_new_query']),
    ('S5', 'Повтор записи → без дубля; изменённый результат → ошибка',
     ['test_runner.RunnerTests.test_publication_is_idempotent_and_refuses_changed_result']),
    ('S6', 'Отмена → поздний ответ/запись completed не публикуется',
     ['test_workflow.WorkflowTests.test_late_completion_cannot_publish_or_mutate_events',
      'test_runner.RunnerTests.test_cancellation_before_publication_leaves_no_result']),
]


def run():
    sys.path.insert(0, str(ROOT / 'tests'))
    records = []
    for sid, description, names in SCENARIOS:
        stream = io.StringIO()
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        events = []
        emit = Runtime.emit
        def record(runtime, *args, **kwargs):
            emit(runtime, *args, **kwargs)
            events.append(asdict(runtime.events[-1]))
        with patch.object(Runtime, 'emit', record):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        records.append({'scenario_id': sid, 'description': description, 'tests': names,
                        'passed': result.wasSuccessful(), 'checks': result.testsRun,
                        'protocol': stream.getvalue(), 'events': events})
    return {'mode': 'fixture', 'scientific_evaluation': False,
            'passed': all(r['passed'] for r in records), 'scenarios': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(encoded(report))
    print(args.output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
