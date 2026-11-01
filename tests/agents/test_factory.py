import pytest

from app.agents.factory import UnsupportedAdapterError, build_adapter
from app.agents.scripted import ScriptedAdapter
from tests.agents.test_scripted import SCRIPT


def test_factory_builds_scripted_and_refuses_others() -> None:
    adapter = build_adapter("scripted", {"script": SCRIPT.model_dump()})
    assert isinstance(adapter, ScriptedAdapter)
    with pytest.raises(UnsupportedAdapterError):
        build_adapter("http", {})
