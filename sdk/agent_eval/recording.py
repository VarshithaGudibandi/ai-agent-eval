import dataclasses
import functools
import inspect
import itertools
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, overload

from agent_eval.redact import RedactHook, redact_value, run_hook

logger = logging.getLogger("agent_eval")

Kind = Literal["model", "tool", "step"]
Tag = str | int | float | bool
# Set by Eval to place a run in its experiment. Live code may not claim them.
EXPERIMENT_TAGS = frozenset({"experiment", "case", "repetition"})

MAX_TEXT = 20_000
MAX_ITEMS = 1_000
MAX_DEPTH = 12


@dataclass
class RunContext:
    """Who the traces belong to, how they are tagged, and where a finished trace goes."""

    agent: str
    config: dict[str, Any]
    label: str | None
    tags: dict[str, Tag]
    deliver: Callable[[dict[str, Any]], None]
    redact: RedactHook | None = None


_eval_context: ContextVar[RunContext | None] = ContextVar("agent_eval_eval", default=None)
_live_context: list[RunContext] = []
_open_span: ContextVar["OpenSpan | None"] = ContextVar("agent_eval_span", default=None)


def set_live_context(context: RunContext | None) -> None:
    _live_context[:] = [] if context is None else [context]


def use_eval_context(context: RunContext | None) -> Token[RunContext | None]:
    return _eval_context.set(context)


def reset_eval_context(token: Token[RunContext | None]) -> None:
    _eval_context.reset(token)


def active_context() -> RunContext | None:
    return _eval_context.get() or (_live_context[0] if _live_context else None)


