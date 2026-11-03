import json
import logging

import pytest

from app.structured_logging import configure_logging


def test_json_logs_include_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger("example").info("claimed job", extra={"job_id": "job_1"})
    line = json.loads(capsys.readouterr().out.strip())
    assert line["message"] == "claimed job"
    assert line["level"] == "info"
    assert line["job_id"] == "job_1"
    assert line["time"].endswith("+00:00")


# Shaped like a real Google key (39 characters) so masking is tested on the real shape, but
# assembled at runtime so no key-shaped string sits in the source for scanners to flag.
FAKE_GOOGLE_KEY = "AIza" + "Sy" + "x" * 33


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_secrets_are_masked_in_every_format(fmt: str, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", fmt)
    logging.getLogger("example").warning(
        f"calling with {FAKE_GOOGLE_KEY} and Bearer aet_abcdefghijklmnopqrstuvwxyz",
        extra={"token": "ses_abcdefghijklmnopqrstuvwxyz0123"},
    )
    out = capsys.readouterr().out
    assert "AIza" not in out
    assert "aet_abc" not in out
    assert "ses_abc" not in out
    assert out.count("[redacted]") == (3 if fmt == "json" else 2)
