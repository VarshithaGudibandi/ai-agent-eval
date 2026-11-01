import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.db.schema_status import schema_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class Liveness(BaseModel):
    status: Literal["ok"]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unreachable"]
    migrations: Literal["at_head", "behind", "unknown"]


@router.get("/live")
async def live() -> Liveness:
    """Report that the process is up. Never touches dependencies."""
    return Liveness(status="ok")


@router.get("/ready", responses={503: {"model": Readiness}})
async def ready(request: Request, response: Response) -> Readiness:
    """Report whether the database is reachable and migrated to the latest revision."""
    try:
        result = await schema_status(request.app.state.engine)
    except Exception:
        logger.warning("readiness check could not reach the database", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Readiness(status="not_ready", database="unreachable", migrations="unknown")
    if not result.at_head:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Readiness(status="not_ready", database="ok", migrations="behind")
    return Readiness(status="ready", database="ok", migrations="at_head")
