import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"
# The platform takes at most 100 traces and 5 MB per request.
BATCH_TRACES = 100
BATCH_BYTES = 4_000_000


class PlatformError(Exception):
    """The platform could not be reached or refused a request."""

    def __init__(self, message: str, status: int | None = None, retry_after: float = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def worth_retrying(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


def settings(url: str | None, token: str | None) -> tuple[str, str | None]:
    """The platform URL and API token, from the arguments or else the environment."""
    return (
        (url or os.environ.get("AGENT_EVAL_URL") or DEFAULT_URL).rstrip("/"),
        token or os.environ.get("AGENT_EVAL_TOKEN") or None,
    )


class PlatformClient:
    """The few platform API calls the SDK makes, each with a timeout."""

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self._http = httpx.Client(
            base_url=url,
            headers={"authorization": f"Bearer {token}", "user-agent": "agent-eval-sdk"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._http.request(
                method,
                path,
                json=body,
                content=content,
                headers={"content-type": "application/json", **(headers or {})},
            )
        except httpx.HTTPError as exc:
            # The exception text can include the URL but never the token.
            raise PlatformError(f"could not reach {self.url}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            try:
                reason = response.json()
            except ValueError:
                reason = response.text[:200]
            retry_after = response.headers.get("retry-after", "0")
            raise PlatformError(
                f"{method} {path} answered {response.status_code}: {reason}",
                response.status_code,
                float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0,
            )
        return response.json()

    def send_traces(self, encoded: list[bytes]) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", "/api/v1/traces", content=b'{"traces":[' + b",".join(encoded) + b"]}"
        )
        return result

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        self._call("POST", "/api/v1/feedback", feedback)

    def save_dataset(self, name: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", "/api/v1/datasets", {"name": name, "cases": cases}
        )
        return result

    def datasets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("GET", "/api/v1/datasets")
        return result

    def start_experiment(self, body: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", "/api/v1/experiments", body, headers={"idempotency-key": idempotency_key}
        )
        return result

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/experiments/{experiment_id}")
        return result

    def agents(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("GET", "/api/v1/agents")
        return result

    def agent_health(self, agent_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/agents/{agent_id}/health")
        return result

    def failures(self, agent_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/agents/{agent_id}/failures")
        return result

    def experiments(self, agent_id: str, limit: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call(
            "GET", f"/api/v1/agents/{agent_id}/experiments?limit={limit}"
        )
        return result

    def save_eval_spec(self, agent_id: str, scorers: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "PUT", f"/api/v1/agents/{agent_id}/eval-spec", {"scorers": scorers}
        )
        return result

    def baseline(self, experiment_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/experiments/{experiment_id}/baseline")
        return result

    def compare(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._call("POST", "/api/v1/comparisons", body)
        return result

    def request_report(self, comparison_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", "/api/v1/reports", {"comparison_id": comparison_id}
        )
        return result

    def report(self, report_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/reports/{report_id}")
        return result

    def finish_experiment(self, experiment_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("POST", f"/api/v1/experiments/{experiment_id}/finish")
        return result


def retrying[T](
    call: Callable[[], T], attempts: int = 3, first_delay: float = 0.5, max_delay: float = 5.0
) -> T:
    """Run a call, retrying only failures that may pass: no connection, 429 and 5xx."""
    delay = first_delay
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except PlatformError as exc:
            if attempt == attempts or not exc.worth_retrying:
                raise
            time.sleep(min(max(delay, exc.retry_after), max_delay))
            delay *= 2
    raise AssertionError("unreachable")


def encode(trace: dict[str, Any]) -> bytes:
    return json.dumps(trace, separators=(",", ":"), default=str).encode()


def batches(encoded: list[bytes]) -> Iterator[list[bytes]]:
    """Group encoded traces into requests the platform will take."""
    batch: list[bytes] = []
    size = 0
    for item in encoded:
        if batch and (len(batch) == BATCH_TRACES or size + len(item) > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item) + 1
    if batch:
        yield batch
