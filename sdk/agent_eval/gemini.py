import logging
from typing import Any

from agent_eval.recording import OpenSpan, active_context, end_span, jsonable, start_span

logger = logging.getLogger("agent_eval")


def wrap_gemini[C](client: C) -> C:
    """Record every generate_content call of a google-genai client as a model span.

    Works for client.models and client.aio.models. Everything else passes straight through
    to the client. The wrapper does not import google-genai; it wraps whatever it is given.
    """
    return _Client(client)  # type: ignore[return-value]


class _Proxy:
    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _Client(_Proxy):
    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self._warned: set[str] = set()

    @property
    def models(self) -> "_Models":
        return _Models(self._target.models, self._warned)

    @property
    def aio(self) -> "_Aio":
        return _Aio(self._target.aio, self._warned)


class _Aio(_Proxy):
    def __init__(self, target: Any, warned: set[str]) -> None:
        super().__init__(target)
        self._warned = warned

    @property
    def models(self) -> "_AsyncModels":
        return _AsyncModels(self._target.models, self._warned)


class _Models(_Proxy):
    def __init__(self, target: Any, warned: set[str]) -> None:
        super().__init__(target)
        self._warned = warned

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        span = _start(kwargs, self._warned)
        try:
            response = self._target.generate_content(*args, **kwargs)
        except BaseException as exc:
            end_span(span, error=exc)
            raise
        _end(span, response)
        return response


class _AsyncModels(_Models):
    async def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        span = _start(kwargs, self._warned)
        try:
            response = await self._target.generate_content(*args, **kwargs)
        except BaseException as exc:
            end_span(span, error=exc)
            raise
        _end(span, response)
        return response


def _get(value: Any, name: str) -> Any:
    """A field of a google-genai object or of the plain dict form it also accepts."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _text_of(content: Any) -> str | None:
    if content is None or isinstance(content, str):
        return content
    parts = _get(content, "parts")
    if parts is None:
        return None
    return "".join(_get(p, "text") or "" for p in parts)


def _tool_names(tools: Any) -> list[str]:
    names = []
    for tool in tools or []:
        for declaration in _get(tool, "function_declarations") or []:
            name = _get(declaration, "name")
            if name:
                names.append(str(name))
    return names


def _config_tool_names(tools: Any) -> list[str]:
    names = []
    for tool in tools if isinstance(tools, list) else []:
        name = tool if isinstance(tool, str) else _get(tool, "name")
        if name:
            names.append(str(name))
    return names


def _check_against_config(
    model: str, system: str | None, tools: list[str], warned: set[str]
) -> None:
    """Warn once per difference between what the agent sends and the config it declared."""
    context = active_context()
    if context is None:
        return
    config = context.config
    differences = []
    if "model" in config and config["model"] != model:
        differences.append(
            ("model", f"the call uses {model!r}, the config says {config['model']!r}")
        )
    if "prompt" in config and system is not None and config["prompt"] != system:
        differences.append(("prompt", "the system prompt differs from the config's prompt"))
    if "tools" in config and sorted(_config_tool_names(config["tools"])) != sorted(tools):
        differences.append(("tools", f"the call offers tools {sorted(tools)}"))
    for key, message in differences:
        if key not in warned:
            warned.add(key)
            logger.warning(
                "agent_eval: %s. The agent version is set by config, so it may be wrong", message
            )


def _start(kwargs: dict[str, Any], warned: set[str]) -> OpenSpan | None:
    try:
        model = str(kwargs.get("model") or "unknown").removeprefix("models/")
        config = kwargs.get("config")
        system = _text_of(_get(config, "system_instruction")) if config is not None else None
        tools = _tool_names(_get(config, "tools")) if config is not None else []
        _check_against_config(model, system, tools, warned)
        call = {"contents": kwargs.get("contents"), "config": config}
    except Exception as exc:
        logger.warning("agent_eval: could not read a model call (%s)", type(exc).__name__)
        model, call = "unknown", {}
    return start_span("generate_content", "model", call, provider="gemini", model=model)


def _end(span: OpenSpan | None, response: Any) -> None:
    if span is None:
        return
    try:
        output, usage = _reply(response), _usage(_get(response, "usage_metadata"))
    except Exception as exc:
        logger.warning("agent_eval: could not read a model reply (%s)", type(exc).__name__)
        output, usage = None, {}
    end_span(span, output=output, **usage)


def _reply(response: Any) -> dict[str, Any]:
    text, calls = [], []
    candidates = _get(response, "candidates") or [None]
    for part in _get(_get(candidates[0], "content"), "parts") or []:
        if _get(part, "text"):
            text.append(_get(part, "text"))
        call = _get(part, "function_call")
        if call is not None:
            calls.append({"name": _get(call, "name"), "arguments": jsonable(_get(call, "args"))})
    reply: dict[str, Any] = {"text": "".join(text) or None}
    if calls:
        reply["tool_calls"] = calls
    return reply


def _usage(metadata: Any) -> dict[str, Any]:
    """Token counts named as the platform names them. Gemini's prompt count includes cached
    tokens, which are priced separately, so they are taken out of the input count."""
    prompt = _get(metadata, "prompt_token_count") if metadata is not None else None
    if not isinstance(prompt, int):
        return {}
    cached = _get(metadata, "cached_content_token_count") or 0
    other = (_get(metadata, "thoughts_token_count") or 0) + (
        _get(metadata, "tool_use_prompt_token_count") or 0
    )
    return {
        "input_tokens": max(prompt - cached, 0),
        "cached_input_tokens": cached,
        "output_tokens": _get(metadata, "candidates_token_count") or 0,
        "other_tokens": other,
    }