def jsonable(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy of any value, bounded in size, that never raises."""
    try:
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if value == value and abs(value) != float("inf") else str(value)
        if isinstance(value, str):
            if len(value) <= MAX_TEXT:
                return value
            return value[:MAX_TEXT] + f"...[{len(value) - MAX_TEXT} more characters]"
        if isinstance(value, bytes):
            return f"<{len(value)} bytes>"
        if depth >= MAX_DEPTH:
            return "<nested too deep>"
        if isinstance(value, Mapping):
            items = list(value.items())[:MAX_ITEMS]
            return {str(k): jsonable(v, depth + 1) for k, v in items}
        if isinstance(value, list | tuple | set | frozenset):
            return [jsonable(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return jsonable(dump(mode="json", exclude_none=True), depth + 1)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return jsonable(dataclasses.asdict(value), depth + 1)
        if isinstance(value, datetime):
            return value.isoformat()
        if callable(value):
            # A stable name, not a repr with a memory address that would change the
            # config fingerprint on every run.
            module = getattr(value, "__module__", None) or ""
            return f"{module}.{getattr(value, '__qualname__', type(value).__name__)}".lstrip(".")
        return jsonable(repr(value), depth + 1)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def config_document(config: dict[str, Any]) -> dict[str, Any]:
    """The config as sent, the same for an experiment and for each of its traces, so both
    get the same fingerprint."""
    document: dict[str, Any] = redact_value(jsonable(config))
    return document


class _Trace:
    def __init__(self, context: RunContext) -> None:
        self.id = uuid.uuid4().hex
        self.context = context
        self.tags: dict[str, Tag] = dict(context.tags)
        self.spans: list[dict[str, Any]] = []
        self.sent = False
        self._ids = itertools.count(1)
        # One wall-clock reading per trace and a monotonic clock after it, so a span can
        # never end before it starts even if the system clock is adjusted mid-run.
        self._wall = datetime.now(UTC)
        self._mono = time.monotonic()

    def now(self) -> datetime:
        return self._wall + timedelta(seconds=time.monotonic() - self._mono)

    def next_span_id(self) -> str:
        return f"s{next(self._ids)}"

    def document(self) -> dict[str, Any]:
        hook = self.context.redact
        spans = []
        for span in self.spans:
            span = dict(span)
            for key in ("input", "output", "error"):
                if span.get(key) is not None:
                    # Masking last, after the hook's result is plain JSON, so nothing the hook
                    # returns can carry a secret past it.
                    span[key] = redact_value(jsonable(run_hook(span[key], hook)))
            if "error" in span and not isinstance(span["error"], str):
                span["error"] = str(span["error"])[:10_000]
            spans.append(span)
        doc: dict[str, Any] = {
            "trace_id": self.id,
            "agent": self.context.agent,
            "config": config_document(self.context.config),
            "tags": redact_value(self.tags),
            "spans": spans,
        }
        if self.context.label is not None:
            doc["label"] = self.context.label
        return doc


class OpenSpan:
    """A span that has started and not yet ended."""

    def __init__(
        self,
        trace: _Trace,
        parent_id: str | None,
        kind: Kind,
        name: str,
        input: Any,
        fields: dict[str, Any],
    ) -> None:
        self.trace = trace
        self.fields = fields
        self.id = trace.next_span_id()
        self.parent_id = parent_id
        self.kind = kind
        self.name = name[:200] or "span"
        self.input = jsonable(input)
        self.start = trace.now()
        self._token: Token[OpenSpan | None] = _open_span.set(self)

    def end(self, output: Any = None, error: BaseException | None = None, **fields: Any) -> None:
        """Close the span. Closing the root sends the whole trace."""
        try:
            _open_span.reset(self._token)
        except ValueError:
            # Ended in another context than it started in, such as a callback.
            pass
        span: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.trace.now().isoformat(),
            "input": self.input,
            "output": jsonable(output),
        }
        if self.parent_id is not None:
            span["parent_id"] = self.parent_id
        if error is not None:
            span["error"] = f"{type(error).__name__}: {error}"[:10_000]
        span.update({k: v for k, v in {**self.fields, **fields}.items() if v is not None})
        if self.trace.sent:
            # A background task outlived its trace; the trace has already gone.
            return
        self.trace.spans.append(span)
        if self.parent_id is None:
            self.trace.sent = True
            document = self.trace.document()
            self.trace.context.deliver(document)


def start_span(name: str, kind: Kind | None, input: Any, **fields: Any) -> OpenSpan | None:
    """Open a span under the current one, or start a trace. None when nothing is recording.

    Never raises: a failure here is logged and the call goes on unrecorded.
    """
    try:
        parent = _open_span.get()
        if parent is not None and not parent.trace.sent:
            return OpenSpan(parent.trace, parent.id, kind or "tool", name, input, fields)
        context = active_context()
        if context is None:
            return None
        return OpenSpan(_Trace(context), None, kind or "step", name, input, fields)
    except Exception as exc:
        logger.warning("agent_eval: could not start a span (%s)", type(exc).__name__)
        return None


def end_span(
    span: OpenSpan | None, output: Any = None, error: BaseException | None = None, **fields: Any
) -> None:
    """Close a span. Never raises."""
    if span is None:
        return
    try:
        span.end(output, error, **fields)
    except Exception as exc:
        logger.warning("agent_eval: could not record a span (%s)", type(exc).__name__)


def current_trace_id() -> str | None:
    """The id of the trace being recorded here, to attach user feedback to it later."""
    span = _open_span.get()
    return None if span is None or span.trace.sent else span.trace.id


def tag(**values: Tag) -> None:
    """Add tags, such as the user's session, to the trace being recorded here."""
    span = _open_span.get()
    if span is None:
        return
    for name, value in values.items():
        if name in EXPERIMENT_TAGS:
            logger.warning("agent_eval: the %r tag is set by Eval and was ignored", name)
        elif isinstance(value, Tag):
            span.trace.tags[name] = value
        else:
            span.trace.tags[name] = str(jsonable(value))


def _arguments(signature: inspect.Signature | None, args: Any, kwargs: Any) -> Any:
    if signature is None:
        return {"args": args, "kwargs": kwargs}
    bound = signature.bind_partial(*args, **kwargs).arguments
    return {k: v for k, v in bound.items() if k not in ("self", "cls")}


def _start(
    signature: inspect.Signature | None, name: str, kind: Kind | None, args: Any, kwargs: Any
) -> OpenSpan | None:
    try:
        arguments = _arguments(signature, args, kwargs)
    except Exception:
        arguments = "<arguments could not be read>"
    return start_span(name, kind, arguments)


@overload
def traced[**P, R](fn: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def traced[**P, R](
    *, name: str | None = None, kind: Kind | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def traced[**P, R](
    fn: Callable[P, R] | None = None, /, *, name: str | None = None, kind: Kind | None = None
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Record each call of a function as a span, with its arguments, result and timing.

    A call with no trace around it starts one, as a step. A call inside a trace is a tool
    unless kind says otherwise. Exceptions from the function are recorded and re-raised
    unchanged; a failure inside the recording never reaches the caller.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = name or getattr(fn, "__qualname__", None) or "call"
        try:
            signature: inspect.Signature | None = inspect.signature(fn)
        except (TypeError, ValueError):
            signature = None

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                span = _start(signature, span_name, kind, args, kwargs)
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:
                    end_span(span, error=exc)
                    raise
                end_span(span, output=result)
                return result

            wrapper: Callable[P, Any] = async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                span = _start(signature, span_name, kind, args, kwargs)
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    end_span(span, error=exc)
                    raise
                end_span(span, output=result)
                return result

            wrapper = sync_wrapper
        wrapper.__agent_eval_traced__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorate if fn is None else decorate(fn)


def is_traced(fn: Callable[..., Any]) -> bool:
    return getattr(fn, "__agent_eval_traced__", False) is True
