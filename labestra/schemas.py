"""Начальные типы данных. Проверки и окончательные поля добавит команда."""

from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.1"


@dataclass(frozen=True)
class Limits:
    max_steps: int = 20
    max_llm_calls: int = 10
    max_search_calls: int = 5
    max_input_tokens: int = 100_000
    max_output_tokens: int = 15_000
    timeout_seconds: float = 300
    max_cost: float = 0.50
    currency: str = "USD"


@dataclass(frozen=True)
class Source:
    source_id: str
    title: str
    origin: str
    availability: Literal["metadata", "abstract", "full_text"]
    external_id: str | None = None
    url: str | None = None
    snapshot_path: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    source_id: str
    text: str
    location: str
    snapshot_sha256: str


@dataclass(frozen=True)
class Corpus:
    corpus_revision_id: str
    collection_id: str
    source_ids: list[str]


@dataclass(frozen=True)
class Case:
    case_id: str
    phase: Literal["J1", "J2"]
    split: Literal["dev", "visible", "holdout"]
    category: str
    group_id: str
    question: str
    collection_id: str
    # Frozen English query shared by baseline and system; empty for clarify/refuse.
    search_query: str = ""
    corpus_revision_id: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class Evidence:
    fragment_id: str
    relation: Literal["supports", "contradicts", "uncertain"]


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    evidence: list[Evidence]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    search_calls: int = 0
    steps: int = 0
    # None means unknown/unmeasured, never silently free.
    cost: float | None = None
    currency: str = "USD"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    case_id: str
    variant: Literal["baseline", "system"]
    mode: Literal["live", "fixture"]
    status: Literal["completed", "needs_clarification", "failed", "budget_exceeded", "cancelled"]
    answer: str
    claims: list[Claim]
    limitations: list[str]
    evidence_status: Literal["sufficient", "insufficient_evidence", "not_assessed"]
    stop_reason: str
    usage: Usage
    duration_seconds: float
    human_active_seconds: float | None = None
    # Ordered initial source search, before human selection; J2 may leave it empty.
    retrieved_source_ids: list[str] = field(default_factory=list)
    corpus_revision_id: str | None = None


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool
    # The runner wrapper binds the actual ID; tool implementations may leave it empty.
    tool_call_id: str = ""


class ToolFailure(Exception):
    def __init__(self, error: ToolError):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class Event:
    run_id: str
    step_id: int
    event_type: str
    name: str
    timestamp_utc: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: ToolError | None = None
    duration_seconds: float | None = None
