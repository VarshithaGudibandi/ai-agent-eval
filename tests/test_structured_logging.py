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
