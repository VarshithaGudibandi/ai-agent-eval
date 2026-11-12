import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.scoring.checks import CHECKS

Severity = Literal["hard", "soft"]
Tag = str | int | float | bool


class AppliesWhen(BaseModel):
    """Which runs a scorer grades. Empty means every run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Every listed tag must have exactly this value on the run.
    tags: dict[str, Tag] = Field(default_factory=dict, max_length=20)
    # At least one of these tools was called.
    tools_used: list[str] = Field(default_factory=list, max_length=20)
    # Only runs whose case has an expected outcome (true), or only runs without one (false).
    has_expected: bool | None = None


class ScorerDefinition(BaseModel):
    """One scorer as a team writes it. Only key and check are required."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    check: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = "hard"
    threshold: float = Field(default=1.0, ge=0, le=1)
    applies_when: AppliesWhen = Field(default_factory=AppliesWhen)

    @model_validator(mode="after")
    def _known_check(self) -> "ScorerDefinition":
        check = CHECKS.get(self.check)
        if check is None:
            raise ValueError(f"unknown check {self.check!r}; known: {', '.join(sorted(CHECKS))}")
        try:
            filled = check.params.model_validate(self.params).model_dump()
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}"
                for e in exc.errors()[:3]
            )
            raise ValueError(f"{self.check} params: {problems}") from None
        # Defaults written out, so two ways of saying the same scorer hash the same.
        object.__setattr__(self, "params", filled)
        return self

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class EvalSpecDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorers: list[ScorerDefinition] = Field(max_length=50)

    @model_validator(mode="after")
    def _unique_keys(self) -> "EvalSpecDefinition":
        keys = [s.key for s in self.scorers]
        doubled = sorted({k for k in keys if keys.count(k) > 1})
        if doubled:
            raise ValueError(f"scorer keys must be unique: {', '.join(doubled)}")
        return self


# What every new agent is graded by until its team saves a spec of its own.
DEFAULT_SCORERS = [
    ScorerDefinition(key="run_completed", check="run_completed"),
    ScorerDefinition(key="tool_errors", check="tool_errors"),
    ScorerDefinition(key="valid_tool_arguments", check="valid_tool_arguments"),
    ScorerDefinition(key="turn_limit", check="turn_limit"),
]
