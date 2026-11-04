import csv
import io
from datetime import UTC, datetime

from app.db import models
from app.db.reports import RunFacts, ScoreFacts
from app.reports.export import CSV_COLUMNS, csv_report, json_report
from app.reports.metrics import scorecards

NOW = datetime(2026, 11, 4, tzinfo=UTC)
EXPERIMENT = models.Experiment(
    id="exp_1",
    workspace_id="ws",
    dataset_version_id="dv_1",
    mode="real",
    split="validation",
    final=False,
    status="completed",
    repetitions=1,
    manifest={
        "dataset_version": {"id": "dv_1", "content_hash": "abc"},
        "agent_configs": [{"id": "cfg_1", "config_hash": "h1"}],
        "evaluators": [{"name": "refund_outcome", "version": "1.1.0"}],
    },
    created_at=NOW,
)
CONFIG = models.AgentConfig(
    id="cfg_1",
    workspace_id="ws",
    name="=HYPERLINK(evil)",
    adapter_type="http",
    settings={"url": "https://agent.example.com/"},
    limits={"max_turns": 12},
    config_hash="h1",
)
RUN = RunFacts(
    run_id="run_1",
    agent_config_id="cfg_1",
    task_version_id="tv_1",
    task_key="-refund",
    category="refund",
    repetition=1,
    execution_status="completed",
    evaluation_status="fail",
    attempt_id="att_1",
    attempts=2,
    scores=[
        ScoreFacts("e", "1", "outcome", "a", True, "fail", "WRONG_REFUND_AMOUNT", {}),
        ScoreFacts("e", "1", "policy", "b", True, "fail", "OVER_REFUND", {}),
    ],
    cost_micros=7,
    self_reported={"tokens": 3},
)


def test_csv_has_one_row_per_run_with_its_versions() -> None:
    rows = list(csv.DictReader(io.StringIO(csv_report(EXPERIMENT, [CONFIG], [RUN]))))
    assert list(rows[0]) == CSV_COLUMNS
    [row] = rows
    assert row["evaluators"] == "refund_outcome@1.1.0"
    assert row["dataset_content_hash"] == "abc"
    assert row["reason_codes"] == "OVER_REFUND;WRONG_REFUND_AMOUNT"
    assert (row["attempts"], row["cost_micros"]) == ("2", "7")


def test_csv_cells_that_a_spreadsheet_would_run_are_written_as_text() -> None:
    [row] = list(csv.DictReader(io.StringIO(csv_report(EXPERIMENT, [CONFIG], [RUN]))))
    assert row["agent_config_name"] == "'=HYPERLINK(evil)"
    assert row["task_key"] == "'-refund"


def test_json_carries_the_manifest_configs_scorecards_and_runs() -> None:
    report = json_report(
        EXPERIMENT, [CONFIG], {"cfg_1": 4}, scorecards(["cfg_1"], [RUN]), [RUN], NOW
    )
    assert report["format_version"] == 1
    assert report["exported_at"] == NOW.isoformat()
    assert report["manifest"] == EXPERIMENT.manifest
    assert report["agent_configs"][0]["held_out_runs"] == 4
    assert report["agent_configs"][0]["settings"] == CONFIG.settings
    assert report["scorecards"][0]["verified_success"]["value"] == 0.0
    [run] = report["runs"]
    assert run["self_reported_usage"] == {"tokens": 3}
    assert run["reason_codes"] == ["OVER_REFUND", "WRONG_REFUND_AMOUNT"]
