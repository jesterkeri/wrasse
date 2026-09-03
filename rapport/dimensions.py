"""Create constrained behavioural dimensions outside the policy hot path."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

import requests
from sibyl_memory_client import NotFoundError


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DIRECTIONS = {"positive", "negative"}
_CONTEXTS = {"deadline_sensitive", "cost_sensitive", "quality_sensitive"}


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
        allowed = {"dimension_id", "signal_direction", "severity", "confidence", "applies_when"}
        if set(value) != allowed:
            raise DimensionError("model output has missing or unknown fields")
        identifier = value["dimension_id"]
        direction = value["signal_direction"]
        applies_when = value["applies_when"]
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise DimensionError("dimension_id must be lower snake case")
        if direction not in _DIRECTIONS:
            raise DimensionError("invalid signal_direction")
        if not isinstance(applies_when, list) or not applies_when:
            raise DimensionError("applies_when must be a non-empty list")
        if any(item not in _CONTEXTS for item in applies_when):
            raise DimensionError("applies_when contains an unsupported context")
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


DIMENSION_JSON_SCHEMA = {
    "name": "rapport_dimension",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["dimension_id", "signal_direction", "severity", "confidence", "applies_when"],
        "properties": {
            "dimension_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
            "signal_direction": {"type": "string", "enum": sorted(_DIRECTIONS)},
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
    prompt = (
        "Infer one reusable behavioural dimension from this neutral, verified event. "
        "Do not make moral judgments or output executable expressions. Event: "
        + json.dumps(event, sort_keys=True)
    )
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
    for _ in range(2):
        try:
            response = post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return DimensionDefinition.from_model(json.loads(content), event_type)
        except (KeyError, TypeError, ValueError, requests.RequestException, DimensionError) as exc:
            last_error = exc
    raise DimensionError("model failed to return a valid dimension after two attempts") from last_error


def load_dimensions(memory: DimensionMemory) -> tuple[DimensionDefinition, ...]:
    definitions = []
    for entity in memory.list_entities("behavior_dimension", status="active", limit=100):
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
        collision = memory.get_entity("behavior_dimension", definition.dimension_id)
    except NotFoundError:
        collision = None
    if collision is not None and collision["body"].get("source_event_type") != event_type:
        raise DimensionError("model reused a dimension id for an incompatible event type")
    memory.set_entity(
        "behavior_dimension",
        definition.dimension_id,
        definition.body(),
        status="active",
    )
    return definition, True
