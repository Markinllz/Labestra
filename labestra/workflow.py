"""Олег: фиксированный J1/J2. Поиск через переданный SearchTools с общим учётом runner."""

from .schemas import Case, RunResult
from .search import SearchTools


def run_system(case: Case, tools: SearchTools, config: dict, run_id: str) -> RunResult:
    """No network calls or fake successes until the actual workflow is implemented."""
    raise NotImplementedError("Participant 3: implement workflow, model calls and budgets")
