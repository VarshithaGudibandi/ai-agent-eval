import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_eval.client import (
    PlatformClient,
    PlatformError,
    batches,
    encode,
    retrying,
    settings,
)

logger = logging.getLogger("agent_eval")

PENDING_DIR = Path(".agent_eval") / "pending"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class PendingTraces:
    """Traces from an Eval that could not be sent yet, one file per experiment.

    Each trace is written and synced to disk before Eval moves on, so stopping the process
    or losing the platform mid-run loses nothing.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path.cwd() / PENDING_DIR

    def _path(self, experiment_id: str) -> Path:
        if not _SAFE_NAME.match(experiment_id):
            raise ValueError(f"not an experiment id: {experiment_id!r}")
        return self.directory / f"{experiment_id}.jsonl"

    def add(self, experiment_id: str, trace: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._path(experiment_id).open("ab") as file:
            file.write(encode(trace) + b"\n")
            file.flush()
            os.fsync(file.fileno())

    def experiments(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.jsonl") if _SAFE_NAME.match(p.stem))

    def traces(self, experiment_id: str) -> list[dict[str, Any]]:
        path = self._path(experiment_id)
        if not path.exists():
            return []
        traces = []
        for line in path.read_bytes().splitlines():
            try:
                traces.append(json.loads(line))
            except ValueError:
                # Only a line cut short by a crash mid-write can fail to parse.
                logger.warning("agent_eval: skipped a damaged line in %s", path)
        return traces

    def keep_only(self, experiment_id: str, traces: list[dict[str, Any]]) -> None:
        path = self._path(experiment_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(b"".join(encode(t) + b"\n" for t in traces))
        os.replace(temporary, path)

    def remove(self, experiment_id: str) -> None:
        self._path(experiment_id).unlink(missing_ok=True)


@dataclass
class FlushResult:
    uploaded: int = 0
    rejected: int = 0
    remaining: int = 0


def upload(client: PlatformClient, store: PendingTraces, experiment_id: str) -> FlushResult:
    """Send one experiment's saved traces, then finish the experiment.

    Stops at the first request that fails and keeps what is left for next time.
    """
    result = FlushResult()
    traces = store.traces(experiment_id)
    try:
        # A wrong token or workspace would get every trace rejected; keep them instead.
        retrying(lambda: client.experiment(experiment_id))
    except PlatformError as exc:
        logger.warning("agent_eval: kept %d traces of %s: %s", len(traces), experiment_id, exc)
        result.remaining = len(traces)
        return result
    sent = 0
    try:
        for batch in batches([encode(t) for t in traces]):
            response = _send(client, batch)
            for rejected in response["rejected"]:
                result.rejected += 1
                logger.warning(
                    "agent_eval: trace %s rejected: %s", rejected["trace_id"], rejected["error"]
                )
            result.uploaded += len(batch) - len(response["rejected"])
            sent += len(batch)
        retrying(lambda: client.finish_experiment(experiment_id))
    except PlatformError as exc:
        logger.warning("agent_eval: could not upload saved traces: %s", exc)
        store.keep_only(experiment_id, traces[sent:])
        result.remaining = len(traces) - sent
        return result
    store.remove(experiment_id)
    return result


def _send(client: PlatformClient, batch: list[bytes]) -> dict[str, Any]:
    try:
        return retrying(lambda: client.send_traces(batch))
    except PlatformError as exc:
        if exc.status != 413:
            raise
    if len(batch) == 1:
        # One trace larger than the platform takes will never go in.
        return {"rejected": [{"trace_id": "?", "error": "larger than the platform accepts"}]}
    # The platform's size limit is lower than the SDK's; send the batch in halves.
    half = len(batch) // 2
    first, second = _send(client, batch[:half]), _send(client, batch[half:])
    return {"rejected": first["rejected"] + second["rejected"]}


def flush(
    url: str | None = None, token: str | None = None, directory: Path | None = None
) -> FlushResult:
    """Upload traces an Eval saved while the platform could not be reached.

    Safe to run any number of times: the platform stores each trace once.
    """
    base_url, api_token = settings(url, token)
    if api_token is None:
        raise ValueError("set AGENT_EVAL_TOKEN or pass token= to upload saved traces")
    store = PendingTraces(directory)
    total = FlushResult()
    client = PlatformClient(base_url, api_token)
    try:
        for experiment_id in store.experiments():
            result = upload(client, store, experiment_id)
            total.uploaded += result.uploaded
            total.rejected += result.rejected
            total.remaining += result.remaining
    finally:
        client.close()
    return total
