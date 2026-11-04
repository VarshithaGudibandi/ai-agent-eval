from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.http_guard import GuardPolicy
from app.db.run_tokens import external_result, issue_run_token


class GatewayLink:
    """Connects an HTTP agent adapter to this attempt's run token and posted result."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        attempt_id: str,
        lease_token: str,
        gateway_url: str,
        signing_secret: str,
        policy: GuardPolicy,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._workspace_id = workspace_id
        self._lease_token = lease_token
        self.attempt_id = attempt_id
        self.gateway_url = gateway_url
        self.signing_secret = signing_secret
        self.policy = policy

    async def run_token(self, seconds: float) -> str:
        async with self._sessionmaker.begin() as session:
            return await issue_run_token(
                session, self._workspace_id, self.attempt_id, self._lease_token, seconds
            )

    async def posted_result(self) -> dict[str, Any] | None:
        async with self._sessionmaker() as session:
            return await external_result(session, self._workspace_id, self.attempt_id)
