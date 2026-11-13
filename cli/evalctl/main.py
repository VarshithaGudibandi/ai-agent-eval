import argparse
import os
import runpy
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_eval.client import PlatformClient, PlatformError, retrying, settings
from agent_eval.evaluation import Eval, completed
from evalctl import report

# Exit codes CI can act on: 0 ship, 1 hold, 2 could not tell (setup or platform error).
SHIP, HOLD, ERROR = 0, 1, 2


def _client(args: argparse.Namespace) -> PlatformClient:
    url, token = settings(args.url, args.token)
    if token is None:
        raise SystemExit("set AGENT_EVAL_TOKEN or pass --token")
    return PlatformClient(url, token, timeout=60)


def _compare(client: PlatformClient, body: dict[str, Any], explain: bool = False) -> int:
    created = retrying(lambda: client.compare(body))
    result = created["result"]
    print(report.comparison(result, f"{client.url}/api/v1/comparisons/{created['id']}"))
    if explain and result["decision"] == "hold":
        _explain(client, created["id"], wait=120)
    return HOLD if result["decision"] == "hold" else SHIP


def _explain(client: PlatformClient, comparison_id: str, wait: float) -> dict[str, Any]:
    """Ask for a report, wait for the worker to write it, and print it."""
    queued = retrying(lambda: client.request_report(comparison_id))
    deadline = time.monotonic() + wait
    done = queued
    while done["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(1)
        done = retrying(lambda: client.report(queued["id"]))
    print(report.explanation(done))
    return done


def _check(client: PlatformClient, run: Eval, baseline: str | None, explain: bool) -> int:
    print(f"\nChecking experiment {run.experiment_id}")
    if run.saved:
        print("  some traces are not on the platform yet; run agent_eval.flush() and retry")
        return ERROR
    if run.runs_scored < run.runs_received:
        print("  scoring did not finish in time, so there is nothing to decide on")
        return ERROR
    experiment = retrying(lambda: client.experiment(run.experiment_id))
    violations = experiment["health"]["rule_violation_rate"] or 0
    code = SHIP
    if violations > 0:
        print(f"  a hard rule failed in {violations * 100:.0f}% of runs")
        code = HOLD
    against = baseline or retrying(lambda: client.baseline(run.experiment_id))["experiment_id"]
    if against is None:
        print("  no earlier version ran on this dataset, so there is nothing to compare with")
        return code
    print(f"  compared with experiment {against}\n")
    body = {
        "baseline": {"experiment_id": against},
        "candidate": {"experiment_id": run.experiment_id},
    }
    return max(code, _compare(client, body, explain=explain))


def explain(args: argparse.Namespace) -> int:
    """Explain why a comparison's candidate got worse."""
    client = _client(args)
    try:
        done = _explain(client, args.comparison, wait=args.wait)
    finally:
        client.close()
    return SHIP if done["status"] == "done" else ERROR


def run(args: argparse.Namespace) -> int:
    """Run an eval file, then hold if a hard rule failed or the gates trip."""
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")
    if args.max_judge_calls is not None:
        # Eval reads the cap from here, so it applies to every Eval in the file.
        os.environ["AGENT_EVAL_MAX_JUDGE_CALLS"] = str(args.max_judge_calls)
    before = len(completed)
    sys.path.insert(0, str(path.resolve().parent))
    runpy.run_path(str(path), run_name="__main__")
    runs = completed[before:]
    if not runs:
        print(f"{path} ran no Eval")
        return ERROR
    client = _client(args)
    try:
        return max(_check(client, r, args.baseline, args.explain) for r in runs)
    finally:
        client.close()


def compare(args: argparse.Namespace) -> int:
    """Compare two experiments, or the agent's last N days of live runs with the N before."""
    client = _client(args)
    try:
        if args.live:
            agents = retrying(client.agents)
            ids = [a["id"] for a in agents if a["name"] == args.live]
            if not ids:
                raise SystemExit(f"no agent named {args.live!r}")
            now = datetime.now(UTC)
            days = timedelta(days=args.days)

            def window(end: datetime) -> dict[str, Any]:
                return {
                    "agent_id": ids[0],
                    "start": (end - days).isoformat(),
                    "end": end.isoformat(),
                }

            return _compare(
                client,
                {"baseline": window(now - days), "candidate": window(now)},
                explain=args.explain,
            )
        if len(args.experiments) != 2:
            raise SystemExit("give two experiment ids, baseline first, or --live AGENT")
        baseline, candidate = args.experiments
        return _compare(
            client,
            {"baseline": {"experiment_id": baseline}, "candidate": {"experiment_id": candidate}},
            explain=args.explain,
        )
    finally:
        client.close()


def _cap(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("must be a whole number, 0 or more")
    return int(value)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="evalctl",
        description="Run evals and compare agent versions. Exits 0 to ship, 1 to hold, 2 on error.",
    )
    top.add_argument("--url", help="platform URL; defaults to AGENT_EVAL_URL")
    top.add_argument("--token", help="API token; defaults to AGENT_EVAL_TOKEN")
    commands = top.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="run an Eval file and check it against its baseline")
    run_cmd.add_argument("file")
    run_cmd.add_argument("--baseline", help="experiment id to compare with, instead of the default")
    run_cmd.add_argument(
        "--max-judge-calls",
        type=_cap,
        metavar="N",
        help="cap the model calls judges may make in each experiment the file runs",
    )
    run_cmd.add_argument(
        "--explain", action="store_true", help="on a hold, ask why and print the report"
    )
    run_cmd.set_defaults(handler=run)
    cmp_cmd = commands.add_parser("compare", help="compare two experiments or two live windows")
    cmp_cmd.add_argument("experiments", nargs="*", help="baseline then candidate experiment id")
    cmp_cmd.add_argument("--live", metavar="AGENT", help="compare an agent's live windows")
    cmp_cmd.add_argument("--days", type=int, default=7, help="live window length in days")
    cmp_cmd.add_argument(
        "--explain", action="store_true", help="on a hold, ask why and print the report"
    )
    cmp_cmd.set_defaults(handler=compare)
    why = commands.add_parser("explain", help="explain why a comparison's candidate got worse")
    why.add_argument("comparison", help="comparison id")
    why.add_argument("--wait", type=float, default=120, help="seconds to wait for the report")
    why.set_defaults(handler=explain)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code: int = args.handler(args)
    except PlatformError as exc:
        print(f"evalctl: {exc}", file=sys.stderr)
        code = ERROR
    if argv is None:
        sys.exit(code)
    return code
