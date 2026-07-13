from __future__ import annotations

import json

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from app.config import (
    Settings,
    get_settings,
)
from app.database import get_session
from app.i18n import (
    SUPPORTED_LANGUAGES,
    SUPPORTED_THEME_PREFERENCES,
    normalize_language,
    normalize_theme_preference,
    translate,
)
from app.models import (
    Bookmark,
    Note,
)
from datetime import (
    datetime,
    timezone,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from app.services import (
    BOOKMARK_URL_LIMIT,
    PROFILE_IMPORT_LIMIT,
    _assert_csrf,
    _bookmark_form_state,
    _build_account_redirect_url,
    _build_profile_export_payload,
    _can_user_delete_paste,
    _can_user_edit_paste,
    _current_user,
    _get_bookmark_or_404,
    _get_note_or_404,
    _get_paste_or_404,
    _import_profile_payload,
    _normalize_account_tab,
    _normalize_profile_transfer_section,
    _normalize_tag,
    _normalize_tags,
    _normalize_url_scheme,
    _note_form_state,
    _remove_history_slug,
    _render_account_page,
    _resolve_language,
    _serialize_tags,
    _set_preference_cookies,
)


router = APIRouter()


@router.get("/account", response_class=HTMLResponse, name="account")
def account(
    request: Request,
    deleted: str | None = Query(default=None),
    updated_tags: str | None = Query(default=None),
    notice: str | None = Query(default=None),
    imported: int | None = Query(default=None),
    tab: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    q: str | None = Query(default=None),
    created: str | None = Query(default=None),
    month: str | None = Query(default=None),
    bookmark_edit: int | None = Query(default=None),
    note_edit: int | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    return _render_account_page(
        request,
        session,
        settings,
        current_user,
        active_tab=_normalize_account_tab(tab),
        active_tag=_normalize_tag(tag or ""),
        active_search=q,
        active_created=created,
        active_month=month,
        deleted=deleted,
        updated_tags=updated_tags,
        notice=notice,
        imported_count=imported,
        bookmark_edit_id=bookmark_edit,
        note_edit_id=note_edit,
    )

@router.post("/account/settings/preferences")
def update_account_preferences(
    request: Request,
    preferred_language: str = Form(default="en"),
    theme_preference: str = Form(default="dark"),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=%2Faccount%3Ftab%3Dsettings", status_code=status.HTTP_303_SEE_OTHER)

    current_language = _resolve_language(request, current_user)
    raw_language = (preferred_language or "").strip().lower()
    raw_theme = (theme_preference or "").strip().lower()
    error: str | None = None

    if raw_language not in SUPPORTED_LANGUAGES:
        error = translate(current_language, "settings.error_language")
    elif raw_theme not in SUPPORTED_THEME_PREFERENCES:
        error = translate(current_language, "settings.error_theme")

    if error:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            settings_form={
                "preferred_language": normalize_language(raw_language),
                "theme_preference": normalize_theme_preference(raw_theme),
            },
            settings_error=error,
        )

    current_user.preferred_language = normalize_language(raw_language)
    current_user.theme_preference = normalize_theme_preference(raw_theme)
    session.add(current_user)
    session.commit()

    redirect_url = _build_account_redirect_url(request, "settings", notice="settings_updated")
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    _set_preference_cookies(response, current_user)
    return response

@router.get("/account/export")
def export_account_archive(
    request: Request,
    section: str = Query(default="all"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=%2Faccount%3Ftab%3Dsettings", status_code=status.HTTP_303_SEE_OTHER)

    normalized_section = _normalize_profile_transfer_section(section)
    if normalized_section is None:
        raise HTTPException(status_code=400, detail="Unsupported export section")

    payload = _build_profile_export_payload(session, current_user, normalized_section)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"rivulet-{current_user.username}-{normalized_section}-{timestamp}.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=body, media_type="application/json; charset=utf-8", headers=headers)

@router.post("/account/import")
async def import_account_archive(
    request: Request,
    section: str = Form(default="all"),
    archive: UploadFile | None = File(default=None),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=%2Faccount%3Ftab%3Dsettings", status_code=status.HTTP_303_SEE_OTHER)

    current_language = _resolve_language(request, current_user)
    normalized_section = _normalize_profile_transfer_section(section)
    if normalized_section is None:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": "all"},
            import_error=translate(current_language, "settings.error_file_shape"),
        )

    if archive is None:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=translate(current_language, "settings.error_file_missing"),
        )

    payload_bytes = await archive.read()
    if not payload_bytes:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=translate(current_language, "settings.error_file_missing"),
        )
    if len(payload_bytes) > PROFILE_IMPORT_LIMIT:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=translate(current_language, "settings.error_file_large"),
        )

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=translate(current_language, "settings.error_file_decode"),
        )

    try:
        imported_count = _import_profile_payload(session, settings, current_user, payload, normalized_section)
    except ValueError as exc:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=str(exc),
        )

    if imported_count <= 0:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="settings",
            import_form={"section": normalized_section},
            import_error=translate(current_language, "settings.error_import_empty"),
        )

    redirect_url = _build_account_redirect_url(request, "settings", notice="import_completed")
    redirect_url = f"{redirect_url}&imported={imported_count}"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    _set_preference_cookies(response, current_user)
    return response

