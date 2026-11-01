from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from app.providers.contract import ModelProvider
from app.providers.gemini import GeminiProvider
from app.providers.ollama import OllamaProvider
from app.settings import Settings

Build = Callable[[str | None, Settings, httpx.AsyncClient], ModelProvider]


class UnknownProviderError(LookupError):
    pass


@dataclass(frozen=True)
class ProviderEntry:
    needs_key: bool
    build: Build


class ProviderRegistry:
    def __init__(self, entries: Mapping[str, ProviderEntry]) -> None:
        self._entries = dict(entries)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def get(self, name: str) -> ProviderEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise UnknownProviderError(name) from None


def _gemini(key: str | None, settings: Settings, http: httpx.AsyncClient) -> ModelProvider:
    if key is None:
        raise ValueError("Gemini needs an API key")
    return GeminiProvider(key, http, settings.gemini_base_url)


def _ollama(key: str | None, settings: Settings, http: httpx.AsyncClient) -> ModelProvider:
    return OllamaProvider(http, settings.ollama_base_url)


def default_providers() -> ProviderRegistry:
    return ProviderRegistry(
        {
            "gemini": ProviderEntry(needs_key=True, build=_gemini),
            "ollama": ProviderEntry(needs_key=False, build=_ollama),
        }
    )
