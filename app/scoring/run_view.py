import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class SpanLike(Protocol):
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime
    input: Any
    output: Any
    error: str | None


@dataclass(frozen=True)
class SpanView:
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime
    input: Any
    output: Any
    error: str | None


@dataclass(frozen=True)
class DeclaredTool:
    name: str
    # The JSON schema of the tool's arguments, if the config gives one.
    parameters: dict[str, Any] | None


@dataclass(frozen=True)
class RunView:
    """What a scorer may look at: one run's spans, the agent version's config, and the
    case's expected outcome. Read-only, so a scorer cannot change what another one sees."""

    tags: dict[str, Any]
    config: dict[str, Any]
    expected: Any
    has_expected: bool
    spans: tuple[SpanView, ...]

    @classmethod
    def build(
        cls,
        spans: Iterable[SpanLike],
        tags: dict[str, Any],
        config: dict[str, Any],
        expected: Any = None,
        has_expected: bool = False,
    ) -> "RunView":
        views = tuple(
            SpanView(
                s.span_id,
                s.parent_span_id,
                s.kind,
                s.name,
                s.started_at,
                s.ended_at,
                s.input,
                s.output,
                s.error,
            )
            for s in sorted(spans, key=lambda s: (s.started_at, s.span_id))
        )
        return cls(tags, config, expected, has_expected, views)

    @property
    def root(self) -> SpanView:
        return next(s for s in self.spans if s.parent_span_id is None)

    @property
    def tool_calls(self) -> list[SpanView]:
        return [s for s in self.spans if s.kind == "tool"]

    @property
    def model_calls(self) -> list[SpanView]:
        return [s for s in self.spans if s.kind == "model"]

    def calls_of(self, tool: str) -> list[SpanView]:
        return [s for s in self.tool_calls if s.name == tool]

    @property
    def reply(self) -> str:
        """The run's final answer as text: the root's output, or its usual reply field."""
        output = self.root.output
        if isinstance(output, dict):
            for key in ("reply", "text", "answer", "output"):
                if isinstance(output.get(key), str):
                    return str(output[key])
        if isinstance(output, str):
            return output
        return "" if output is None else json.dumps(output, sort_keys=True)

    @property
    def declared_tools(self) -> dict[str, DeclaredTool] | None:
        """Tools the config declares, by name. None when the config does not say."""
        tools = self.config.get("tools")
        if not isinstance(tools, list):
            return None
        declared: dict[str, DeclaredTool] = {}
        for tool in tools:
            # Either a list of declarations or Gemini's {"function_declarations": [...]}.
            entries = tool.get("function_declarations", [tool]) if isinstance(tool, dict) else []
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    parameters = entry.get("parameters")
                    declared[entry["name"]] = DeclaredTool(
                        entry["name"], parameters if isinstance(parameters, dict) else None
                    )
        return declared
