import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from app.scoring.run_view import RunView, SpanView

# Every deterministic check a scorer can run. Built-ins need nothing from the team; rules
# are templates the team fills with parameters. Neither runs code the team wrote.

CheckType = Literal["builtin", "rule"]
Scope = Literal["run", "step"]


@dataclass(frozen=True)
class Outcome:
    """One result of a check: for the whole run, or for one span when span_id is set."""

    value: float
    reason: str
    evidence: tuple[str, ...] = ()
    span_id: str | None = None


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoParams(_Params):
    pass


class TurnLimitParams(_Params):
    max_turns: int = Field(default=10, ge=1, le=1000)


class ToolOrderParams(_Params):
    tool: str = Field(min_length=1)
    after: list[str] = Field(min_length=1)


class ToolParams(_Params):
    tool: str = Field(min_length=1)


class ToolCallLimitParams(_Params):
    tool: str = Field(min_length=1)
    max_calls: int = Field(ge=0)


class TextParams(_Params):
    text: str = Field(min_length=1, max_length=1000)


class MatchesExpectedParams(_Params):
    # A field of the case's expected outcome, the tool whose argument should equal it, and
    # which argument. An expected value of null means the tool must not be called at all.
    field: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    argument: str = Field(min_length=1)


def _passed(value: bool) -> float:
    return 1.0 if value else 0.0


def run_completed(run: RunView, _: NoParams) -> list[Outcome]:
    root = run.root
    if root.error is None:
        return [Outcome(1.0, "the run finished without an error", (root.span_id,))]
    return [Outcome(0.0, f"the run ended with an error: {root.error[:200]}", (root.span_id,))]


def tool_errors(run: RunView, _: NoParams) -> list[Outcome]:
    return [
        Outcome(
            _passed(span.error is None),
            "no error" if span.error is None else f"{span.name} failed: {span.error[:200]}",
            (span.span_id,),
            span.span_id,
        )
        for span in run.tool_calls
    ]


def _lower_types(schema: Any) -> Any:
    # Gemini declarations may spell types in capitals ("STRING"); JSON schema wants "string".
    if isinstance(schema, dict):
        return {
            k: v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)
            for k, v in schema.items()
        }
    if isinstance(schema, list):
        return [_lower_types(v) for v in schema]
    return schema


def valid_tool_arguments(run: RunView, _: NoParams) -> list[Outcome]:
    declared = run.declared_tools
    if not declared:
        # Nothing to check against: the config does not list the agent's tools.
        return []
    outcomes = []
    for span in run.tool_calls:
        tool = declared.get(span.name)
        if tool is None:
            reason, ok = f"{span.name} is not one of the tools the config declares", False
        elif tool.parameters is None:
            reason, ok = f"{span.name} declares no parameters to check", True
        else:
            try:
                jsonschema.validate(span.input, _lower_types(tool.parameters))
                reason, ok = f"the arguments of {span.name} fit its schema", True
            except jsonschema.ValidationError as exc:
                where = "/".join(str(p) for p in exc.absolute_path) or "arguments"
                reason, ok = f"{span.name} {where}: {exc.message[:200]}", False
        outcomes.append(Outcome(_passed(ok), reason, (span.span_id,), span.span_id))
    return outcomes


def turn_limit(run: RunView, params: TurnLimitParams) -> list[Outcome]:
    turns = len(run.model_calls)
    evidence = tuple(s.span_id for s in run.model_calls[params.max_turns :])
    if turns <= params.max_turns:
        return [Outcome(1.0, f"{turns} model calls, the limit is {params.max_turns}")]
    return [Outcome(0.0, f"{turns} model calls, over the limit of {params.max_turns}", evidence)]


