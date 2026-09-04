"""Create constrained behavioural dimensions outside the policy hot path."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

import requests
from sibyl_memory_client import NotFoundError

from .evidence import SUBJECTS_OF, VALENCE_OF

#: What each outcome actually means, in the words a person would use. The model is shown this
#: rather than being left to infer intent from an identifier.
_MEANINGS = {
    "timeout_claimed_without_delivery":
        "a provider accepted a paid deal, never delivered, and the deadline passed",
    "delivered_and_released_by_buyer":
        "a provider delivered and the buyer released payment straight away",
    "delivered_and_claimed_after_delay":
        "a provider delivered, the buyer did not release payment, and the provider had to "
        "wait out the full payout delay before it could collect",
}


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DIRECTIONS = {"positive", "negative"}
_CONTEXTS = {"deadline_sensitive", "cost_sensitive", "quality_sensitive"}


#: Answers no retry can improve: a bad key, no credit, a refused or malformed request.
_PERMANENT_STATUSES = frozenset({400, 401, 402, 403, 404, 422})

#: One short pause between the two attempts, so a blip gets a second chance and a broken
#: endpoint does not get hammered.
_BACKOFF_SECONDS = 1.0

_sleep = time.sleep


class DimensionError(RuntimeError):
    pass


class DimensionMemory(Protocol):
    def list_entities(self, category: str | None = None, *, status: str | None = None, limit: int = 100): ...
    def get_entity(self, category: str, name: str): ...
    def set_entity(self, category: str, name: str, body, *, status: str | None = None): ...


@dataclass(frozen=True)
class DimensionDefinition:
    dimension_id: str
    source_event_type: str
    signal_direction: str
    severity: float
    confidence: float
    applies_when: tuple[str, ...]

    @classmethod
    def from_model(cls, value: dict[str, Any], source_event_type: str) -> "DimensionDefinition":
        """Build a dimension from a model answer, with the direction supplied by the contract.

        `signal_direction` is accepted here only because stored definitions carry it. A fresh
        model answer never provides it: which way an outcome points is decided by what the
        contract says happened, not by a sentence a model produced.
        """

        allowed = {"dimension_id", "signal_direction", "severity", "confidence", "applies_when"}
        if set(value) != allowed:
            raise DimensionError("model output has missing or unknown fields")
        identifier = value["dimension_id"]
        direction = value["signal_direction"]
        expected = VALENCE_OF.get(source_event_type)
        if expected is not None and direction != expected:
            raise DimensionError(
                f"{source_event_type} is {expected} by the contract's own account, and a "
                f"dimension claiming {direction!r} would price it backwards"
            )
        applies_when = value["applies_when"]
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise DimensionError("dimension_id must be lower snake case")
        if direction not in _DIRECTIONS:
            raise DimensionError("invalid signal_direction")
        if not isinstance(applies_when, list) or not applies_when:
            raise DimensionError("applies_when must be a non-empty list")
        if any(item not in _CONTEXTS for item in applies_when):
            raise DimensionError("applies_when contains an unsupported context")
        if len(set(applies_when)) != len(applies_when):
            # The JSON schema says uniqueItems, and a real model call returned a duplicate
            # anyway. A schema the provider does not enforce is a request, not a guarantee.
            raise DimensionError("applies_when repeats a context")
        severity = _bounded_number(value["severity"], "severity")
        confidence = _bounded_number(value["confidence"], "confidence")
        return cls(identifier, source_event_type, direction, severity, confidence, tuple(applies_when))

    def body(self) -> dict[str, Any]:
        body = asdict(self)
        body["applies_when"] = list(self.applies_when)
        return body


def _bounded_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DimensionError(f"{name} must be numeric")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DimensionError(f"{name} must be between 0 and 1")
    return number


#: Where learned dimensions live. Named here so callers stop repeating the literal.
DIMENSION_CATEGORY = "behavior_dimension"

DIMENSION_JSON_SCHEMA = {
    "name": "wrasse_dimension",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["dimension_id", "severity", "confidence", "applies_when"],
        "properties": {
            "dimension_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
            "severity": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "applies_when": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "enum": sorted(_CONTEXTS)},
            },
        },
    },
}


def create_dimension(
    event: dict[str, Any],
    *,
    api_key: str,
    model: str = "openai/gpt-oss-20b",
    post: Callable[..., Any] = requests.post,
) -> DimensionDefinition:
    """Ask once, validate strictly, and retry once on malformed output."""

    event_type = str(event.get("event_type", ""))
    if not event_type:
        raise DimensionError("event_type is required")

    subjects = SUBJECTS_OF.get(event_type)
    if subjects is None:
        raise DimensionError(f"{event_type!r} is not an outcome this build recognises")
    subject = " and ".join(sorted(subjects))

    # The first live call returned `positive` for a provider that never delivered, and a
    # severity of zero, because the prompt never said whose conduct was being judged or what
    # the direction meant. The model was answering a question nobody had asked it.
    prompt = "\n".join([
        "You are describing one reusable behavioural dimension implied by a single verified "
        "onchain outcome, so that a future counterparty can price it.",
        "",
        f"The outcome is: {_MEANINGS[event_type]}",
        f"It is evidence about the conduct of the {subject.upper()}, and about nobody else.",
        "",
        f"It is already established that this outcome is {VALENCE_OF[event_type]} for a "
        "counterparty. Do not restate that or argue with it. Say what the behaviour is and "
        "how much it should matter.",
        "severity is how much this outcome should move terms, from 0 for not at all to 1 for "
        "as much as anything could. confidence is how strongly this single outcome supports "
        "that reading.",
        "applies_when lists the distinct buyer priorities this matters to. Do not repeat one.",
        "",
        "Give the dimension a lower_snake_case id naming the behaviour, not the party.",
        "State no moral judgment and output no executable expression.",
        "",
        # An allowlisted projection, assembled from constants rather than from the stored
        # body. Handing a model a dictionary out of a database is a way to let whatever ended
        # up in that database write part of the prompt.
        "Outcome: " + json.dumps({
            "event_type": event_type,
            "subjects": sorted(subjects),
            "valence": VALENCE_OF[event_type],
        }, sort_keys=True),
    ])
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Return only the requested behavioural-dimension JSON."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_schema", "json_schema": DIMENSION_JSON_SCHEMA},
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            # A bad key, no credit or a refused request will refuse identically next time.
            # Retrying those wastes the attempt that a genuinely transient failure needs.
            status = getattr(response, "status_code", None)
            if status is not None and status in _PERMANENT_STATUSES:
                raise DimensionError(
                    f"the model endpoint answered {status}, which a retry cannot change"
                )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            answer = json.loads(content)
            answer.pop("signal_direction", None)  # not the model's to decide
            answer["signal_direction"] = VALENCE_OF[event_type]
            return DimensionDefinition.from_model(answer, event_type)
        except DimensionError:
            raise
        except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
            last_error = exc
            if attempt == 0:
                _sleep(_BACKOFF_SECONDS)
    raise DimensionError("model failed to return a valid dimension after two attempts") from last_error


def load_dimensions(memory: DimensionMemory) -> tuple[DimensionDefinition, ...]:
    definitions = []
    for entity in memory.list_entities(DIMENSION_CATEGORY, status="active", limit=100):
        body = entity["body"]
        source_event_type = str(body["source_event_type"])
        model_body = {key: body[key] for key in (
            "dimension_id", "signal_direction", "severity", "confidence", "applies_when"
        )}
        definitions.append(DimensionDefinition.from_model(model_body, source_event_type))
    return tuple(definitions)


def get_or_create_dimension(
    memory: DimensionMemory,
    event: dict[str, Any],
    *,
    api_key: str,
    model: str = "openai/gpt-oss-20b",
    post: Callable[..., Any] = requests.post,
) -> tuple[DimensionDefinition, bool]:
    event_type = str(event.get("event_type", ""))
    for definition in load_dimensions(memory):
        if definition.source_event_type == event_type:
            return definition, False

    definition = create_dimension(event, api_key=api_key, model=model, post=post)
    try:
        collision = memory.get_entity(DIMENSION_CATEGORY, definition.dimension_id)
    except NotFoundError:
        collision = None
    if collision is not None and collision["body"].get("source_event_type") != event_type:
        raise DimensionError("model reused a dimension id for an incompatible event type")
    memory.set_entity(
        DIMENSION_CATEGORY,
        definition.dimension_id,
        definition.body(),
        status="active",
    )
    return definition, True
