import pytest

from app.environments.registry import UnknownEnvironmentError, default_registry


def test_default_registry_has_support_retail() -> None:
    assert default_registry().keys() == ["support-retail@1"]


def test_unknown_environment_is_an_error() -> None:
    with pytest.raises(UnknownEnvironmentError):
        default_registry().create("nowhere@1", store=None)  # type: ignore[arg-type]
