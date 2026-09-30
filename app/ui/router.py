"""Serves the Operator Console page.

One route, `GET /ui`, returning a static HTML file. Deliberately unauthenticated:
the page itself carries no data, and every call it makes from the browser goes
through the same `Authorization: Bearer` checks as the CLI (section 6.4). Serving
the shell without a session is what lets it render its own sign-in form.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["ui"])

_INDEX = Path(__file__).resolve().parent / "index.html"


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def operator_console() -> HTMLResponse:
    return HTMLResponse(_INDEX.read_text(encoding="utf-8"))


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Land a browser on something useful rather than a 404."""
    return RedirectResponse(url="/ui")
