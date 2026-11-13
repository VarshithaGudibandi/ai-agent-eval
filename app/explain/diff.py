import difflib
import json
from dataclasses import dataclass
from typing import Any, Literal

Field = Literal["model", "prompt", "tools", "settings"]


@dataclass(frozen=True)
class ConfigChange:
    """One part of the agent's config that differs between the two versions."""

    field: Field
    summary: str
    # For the prompt, the changed lines with - and + in front; for tools and settings, what
    # was added, removed or changed.
    detail: tuple[str, ...] = ()


def _tools(config: dict[str, Any]) -> dict[str, str]:
    tools = config.get("tools")
    named: dict[str, str] = {}
    for tool in tools if isinstance(tools, list) else []:
        entries = tool.get("function_declarations", [tool]) if isinstance(tool, dict) else [tool]
        for entry in entries if isinstance(entries, list) else []:
            name = (
                entry
                if isinstance(entry, str)
                else entry.get("name")
                if isinstance(entry, dict)
                else None
            )
            if isinstance(name, str):
                named[name] = json.dumps(entry, sort_keys=True)
    return named


def config_diff(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[ConfigChange]:
    """What changed between two agent versions, by part, in plain words."""
    changes = []
    if baseline.get("model") != candidate.get("model"):
        changes.append(
            ConfigChange("model", f"model {baseline.get('model')!r} -> {candidate.get('model')!r}")
        )
    old_prompt, new_prompt = str(baseline.get("prompt") or ""), str(candidate.get("prompt") or "")
    if old_prompt != new_prompt:
        # Sentences, not lines: prompts are often one long line.
        old = [s.strip() for s in old_prompt.replace("\n", " ").split(". ") if s.strip()]
        new = [s.strip() for s in new_prompt.replace("\n", " ").split(". ") if s.strip()]
        lines = tuple(
            line
            for line in difflib.unified_diff(old, new, lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("---", "+++"))
        )
        removed = sum(line.startswith("-") for line in lines)
        added = len(lines) - removed
        changes.append(
            ConfigChange("prompt", f"prompt: {removed} sentences removed, {added} added", lines)
        )
    old_tools, new_tools = _tools(baseline), _tools(candidate)
    if old_tools != new_tools:
        detail = (
            [f"- {name}" for name in sorted(set(old_tools) - set(new_tools))]
            + [f"+ {name}" for name in sorted(set(new_tools) - set(old_tools))]
            + [
                f"~ {name}"
                for name in sorted(set(old_tools) & set(new_tools))
                if old_tools[name] != new_tools[name]
            ]
        )
        changes.append(ConfigChange("tools", "tools: " + ", ".join(detail), tuple(detail)))
    rest = {"model", "prompt", "tools"}
    keys = sorted(
        k
        for k in set(baseline) | set(candidate)
        if k not in rest and baseline.get(k) != candidate.get(k)
    )
    if keys:
        settings = tuple(f"{k}: {baseline.get(k)!r} -> {candidate.get(k)!r}" for k in keys)
        changes.append(ConfigChange("settings", f"settings changed: {', '.join(keys)}", settings))
    return changes