@router.post("/account/bookmarks")
def create_bookmark(
    request: Request,
    title: str = Form(default=""),
    url: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)
    current_language = _resolve_language(request, current_user)

    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)
    normalized_url = _normalize_url_scheme(url)
    cleaned_title = title.strip()[:120]
    cleaned_description = description.strip()

    error: str | None = None
    if normalized_url is None:
        error = translate(current_language, "error.bookmark_url")
    elif len(normalized_url) > BOOKMARK_URL_LIMIT:
        error = translate(current_language, "error.bookmark_url_limit", limit=BOOKMARK_URL_LIMIT)
    elif len(cleaned_description) > 4000:
        error = translate(current_language, "error.bookmark_description_limit")

    if error:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="bookmarks",
            active_tag=normalized_filter_tag,
            active_search=filter_search,
            active_created=filter_created,
            active_month=filter_month,
            bookmark_form=_bookmark_form_state(
                title=cleaned_title,
                url=url,
                description=cleaned_description,
                tags=normalized_tags,
            ),
            bookmark_error=error,
        )

    bookmark = Bookmark(
        user_id=current_user.id,
        title=cleaned_title or None,
        url=normalized_url,
        description=cleaned_description or None,
        tags_json=_serialize_tags(normalized_tags),
    )
    session.add(bookmark)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        "bookmarks",
        notice="bookmark_created",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/bookmarks/{bookmark_id}")
def update_bookmark(
    bookmark_id: int,
    request: Request,
    title: str = Form(default=""),
    url: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)
    current_language = _resolve_language(request, current_user)

    bookmark = _get_bookmark_or_404(session, bookmark_id, current_user)
    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)
    normalized_url = _normalize_url_scheme(url)
    cleaned_title = title.strip()[:120]
    cleaned_description = description.strip()

    error: str | None = None
    if normalized_url is None:
        error = translate(current_language, "error.bookmark_url")
    elif len(normalized_url) > BOOKMARK_URL_LIMIT:
        error = translate(current_language, "error.bookmark_url_limit", limit=BOOKMARK_URL_LIMIT)
    elif len(cleaned_description) > 4000:
        error = translate(current_language, "error.bookmark_description_limit")

    if error:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="bookmarks",
            active_tag=normalized_filter_tag,
            active_search=filter_search,
            active_created=filter_created,
            active_month=filter_month,
            bookmark_form=_bookmark_form_state(
                bookmark_id=bookmark.id,
                title=cleaned_title,
                url=url,
                description=cleaned_description,
                tags=normalized_tags,
            ),
            bookmark_error=error,
        )

    bookmark.title = cleaned_title or None
    bookmark.url = normalized_url
    bookmark.description = cleaned_description or None
    bookmark.tags_json = _serialize_tags(normalized_tags)
    bookmark.updated_at = datetime.now(timezone.utc)
    session.add(bookmark)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        "bookmarks",
        notice="bookmark_updated",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/bookmarks/{bookmark_id}/tags")
