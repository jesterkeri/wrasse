"""Fail-closed access to the evidence needed to produce deal terms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sibyl_memory_client import SibylMemoryError, VerdictCode, refine_zero


class MemoryRequired(RuntimeError):
    """Raised when Wrasse cannot safely read its persistent memory."""


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


def fuzzy_search_never_for_pricing(
    memory: MemoryReader,
    counterparty: str,
    *,
    limit: int = 100,
) -> EvidenceRecall:
    """Fuzzy, capped recall for discovery and display. **Never for setting a price.**

    The name is the warning, because the old one was `recall_counterparty_evidence` and the
    `recall` command called it against the wrong database for weeks.

    Two properties make it unfit for pricing, and both are by design here. It searches rather
    than reading the exact counterparty index, so it can miss a matching row without ever
    reaching its limit. And the limit is applied *before* the counterparty filter, so a hundred
    unrelated rows report no match while the evidence sits behind them. `WrasseStore.recall`
    is the pricing path: exact lookup, identity checked, pending markers refused, every row
    validated.

    A reachable store with no exact evidence for this counterparty is an honest cold start.
    Any Sibyl failure is fatal: callers must not generate terms from guessed or silently empty
    history.
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

