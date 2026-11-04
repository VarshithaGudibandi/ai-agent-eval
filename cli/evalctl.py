"""evalctl: run agent evaluations from a terminal or a CI pipeline.

Set EVALCTL_URL (default http://127.0.0.1:8000) and EVALCTL_TOKEN (a workspace API token).
`evalctl run` exits non-zero when verified success is below the threshold, when any
executed violation appears, or when the candidate regresses on more tasks than allowed.

Exit codes: 0 every gate passed, 1 a gate failed, 2 a usage or API error, 3 the experiment
did not finish in time or was paused.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import httpx

PASSED, GATE_FAILED, ERROR, UNFINISHED = 0, 1, 2, 3
FINISHED = ("completed", "closed_partial", "cancelled")
PAUSED = ("paused_budget", "paused_user")


class CommandError(Exception):
    """Something the user must fix. Printed as is, never with a traceback."""


class Api:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _check(self, response: httpx.Response) -> Any:
        if response.is_success:
            return response.json() if response.content else None
        try:
            body = response.json()
        except ValueError:
            body = {}
        # Domain errors are flat; validation errors sit under "detail".
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, list):
            message = "; ".join(str(d.get("msg", d)) for d in detail)
        elif isinstance(detail, dict):
            message = " ".join(str(v) for v in (detail.get("error"), detail.get("message")) if v)
            if detail.get("field"):
                message += f" (field: {detail['field']})"
        else:
            message = str(detail)
        request = response.request
        raise CommandError(
            f"{request.method} {request.url.path} failed with {response.status_code}: {message}"
        )

    def get(self, path: str, **params: Any) -> Any:
        return self._check(self._client.get(path, params=params))

    def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self._check(self._client.post(path, json=body, headers=headers))

    def raw(self, path: str, **params: Any) -> httpx.Response:
        response = self._client.get(path, params=params)
        self._check(response)
        return response


def _client() -> httpx.Client:
    token = os.environ.get("EVALCTL_TOKEN")
    if not token:
        raise CommandError("set EVALCTL_TOKEN to a workspace API token")
    return httpx.Client(
        base_url=os.environ.get("EVALCTL_URL", "http://127.0.0.1:8000"),
        headers={"authorization": f"Bearer {token}"},
        timeout=60,
    )


def _config_id(api: Api, name_or_id: str) -> str:
    configs = api.get("/api/v1/agent-configs")
    if any(c["id"] == name_or_id for c in configs):
        return name_or_id
    # Configs are immutable, so a name can have several versions. The newest one wins.
    named = [c for c in configs if c["name"] == name_or_id]
    if not named:
        raise CommandError(f"no agent config named {name_or_id!r}")
    return str(max(named, key=lambda c: c["created_at"])["id"])


def _dataset_version_id(api: Api, name: str, version: int | None) -> str:
    for dataset in api.get("/api/v1/datasets"):
        if dataset["name"] != name:
            continue
        versions = dataset["versions"]
        if version is not None:
            versions = [v for v in versions if v["version"] == version]
        if versions:
            return str(max(versions, key=lambda v: v["version"])["id"])
        raise CommandError(f"dataset {name!r} has no version {version}")
    raise CommandError(f"no dataset named {name!r}")


def _rate(rate: dict[str, Any]) -> str:
    value = rate["value"]
    shown = "n/a" if value is None else f"{value:.0%}"
    return f"{shown} ({rate['numerator']}/{rate['denominator']})"


def _wait(api: Api, experiment_id: str, timeout: float, poll: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        experiment: dict[str, Any] = api.get(f"/api/v1/experiments/{experiment_id}")
        status = experiment["status"]
        if status in FINISHED or status in PAUSED:
            return experiment
        if time.monotonic() >= deadline:
            return experiment
        time.sleep(poll)


def run(args: argparse.Namespace, api: Api) -> int:
    candidate = _config_id(api, args.config)
    configs = [candidate]
    baseline = None
    if args.baseline:
        baseline = _config_id(api, args.baseline)
        if baseline == candidate:
            raise CommandError("the baseline and the candidate are the same config")
        # Both in one experiment: same queue, same faults, same moment.
        configs = [baseline, candidate]
    budget = {
        k: v
        for k, v in (
            ("money_micros", args.budget_money_micros),
            ("tokens", args.budget_tokens),
            ("requests", args.budget_requests),
        )
        if v is not None
    }
    body = {
        "dataset_version_id": _dataset_version_id(api, args.dataset, args.version),
        "agent_config_ids": configs,
        "repetitions": args.repetitions,
        "mode": args.mode,
        "split": args.split,
        "final": args.final,
        "budget": budget,
    }
    headers = {"idempotency-key": args.idempotency_key} if args.idempotency_key else None
    experiment = api.post("/api/v1/experiments", body, headers=headers)
    print(f"experiment {experiment['id']}: {len(configs)} config(s) on the {args.split} split")
    if not args.wait:
        return PASSED
    experiment = _wait(api, experiment["id"], args.timeout, args.poll)
    if experiment["status"] not in FINISHED:
        print(
            f"experiment {experiment['id']} is {experiment['status']}, not finished",
            file=sys.stderr,
        )
        return UNFINISHED
    cards = {c["agent_config_id"]: c for c in experiment["scorecards"]}
    card = cards[candidate]
    result: dict[str, Any] = {"experiment_id": experiment["id"], "scorecard": card, "gates": []}
    print(f"verified success   {_rate(card['verified_success'])}")
    print(f"coverage           {_rate(card['coverage'])}")
    print(f"executed violations {_rate(card['executed_violation_rate'])}")
    print(f"unsafe attempts    {_rate(card['unsafe_attempt_rate'])}")
    for reason in card["failure_reasons"][:5]:
        print(f"  {reason['runs']:>3} runs  {reason['reason_code']} ({reason['layer']})")

    gates: list[tuple[str, bool, str]] = []
    success = card["verified_success"]["value"]
    gates.append(
        (
            "verified success",
            success is not None and success >= args.min_success,
            f"{_rate(card['verified_success'])}, need at least {args.min_success:.0%}",
        )
    )
    executed = card["executed_violation_rate"]["numerator"]
    gates.append(
        (
            "executed violations",
            executed <= args.max_executed_violations,
            f"{executed} runs, allowed {args.max_executed_violations}",
        )
    )
    if baseline is not None:
        comparison = api.post(
            "/api/v1/comparisons",
            {
                "baseline": {"experiment_id": experiment["id"], "agent_config_id": baseline},
                "candidate": {"experiment_id": experiment["id"], "agent_config_id": candidate},
            },
        )
        compared = comparison["result"]
        result["comparison_id"] = comparison["id"]
        print(
            f"against the baseline: {compared['losses']} regressed, {compared['wins']} improved, "
            f"{compared['ties']} unchanged (comparison {comparison['id']})"
        )
        for task in compared["tasks"]:
            if task["outcome"] == "loss":
                reasons = ", ".join(task["candidate"]["reason_codes"]) or "no reason recorded"
                print(
                    f"  regressed {task['task_key']}: {task['baseline']['passed']}/"
                    f"{task['baseline']['scorable']} -> {task['candidate']['passed']}/"
                    f"{task['candidate']['scorable']} ({reasons})"
                )
        for note in compared["notes"]:
            print(f"  note: {note}")
        if args.max_regressions is not None:
            gates.append(
                (
                    "regressions",
                    compared["losses"] <= args.max_regressions,
                    f"{compared['losses']} tasks, allowed {args.max_regressions}",
                )
            )
    failed = False
    for name, ok, detail in gates:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        result["gates"].append({"gate": name, "passed": ok, "detail": detail})
        failed = failed or not ok
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
    return GATE_FAILED if failed else PASSED


def import_dataset(args: argparse.Namespace, api: Api) -> int:
    folder = Path(args.path)
    files = sorted((folder / "tasks").glob("*.json")) or sorted(folder.glob("*.json"))
    if not files:
        raise CommandError(f"no task JSON files in {folder} or {folder / 'tasks'}")
    tasks = [json.loads(f.read_text()) for f in files]
    imported = api.post("/api/v1/datasets/import", {"name": args.name, "tasks": tasks})
    print(
        f"dataset {args.name} version {imported['version']}: {len(tasks)} tasks "
        f"({imported['dataset_version_id']})"
    )
    return PASSED


def create_config(args: argparse.Namespace, api: Api) -> int:
    settings: dict[str, Any] = (
        json.loads(Path(args.settings_file).read_text()) if args.settings_file else {}
    )
    if args.url:
        settings["url"] = args.url
    body: dict[str, Any] = {"name": args.name, "adapter_type": args.adapter, "settings": settings}
    if args.limits_file:
        body["limits"] = json.loads(Path(args.limits_file).read_text())
    config = api.post("/api/v1/agent-configs", body)
    print(config["id"])
    return PASSED


def _side(value: str) -> dict[str, str]:
    experiment, _, config = value.partition(":")
    return {"experiment_id": experiment, **({"agent_config_id": config} if config else {})}


def compare(args: argparse.Namespace, api: Api) -> int:
    made = api.post(
        "/api/v1/comparisons",
        {"baseline": _side(args.baseline), "candidate": _side(args.candidate)},
    )
    result = made["result"]
    print(
        f"comparison {made['id']}: {result['losses']} regressed, {result['wins']} improved, "
        f"{result['ties']} unchanged"
    )
    for task in result["tasks"]:
        if task["outcome"] == "loss":
            print(f"  regressed {task['task_key']}: {', '.join(task['candidate']['reason_codes'])}")
    for note in result["notes"]:
        print(f"  note: {note}")
    return PASSED


def export(args: argparse.Namespace, api: Api) -> int:
    response = api.raw(f"/api/v1/experiments/{args.experiment}/export", format=args.format)
    if args.output:
        Path(args.output).write_bytes(response.content)
    else:
        sys.stdout.write(response.text)
    return PASSED


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="evalctl", description=__doc__.splitlines()[0] if __doc__ else None
    )
    commands = top.add_subparsers(dest="command", required=True)

    r = commands.add_parser("run", help="run an experiment and gate on its results")
    r.add_argument("--config", required=True, help="the candidate agent config, by name or id")
    r.add_argument("--baseline", help="a config to compare against in the same experiment")
    r.add_argument("--dataset", required=True, help="dataset name")
    r.add_argument("--version", type=int, help="dataset version (default: the newest)")
    r.add_argument("--split", default="validation", choices=["dev", "validation", "held_out"])
    r.add_argument("--final", action="store_true", help="needed to run held-out tasks")
    r.add_argument("--mode", default="real", choices=["real", "demo"])
    r.add_argument("--repetitions", type=int, default=1)
    r.add_argument("--wait", action="store_true", help="wait for results and apply the gates")
    r.add_argument("--timeout", type=float, default=3600, help="seconds to wait")
    r.add_argument("--poll", type=float, default=5, help="seconds between progress checks")
    r.add_argument("--min-success", type=float, default=0.8, help="lowest verified success")
    r.add_argument("--max-executed-violations", type=int, default=0)
    r.add_argument("--max-regressions", type=int, help="most tasks allowed to regress")
    r.add_argument("--budget-money-micros", type=int)
    r.add_argument("--budget-tokens", type=int)
    r.add_argument("--budget-requests", type=int)
    r.add_argument("--idempotency-key", help="makes a retried CI step reuse its experiment")
    r.add_argument("--json", metavar="FILE", help="also write the result as JSON")
    r.set_defaults(handler=run)

    d = commands.add_parser("import", help="import a folder of task JSON files")
    d.add_argument("path")
    d.add_argument("--name", required=True)
    d.set_defaults(handler=import_dataset)

    c = commands.add_parser("config", help="register an agent config")
    c.add_argument("--name", required=True)
    c.add_argument("--adapter", default="http", choices=["http", "tool_loop", "scripted"])
    c.add_argument("--url", help="the agent's URL, for HTTP agents")
    c.add_argument("--settings-file", help="JSON file with the adapter settings")
    c.add_argument("--limits-file", help="JSON file with run limits")
    c.set_defaults(handler=create_config)

    m = commands.add_parser("compare", help="compare two configs: EXPERIMENT[:CONFIG] each")
    m.add_argument("baseline")
    m.add_argument("candidate")
    m.set_defaults(handler=compare)

    e = commands.add_parser("export", help="export an experiment's report")
    e.add_argument("experiment")
    e.add_argument("--format", default="json", choices=["json", "csv"])
    e.add_argument("--output", "-o")
    e.set_defaults(handler=export)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with _client() as client:
            code: int = args.handler(args, Api(client))
            return code
    except CommandError as exc:
        print(f"evalctl: {exc}", file=sys.stderr)
        return ERROR
    except httpx.HTTPError as exc:
        print(f"evalctl: could not reach the platform: {type(exc).__name__}", file=sys.stderr)
        return ERROR


def entry() -> NoReturn:
    sys.exit(main())


if __name__ == "__main__":
    entry()