def update_account_bookmark_tags(
    bookmark_id: int,
    request: Request,
    tags: str = Form(default=""),
    tab: str = Form(default="bookmarks"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    bookmark = _get_bookmark_or_404(session, bookmark_id, current_user)
    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)

    bookmark.tags_json = _serialize_tags(normalized_tags)
    bookmark.updated_at = datetime.now(timezone.utc)
    session.add(bookmark)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        notice="bookmark_tags_updated",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/bookmarks/{bookmark_id}/delete")
def delete_account_bookmark(
    bookmark_id: int,
    request: Request,
    tab: str = Form(default="bookmarks"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    bookmark = _get_bookmark_or_404(session, bookmark_id, current_user)
    normalized_filter_tag = _normalize_tag(filter_tag)
    session.delete(bookmark)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        notice="bookmark_deleted",
        tag=normalized_filter_tag,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/notes")
def create_note(
    request: Request,
    title: str = Form(default=""),
    content: str = Form(...),
    tags: str = Form(default=""),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)
    current_language = _resolve_language(request, current_user)

    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)
    cleaned_title = title.strip()[:120]
    note_content = content

    error: str | None = None
    if not note_content.strip():
        error = translate(current_language, "error.empty_note")
    elif len(note_content) > settings.max_content_size:
        error = translate(current_language, "error.note_limit", limit=settings.max_content_size)

    if error:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="notes",
            active_tag=normalized_filter_tag,
            active_search=filter_search,
            active_created=filter_created,
            active_month=filter_month,
            note_form=_note_form_state(
                title=cleaned_title,
                content=note_content,
                tags=normalized_tags,
            ),
            note_error=error,
        )

    note = Note(
        user_id=current_user.id,
        title=cleaned_title or None,
        content=note_content,
        tags_json=_serialize_tags(normalized_tags),
    )
    session.add(note)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        "notes",
        notice="note_created",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/notes/{note_id}")
def update_note(
    note_id: int,
    request: Request,
    title: str = Form(default=""),
    content: str = Form(...),
    tags: str = Form(default=""),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)
    current_language = _resolve_language(request, current_user)

    note = _get_note_or_404(session, note_id, current_user)
    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)
    cleaned_title = title.strip()[:120]
    note_content = content

    error: str | None = None
    if not note_content.strip():
        error = translate(current_language, "error.empty_note")
    elif len(note_content) > settings.max_content_size:
        error = translate(current_language, "error.note_limit", limit=settings.max_content_size)

    if error:
        return _render_account_page(
            request,
            session,
            settings,
            current_user,
            active_tab="notes",
            active_tag=normalized_filter_tag,
            active_search=filter_search,
            active_created=filter_created,
            active_month=filter_month,
            note_form=_note_form_state(
                note_id=note.id,
                title=cleaned_title,
                content=note_content,
                tags=normalized_tags,
            ),
            note_error=error,
        )

    note.title = cleaned_title or None
    note.content = note_content
    note.tags_json = _serialize_tags(normalized_tags)
    note.updated_at = datetime.now(timezone.utc)
    session.add(note)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        "notes",
        notice="note_updated",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/notes/{note_id}/tags")
def update_account_note_tags(
    note_id: int,
    request: Request,
    tags: str = Form(default=""),
    tab: str = Form(default="notes"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    note = _get_note_or_404(session, note_id, current_user)
    normalized_tags = _normalize_tags(tags)
    normalized_filter_tag = _normalize_tag(filter_tag)

    note.tags_json = _serialize_tags(normalized_tags)
    note.updated_at = datetime.now(timezone.utc)
    session.add(note)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        notice="note_tags_updated",
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/notes/{note_id}/delete")
def delete_account_note(
    note_id: int,
    request: Request,
    tab: str = Form(default="notes"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    note = _get_note_or_404(session, note_id, current_user)
    normalized_filter_tag = _normalize_tag(filter_tag)
    session.delete(note)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        notice="note_deleted",
        tag=normalized_filter_tag,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/tags/{slug}")
def update_account_tags(
    slug: str,
    request: Request,
    tags: str = Form(default=""),
    tab: str = Form(default="saved"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
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

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        updated_tags=paste.slug,
        tag=normalized_filter_tag if normalized_filter_tag in normalized_tags else None,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@router.post("/account/delete/{slug}")
def delete_account_paste(
    slug: str,
    request: Request,
    tab: str = Form(default="saved"),
    filter_tag: str = Form(default=""),
    filter_search: str = Form(default=""),
    filter_created: str = Form(default=""),
    filter_month: str = Form(default=""),
    csrf_token: str = Form(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _assert_csrf(request, csrf_token)
    current_user = _current_user(request, session, settings)
    if current_user is None:
        return RedirectResponse(url="/login?next=/account", status_code=status.HTTP_303_SEE_OTHER)

    paste = _get_paste_or_404(session, slug)
    if not _can_user_delete_paste(paste, current_user):
        raise HTTPException(status_code=403, detail="You can only delete your own paste")

    deleted_slug = paste.slug
    normalized_filter_tag = _normalize_tag(filter_tag)
    session.delete(paste)
    session.commit()

    redirect_url = _build_account_redirect_url(
        request,
        tab,
        deleted=deleted_slug,
        tag=normalized_filter_tag,
        search_query=filter_search,
        created=filter_created,
        month=filter_month,
    )
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    _remove_history_slug(response, request, deleted_slug)
    return response
