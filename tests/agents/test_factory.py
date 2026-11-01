import pytest

from app.agents.factory import UnsupportedAdapterError, build_adapter, validate_settings
from app.agents.scripted import ScriptedAdapter
from tests.agents.test_scripted import SCRIPT


def test_factory_builds_scripted_and_refuses_others() -> None:
    adapter = build_adapter("scripted", {"script": SCRIPT.model_dump()})
    assert isinstance(adapter, ScriptedAdapter)
    with pytest.raises(UnsupportedAdapterError):
        build_adapter("http", {})


def test_tool_loop_settings_are_checked_without_a_provider() -> None:
    validate_settings("tool_loop", {"provider": "gemini", "model": "m"})
    with pytest.raises(ValueError):
        validate_settings("tool_loop", {"provider": "gemini"})
    with pytest.raises(UnsupportedAdapterError):
        build_adapter("tool_loop", {"provider": "gemini", "model": "m"})
