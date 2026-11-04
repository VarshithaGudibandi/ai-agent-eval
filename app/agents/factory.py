from typing import Any

from app.agents.contract import AgentAdapter
from app.agents.http import ExternalLink, HttpAgentAdapter, HttpAgentSettings
from app.agents.scripted import Script, ScriptedAdapter
from app.agents.tool_loop import ModelSource, ToolLoopAdapter, ToolLoopSettings
from app.db.models import AdapterType


class UnsupportedAdapterError(ValueError):
    pass


def validate_settings(adapter_type: str, settings: dict[str, Any]) -> None:
    """Raise if the settings cannot build this adapter. Never contacts a provider."""
    if adapter_type == AdapterType.SCRIPTED:
        Script.model_validate(settings["script"])
    elif adapter_type == AdapterType.TOOL_LOOP:
        ToolLoopSettings.model_validate(settings)
    elif adapter_type == AdapterType.HTTP:
        HttpAgentSettings.model_validate(settings)
    else:
        raise UnsupportedAdapterError(adapter_type)


def build_adapter(
    adapter_type: str,
    settings: dict[str, Any],
    models: ModelSource | None = None,
    link: ExternalLink | None = None,
) -> AgentAdapter:
    if adapter_type == AdapterType.SCRIPTED:
        return ScriptedAdapter(Script.model_validate(settings["script"]))
    if adapter_type == AdapterType.TOOL_LOOP:
        if models is None:
            raise UnsupportedAdapterError("the tool loop needs a model source")
        return ToolLoopAdapter(ToolLoopSettings.model_validate(settings), models)
    if adapter_type == AdapterType.HTTP:
        if link is None:
            raise UnsupportedAdapterError("an HTTP agent needs a link to the gateway")
        return HttpAgentAdapter(HttpAgentSettings.model_validate(settings), link)
    raise UnsupportedAdapterError(adapter_type)
