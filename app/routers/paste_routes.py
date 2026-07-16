from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from app.config import (
    Settings,
    get_settings,
)
from app.database import get_session
from app.i18n import translate
from app.models import (
    Paste,
    PasteRevision,
)
from app.rendering import (
    decide_view_mode,
    generate_edit_key,
    normalize_slug,
    toggle_task_item,
)
from app.validation import validate_content
from datetime import (
    datetime,
    timezone,
)
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import (
    Session,
    joinedload,
)

from app.services import (
    _apply_task_lists,
    _apply_wiki_links,
    _assert_can_edit_paste,
    _assert_csrf,
    _base_context,
    _build_content_view,
    _can_user_edit_paste,
    _changes_url,
    _claim_slug,
    _current_user,
    _edit_url,
    _edit_with_error,
    _expiry_status_text,
    _format_tags_input,
    _get_paste_or_404,
    _get_revision_or_404,
    _history_items,
    _home_with_error,
    _is_owned_by_user,
    _is_paste_expired,
    _maybe_attach_collaborator,
    _normalize_tags,
    _normalize_url_scheme,
    _paste_backlinks,
    _paste_tags,
    _profile_user_label,
    _record_revision,
    _resolve_effective_syntax,
    _resolve_expiry,
    _resolve_language,
    _resolve_pygments_theme,
    _resolve_short_link_title,
    _revision_compare_url,
    _revision_event_label,
    _revision_snapshot_url,
    _serialize_tags,
    _set_history_cookie,
    _sync_stickers,
    _sync_wiki_links,
    _validate_editor_options,
    templates,
)
from app.rendering import pygments_theme_css


router = APIRouter()


@router.post("/publish")
def publish(
    request: Request,
    title: str = Form(default=""),
    tags: str = Form(default=""),
    content: str = Form(...),
    custom_slug: str = Form(default=""),
    syntax: str = Form(default="auto"),
    mode: str = Form(default="auto"),
    expires: str = Form(default="never"),
    encrypted: str = Form(default=""),
    stickers: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    current_language = _resolve_language(request, current_user)
    is_encrypted = encrypted.strip().lower() in {"true", "on", "1"}
    syntax = syntax.strip().lower()
    mode = mode.strip().lower()
    if is_encrypted:
        # Zero-knowledge payload: an opaque base64 blob the server must not
        # parse, validate, or try to pretty-print.
        syntax = "text"
        mode = "code"
    title = title.strip()[:120] or None
    normalized_tags = _normalize_tags(tags)
    effective_syntax = "text" if is_encrypted else _resolve_effective_syntax(content, syntax, mode)

    option_error = _validate_editor_options(syntax, mode, current_language)
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
            expires=expires,
        )

    if not content.strip():
        return _home_with_error(
            request,
            settings,
            session,
            translate(current_language, "error.empty_paste"),
            title or "",
            tags,
            content,
            custom_slug,
            syntax,
            mode,
            requested_syntax=syntax,
            expires=expires,
        )

    if len(content) > settings.max_content_size:
        return _home_with_error(
            request,
            settings,
            session,
            translate(current_language, "error.content_limit", limit=settings.max_content_size),
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
            expires=expires,
        )

    stripped_content = content.strip()
    if is_encrypted:
        is_url, view_mode = False, "code"
        validation_issue = None
    else:
        if mode == "link":
            normalized_url = _normalize_url_scheme(stripped_content)
            if normalized_url:
                stripped_content = normalized_url
        is_url, view_mode = decide_view_mode(stripped_content, mode, effective_syntax)
        title = _resolve_short_link_title(title, stripped_content, is_url)
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
            expires=expires,
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
            expires=expires,
        )

    expire_at, expire_after_views = _resolve_expiry(expires)
    paste = Paste(
        slug=slug,
        title=title,
        tags_json=_serialize_tags(normalized_tags),
        content=stripped_content if is_url else content,
        syntax=effective_syntax,
        view_mode=view_mode,
        is_url=is_url,
        is_encrypted=is_encrypted,
        edit_key=generate_edit_key(),
        expire_at=expire_at,
        expire_after_views=expire_after_views,
        owner_id=current_user.id if current_user else None,
        creator_id=current_user.id if current_user else None,
        last_editor_id=current_user.id if current_user else None,
    )
    session.add(paste)
    session.flush()
    if view_mode == "markdown" and not is_encrypted:
        _sync_wiki_links(session, paste, content)
    if not is_encrypted:
        _sync_stickers(session, paste, stickers)
    _record_revision(session, paste, current_user, event="created")
    session.commit()

    response = RedirectResponse(
        url=request.url_for("success_page", slug=paste.slug).include_query_params(key=paste.edit_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_history_cookie(response, request, paste.slug, paste.edit_key)
    return response

@router.get("/success/{slug}", response_class=HTMLResponse, name="success_page")
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
    current_language = _resolve_language(request, current_user)
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
            expiry_text=_expiry_status_text(paste, current_language),
        ),
    )

