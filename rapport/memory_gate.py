"""Fail-closed access to the evidence needed to produce deal terms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sibyl_memory_client import SibylMemoryError, VerdictCode, refine_zero


class MemoryRequired(RuntimeError):
    """Raised when Rapport cannot safely read its persistent memory."""


class MemoryReader(Protocol):
    def search_entities(
        self,
        query: str,
        *,
        limit: int = 20,
        prefix: bool = False,
        category: str | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class EvidenceRecall:
    counterparty: str
    evidence: tuple[dict[str, Any], ...]
    verdict: str

    @property
    def is_cold_start(self) -> bool:
        return not self.evidence and self.verdict in {
            VerdictCode.EMPTY_STORE.value,
            VerdictCode.NO_MATCH.value,
        }


def recall_counterparty_evidence(
    memory: MemoryReader,
    counterparty: str,
    *,
    limit: int = 100,
) -> EvidenceRecall:
    """Recall verified chain events, or stop if memory cannot be consulted.

    A reachable store with no exact evidence for this counterparty is an honest
    cold start. Any Sibyl failure is fatal: callers must not generate terms from
    guessed or silently empty history.
    """

    normalized = counterparty.lower()
    try:
        results = memory.search_entities(
            counterparty,
            category="chain_event",
            limit=limit,
        )
        if not results:
            results = refine_zero(memory, results)
        evidence = tuple(
            row["body"]
            for row in results
            if isinstance(row.get("body"), dict)
            and str(row["body"].get("provider", "")).lower() == normalized
        )
        verdict = results.verdict.code.value
        if results and not evidence:
            verdict = VerdictCode.NO_MATCH.value
        return EvidenceRecall(normalized, evidence, verdict)
    except SibylMemoryError as exc:
        raise MemoryRequired("Sibyl Memory is required to generate deal terms") from exc

