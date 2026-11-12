from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# The JSON document the SDK sends for one agent run. It is checked strictly at the edge, so
# everything after ingest can trust its shape.

Tag = str | int | float | bool


class Span(BaseModel):
    """One step of a run: a model call, a tool call, or any other step the agent took."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    kind: Literal["model", "tool", "step"]
    name: str = Field(min_length=1, max_length=200)
    start: AwareDatetime
    end: AwareDatetime
    input: Any = None
    output: Any = None
    error: str | None = Field(default=None, max_length=10_000)
    # Model spans only.
    provider: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=200)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    other_tokens: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "Span":
        if self.end < self.start:
            raise ValueError(f"span {self.id} ends before it starts")
        if self.kind == "model" and not self.model:
            raise ValueError(f"model span {self.id} must name its model")
        return self


class Trace(BaseModel):
    """One agent run as the SDK records it."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    agent: str = Field(min_length=1, max_length=200)
    # What makes this version of the agent: model, prompt text, tool schemas, settings.
    config: dict[str, Any]
    label: str | None = Field(default=None, min_length=1, max_length=100)
    tags: dict[str, Tag] = Field(default_factory=dict, max_length=50)
    spans: list[Span] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _one_tree(self) -> "Trace":
        ids = [s.id for s in self.spans]
        if len(ids) != len(set(ids)):
            raise ValueError("span ids must be unique within a trace")
        known = set(ids)
        roots = [s for s in self.spans if s.parent_id is None]
        if len(roots) != 1:
            raise ValueError(f"a trace needs exactly one root span, found {len(roots)}")
        orphans = sorted(s.id for s in self.spans if s.parent_id and s.parent_id not in known)
        if orphans:
            raise ValueError(f"spans name a parent that is not in the trace: {orphans[:5]}")
        # One root and every parent present still allows a loop off to the side, so every
        # span must lead up to the root. Spans already known to get there end the walk early.
        parent = {s.id: s.parent_id for s in self.spans}
        reaches_root = {roots[0].id}
        for span_id in ids:
            path: set[str] = set()
            current: str | None = span_id
            while current is not None and current not in reaches_root:
                if current in path:
                    raise ValueError(f"spans form a loop at {current}")
                path.add(current)
                current = parent[current]
            reaches_root |= path
        return self

    @property
    def root(self) -> Span:
        return next(s for s in self.spans if s.parent_id is None)
