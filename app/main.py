from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import (
    AUTH_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session_token,
    hash_password,
    normalize_next_path,
    read_session_token,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import get_session, init_database
from app.models import Paste, PasteCollaborator, PasteRevision, User
from app.rendering import (
    decide_view_mode,
    generate_edit_key,
    generate_slug,
    normalize_slug,
    render_code_lines,
    render_markdown,
    validate_slug,
)
from app.validation import ValidationIssue, detect_syntax, validate_content

app = FastAPI(title="Rivulet Bin")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

SYNTAX_OPTIONS = [
    ("auto", "Auto detect"),
    ("text", "Plain text"),
    ("markdown", "Markdown"),
    ("python", "Python"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("json", "JSON"),
    ("bash", "Bash"),
    ("html", "HTML"),
    ("css", "CSS"),
    ("sql", "SQL"),
    ("yaml", "YAML"),
]

MODE_OPTIONS = [
    ("auto", "Auto"),
    ("code", "Code / notes"),
    ("markdown", "Markdown"),
    ("link", "Short link"),
]

VALID_SYNTAX_VALUES = {value for value, _ in SYNTAX_OPTIONS}
VALID_MODE_VALUES = {value for value, _ in MODE_OPTIONS}

HISTORY_COOKIE_NAME = "rivulet_history"
HISTORY_LIMIT = 20
ASSET_VERSION = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
TAG_SPLIT_RE = re.compile(r"[\s,]+")
TAG_SANITIZE_RE = re.compile(r"[^\w-]+", re.UNICODE)
MAX_TAGS = 8
MAX_TAG_LENGTH = 32


@app.on_event("startup")
def on_startup() -> None:
    init_database()


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
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
                "syntax": "auto",
                "mode": "auto",
            },
            error=None,
            history_items=_history_items(request, session),
        ),
    )


@app.get("/about", response_class=HTMLResponse)
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


