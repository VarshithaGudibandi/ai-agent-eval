from typing import Any

from agent_eval.client import PlatformClient, retrying, settings


def save_spec_with(client: PlatformClient, agent: str, scorers: list[dict[str, Any]]) -> int:
    ids = [found["id"] for found in retrying(client.agents) if found["name"] == agent]
    if not ids:
        raise ValueError(f"no agent named {agent!r} yet: send it a trace or run an Eval first")
    saved = retrying(lambda: client.save_eval_spec(ids[0], scorers))
    return int(saved["version"])


def save_eval_spec(
    agent: str,
    scorers: list[dict[str, Any]],
    url: str | None = None,
    token: str | None = None,
) -> int:
    """Save the scorers that grade an agent's runs, and return the spec version.

    Each scorer is a dict such as {"key": "refund_after_lookup", "check": "tool_order",
    "params": {"tool": "issue_refund", "after": ["get_order"]}}. Saving the same scorers
    again keeps the current version. Runs that arrive afterwards are graded by it.
    """
    base_url, api_token = settings(url, token)
    if api_token is None:
        raise ValueError("set AGENT_EVAL_TOKEN or pass token= to save an eval spec")
    client = PlatformClient(base_url, api_token)
    try:
        return save_spec_with(client, agent, scorers)
    finally:
        client.close()
