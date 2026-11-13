import logging
import sys
from collections.abc import Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_eval.client import PlatformClient, PlatformError, settings
from agent_eval_mcp import answers

# Each tool reads the platform's API with the token in AGENT_EVAL_TOKEN, in that token's
# workspace and with its role. Nothing here runs code from your agent or writes anything but
# comparisons and reports.

server = MCPServer(
    "agent-eval",
    instructions=(
        "Answers about AI agents measured by the AI Agent Eval platform: their health, how "
        "their runs fail, whether the newest version broke anything, and why. Answers are "
        "about results already recorded; to measure a change, run `evalctl run <eval file>` "
        "in a terminal first."
    ),
)


async def _ask[Answer](question: Callable[[PlatformClient], Answer]) -> Answer:
    url, token = settings(None, None)
    if token is None:
        raise ToolError("Set AGENT_EVAL_TOKEN to an API token from the platform's settings.")

    def call() -> Answer:
        client = PlatformClient(url, token)
        try:
            return question(client)
        except PlatformError as error:
            raise answers.explain_error(error, url) from None
        finally:
            client.close()

    try:
        return await anyio.to_thread.run_sync(call)
    except answers.AnswerError as error:
        raise ToolError(str(error)) from None


@server.tool()
async def list_agents() -> answers.AgentsAnswer:
    """List the agents in this workspace with their newest and shipped versions."""
    return await _ask(answers.list_agents)


@server.tool()
async def agent_health(agent: str) -> answers.HealthAnswer:
    """How an agent is doing: success rate, rule violations, consistency, run time, cost per
    successful run and runs scored, for its newest experiment against that one's baseline, or
    with only live traffic, the last 7 days against the 7 before. Says which experiment it is
    about and how old it is."""
    return await _ask(lambda c: answers.agent_health(c, agent, answers.now_utc()))


@server.tool()
async def show_failing_runs(agent: str) -> answers.FailuresAnswer:
    """How an agent's newest experiment's runs failed, grouped by reason, commonest first, with
    links to example runs. With no experiments, the last 7 days of live runs."""
    return await _ask(lambda c: answers.show_failing_runs(c, agent, answers.now_utc()))


def main() -> None:
    # stdout carries the protocol, so anything else goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server.run("stdio")
