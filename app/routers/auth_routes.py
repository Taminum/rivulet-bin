from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Query,
    Request,
    status,
)
from app.auth import (
    hash_password,
    normalize_next_path,
    verify_password,
)
from app.config import (
    Settings,
    get_settings,
)
from app.database import get_session
from app.i18n import translate
from app.models import User
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import (
    _assert_csrf,
    _clear_auth_cookie,
    _current_user,
    _normalize_username,
    _render_auth_page,
    _resolve_language,
    _set_auth_cookie,
    _set_preference_cookies,
    _validate_registration_form,
)


router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
def register_form(
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    language = _resolve_language(request, current_user)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    return _render_auth_page(
        request,
        settings,
        title=translate(language, "auth.register_title"),
        auth_mode="register",
        form_values={"username": ""},
        next_path=target_path,
    )

@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    next_path: str = Form(default="/account"),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    language = _resolve_language(request, current_user)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    normalized_username = _normalize_username(username)
    error = _validate_registration_form(normalized_username, password, confirm_password, language)
    if error:
        return _render_auth_page(
            request,
            settings,
            title=translate(language, "auth.register_title"),
            auth_mode="register",
            form_values={"username": normalized_username},
            error=error,
            next_path=target_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    existing_user = session.scalar(select(User).where(User.username == normalized_username))
    if existing_user is not None:
        return _render_auth_page(
            request,
            settings,
            title=translate(language, "auth.register_title"),
            auth_mode="register",
            form_values={"username": normalized_username},
            error=translate(language, "auth.error_taken"),
            next_path=target_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = User(
        username=normalized_username,
        password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()

    response = RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, user.id, settings)
    _set_preference_cookies(response, user)
    return response

@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    language = _resolve_language(request, current_user)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    return _render_auth_page(
        request,
        settings,
        title=translate(language, "auth.login_title"),
        auth_mode="login",
        form_values={"username": ""},
        next_path=target_path,
    )

@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/account"),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    language = _resolve_language(request, current_user)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    normalized_username = _normalize_username(username)
    user = session.scalar(select(User).where(User.username == normalized_username))
    if user is None or not verify_password(password, user.password_hash):
        return _render_auth_page(
            request,
            settings,
            title=translate(language, "auth.login_title"),
            auth_mode="login",
            form_values={"username": normalized_username},
            error=translate(language, "auth.error_wrong_credentials"),
            next_path=target_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, user.id, settings)
    _set_preference_cookies(response, user)
    return response

@router.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _clear_auth_cookie(response)
    return response