def tool_order(run: RunView, params: ToolOrderParams) -> list[Outcome]:
    calls = run.calls_of(params.tool)
    if not calls:
        return [Outcome(1.0, f"{params.tool} was not called")]
    earlier = [s for s in run.tool_calls if s.name in params.after]
    early: list[SpanView] = [
        call for call in calls if not any(e.ended_at <= call.started_at for e in earlier)
    ]
    needed = " or ".join(params.after)
    if early:
        return [
            Outcome(
                0.0,
                f"{params.tool} was called before {needed} had finished",
                tuple(s.span_id for s in early),
            )
        ]
    return [Outcome(1.0, f"every {params.tool} call came after {needed}")]


def tool_not_called(run: RunView, params: ToolParams) -> list[Outcome]:
    calls = run.calls_of(params.tool)
    if calls:
        return [
            Outcome(
                0.0,
                f"{params.tool} was called {len(calls)} times",
                tuple(s.span_id for s in calls),
            )
        ]
    return [Outcome(1.0, f"{params.tool} was not called")]


def tool_call_limit(run: RunView, params: ToolCallLimitParams) -> list[Outcome]:
    calls = run.calls_of(params.tool)
    if len(calls) <= params.max_calls:
        return [Outcome(1.0, f"{params.tool} called {len(calls)} of at most {params.max_calls}")]
    return [
        Outcome(
            0.0,
            f"{params.tool} called {len(calls)} times, more than {params.max_calls}",
            tuple(s.span_id for s in calls[params.max_calls :]),
        )
    ]


def reply_contains(run: RunView, params: TextParams) -> list[Outcome]:
    found = params.text.casefold() in run.reply.casefold()
    reason = f"the reply {'mentions' if found else 'does not mention'} {params.text!r}"
    return [Outcome(_passed(found), reason, (run.root.span_id,))]


def reply_excludes(run: RunView, params: TextParams) -> list[Outcome]:
    found = params.text.casefold() in run.reply.casefold()
    reason = f"the reply {'contains' if found else 'does not contain'} {params.text!r}"
    return [Outcome(_passed(not found), reason, (run.root.span_id,))]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return bool(a == b)


def matches_expected(run: RunView, params: MatchesExpectedParams) -> list[Outcome]:
    expected = run.expected
    if not isinstance(expected, dict) or params.field not in expected:
        return []
    want = expected[params.field]
    calls = run.calls_of(params.tool)
    if want is None:
        if calls:
            reason = f"expected no {params.tool}, but it was called"
            return [Outcome(0.0, reason, tuple(s.span_id for s in calls))]
        return [Outcome(1.0, f"{params.tool} was not called, as expected")]
    if not calls:
        return [Outcome(0.0, f"expected {params.tool} with {params.argument}={want!r}")]
    given = [s.input.get(params.argument) if isinstance(s.input, dict) else None for s in calls]
    if any(_same(g, want) for g in given):
        return [Outcome(1.0, f"{params.tool} {params.argument} was {want!r}, as expected")]
    return [
        Outcome(
            0.0,
            f"{params.tool} {params.argument} was {given[-1]!r}, expected {want!r}",
            tuple(s.span_id for s in calls),
        )
    ]


@dataclass(frozen=True)
class Check:
    kind: CheckType
    scope: Scope
    params: type[_Params]
    run: Callable[[RunView, Any], list[Outcome]]


CHECKS: dict[str, Check] = {
    "run_completed": Check("builtin", "run", NoParams, run_completed),
    "tool_errors": Check("builtin", "step", NoParams, tool_errors),
    "valid_tool_arguments": Check("builtin", "step", NoParams, valid_tool_arguments),
    "turn_limit": Check("builtin", "run", TurnLimitParams, turn_limit),
    "tool_order": Check("rule", "run", ToolOrderParams, tool_order),
    "tool_not_called": Check("rule", "run", ToolParams, tool_not_called),
    "tool_call_limit": Check("rule", "run", ToolCallLimitParams, tool_call_limit),
    "reply_contains": Check("rule", "run", TextParams, reply_contains),
    "reply_excludes": Check("rule", "run", TextParams, reply_excludes),
    "matches_expected": Check("rule", "run", MatchesExpectedParams, matches_expected),
}
