from typing import Any

from app.agents.contract import AgentAdapter
from app.agents.scripted import Script, ScriptedAdapter
from app.db.models import AdapterType


class UnsupportedAdapterError(ValueError):
    pass


def build_adapter(adapter_type: str, settings: dict[str, Any]) -> AgentAdapter:
    if adapter_type == AdapterType.SCRIPTED:
        return ScriptedAdapter(Script.model_validate(settings["script"]))
    raise UnsupportedAdapterError(adapter_type)
