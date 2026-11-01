"""Event contract v1: the only shape this client writes and the profiler
server reads. It matches the server's ``TraceEvent`` field for field, so
a prepared dataset is accepted by today's ``/analyze`` unchanged."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"


class _Strict(BaseModel):
    # an unknown field is a contract break, not something to pass along
    model_config = ConfigDict(extra="forbid")


class TokenUsage(_Strict):
    input: int = 0
    output: int = 0


class Decision(_Strict):
    type: str
    selected: str


class Tool(_Strict):
    name: str


class TraceEvent(_Strict):
    trace_id: str
    run_id: str
    workflow: str
    agent: str | None = None
    event_type: str
    node: str
    parent: str | None = None
    input_state: dict = Field(default_factory=dict)
    decision: Decision | None = None
    tool: Tool | None = None
    model: str | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    cost: float = 0.0
    result: str | None = None
    workflow_version: str | None = None
    trigger: str | None = None
    # never written by this client: the user's prompt stays on the machine
    user_input: str | None = None
    # False: input_state is only part of what the action saw. None = not
    # declared. Tool calls record their whole input, so they declare True.
    input_complete: bool | None = None
    has_side_effects: bool | None = None
    idempotent: bool | None = None
    cache_eligible: bool | None = None
    cache_ttl_seconds: int | None = None
    invalidation_keys: list[str] | None = None