@router.get("/raw/{slug}", response_class=PlainTextResponse, name="raw_paste")
def raw_paste(slug: str, session: Session = Depends(get_session)) -> PlainTextResponse:
    paste = _get_paste_or_404(session, slug)
    return PlainTextResponse(paste.content)

@router.get("/edit/{slug}", response_class=HTMLResponse, name="edit_paste_form")
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

@router.post("/edit/{slug}")
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
    stickers: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    _assert_csrf(request, csrf_token)
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    current_language = _resolve_language(request, current_user)
    _assert_can_edit_paste(paste, key, current_user)
    _maybe_attach_collaborator(session, paste, current_user, key)
    previous_slug = paste.slug
    # Encryption status is fixed at publish time; the edit form only carries
    # a re-encrypted payload for pastes that were created encrypted.
    is_encrypted = paste.is_encrypted

    syntax = syntax.strip().lower()
    mode = mode.strip().lower()
    if is_encrypted:
        syntax = "text"
        mode = "code"
    title = title.strip()[:120] or None
    normalized_tags = _normalize_tags(tags)
    effective_syntax = "text" if is_encrypted else _resolve_effective_syntax(content, syntax, mode)

    option_error = _validate_editor_options(syntax, mode, current_language)
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
            translate(current_language, "error.empty_paste"),
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
            translate(current_language, "error.content_limit", limit=settings.max_content_size),
            title or "",
            tags,
            content,
            custom_slug,
            effective_syntax,
            mode,
            requested_syntax=syntax,
        )

    stripped_content = content.strip()
    if is_encrypted:
        is_url, view_mode = False, "code"
        validation_issue = None
    else:
        if mode == "link":
            normalized_url = _normalize_url_scheme(stripped_content)
            if normalized_url:
                stripped_content = normalized_url
        is_url, view_mode = decide_view_mode(stripped_content, mode, effective_syntax)
        title = _resolve_short_link_title(title, stripped_content, is_url)
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
    paste.content = stripped_content if is_url else content
    paste.syntax = effective_syntax
    paste.view_mode = view_mode
    paste.is_url = is_url
    if current_user and (paste.owner_id is None or paste.owner_id == current_user.id):
        paste.owner_id = current_user.id
    paste.last_editor_id = current_user.id if current_user else None
    paste.updated_at = datetime.now(timezone.utc)
    session.add(paste)
    session.flush()
    _sync_wiki_links(session, paste, content if (view_mode == "markdown" and not is_encrypted) else "")
    if not is_encrypted:
        _sync_stickers(session, paste, stickers)
    _record_revision(session, paste, current_user, event="saved")
    session.commit()

    redirect_target = request.url_for("success_page", slug=paste.slug).include_query_params(key=key, updated=1)
    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_303_SEE_OTHER)
    _set_history_cookie(response, request, paste.slug, key, previous_slug=previous_slug)
    return response

@router.get("/revisions/{slug}", response_class=HTMLResponse, name="revisions_page")
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

@router.get("/revisions/{slug}/{revision_number}/compare", response_class=HTMLResponse, name="revision_compare")
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

@router.get("/revisions/{slug}/{revision_number}", response_class=HTMLResponse, name="revision_view")
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

