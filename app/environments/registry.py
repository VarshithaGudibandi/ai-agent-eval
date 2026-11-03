from collections.abc import Callable, Mapping

from app.environments.contract import Environment
from app.environments.it_support.environment import ItSupport
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import SupportRetail

EnvironmentFactory = Callable[[EnvStateStore], Environment]


class UnknownEnvironmentError(LookupError):
    pass


class EnvironmentRegistry:
    def __init__(self, factories: Mapping[str, EnvironmentFactory]) -> None:
        self._factories = dict(factories)

    def keys(self) -> list[str]:
        return sorted(self._factories)

    def create(self, key: str, store: EnvStateStore) -> Environment:
        try:
            factory = self._factories[key]
        except KeyError:
            raise UnknownEnvironmentError(key) from None
        return factory(store)


def default_registry() -> EnvironmentRegistry:
    return EnvironmentRegistry({"support-retail@1": SupportRetail, "it-support@1": ItSupport})
