"""Participant 3: fixed J1/J2 workflow, LLM adapter and limits."""

from .schemas import Case, RunResult
from .search import SearchTools


def run_system(case: Case, tools: SearchTools, config: dict, run_id: str) -> RunResult:
    """No network calls or fake successes until the actual workflow is implemented."""
    raise NotImplementedError("Participant 3: implement workflow, model calls and budgets")
