import atexit
import logging
import uuid
from typing import Any

from agent_eval.client import PlatformClient, settings
from agent_eval.recording import RunContext, set_live_context
from agent_eval.redact import RedactHook, redact_value, run_hook
from agent_eval.sender import BackgroundSender

logger = logging.getLogger("agent_eval")

_live: list[tuple[BackgroundSender, RedactHook | None]] = []


def init(
    agent: str,
    config: dict[str, Any],
    environment: str = "production",
    label: str | None = None,
    redact: RedactHook | None = None,
    url: str | None = None,
    token: str | None = None,
) -> None:
    """Start sending live traces: every top-level @traced call becomes one trace.

    The URL and token default to AGENT_EVAL_URL and AGENT_EVAL_TOKEN. This never raises:
    without a token, or with a platform that cannot be reached, the app runs unrecorded.
    """
    try:
        _start(agent, config, environment, label, redact, url, token)
    except Exception as exc:
        logger.warning("agent_eval: live tracing is off (%s)", type(exc).__name__)


def _start(
    agent: str,
    config: dict[str, Any],
    environment: str,
    label: str | None,
    redact: RedactHook | None,
    url: str | None,
    token: str | None,
) -> None:
    shutdown()
    base_url, api_token = settings(url, token)
    if api_token is None:
        logger.warning("agent_eval: AGENT_EVAL_TOKEN is not set, so live tracing is off")
        return
    sender = BackgroundSender(lambda: PlatformClient(base_url, api_token, timeout=5.0))
    if not _live:
        atexit.register(shutdown)
    _live.append((sender, redact))
    set_live_context(
        RunContext(
            agent=agent,
            config=config,
            label=label,
            tags={"environment": environment},
            deliver=sender.submit_trace,
            redact=redact,
        )
    )


def shutdown(timeout: float = 2.0) -> None:
    """Stop live tracing, sending what is already queued for up to timeout seconds."""
    set_live_context(None)
    while _live:
        sender, _ = _live.pop()
        try:
            sender.close(timeout)
        except Exception as exc:
            logger.warning("agent_eval: could not stop the sender (%s)", type(exc).__name__)


def log_feedback(trace_id: str, score: float, comment: str | None = None) -> None:
    """Send a user's rating of a trace: 1 for thumbs up, 0 for thumbs down.

    Use current_trace_id() inside the traced handler to know which trace to rate. Needs
    init(); never raises.
    """
    try:
        if not _live:
            logger.warning("agent_eval: log_feedback needs init() first")
            return
        if not isinstance(score, int | float) or not 0 <= score <= 1:
            logger.warning("agent_eval: feedback score must be from 0 to 1, got %r", score)
            return
        sender, hook = _live[-1]
        feedback: dict[str, Any] = {
            "feedback_id": uuid.uuid4().hex,
            "trace_id": str(trace_id),
            "score": float(score),
        }
        if comment is not None:
            feedback["comment"] = str(redact_value(str(run_hook(comment, hook))))[:5000]
        sender.submit_feedback(feedback)
    except Exception as exc:
        logger.warning("agent_eval: could not send feedback (%s)", type(exc).__name__)
