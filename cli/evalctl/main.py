import argparse
import runpy
import sys
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


def _compare(client: PlatformClient, body: dict[str, Any]) -> int:
    created = retrying(lambda: client.compare(body))
    result = created["result"]
    print(report.comparison(result, f"{client.url}/api/v1/comparisons/{created['id']}"))
    return HOLD if result["decision"] == "hold" else SHIP


def _check(client: PlatformClient, run: Eval, baseline: str | None) -> int:
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
    return max(code, _compare(client, body))


def run(args: argparse.Namespace) -> int:
    """Run an eval file, then hold if a hard rule failed or the gates trip."""
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")
    before = len(completed)
    sys.path.insert(0, str(path.resolve().parent))
    runpy.run_path(str(path), run_name="__main__")
    runs = completed[before:]
    if not runs:
        print(f"{path} ran no Eval")
        return ERROR
    client = _client(args)
    try:
        return max(_check(client, r, args.baseline) for r in runs)
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

            return _compare(client, {"baseline": window(now - days), "candidate": window(now)})
        if len(args.experiments) != 2:
            raise SystemExit("give two experiment ids, baseline first, or --live AGENT")
        baseline, candidate = args.experiments
        return _compare(
            client,
            {"baseline": {"experiment_id": baseline}, "candidate": {"experiment_id": candidate}},
        )
    finally:
        client.close()


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
    run_cmd.set_defaults(handler=run)
    cmp_cmd = commands.add_parser("compare", help="compare two experiments or two live windows")
    cmp_cmd.add_argument("experiments", nargs="*", help="baseline then candidate experiment id")
    cmp_cmd.add_argument("--live", metavar="AGENT", help="compare an agent's live windows")
    cmp_cmd.add_argument("--days", type=int, default=7, help="live window length in days")
    cmp_cmd.set_defaults(handler=compare)
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
