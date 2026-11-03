import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_heartbeat_must_fit_twice_in_a_lease() -> None:
    assert Settings(lease_seconds=30, heartbeat_seconds=15).heartbeat_seconds == 15
    with pytest.raises(ValidationError, match="half of lease_seconds"):
        Settings(lease_seconds=30, heartbeat_seconds=16)
