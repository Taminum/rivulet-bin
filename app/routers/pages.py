from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    status,
)
from app.config import (
    Settings,
    get_settings,
)
from app.database import get_session
from app.models import Paste
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import (
    VALID_MODE_VALUES,
    VALID_SYNTAX_VALUES,
    _base_context,
    _current_user,
    _history_items,
    templates,
)


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    mode: str | None = Query(default=None),
    syntax: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    requested_mode = mode if mode in VALID_MODE_VALUES else "auto"
    requested_syntax = syntax if syntax in VALID_SYNTAX_VALUES else "auto"
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=_base_context(
            request,
            settings,
            title="New paste",
            current_user=current_user,
            syntax_management="auto",
            form_values={
                "title": "",
                "tags": "",
                "content": "",
                "custom_slug": "",
                "syntax": requested_syntax,
                "mode": requested_mode,
            },
            error=None,
            history_items=_history_items(request, session),
        ),
    )

@router.get("/about", response_class=HTMLResponse)
def about(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context=_base_context(request, settings, title="About", current_user=current_user),
    )

@router.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}

@router.get("/search", response_class=HTMLResponse, name="search_page")
def search_page(
    request: Request,
    q: str = Query(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    results = []
    if q.strip():
        pattern = f"%{q.strip()}%"
        pastes = session.scalars(
            select(Paste).where(
                (Paste.title.ilike(pattern)) | (Paste.content.ilike(pattern))
            ).order_by(Paste.created_at.desc()).limit(50)
        ).all()
        origin = str(request.base_url).rstrip("/")
        for paste in pastes:
            public_url = origin + request.url_for("view_paste", slug=paste.slug).path
            preview = paste.content[:200].replace("\n", " ")
            results.append({
                "paste": paste,
                "public_url": public_url,
                "preview": preview,
            })

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context=_base_context(
            request,
            settings,
            title="Search",
            current_user=current_user,
            query=q,
            results=results,
        ),
    )
