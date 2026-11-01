import os

import pytest


@pytest.fixture(autouse=True)
def only_when_asked() -> None:
    # These tests call a real, possibly paid, model. They run only when asked for on purpose.
    if os.environ.get("RUN_PAID_TESTS") != "1" or not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("paid tests run only with RUN_PAID_TESTS=1 and GEMINI_API_KEY set")
