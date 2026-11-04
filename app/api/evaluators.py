from fastapi import APIRouter

from app.api.dependencies import Viewer
from app.evaluators.coverage import Coverage, published_coverage

router = APIRouter(prefix="/api/v1/evaluators", tags=["evaluators"])


@router.get("")
async def evaluators(principal: Viewer) -> Coverage:
    """Each evaluator version with the broken traces it is proven to catch, and known gaps."""
    return published_coverage()