@app.get("/register", response_class=HTMLResponse)
def register_form(
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    return _render_auth_page(
        request,
        settings,
        title="Create account",
        auth_mode="register",
        form_values={"username": ""},
        next_path=target_path,
    )


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    next_path: str = Form(default="/account"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    current_user = _current_user(request, session, settings)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    normalized_username = _normalize_username(username)
    error = _validate_registration_form(normalized_username, password, confirm_password)
    if error:
        return _render_auth_page(
            request,
            settings,
            title="Create account",
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
            title="Create account",
            auth_mode="register",
            form_values={"username": normalized_username},
            error="This username is already taken.",
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
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next_path: str | None = Query(default=None, alias="next"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    return _render_auth_page(
        request,
        settings,
        title="Sign in",
        auth_mode="login",
        form_values={"username": ""},
        next_path=target_path,
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form(default="/account"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    current_user = _current_user(request, session, settings)
    target_path = normalize_next_path(next_path)
    if current_user:
        return RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)

    normalized_username = _normalize_username(username)
    user = session.scalar(select(User).where(User.username == normalized_username))
    if user is None or not verify_password(password, user.password_hash):
        return _render_auth_page(
            request,
            settings,
            title="Sign in",
            auth_mode="login",
            form_values={"username": normalized_username},
            error="Wrong username or password.",
            next_path=target_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(url=target_path, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, user.id, settings)
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _clear_auth_cookie(response)
    return response


@app.get("/account", response_class=HTMLResponse, name="account")
def account(
    request: Request,
    deleted: str | None = Query(default=None),
    updated_tags: str | None = Query(default=None),
    tab: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    active_tab = _normalize_account_tab(tab)
    active_tag = _normalize_tag(tag or "")

    owned_pastes = session.scalars(
        select(Paste)
        .options(joinedload(Paste.creator), joinedload(Paste.last_editor))
        .where(Paste.owner_id == current_user.id)
        .order_by(Paste.updated_at.desc(), Paste.created_at.desc())
    ).all()

    shared_memberships = session.scalars(
        select(PasteCollaborator)
        .options(
            joinedload(PasteCollaborator.paste).joinedload(Paste.creator),
            joinedload(PasteCollaborator.paste).joinedload(Paste.last_editor),
        )
        .where(PasteCollaborator.user_id == current_user.id)
        .order_by(PasteCollaborator.joined_at.desc())
    ).all()

    shared_pastes = [
        membership.paste
        for membership in shared_memberships
        if membership.paste is not None and membership.paste.owner_id != current_user.id
    ]
    owned_items = _profile_items(request, owned_pastes, current_user, shared=False)
    shared_items = _profile_items(request, shared_pastes, current_user, shared=True)
    filtered_owned_items = _filter_profile_items_by_tag(owned_items, active_tag)
    filtered_shared_items = _filter_profile_items_by_tag(shared_items, active_tag)
    profile_items = filtered_shared_items if active_tab == "shared" else filtered_owned_items

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context=_base_context(
            request,
            settings,
            title="Profile",
            current_user=current_user,
            account_tab=active_tab,
            active_tag=active_tag,
            profile_items=profile_items,
            profile_count=len(profile_items),
            owned_count=len(owned_items),
            shared_count=len(shared_items),
            saved_tab_url=_account_url(request, "saved", active_tag),
            shared_tab_url=_account_url(request, "shared", active_tag),
            clear_tag_url=_account_url(request, active_tab),
            account_message=_account_message(deleted, updated_tags),
        ),
    )


@app.post("/account/tags/{slug}")
def update_account_tags(
    slug: str,
    request: Request,
    tags: str = Form(default=""),
    tab: str = Form(default="saved"),
    filter_tag: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    paste = _get_paste_or_404(session, slug)
    if not _can_user_edit_paste(paste, current_user):
        raise HTTPException(status_code=403, detail="You can only edit tags on documents you can modify")

    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)

    paste.tags_json = _serialize_tags(normalized_tags)
    paste.last_editor_id = current_user.id
    paste.updated_at = datetime.now(timezone.utc)
    session.add(paste)
    session.commit()

    redirect_url = request.url_for("account").include_query_params(
        tab=_normalize_account_tab(tab),
        updated_tags=paste.slug,
    )
    if normalized_filter_tag and normalized_filter_tag in normalized_tags:
        redirect_url = redirect_url.include_query_params(tag=normalized_filter_tag)

    return RedirectResponse(url=str(redirect_url), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/publish")
def publish(
    request: Request,
    title: str = Form(default=""),
    tags: str = Form(default=""),
    content: str = Form(...),
    custom_slug: str = Form(default=""),
    syntax: str = Form(default="auto"),
    mode: str = Form(default="auto"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    current_user = _current_user(request, session, settings)
    syntax = syntax.strip().lower()
    mode = mode.strip().lower()
    title = title.strip()[:120] or None
    normalized_tags = _normalize_tags(tags)
    effective_syntax = _resolve_effective_syntax(content, syntax, mode)

    option_error = _validate_editor_options(syntax, mode)
    if option_error:
        return _home_with_error(
            request,
            settings,
            session,
            option_error,
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    if not content.strip():
        return _home_with_error(
            request,
            settings,
            session,
            "Paste content can't be empty.",
            title or "",
            tags,
            content,
            custom_slug,
            syntax,
            mode,
            requested_syntax=syntax,
        )

    if len(content) > settings.max_content_size:
        return _home_with_error(
            request,
            settings,
            session,
            f"Content is too large. Limit is {settings.max_content_size} characters.",
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    is_url, view_mode = decide_view_mode(content.strip(), mode, effective_syntax)
    validation_issue = None if is_url else validate_content(content, effective_syntax)
    if validation_issue:
        return _home_with_error(
            request,
            settings,
            session,
            validation_issue.to_message(effective_syntax),
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            validation_issue=validation_issue,
            requested_syntax=syntax,
        )

    slug = _claim_slug(session, settings, custom_slug)
    if slug is None:
        return _home_with_error(
            request,
            settings,
            session,
            "This custom URL is unavailable. Use 3-64 letters, numbers, underscores, or hyphens.",
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    paste = Paste(
        slug=slug,
        title=title,
        tags_json=_serialize_tags(normalized_tags),
        content=content.strip() if is_url else content,
        syntax=effective_syntax,
        view_mode=view_mode,
        is_url=is_url,
        edit_key=generate_edit_key(),
        owner_id=current_user.id if current_user else None,
        creator_id=current_user.id if current_user else None,
        last_editor_id=current_user.id if current_user else None,
    )
    session.add(paste)
    session.flush()
    _record_revision(session, paste, current_user, event="created")
    session.commit()

    response = RedirectResponse(
        url=request.url_for("success_page", slug=paste.slug).include_query_params(key=paste.edit_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_history_cookie(response, request, paste.slug, paste.edit_key)
    return response


@app.get("/success/{slug}", response_class=HTMLResponse, name="success_page")
def success_page(
    slug: str,
    request: Request,
    key: str | None = Query(default=None),
    updated: int = Query(default=0),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    origin = str(request.base_url).rstrip("/")
    public_url = origin + request.url_for("view_paste", slug=paste.slug).path
    raw_url = origin + request.url_for("raw_paste", slug=paste.slug).path
    edit_url = origin + request.url_for("edit_paste_form", slug=paste.slug).path
    if key:
        edit_url = f"{edit_url}?key={key}"
    changes_url = _changes_url(request, paste, current_user, key)

    return templates.TemplateResponse(
        request=request,
        name="success.html",
        context=_base_context(
            request,
            settings,
            title="Saved",
            current_user=current_user,
            paste=paste,
            tags=_paste_tags(paste),
            public_url=public_url,
            raw_url=raw_url,
            edit_url=edit_url,
            changes_url=changes_url,
            show_edit=key == paste.edit_key or _can_user_edit_paste(paste, current_user),
            updated=bool(updated),
            is_owned_by_current_user=_is_owned_by_user(paste, current_user),
        ),
    )


@app.get("/raw/{slug}", response_class=PlainTextResponse, name="raw_paste")
def raw_paste(slug: str, session: Session = Depends(get_session)) -> PlainTextResponse:
    paste = _get_paste_or_404(session, slug)
    return PlainTextResponse(paste.content)


@app.get("/edit/{slug}", response_class=HTMLResponse, name="edit_paste_form")
def edit_paste_form(
    slug: str,
    request: Request,
    key: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    _assert_can_edit_paste(paste, key, current_user)
    collaboration_joined = _maybe_attach_collaborator(session, paste, current_user, key)
    form_key = paste.edit_key if _is_owned_by_user(paste, current_user) else (key or paste.edit_key)
    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context=_base_context(
            request,
            settings,
            title=f"Edit {paste.slug}",
            current_user=current_user,
            paste=paste,
            key=form_key,
            changes_url=_changes_url(request, paste, current_user, key),
            collaboration_joined=collaboration_joined,
            syntax_management="manual",
            form_values={
                "title": paste.title or "",
                "tags": _format_tags_input(_paste_tags(paste)),
                "content": paste.content,
                "custom_slug": paste.slug,
                "syntax": paste.syntax,
                "mode": paste.view_mode,
            },
            error=None,
            history_items=_history_items(request, session, current_slug=paste.slug),
        ),
    )


@app.post("/edit/{slug}")
def edit_paste(
    slug: str,
    request: Request,
    key: str = Form(default=""),
    title: str = Form(default=""),
    tags: str = Form(default=""),
    content: str = Form(...),
    custom_slug: str = Form(default=""),
    syntax: str = Form(default="auto"),
    mode: str = Form(default="auto"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    _assert_can_edit_paste(paste, key, current_user)
    _maybe_attach_collaborator(session, paste, current_user, key)
    previous_slug = paste.slug

    syntax = syntax.strip().lower()
    mode = mode.strip().lower()
    title = title.strip()[:120] or None
    normalized_tags = _normalize_tags(tags)
    effective_syntax = _resolve_effective_syntax(content, syntax, mode)

    option_error = _validate_editor_options(syntax, mode)
    if option_error:
        return _edit_with_error(
            request,
            settings,
            session,
            paste,
            key,
            option_error,
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    if not content.strip():
        return _edit_with_error(
            request,
            settings,
            session,
            paste,
            key,
            "Paste content can't be empty.",
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    if len(content) > settings.max_content_size:
        return _edit_with_error(
            request,
            settings,
            session,
            paste,
            key,
            f"Content is too large. Limit is {settings.max_content_size} characters.",
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    is_url, view_mode = decide_view_mode(content.strip(), mode, effective_syntax)
    validation_issue = None if is_url else validate_content(content, effective_syntax)
    if validation_issue:
        return _edit_with_error(
            request,
            settings,
            session,
            paste,
            key,
            validation_issue.to_message(effective_syntax),
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            validation_issue=validation_issue,
            requested_syntax=syntax,
        )

    new_slug = normalize_slug(custom_slug)
    if new_slug != paste.slug:
        claimed = _claim_slug(session, settings, new_slug)
        if claimed is None:
            return _edit_with_error(
                request,
                settings,
                session,
                paste,
                key,
                "This custom URL is unavailable. Use 3-64 letters, numbers, underscores, or hyphens.",
                title or "",
                tags,
                content,
                custom_slug,
                effective_syntax,
                mode,
                requested_syntax=syntax,
            )
        paste.slug = claimed

    paste.title = title
    paste.tags_json = _serialize_tags(normalized_tags)
    paste.content = content.strip() if is_url else content
    paste.syntax = effective_syntax
    paste.view_mode = view_mode
    paste.is_url = is_url
    if current_user and (paste.owner_id is None or paste.owner_id == current_user.id):
        paste.owner_id = current_user.id
    paste.last_editor_id = current_user.id if current_user else None
    paste.updated_at = datetime.now(timezone.utc)
    session.add(paste)
    session.flush()
    _record_revision(session, paste, current_user, event="saved")
    session.commit()

    redirect_target = request.url_for("success_page", slug=paste.slug).include_query_params(key=key, updated=1)
    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_303_SEE_OTHER)
    _set_history_cookie(response, request, paste.slug, key, previous_slug=previous_slug)
    return response


@app.post("/account/delete/{slug}")
def delete_account_paste(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    paste = _get_paste_or_404(session, slug)
    if not _is_owned_by_user(paste, current_user):
        raise HTTPException(status_code=403, detail="You can only delete your own paste")

    deleted_slug = paste.slug
    session.delete(paste)
    session.commit()

    response = RedirectResponse(
        url=str(request.url_for("account").include_query_params(deleted=deleted_slug)),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _remove_history_slug(response, request, deleted_slug)
    return response


@app.get("/revisions/{slug}", response_class=HTMLResponse, name="revisions_page")
def revisions_page(
    slug: str,
    request: Request,
    key: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    _assert_can_edit_paste(paste, key, current_user)

    revisions = session.scalars(
        select(PasteRevision)
        .options(joinedload(PasteRevision.editor))
        .where(PasteRevision.paste_id == paste.id)
        .order_by(PasteRevision.revision_number.desc())
    ).all()
    revision_entries = [
        {
            "revision": revision,
            "snapshot_url": _revision_snapshot_url(request, paste, revision.revision_number, current_user, key),
            "compare_url": _revision_compare_url(request, paste, revision.revision_number, current_user, key),
        }
        for revision in revisions
    ]

    return templates.TemplateResponse(
        request=request,
        name="revisions.html",
        context=_base_context(
            request,
            settings,
            title=f"Changes {paste.slug}",
            current_user=current_user,
            paste=paste,
            revision_entries=revision_entries,
            revision_count=len(revisions),
            current_edit_url=_edit_url(request, paste, current_user, key),
            current_view_url=request.url_for("view_paste", slug=paste.slug).path,
            creator_label=_profile_user_label(paste.creator),
        ),
    )


@app.get("/revisions/{slug}/{revision_number}/compare", response_class=HTMLResponse, name="revision_compare")
def revision_compare(
    slug: str,
    revision_number: int,
    request: Request,
    key: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    _assert_can_edit_paste(paste, key, current_user)

    revision = _get_revision_or_404(session, paste.id, revision_number)
    current_panel = _build_content_view(paste.content, paste.syntax, paste.view_mode)
    revision_panel = _build_content_view(revision.content, revision.syntax, revision.view_mode)

    return templates.TemplateResponse(
        request=request,
        name="revision_compare.html",
        context=_base_context(
            request,
            settings,
            title=f"Compare {paste.slug} v{revision.revision_number}",
            current_user=current_user,
            paste=paste,
            revision=revision,
            current_panel=current_panel,
            revision_panel=revision_panel,
            current_view_url=request.url_for("view_paste", slug=paste.slug).path,
            current_edit_url=_edit_url(request, paste, current_user, key),
            revisions_url=_changes_url(request, paste, current_user, key),
            snapshot_url=_revision_snapshot_url(request, paste, revision.revision_number, current_user, key),
            revision_editor_label=_profile_user_label(revision.editor),
            revision_event_label=_revision_event_label(revision.event),
            current_last_editor_label=_profile_user_label(paste.last_editor),
        ),
    )


@app.get("/revisions/{slug}/{revision_number}", response_class=HTMLResponse, name="revision_view")
def revision_view(
    slug: str,
    revision_number: int,
    request: Request,
    key: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    _assert_can_edit_paste(paste, key, current_user)

    revision = _get_revision_or_404(session, paste.id, revision_number)
    revision_view_data = _build_content_view(revision.content, revision.syntax, revision.view_mode)

    return templates.TemplateResponse(
        request=request,
        name="revision_view.html",
        context=_base_context(
            request,
            settings,
            title=f"{paste.slug} v{revision.revision_number}",
            current_user=current_user,
            paste=paste,
            revision=revision,
            compare_url=_revision_compare_url(request, paste, revision.revision_number, current_user, key),
            rendered_markdown=revision_view_data["rendered_markdown"],
            lines=revision_view_data["lines"],
            resolved_view_syntax=revision_view_data["resolved_view_syntax"],
            line_count=revision_view_data["line_count"],
            content_length=revision_view_data["content_length"],
            current_view_url=request.url_for("view_paste", slug=paste.slug).path,
            current_edit_url=_edit_url(request, paste, current_user, key),
            revisions_url=_changes_url(request, paste, current_user, key),
            revision_editor_label=_profile_user_label(revision.editor),
            revision_event_label=_revision_event_label(revision.event),
        ),
    )


@app.get("/{slug}", response_class=HTMLResponse, name="view_paste")
def view_paste(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    if paste.is_url:
        return RedirectResponse(url=paste.content, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    view_data = _build_content_view(paste.content, paste.syntax, paste.view_mode)

    return templates.TemplateResponse(
        request=request,
        name="view.html",
        context=_base_context(
            request,
            settings,
            title=paste.title or paste.slug,
            current_user=current_user,
            paste=paste,
            tags=_paste_tags(paste),
            rendered_markdown=view_data["rendered_markdown"],
            lines=view_data["lines"],
            resolved_view_syntax=view_data["resolved_view_syntax"],
            line_count=view_data["line_count"],
            content_length=view_data["content_length"],
            can_edit=_can_user_edit_paste(paste, current_user),
            changes_url=_changes_url(request, paste, current_user),
        ),
    )


def _base_context(request: Request, settings: Settings, current_user: User | None = None, **extra):
    return {
        "request": request,
        "site_name": settings.site_name,
        "tagline": settings.tagline,
        "asset_version": ASSET_VERSION,
        "current_user": current_user,
        "syntax_options": SYNTAX_OPTIONS,
        "mode_options": MODE_OPTIONS,
        **extra,
    }


def _resolve_effective_syntax(content: str, syntax: str, mode: str) -> str:
    if syntax != "auto":
        return syntax
    if mode in {"markdown", "link"}:
        return syntax
    detected = detect_syntax(content)
    return detected if detected in VALID_SYNTAX_VALUES else syntax


def _resolve_view_syntax(content: str, syntax: str, mode: str) -> str:
    if mode != "code" or syntax in {"markdown", "text"}:
        return syntax

    detected = detect_syntax(content)
    if detected not in VALID_SYNTAX_VALUES:
        return syntax

    if syntax == "auto":
        return detected

    structured_fallbacks = {
        "css": {"json", "yaml"},
        "javascript": {"json", "yaml"},
        "typescript": {"json", "yaml"},
        "yaml": {"json"},
    }
    if detected in structured_fallbacks.get(syntax, set()):
        return detected

    return syntax


def _viewer_mode_label(view_mode: str) -> str:
    labels = {
        "code": "Code",
        "markdown": "Markdown",
        "link": "Short link",
    }
    return labels.get(view_mode, view_mode.capitalize())


def _viewer_syntax_label(syntax: str, resolved_view_syntax: str, view_mode: str) -> str:
    if view_mode == "markdown":
        return "MARKDOWN"
    if view_mode == "link":
        return "TEXT"

    label = resolved_view_syntax if resolved_view_syntax in VALID_SYNTAX_VALUES else syntax
    return (label or "text").upper()


def _build_content_view(content: str, syntax: str, view_mode: str) -> dict[str, object]:
    if view_mode == "markdown":
        rendered_markdown = render_markdown(content)
        resolved_view_syntax = "markdown"
        lines = None
    else:
        rendered_markdown = None
        resolved_view_syntax = "text" if view_mode == "link" else _resolve_view_syntax(content, syntax, view_mode)
        lines = render_code_lines(content, resolved_view_syntax)

    return {
        "rendered_markdown": rendered_markdown,
        "lines": lines,
        "resolved_view_syntax": resolved_view_syntax,
        "line_count": max(1, content.count("\n") + 1),
        "content_length": len(content),
        "view_mode_label": _viewer_mode_label(view_mode),
        "display_syntax": _viewer_syntax_label(syntax, resolved_view_syntax, view_mode),
    }


def _normalize_tag(value: str) -> str | None:
    cleaned = TAG_SANITIZE_RE.sub("", value.strip().lstrip("#")).strip("-_").lower()
    if not cleaned:
        return None
    return cleaned[:MAX_TAG_LENGTH]


def _normalize_tags(value: str) -> list[str]:
    if not value.strip():
        return []

    tags: list[str] = []
    seen: set[str] = set()
    for token in TAG_SPLIT_RE.split(value):
        normalized = _normalize_tag(token)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        tags.append(normalized)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _serialize_tags(tags: list[str]) -> str:
    return json.dumps(tags[:MAX_TAGS], ensure_ascii=False, separators=(",", ":"))


def _deserialize_tags(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return _normalize_tags(str(raw_value))

    if not isinstance(parsed, list):
        return _normalize_tags(str(raw_value))

    tags: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        normalized = _normalize_tag(str(item))
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        tags.append(normalized)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _paste_tags(paste: Paste) -> list[str]:
    return _deserialize_tags(getattr(paste, "tags_json", "[]"))


def _format_tags_input(tags: list[str]) -> str:
    return " ".join(f"#{tag}" for tag in tags)


def _filter_profile_items_by_tag(items: list[dict[str, object]], tag: str | None) -> list[dict[str, object]]:
    if not tag:
        return items
    return [item for item in items if tag in item.get("tags", [])]


def _validate_editor_options(syntax: str, mode: str) -> str | None:
    if syntax not in VALID_SYNTAX_VALUES:
        return "Unsupported syntax value."
    if mode not in VALID_MODE_VALUES:
        return "Unsupported mode value."
    return None


def _current_user(request: Request, session: Session, settings: Settings) -> User | None:
    if getattr(request.state, "current_user_loaded", False):
        return getattr(request.state, "current_user", None)

    token = request.cookies.get(AUTH_COOKIE_NAME)
    user_id = read_session_token(token, settings.secret_salt)
    user = session.get(User, user_id) if user_id else None
    request.state.current_user = user
    request.state.current_user_loaded = True
    return user


def _set_auth_cookie(response: RedirectResponse, user_id: int, settings: Settings) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(user_id, settings.secret_salt),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _clear_auth_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, httponly=True, samesite="lax")


def _normalize_username(value: str) -> str:
    return value.strip().lower()


def _validate_registration_form(username: str, password: str, confirm_password: str) -> str | None:
    if not USERNAME_RE.fullmatch(username):
        return "Username must be 3-32 characters long and use only lowercase letters, numbers, or underscores."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if password != confirm_password:
        return "Passwords do not match."
    return None


def _render_auth_page(
    request: Request,
    settings: Settings,
    title: str,
    auth_mode: str,
    form_values: dict[str, str],
    next_path: str,
    error: str | None = None,
    current_user: User | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context=_base_context(
            request,
            settings,
            title=title,
            current_user=current_user,
            auth_mode=auth_mode,
            form_values=form_values,
            next_path=next_path,
            error=error,
        ),
        status_code=status_code,
    )


def _profile_items(
    request: Request,
    pastes: list[Paste],
    current_user: User | None,
    *,
    shared: bool,
) -> list[dict[str, object]]:
    origin = str(request.base_url).rstrip("/")
    items: list[dict[str, object]] = []
    for paste in pastes:
        resolved_syntax = _profile_resolved_syntax(paste)
        share_edit_url = origin + request.url_for("edit_paste_form", slug=paste.slug).path
        share_edit_url = f"{share_edit_url}?key={paste.edit_key}"
        items.append(
            {
                "paste": paste,
                "tags": _paste_tags(paste),
                "public_url": origin + request.url_for("view_paste", slug=paste.slug).path,
                "raw_url": origin + request.url_for("raw_paste", slug=paste.slug).path,
                "edit_url": request.url_for("edit_paste_form", slug=paste.slug).path,
                "share_edit_url": share_edit_url,
                "changes_url": request.url_for("revisions_page", slug=paste.slug).path,
                "preview_kind": _profile_preview_kind(paste, resolved_syntax),
                "preview_text": _profile_preview_text(paste),
                "preview_label": _profile_preview_label(paste, resolved_syntax),
                "display_syntax": _profile_syntax_label(paste, resolved_syntax),
                "creator_label": _profile_user_label(paste.creator),
                "last_editor_label": _profile_user_label(paste.last_editor),
                "is_shared": shared,
                "can_manage_tags": _can_user_edit_paste(paste, current_user),
                "can_delete": _is_owned_by_user(paste, current_user),
            }
        )
    return items


def _profile_preview_kind(paste: Paste, resolved_syntax: str) -> str:
    if paste.is_url or paste.view_mode == "link":
        return "link"
    if paste.view_mode == "markdown":
        return "markdown"
    if resolved_syntax == "text":
        return "text"
    return "code"


def _profile_preview_text(paste: Paste) -> str:
    content = paste.content.replace("\r\n", "\n").strip()
    if not content:
        return "No content"

    if paste.is_url or paste.view_mode == "link":
        return content

    lines = content.split("\n")
    excerpt = "\n".join(lines[:8]).strip()
    if len(lines) > 8 or len(excerpt) > 340:
        excerpt = excerpt[:340].rstrip() + "..."
    return excerpt


def _profile_preview_label(paste: Paste, resolved_syntax: str) -> str:
    if paste.is_url or paste.view_mode == "link":
        return "Short link"
    return _profile_syntax_label(paste, resolved_syntax)


def _profile_syntax_label(paste: Paste, resolved_syntax: str) -> str:
    if paste.is_url or paste.view_mode == "link":
        return "LINK"
    if paste.view_mode == "markdown":
        return "MARKDOWN"
    return resolved_syntax.upper()


def _profile_resolved_syntax(paste: Paste) -> str:
    if paste.view_mode != "code":
        return paste.syntax

    resolved_syntax = _resolve_view_syntax(paste.content, paste.syntax, paste.view_mode)
    return resolved_syntax if resolved_syntax in VALID_SYNTAX_VALUES else "text"


def _profile_user_label(user: User | None) -> str:
    if user is None:
        return "Guest"
    return f"@{user.username}"


def _revision_event_label(event: str) -> str:
    return "Created" if event == "created" else "Saved"


def _normalize_account_tab(tab: str | None) -> str:
    return "shared" if tab == "shared" else "saved"


def _account_url(request: Request, tab: str, tag: str | None = None) -> str:
    url = request.url_for("account").include_query_params(tab=_normalize_account_tab(tab))
    if tag:
        url = url.include_query_params(tag=tag)
    return str(url)


def _account_message(deleted: str | None, updated_tags: str | None = None) -> str | None:
    updated_slug = normalize_slug(updated_tags or "")
    if updated_slug:
        return f"Tags updated for {updated_slug}."

    deleted_slug = normalize_slug(deleted or "")
    if not deleted_slug:
        return None
    return f"Paste {deleted_slug} deleted."


def _is_owned_by_user(paste: Paste, user: User | None) -> bool:
    return user is not None and paste.owner_id == user.id


def _is_collaborator_for_user(paste: Paste, user: User | None) -> bool:
    if user is None:
        return False
    return any(collaborator.user_id == user.id for collaborator in paste.collaborators)


def _can_user_edit_paste(paste: Paste, user: User | None) -> bool:
    return _is_owned_by_user(paste, user) or _is_collaborator_for_user(paste, user)


def _changes_url(request: Request, paste: Paste, current_user: User | None, key: str | None = None) -> str:
    url = request.url_for("revisions_page", slug=paste.slug).path
    if key and not _can_user_edit_paste(paste, current_user):
        return f"{url}?key={key}"
    return url


def _revision_snapshot_url(
    request: Request,
    paste: Paste,
    revision_number: int,
    current_user: User | None,
    key: str | None = None,
) -> str:
    url = request.url_for("revision_view", slug=paste.slug, revision_number=revision_number).path
    if key and not _can_user_edit_paste(paste, current_user):
        return f"{url}?key={key}"
    return url


def _revision_compare_url(
    request: Request,
    paste: Paste,
    revision_number: int,
    current_user: User | None,
    key: str | None = None,
) -> str:
    url = request.url_for("revision_compare", slug=paste.slug, revision_number=revision_number).path
    if key and not _can_user_edit_paste(paste, current_user):
        return f"{url}?key={key}"
    return url


def _edit_url(request: Request, paste: Paste, current_user: User | None, key: str | None = None) -> str:
    url = request.url_for("edit_paste_form", slug=paste.slug).path
    if key and not _can_user_edit_paste(paste, current_user):
        return f"{url}?key={key}"
    return url


def _history_items(
    request: Request,
    session: Session,
    current_slug: str | None = None,
) -> list[dict[str, object]]:
    entries = _read_history_cookie(request)
    slugs = [entry["slug"] for entry in entries]
    if not slugs:
        return []

    pastes = session.scalars(select(Paste).where(Paste.slug.in_(slugs))).all()
    paste_map = {paste.slug: paste for paste in pastes}
    origin = str(request.base_url).rstrip("/")
    items: list[dict[str, object]] = []

    for entry in entries:
        paste = paste_map.get(entry["slug"])
        if paste is None:
            continue

        edit_url = origin + request.url_for("edit_paste_form", slug=paste.slug).path
        edit_url = f"{edit_url}?key={entry['key']}"
        public_url = origin + request.url_for("view_paste", slug=paste.slug).path
        items.append(
            {
                "paste": paste,
                "edit_url": edit_url,
                "public_url": public_url,
                "is_active": paste.slug == current_slug,
            }
        )

    return items


def _read_history_cookie(request: Request) -> list[dict[str, str]]:
    raw_cookie = request.cookies.get(HISTORY_COOKIE_NAME)
    if not raw_cookie:
        return []

    try:
        padded_cookie = raw_cookie + "=" * (-len(raw_cookie) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded_cookie.encode("utf-8")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []

    entries: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        slug = normalize_slug(str(item.get("slug", "")))
        key = str(item.get("key", "")).strip()
        if not validate_slug(slug) or not key or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        entries.append({"slug": slug, "key": key})
        if len(entries) >= HISTORY_LIMIT:
            break

    return entries


def _write_history_cookie(response: RedirectResponse, entries: list[dict[str, str]]) -> None:
    if not entries:
        response.delete_cookie(HISTORY_COOKIE_NAME, httponly=True, samesite="lax")
        return

    serialized = json.dumps(entries[:HISTORY_LIMIT], separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(serialized).decode("utf-8").rstrip("=")
    response.set_cookie(
        HISTORY_COOKIE_NAME,
        encoded,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="lax",
    )


def _set_history_cookie(
    response: RedirectResponse,
    request: Request,
    slug: str,
    edit_key: str,
    previous_slug: str | None = None,
) -> None:
    updated_entries = [
        entry
        for entry in _read_history_cookie(request)
        if entry["slug"] not in {slug, previous_slug}
    ]
    updated_entries.insert(0, {"slug": slug, "key": edit_key})
    _write_history_cookie(response, updated_entries)


def _remove_history_slug(response: RedirectResponse, request: Request, slug: str) -> None:
    updated_entries = [entry for entry in _read_history_cookie(request) if entry["slug"] != slug]
    _write_history_cookie(response, updated_entries)


def _claim_slug(session: Session, settings: Settings, custom_slug: str) -> str | None:
    normalized = normalize_slug(custom_slug)
    if normalized:
        if normalized in settings.reserved_slugs or not validate_slug(normalized):
            return None
        existing = session.scalar(select(Paste).where(Paste.slug == normalized))
        return None if existing else normalized

    for _ in range(20):
        generated = generate_slug()
        if generated in settings.reserved_slugs:
            continue
        existing = session.scalar(select(Paste).where(Paste.slug == generated))
        if not existing:
            return generated
    return None


def _get_paste_or_404(session: Session, slug: str) -> Paste:
    paste = session.scalar(select(Paste).where(Paste.slug == slug))
    if paste is None:
        raise HTTPException(status_code=404, detail="Paste not found")
    return paste


def _get_revision_or_404(session: Session, paste_id: int, revision_number: int) -> PasteRevision:
    revision = session.scalar(
        select(PasteRevision)
        .options(joinedload(PasteRevision.editor))
        .where(
            PasteRevision.paste_id == paste_id,
            PasteRevision.revision_number == revision_number,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return revision


def _assert_can_edit_paste(paste: Paste, key: str | None, current_user: User | None) -> None:
    if key and key == paste.edit_key:
        return
    if _can_user_edit_paste(paste, current_user):
        return
    raise HTTPException(status_code=403, detail="Wrong edit key")


def _maybe_attach_collaborator(
    session: Session,
    paste: Paste,
    current_user: User | None,
    key: str | None,
) -> bool:
    if current_user is None or not key or key != paste.edit_key:
        return False
    if _is_owned_by_user(paste, current_user) or _is_collaborator_for_user(paste, current_user):
        return False

    session.add(PasteCollaborator(paste_id=paste.id, user_id=current_user.id))
    session.commit()
    session.refresh(paste)
    return True


def _record_revision(session: Session, paste: Paste, editor: User | None, *, event: str) -> None:
    latest_revision_number = session.scalar(
        select(PasteRevision.revision_number)
        .where(PasteRevision.paste_id == paste.id)
        .order_by(PasteRevision.revision_number.desc())
        .limit(1)
    )
    session.add(
        PasteRevision(
            paste_id=paste.id,
            revision_number=(latest_revision_number or 0) + 1,
            event=event,
            title=paste.title,
            content=paste.content,
            syntax=paste.syntax,
            view_mode=paste.view_mode,
            editor_id=editor.id if editor else None,
        )
    )


def _home_with_error(
    request: Request,
    settings: Settings,
    session: Session,
    error: str,
    title: str,
    tags: str,
    content: str,
    custom_slug: str,
    syntax: str,
    mode: str,
    validation_issue: ValidationIssue | None = None,
    requested_syntax: str | None = None,
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=_base_context(
            request,
            settings,
            title="New paste",
            current_user=current_user,
            syntax_management=_syntax_management(requested_syntax or syntax),
            form_values={
                "title": title,
                "tags": tags,
                "content": content,
                "custom_slug": custom_slug,
                "syntax": syntax,
                "mode": mode,
            },
            error=error,
            validation_error=_serialize_validation_issue(validation_issue),
            history_items=_history_items(request, session),
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _edit_with_error(
    request: Request,
    settings: Settings,
    session: Session,
    paste: Paste,
    key: str,
    error: str,
    title: str,
    tags: str,
    content: str,
    custom_slug: str,
    syntax: str,
    mode: str,
    validation_issue: ValidationIssue | None = None,
    requested_syntax: str | None = None,
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context=_base_context(
            request,
            settings,
            title=f"Edit {paste.slug}",
            current_user=current_user,
            paste=paste,
            key=key,
            changes_url=_changes_url(request, paste, current_user, key),
            collaboration_joined=False,
            syntax_management=_syntax_management(requested_syntax or syntax),
            form_values={
                "title": title,
                "tags": tags,
                "content": content,
                "custom_slug": custom_slug,
                "syntax": syntax,
                "mode": mode,
            },
            error=error,
            validation_error=_serialize_validation_issue(validation_issue),
            history_items=_history_items(request, session, current_slug=paste.slug),
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _serialize_validation_issue(issue: ValidationIssue | None) -> dict[str, int | None] | None:
    if issue is None:
        return None
    return {
        "line": issue.line,
        "column": issue.column,
    }


def _syntax_management(syntax: str) -> str:
    return "auto" if syntax == "auto" else "manual"
