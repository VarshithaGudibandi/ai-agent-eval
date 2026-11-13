from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web.rendering import render

router = APIRouter(include_in_schema=False)


@router.get("/login")
async def login(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/agents", status_code=303)
