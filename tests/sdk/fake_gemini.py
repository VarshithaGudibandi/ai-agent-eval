from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Stands in for google-genai's client: the same call shape and response objects, so the
# wrapper and the example agent can be tested with no key and no network.


@dataclass
class FunctionCall:
    name: str
    args: dict[str, Any]


@dataclass
class Part:
    text: str | None = None
    function_call: FunctionCall | None = None


@dataclass
class Content:
    parts: list[Part]
    role: str = "model"


@dataclass
class Candidate:
    content: Content


@dataclass
class UsageMetadata:
    prompt_token_count: int
    candidates_token_count: int
    cached_content_token_count: int | None = None
    thoughts_token_count: int | None = None


@dataclass
class Response:
    candidates: list[Candidate]
    usage_metadata: UsageMetadata | None = None


def reply(text: str | None = None, *calls: tuple[str, dict[str, Any]]) -> Response:
    parts = [Part(text=text)] if text else []
    parts += [Part(function_call=FunctionCall(name, args)) for name, args in calls]
    return Response([Candidate(Content(parts))], UsageMetadata(120, 15, 20, 5))


Script = Callable[[str, Any, Any], Response]


@dataclass
class FakeModels:
    script: Script
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Response:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.script(model, contents, config)


@dataclass
class FakeAsyncModels:
    models: FakeModels

    async def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Response:
        return self.models.generate_content(model=model, contents=contents, config=config)


@dataclass
class FakeAio:
    models: FakeAsyncModels


class FakeGemini:
    def __init__(self, script: Script) -> None:
        self.models = FakeModels(script)
        self.aio = FakeAio(FakeAsyncModels(self.models))
        self.api_version = "v1beta"
