import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_eval.client import PlatformClient, PlatformError, encode, retrying, settings
from agent_eval.eval_spec import save_spec_with
from agent_eval.pending import FlushResult, PendingTraces, upload
from agent_eval.recording import (
    RunContext,
    config_document,
    is_traced,
    reset_eval_context,
    traced,
    use_eval_context,
)
from agent_eval.redact import RedactHook

logger = logging.getLogger("agent_eval")

CASE_FIELDS = {"key", "input", "expected", "tags", "notes"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Cases from a JSON list (or {"cases": [...]}) or a JSON Lines file, one case a line."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        cases = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        cases = data["cases"] if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} holds no cases")
    for number, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or "key" not in case or "input" not in case:
            raise ValueError(f"case {number} in {path} needs a key and an input")
        unknown = set(case) - CASE_FIELDS
        if unknown:
            raise ValueError(f"case {number} in {path} has unknown fields {sorted(unknown)}")
    return cases


class Eval:
    """Run an agent on every case of a dataset and send each run to the platform.

    Creating an Eval runs it to the end and prints a summary. The dataset is a local JSON or
    JSON Lines file, uploaded first as a new dataset version when its cases changed, or the
    name of a dataset already on the platform. task is called once per case and repetition
    with the case's input; each call becomes one trace tagged with its experiment, case and
    repetition. Traces that cannot be sent are saved under .agent_eval/pending and sent at
    the end, by the next Eval, or by agent_eval.flush().

    scorers, if given, are saved as the agent's eval spec before any case runs. At the end
    Eval waits up to score_timeout seconds for every run to be scored.
    """

    def __init__(
        self,
        agent: str,
        task: Callable[[Any], Any],
        dataset: str | Path,
        config: dict[str, Any],
        repetitions: int = 3,
        label: str | None = None,
        redact: RedactHook | None = None,
        url: str | None = None,
        token: str | None = None,
        pending_dir: Path | None = None,
        scorers: list[dict[str, Any]] | None = None,
        score_timeout: float = 120.0,
    ) -> None:
        if not isinstance(repetitions, int) or not 1 <= repetitions <= 100:
            raise ValueError("repetitions must be a whole number from 1 to 100")
        base_url, api_token = settings(url, token)
        if api_token is None:
            raise ValueError("set AGENT_EVAL_TOKEN or pass token= to run an Eval")
        self.agent, self.label, self.redact = agent, label, redact
        self.repetitions = repetitions
        self.config = config_document(config)
        self.task = task if is_traced(task) else traced(task)
        self.store = PendingTraces(pending_dir)
        self.task_errors = 0
        self.saved = 0
        self.rejected: list[dict[str, Any]] = []
        self.runs_scored = 0
        self.runs_passed = 0
        self.failures: dict[str, int] = {}
        self.score_timeout = score_timeout
        self._platform_down = False
        self._client = PlatformClient(base_url, api_token)
        try:
            self._flush_earlier()
            self._start(dataset)
            if scorers is not None:
                version = save_spec_with(self._client, agent, scorers)
                print(f"agent_eval: {agent} is graded by eval spec version {version}")
            self._run()
            self._finish()
        finally:
            self._client.close()
        print(self.summary())

    def _flush_earlier(self) -> None:
        for experiment_id in self.store.experiments():
            result = upload(self._client, self.store, experiment_id)
            if result.uploaded:
                print(f"agent_eval: uploaded {result.uploaded} saved traces of {experiment_id}")

    def _dataset_version(self, dataset: str | Path) -> str:
        path = Path(dataset)
        if path.suffix in (".json", ".jsonl") or path.exists():
            saved = retrying(lambda: self._client.save_dataset(path.stem, load_cases(path)))
            return str(saved["id"])
        for found in retrying(self._client.datasets):
            if found["name"] == str(dataset) and found["versions"]:
                newest = max(found["versions"], key=lambda v: v["version"])
                return str(newest["id"])
        raise ValueError(f"no dataset file or platform dataset named {dataset!r}")

    def _start(self, dataset: str | Path) -> None:
        body = {
            "agent": self.agent,
            "config": self.config,
            "label": self.label,
            "dataset_version_id": self._dataset_version(dataset),
            "repetitions": self.repetitions,
        }
        # The same key on every retry, so a start whose answer was lost is not started twice.
        key = uuid.uuid4().hex
        experiment = retrying(lambda: self._client.start_experiment(body, key))
        self.experiment_id: str = experiment["id"]
        self.cases: list[dict[str, Any]] = experiment["cases"]
        self.expected_runs: int = experiment["expected_runs"]
        self.runs_received: int = experiment["runs_received"]

    def _slots(self) -> list[tuple[dict[str, Any], int]]:
        return [(case, rep) for case in self.cases for rep in range(1, self.repetitions + 1)]

    def _context(self, case: dict[str, Any], repetition: int) -> RunContext:
        return RunContext(
            agent=self.agent,
            config=self.config,
            label=self.label,
            tags={"experiment": self.experiment_id, "case": case["key"], "repetition": repetition},
            deliver=self._deliver,
            redact=self.redact,
        )

    def _run(self) -> None:
        if inspect.iscoroutinefunction(self.task):
            asyncio.run(self._run_async())
            return
        for case, repetition in self._slots():
            token = use_eval_context(self._context(case, repetition))
            try:
                self.task(case["input"])
            except Exception as exc:
                self._task_failed(case, exc)
            finally:
                reset_eval_context(token)

    async def _run_async(self) -> None:
        for case, repetition in self._slots():
            token = use_eval_context(self._context(case, repetition))
            try:
                await self.task(case["input"])
            except Exception as exc:
                self._task_failed(case, exc)
            finally:
                reset_eval_context(token)

    def _task_failed(self, case: dict[str, Any], exc: Exception) -> None:
        # The error is on the run's trace; one failing case does not stop the others.
        self.task_errors += 1
        logger.warning("agent_eval: case %s raised %s", case["key"], type(exc).__name__)

    def _deliver(self, trace: dict[str, Any]) -> None:
        if not self._platform_down:
            try:
                result = retrying(lambda: self._client.send_traces([encode(trace)]), attempts=2)
                self.rejected += result["rejected"]
                return
            except PlatformError as exc:
                # Save the rest straight to disk instead of waiting on each one.
                logger.warning("agent_eval: saving traces locally, %s", exc)
                self._platform_down = True
        self.store.add(self.experiment_id, trace)
        self.saved += 1

    def _finish(self) -> None:
        if self.saved:
            result: FlushResult = upload(self._client, self.store, self.experiment_id)
            self.saved = result.remaining
            if result.remaining:
                return
        else:
            try:
                retrying(lambda: self._client.finish_experiment(self.experiment_id))
            except PlatformError as exc:
                logger.warning("agent_eval: could not finish the experiment: %s", exc)
        self._wait_for_scores()

    def _wait_for_scores(self) -> None:
        # Scoring runs on the platform's worker, a moment behind ingest.
        deadline = time.monotonic() + self.score_timeout
        while True:
            try:
                progress = retrying(lambda: self._client.experiment(self.experiment_id))
            except PlatformError as exc:
                logger.warning("agent_eval: could not read the experiment back: %s", exc)
                return
            self.runs_received = progress["runs_received"]
            self.runs_scored = progress["runs_scored"]
            self.runs_passed = progress["runs_passed"]
            self.failures = progress["failures"]
            if self.runs_scored >= self.runs_received or time.monotonic() >= deadline:
                return
            time.sleep(0.5)

    def summary(self) -> str:
        lines = [
            f"Experiment {self.experiment_id}: {self.agent}, {len(self.cases)} cases"
            f" x {self.repetitions} repetitions",
            f"  {self.runs_received} of {self.expected_runs} runs received,"
            f" {self.task_errors} raised an error, {len(self.rejected)} rejected",
        ]
        if self.runs_scored < self.runs_received:
            lines.append(
                f"  scoring is not finished: {self.runs_scored} of {self.runs_received} runs"
                f" scored after {self.score_timeout:.0f} seconds"
            )
        elif self.runs_scored:
            lines.append(f"  {self.runs_passed} of {self.runs_scored} runs passed")
        for key, count in self.failures.items():
            lines.append(f"  {key} failed in {count} runs")
        for rejected in self.rejected[:5]:
            lines.append(f"  rejected {rejected['trace_id']}: {rejected['error']}")
        if self.saved:
            lines.append(
                f"  {self.saved} traces could not be sent and are saved in"
                f" {self.store.directory}. Run agent_eval.flush() once the platform is back."
            )
        lines.append(f"  {self._client.url}/api/v1/experiments/{self.experiment_id}")
        return "\n".join(lines)
