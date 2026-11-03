import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.evaluators.contract import ToolCallView


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CallPattern(SpecModel):
    """A tool call with at least these argument values."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    def matches(self, call: ToolCallView) -> bool:
        return call.tool == self.tool and all(
            call.arguments.get(k) == v for k, v in self.args.items()
        )

    def label(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in sorted(self.args.items()))
        return f"{self.tool}({args})"


class OrderRule(SpecModel):
    """Every call of `tool` needs an earlier successful call of each tool in `after`."""

    tool: str
    after: list[str] = Field(min_length=1)
    # When set, the earlier call must name the same value for this argument, such as the same
    # order, as long as both calls name one.
    match_arg: str | None = None


class Pattern(SpecModel):
    name: str
    any_of: list[str] = Field(min_length=1)

    @field_validator("any_of")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc
        return patterns

    def found_in(self, text: str) -> bool:
        return any(re.search(p, text, re.IGNORECASE) for p in self.any_of)


class Claim(Pattern):
    """Something the reply must not say, and the reason code if it does."""

    code: str = Field(pattern=r"^[A-Z][A-Z_]+$")
    # A status claim is undone by "not", "once" and the like. A leak is not: naming another
    # customer's item in a denial still reveals it, so leak checks set this to false.
    negatable: bool = True


class ResponseSpec(SpecModel):
    must_include: list[Pattern] = Field(default_factory=list)
    must_not_claim: list[Claim] = Field(default_factory=list)
