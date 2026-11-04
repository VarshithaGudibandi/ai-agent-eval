import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.db import models
from app.db.reports import RunFacts
from app.reports.metrics import Scorecard

FORMAT_VERSION = 1


class DemoNotExportableError(Exception):
    """Demo results are canned answers and must never leave as a real report."""


CSV_COLUMNS = [
    "experiment_id",
    "mode",
    "split",
    "dataset_version_id",
    "dataset_content_hash",
    "evaluators",
    "agent_config_id",
    "agent_config_name",
    "config_hash",
    "task_key",
    "category",
    "repetition",
    "run_id",
    "execution_status",
    "evaluation_status",
    "attempts",
    "reason_codes",
    "queue_ms",
    "duration_ms",
    "model_requests",
    "model_tokens",
    "cost_micros",
    "cost_known",
]
# A spreadsheet runs a cell that starts with one of these as a formula. Names and keys come
# from users, so such cells are written as plain text.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _reasons(run: RunFacts) -> list[str]:
    return sorted({s.reason_code for s in run.scores if s.status == "fail" and s.reason_code})


def _run_row(run: RunFacts) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "agent_config_id": run.agent_config_id,
        "task_key": run.task_key,
        "category": run.category,
        "repetition": run.repetition,
        "execution_status": run.execution_status,
        "evaluation_status": run.evaluation_status,
        "attempts": run.attempts,
        "reason_codes": _reasons(run),
        "queue_ms": run.queue_ms,
        "duration_ms": run.duration_ms,
        "model_requests": run.model_requests,
        "model_tokens": run.model_tokens,
        "cost_micros": run.cost_micros,
        "cost_known": run.cost_known,
        "self_reported_usage": run.self_reported,
        "resolved_models": sorted(run.resolved_models),
    }


def json_report(
    experiment: models.Experiment,
    configs: Sequence[models.AgentConfig],
    held_out: dict[str, int],
    cards: Sequence[Scorecard],
    runs: Sequence[RunFacts],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything needed to see what ran, under which versions, and what came of it."""
    return {
        "format_version": FORMAT_VERSION,
        "exported_at": (now or datetime.now(UTC)).isoformat(),
        "experiment": {
            "id": experiment.id,
            "mode": experiment.mode,
            "split": experiment.split,
            "final": experiment.final,
            "status": experiment.status,
            "repetitions": experiment.repetitions,
            "created_at": experiment.created_at.isoformat(),
        },
        "manifest": experiment.manifest,
        "agent_configs": [
            {
                "id": c.id,
                "name": c.name,
                "adapter_type": c.adapter_type,
                "config_hash": c.config_hash,
                "settings": c.settings,
                "limits": c.limits,
                "held_out_runs": held_out.get(c.id, 0),
            }
            for c in configs
        ],
        "scorecards": [card.model_dump(mode="json") for card in cards],
        "runs": [_run_row(r) for r in runs],
    }


def _cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def csv_report(
    experiment: models.Experiment,
    configs: Sequence[models.AgentConfig],
    runs: Sequence[RunFacts],
) -> str:
    """One row per run, each carrying the versions it was graded under."""
    by_id = {c.id: c for c in configs}
    manifest = experiment.manifest
    evaluators = ";".join(f"{e['name']}@{e['version']}" for e in manifest["evaluators"])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for run in runs:
        config = by_id[run.agent_config_id]
        row = {
            **_run_row(run),
            "experiment_id": experiment.id,
            "mode": experiment.mode,
            "split": experiment.split,
            "dataset_version_id": experiment.dataset_version_id,
            "dataset_content_hash": manifest["dataset_version"]["content_hash"],
            "evaluators": evaluators,
            "agent_config_name": config.name,
            "config_hash": config.config_hash,
            "reason_codes": ";".join(_reasons(run)),
        }
        writer.writerow({k: _cell(row[k]) for k in CSV_COLUMNS})
    return out.getvalue()
