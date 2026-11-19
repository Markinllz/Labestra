"""Сабухи: загрузка, создание LocalSearch, общий учёт, baseline UI, сохранение и метрики."""

import argparse

from .schemas import Case, RunResult
from .search import SearchTools


def run_case(case: Case, tools: SearchTools, config: dict, run_id: str, variant: str) -> RunResult:
    raise NotImplementedError


def main() -> int:
    parser = argparse.ArgumentParser(description="Labestra: каркас учебного проекта КТ3")
    parser.add_argument("--variant", choices=("baseline", "system"), required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--config", default="configs/default.json")
    parser.parse_args()
    parser.error("Запуск ещё не реализован: заполните runner.py, search.py и workflow.py")
    return 2