@router.get("/raw/{slug}/pdf")
def export_pdf(slug: str, session: Session = Depends(get_session)):
    paste = _get_paste_or_404(session, slug)
    try:
        from fpdf import FPDF
    except ImportError:
        raise HTTPException(status_code=501, detail="PDF export not available")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=10)

    title = paste.title or paste.slug
    pdf.cell(text=f"{title}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Courier", size=8)
    meta = f"Created: {paste.created_at.strftime('%Y-%m-%d %H:%M') if paste.created_at else 'N/A'} | Views: {paste.view_count} | Syntax: {paste.syntax}"
    pdf.cell(text=meta, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_font("Courier", size=10)
    pdf_lines = ["Encrypted content"] if paste.is_encrypted else paste.content.split("\n")
    for line in pdf_lines:
        pdf.cell(text=line[:200], new_x="LMARGIN", new_y="NEXT")

    pdf_bytes = bytes(pdf.output())
    filename = f"{paste.slug}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@router.post("/toggle/{slug}")
def toggle_task(
    slug: str,
    request: Request,
    task_index: int = Form(...),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    _assert_csrf(request, csrf_token)
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)

    if _is_paste_expired(paste):
        raise HTTPException(status_code=404, detail="Paste not found")
    if paste.is_encrypted or paste.view_mode != "markdown":
        raise HTTPException(status_code=400, detail="This paste has no task list")
    if not _can_user_edit_paste(paste, current_user):
        raise HTTPException(status_code=403, detail="Not allowed to edit this paste")

    result = toggle_task_item(paste.content, task_index)
    if result is None:
        raise HTTPException(status_code=400, detail="Invalid task index")

    updated_content, checked = result
    paste.content = updated_content
    paste.last_editor_id = current_user.id if current_user else None
    paste.updated_at = datetime.now(timezone.utc)
    session.add(paste)
    session.commit()

    return {"checked": checked}

@router.get("/{slug}", response_class=HTMLResponse, name="view_paste")
def view_paste(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    paste = _get_paste_or_404(session, slug)
    current_user = _current_user(request, session, settings)
    current_language = _resolve_language(request, current_user)

    if _is_paste_expired(paste):
        expired_reason = "views" if paste.expire_after_views is not None else "time"
        expired_at = paste.expire_at
        session.delete(paste)
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="expired.html",
            status_code=status.HTTP_410_GONE,
            context=_base_context(
                request,
                settings,
                title=translate(current_language, "expired.title"),
                current_user=current_user,
                reason=expired_reason,
                expired_at=expired_at,
            ),
        )

    paste.view_count += 1
    paste.last_viewed_at = datetime.now(timezone.utc)
    session.commit()

    if paste.is_url:
        return RedirectResponse(url=paste.content, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if paste.is_encrypted:
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
                rendered_markdown=None,
                lines=None,
                resolved_view_syntax="text",
                line_count=1,
                content_length=len(paste.content),
                can_edit=_can_user_edit_paste(paste, current_user),
                changes_url=_changes_url(request, paste, current_user),
                expiry_text=_expiry_status_text(paste, current_language),
                backlinks=[],
                pygments_css="",
            ),
        )

    pygments_theme = _resolve_pygments_theme(current_user)
    view_data = _build_content_view(paste.content, paste.syntax, paste.view_mode, pygments_theme)
    rendered_markdown = view_data["rendered_markdown"]
    if rendered_markdown:
        rendered_markdown = _apply_wiki_links(rendered_markdown, session)
        rendered_markdown = _apply_task_lists(rendered_markdown, _can_user_edit_paste(paste, current_user))

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
            rendered_markdown=rendered_markdown,
            lines=view_data["lines"],
            resolved_view_syntax=view_data["resolved_view_syntax"],
            line_count=view_data["line_count"],
            content_length=view_data["content_length"],
            can_edit=_can_user_edit_paste(paste, current_user),
            changes_url=_changes_url(request, paste, current_user),
            expiry_text=_expiry_status_text(paste, current_language),
            backlinks=_paste_backlinks(session, paste),
            pygments_css=pygments_theme_css(pygments_theme),
        ),
    )
