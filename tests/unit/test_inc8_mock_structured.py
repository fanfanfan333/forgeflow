"""INC8-A3 — ``MockChatModel.with_structured_output`` must produce a valid
instance for a schema that has **required** fields (P0-2 unblocker).

Old behaviour: ``model_validate({})`` raised, the exception was swallowed and the
callable returned ``{}`` — so downstream attribute access (e.g. supervisor
``decision.next``) blew up with ``AttributeError`` and the real graph was
unrunnable under ``LLM_PROVIDER=mock``. The mock now seeds every required field
with a type-appropriate, deterministic default; a schema with **no** required
fields behaves exactly as before (``model_validate({})``).
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel

from forgeflow.models.provider import MockChatModel


def _structured(schema):
    return MockChatModel().with_structured_output(schema).invoke("anything")


class _Routing(BaseModel):
    next: str
    confidence: float
    retries: int
    tags: list[str] = []


class _NoRequired(BaseModel):
    name: str = "z"
    count: int = 7


class _Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class _Inner(BaseModel):
    n: int


class _Complex(BaseModel):
    name: str
    maybe: Optional[str]
    color: _Color
    inner: _Inner
    mapping: dict


def test_required_fields_get_typed_defaults():
    out = _structured(_Routing)
    assert isinstance(out, _Routing)
    assert out.next == ""
    assert out.confidence == 0.0
    assert out.retries == 0
    assert out.tags == []


def test_no_required_fields_matches_model_validate_empty():
    out = _structured(_NoRequired)
    assert out == _NoRequired.model_validate({})
    assert out.name == "z" and out.count == 7


def test_optional_nested_enum_and_dict_are_handled():
    out = _structured(_Complex)
    assert out.name == ""
    assert out.maybe is None
    assert out.color is _Color.RED
    assert out.inner.n == 0
    assert out.mapping == {}


def test_supervisor_routing_decision_is_usable_under_mock():
    from forgeflow.agents.supervisor import RoutingDecision

    out = _structured(RoutingDecision)
    assert isinstance(out, RoutingDecision)
    # The old ``{}`` made this next line raise ``AttributeError``.
    assert out.next
    assert out.next in {
        "researcher",
        "analyzer",
        "executor",
        "human_approval",
        "FINISH",
    }


def test_output_is_deterministic_across_calls():
    assert _structured(_Routing) == _structured(_Routing)
