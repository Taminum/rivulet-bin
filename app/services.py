from __future__ import annotations

import base64
import calendar
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time

from app.auth import (
    AUTH_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session_token,
    read_session_token,
)
from app.config import Settings
from app.i18n import (
    SUPPORTED_LANGUAGES,
    SUPPORTED_THEME_PREFERENCES,
    calendar_label,
    calendar_weekdays,
    client_messages,
    expire_options as translated_expire_options,
    language_options,
    mode_options as translated_mode_options,
    normalize_language,
    normalize_theme_preference,
    syntax_options as translated_syntax_options,
    theme_options as translated_theme_options,
    translate,
)
from app.models import (
    Bookmark,
    Note,
    Paste,
    PasteCollaborator,
    PasteLink,
    PasteRevision,
    Sticker,
    User,
)
from app.rendering import (
    DEFAULT_PYGMENTS_THEME,
    PYGMENTS_THEMES,
    WIKI_LINK_RE,
    extract_wiki_links,
    generate_edit_key,
    generate_slug,
    normalize_pygments_theme,
    normalize_slug,
    pygments_theme_css,
    render_code_lines,
    render_markdown,
    render_preview_html,
    validate_slug,
)
from app.validation import (
    ValidationIssue,
    detect_syntax,
)
from collections import (
    Counter,
    defaultdict,
)
from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)
from fastapi import (
    HTTPException,
    Request,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from html.parser import HTMLParser
from markupsafe import (
    Markup,
    escape,
)
from sqlalchemy import select
from sqlalchemy.orm import (
    Session,
    joinedload,
    selectinload,
)
from urllib.parse import urlparse
from urllib.request import (
    Request as UrlRequest,
    urlopen,
)


templates = Jinja2Templates(directory="app/templates")

SYNTAX_OPTIONS = [
    ("auto", "Auto detect"),
    ("text", "Plain text"),
    ("markdown", "Markdown"),
    ("python", "Python"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("json", "JSON"),
    ("bash", "Bash"),
    ("shell", "Shell"),
    ("html", "HTML"),
    ("css", "CSS"),
    ("sql", "SQL"),
    ("yaml", "YAML"),
    ("toml", "TOML"),
    ("xml", "XML"),
    ("java", "Java"),
    ("c", "C"),
    ("cpp", "C++"),
    ("csharp", "C#"),
    ("go", "Go"),
    ("rust", "Rust"),
    ("php", "PHP"),
    ("ruby", "Ruby"),
    ("swift", "Swift"),
    ("kotlin", "Kotlin"),
    ("scala", "Scala"),
    ("r", "R"),
    ("lua", "Lua"),
    ("perl", "Perl"),
    ("haskell", "Haskell"),
    ("elixir", "Elixir"),
    ("dart", "Dart"),
    ("dockerfile", "Dockerfile"),
    ("nginx", "Nginx"),
    ("makefile", "Makefile"),
    ("diff", "Diff"),
    ("ini", "INI"),
    ("properties", "Properties"),
    ("lisp", "Lisp"),
    ("fortran", "Fortran"),
    ("assembly", "Assembly"),
    ("powershell", "PowerShell"),
    ("cuda", "CUDA"),
    ("protobuf", "Protocol Buffers"),
    ("graphql", "GraphQL"),
    ("terraform", "Terraform"),
    ("groovy", "Groovy"),
    ("matlab", "MATLAB"),
    ("ocaml", "OCaml"),
    ("fsharp", "F#"),
    ("clojure", "Clojure"),
    ("scheme", "Scheme"),
    ("julia", "Julia"),
    ("typescriptreact", "TypeScript React"),
    ("javascriptreact", "JavaScript React"),
    ("cmake", "CMake"),
    ("ansible", "Ansible"),
    ("systemd", "Systemd"),
    ("crontab", "Crontab"),
    ("csv", "CSV"),
    ("tsv", "TSV"),
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

LANGUAGE_COOKIE_NAME = "rivulet_lang"

THEME_COOKIE_NAME = "rivulet_theme"

TAG_SPLIT_RE = re.compile(r"[\s,]+")

TAG_SANITIZE_RE = re.compile(r"[^\w-]+", re.UNICODE)

MAX_TAGS = 8

MAX_TAG_LENGTH = 32

BOOKMARK_URL_LIMIT = 2048

PROFILE_IMPORT_LIMIT = 25_000_000

PROFILE_EXPORT_SECTIONS = ("all", "saved", "shared", "bookmarks", "notes", "links")

NOTE_INLINE_TOKEN_RE = re.compile(
    r"https?://[^\s<]+|(?<![\w/])/(?:[A-Za-z0-9][\w-]*)(?:/(?:[A-Za-z0-9][\w-]*))*"
)

ACCOUNT_NOTICE_KEYS = {
    "bookmark_created": "notice.bookmark_created",
    "bookmark_updated": "notice.bookmark_updated",
    "bookmark_deleted": "notice.bookmark_deleted",
    "bookmark_tags_updated": "notice.bookmark_tags_updated",
    "note_created": "notice.note_created",
    "note_updated": "notice.note_updated",
    "note_deleted": "notice.note_deleted",
    "note_tags_updated": "notice.note_tags_updated",
    "settings_updated": "notice.settings_updated",
    "import_completed": "notice.import_completed",
}

_rate_limit_store: dict[str, list[float]] = defaultdict(list)

RATE_LIMIT_WINDOW = 60.0

RATE_LIMIT_MAX = 30

def _check_rate_limit(key: str, *, limit: int = RATE_LIMIT_MAX, window: float = RATE_LIMIT_WINDOW) -> bool:
    now = time.time()
    timestamps = _rate_limit_store[key]
    _rate_limit_store[key] = [t for t in timestamps if now - t < window]
    if len(_rate_limit_store[key]) >= limit:
        return False
    _rate_limit_store[key].append(now)
    return True

def _assert_csrf(request: Request, form_token: str) -> None:
    cookie_token = request.cookies.get("csrf_token", "")
    if not cookie_token or not form_token or not hmac.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

def _base_context(request: Request, settings: Settings, current_user: User | None = None, **extra):
    language = _resolve_language(request, current_user)
    theme_preference = _resolve_theme_preference(request, current_user)

    def t(key: str, **kwargs: object) -> str:
        return translate(language, key, **kwargs)

    csrf_token = request.cookies.get("csrf_token") or secrets.token_hex(32)

    return {
        "request": request,
        "site_name": settings.site_name,
        "tagline": settings.tagline,
        "asset_version": ASSET_VERSION,
        "current_user": current_user,
        "current_language": language,
        "default_theme_preference": theme_preference,
        "language_options": language_options(),
        "theme_options": translated_theme_options(language),
        "syntax_options": translated_syntax_options(language),
        "mode_options": translated_mode_options(language),
        "expire_options": translated_expire_options(language),
        "client_texts": client_messages(language),
        "csrf_token": csrf_token,
        "t": t,
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

def _build_content_view(
    content: str,
    syntax: str,
    view_mode: str,
    pygments_theme: str = DEFAULT_PYGMENTS_THEME,
) -> dict[str, object]:
    if view_mode == "markdown":
        rendered_markdown = render_markdown(content)
        resolved_view_syntax = "markdown"
        lines = None
    else:
        rendered_markdown = None
        resolved_view_syntax = "text" if view_mode == "link" else _resolve_view_syntax(content, syntax, view_mode)
        lines = render_code_lines(content, resolved_view_syntax, style=pygments_theme)

    return {
        "rendered_markdown": rendered_markdown,
        "lines": lines,
        "resolved_view_syntax": resolved_view_syntax,
        "line_count": max(1, content.count("\n") + 1),
        "content_length": len(content),
        "view_mode_label": _viewer_mode_label(view_mode),
        "display_syntax": _viewer_syntax_label(syntax, resolved_view_syntax, view_mode),
    }

_PYGMENTS_PREVIEW_SNIPPET = 'def hello():\n    # the answer\n    return "42"'
_pygments_preview_cache: list[dict[str, object]] | None = None

def _pygments_preview_blocks() -> list[dict[str, object]]:
    global _pygments_preview_cache
    if _pygments_preview_cache is None:
        blocks: list[dict[str, object]] = []
        for name in PYGMENTS_THEMES:
            blocks.append(
                {
                    "name": name,
                    "css": pygments_theme_css(name, scope=f"#pyg-preview-{name}"),
                    "lines": render_code_lines(_PYGMENTS_PREVIEW_SNIPPET, "python", style=name),
                }
            )
        _pygments_preview_cache = blocks
    return _pygments_preview_cache

def _resolve_pygments_theme(current_user: User | None) -> str:
    if current_user is None:
        return DEFAULT_PYGMENTS_THEME
    return normalize_pygments_theme(getattr(current_user, "pygments_theme", None))

def _sync_wiki_links(session: Session, paste: Paste, content: str) -> None:
    session.query(PasteLink).filter(PasteLink.source_paste_id == paste.id).delete()
    for slug in extract_wiki_links(content):
        if slug != paste.slug:
            session.add(PasteLink(source_paste_id=paste.id, target_slug=slug))

def _apply_wiki_links(html: str, session: Session) -> Markup:
    slugs = {match.group(1) for match in WIKI_LINK_RE.finditer(html)}
    if not slugs:
        return Markup(html)

    existing = set(
        session.scalars(select(Paste.slug).where(Paste.slug.in_(slugs))).all()
    )

    def _replace(match: re.Match) -> str:
        slug = match.group(1)
        label = match.group(2) or slug
        if slug in existing:
            return f'<a class="wiki-link" href="/{slug}">{label}</a>'
        return f'<span class="wiki-link-broken" title="Paste not found">{label}</span>'

    return Markup(WIKI_LINK_RE.sub(_replace, html))

TASK_LI_RE = re.compile(r"<li>(<p>)?\[([ xX])\](?:\s+|(?=<))")

def _apply_task_lists(html: str, editable: bool) -> Markup:
    counter = {"n": 0}

    def _replace(match: re.Match) -> str:
        p_open = match.group(1) or ""
        checked = match.group(2).lower() == "x"
        index = counter["n"]
        counter["n"] += 1
        attrs = " checked" if checked else ""
        if not editable:
            attrs += " disabled"
        return (
            f'<li class="task-list-item">{p_open}'
            f'<input type="checkbox" class="task-checkbox" data-task-index="{index}"{attrs}> '
        )

    return Markup(TASK_LI_RE.sub(_replace, html))

def _paste_backlinks(session: Session, paste: Paste) -> list[Paste]:
    return list(
        session.scalars(
            select(Paste)
            .join(PasteLink, PasteLink.source_paste_id == Paste.id)
            .where(PasteLink.target_slug == paste.slug)
            .order_by(Paste.updated_at.desc())
        ).all()
    )

MAX_STICKER_LENGTH = 280
MAX_STICKERS_PER_PASTE = 20

def _parse_sticker_texts(raw: str) -> list[str]:
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []

    texts: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()[:MAX_STICKER_LENGTH]
        if cleaned:
            texts.append(cleaned)
        if len(texts) >= MAX_STICKERS_PER_PASTE:
            break
    return texts

def _sync_stickers(session: Session, paste: Paste, raw_stickers: str) -> None:
    session.query(Sticker).filter(Sticker.paste_id == paste.id).delete()
    for text in _parse_sticker_texts(raw_stickers):
        session.add(Sticker(paste_id=paste.id, text=text))

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

def _normalize_account_search(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())[:120]

def _normalize_account_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None

def _normalize_account_month(value: str | date | None, fallback: date | None = None) -> date:
    if isinstance(value, date):
        return value.replace(day=1)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip(), "%Y-%m").date().replace(day=1)
        except ValueError:
            pass
    base = fallback or datetime.now(timezone.utc).date()
    return base.replace(day=1)

def _format_account_date(value: date | None) -> str | None:
    return value.isoformat() if value else None

def _format_account_month(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None

def _shift_account_month(month_start: date, delta: int) -> date:
    total_months = month_start.year * 12 + month_start.month - 1 + delta
    year, month_index = divmod(total_months, 12)
    return date(year, month_index + 1, 1)

def _filter_profile_items_by_tag(items: list[dict[str, object]], tag: str | None) -> list[dict[str, object]]:
    if not tag:
        return items
    return [item for item in items if tag in item.get("tags", [])]

def _filter_profile_items_by_search(items: list[dict[str, object]], search_query: str) -> list[dict[str, object]]:
    normalized_search = _normalize_account_search(search_query)
    if not normalized_search:
        return items

    tokens = [token.lower() for token in normalized_search.split()]
    if not tokens:
        return items

    filtered_items: list[dict[str, object]] = []
    for item in items:
        haystack = str(item.get("search_text", "")).lower()
        if all(token in haystack for token in tokens):
            filtered_items.append(item)
    return filtered_items

def _filter_profile_items_by_created_date(items: list[dict[str, object]], created_on: date | None) -> list[dict[str, object]]:
    if created_on is None:
        return items
    return [item for item in items if item.get("created_date") == created_on]

def _validate_editor_options(syntax: str, mode: str, language: str) -> str | None:
    if syntax not in VALID_SYNTAX_VALUES:
        return translate(language, "error.options")
    if mode not in VALID_MODE_VALUES:
        return translate(language, "error.options")
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

def _resolve_language(request: Request, current_user: User | None = None) -> str:
    if current_user is not None:
        return normalize_language(getattr(current_user, "preferred_language", None))
    return normalize_language(request.cookies.get(LANGUAGE_COOKIE_NAME))

def _resolve_theme_preference(request: Request, current_user: User | None = None) -> str:
    if current_user is not None:
        return normalize_theme_preference(getattr(current_user, "theme_preference", None))
    return normalize_theme_preference(request.cookies.get(THEME_COOKIE_NAME))

def _set_preference_cookies(response: Response, user: User) -> None:
    response.set_cookie(
        LANGUAGE_COOKIE_NAME,
        normalize_language(user.preferred_language),
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )
    response.set_cookie(
        THEME_COOKIE_NAME,
        normalize_theme_preference(user.theme_preference),
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )

def _set_auth_cookie(response: RedirectResponse, user_id: int, settings: Settings) -> None:
    is_https = response.headers.get("location", "").startswith("https") if "location" in response.headers else False
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(user_id, settings.secret_salt),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=False,
        samesite="lax",
    )

def _clear_auth_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, httponly=True, samesite="lax")

def _normalize_username(value: str) -> str:
    return value.strip().lower()

def _validate_registration_form(username: str, password: str, confirm_password: str, language: str) -> str | None:
    if not USERNAME_RE.fullmatch(username):
        return translate(language, "auth.error_username")
    if len(password) < 8:
        return translate(language, "auth.error_password_length")
    if password != confirm_password:
        return translate(language, "auth.error_password_mismatch")
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

def _render_account_page(
    request: Request,
    session: Session,
    settings: Settings,
    current_user: User,
    *,
    active_tab: str,
    active_tag: str | None = None,
    active_search: str | None = None,
    active_created: str | date | None = None,
    active_month: str | date | None = None,
    deleted: str | None = None,
    updated_tags: str | None = None,
    notice: str | None = None,
    bookmark_edit_id: int | None = None,
    note_edit_id: int | None = None,
    bookmark_form: dict[str, str] | None = None,
    bookmark_error: str | None = None,
    note_form: dict[str, str] | None = None,
    note_error: str | None = None,
    settings_form: dict[str, str] | None = None,
    settings_error: str | None = None,
    import_form: dict[str, str] | None = None,
    import_error: str | None = None,
    imported_count: int | None = None,
) -> HTMLResponse:
    language = _resolve_language(request, current_user)
    tr = lambda key, **kwargs: translate(language, key, **kwargs)
    normalized_tab = _normalize_account_tab(active_tab)
    normalized_tag = _normalize_tag(active_tag or "")
    normalized_search = _normalize_account_search(active_search)
    normalized_created = _normalize_account_date(active_created)
    normalized_month = _normalize_account_month(active_month, fallback=normalized_created)

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

    bookmarks = session.scalars(
        select(Bookmark)
        .where(Bookmark.user_id == current_user.id)
        .order_by(Bookmark.updated_at.desc(), Bookmark.created_at.desc())
    ).all()

    notes = session.scalars(
        select(Note)
        .where(Note.user_id == current_user.id)
        .order_by(Note.updated_at.desc(), Note.created_at.desc())
    ).all()

    shared_pastes = [
        membership.paste
        for membership in shared_memberships
        if membership.paste is not None and membership.paste.owner_id != current_user.id
    ]

    owned_items = _profile_items(request, owned_pastes, current_user, shared=False, language=language)
    shared_items = _profile_items(request, shared_pastes, current_user, shared=True, language=language)
    saved_items = [item for item in owned_items if item.get("preview_kind") != "link"]
    short_link_items = [item for item in owned_items if item.get("preview_kind") == "link"]
    bookmark_items = _bookmark_items(
        request,
        bookmarks,
        normalized_tag,
        normalized_search,
        normalized_created,
        normalized_month,
    )
    note_items = _note_items(
        request,
        notes,
        normalized_tag,
        normalized_search,
        normalized_created,
        normalized_month,
    )

    tab_items = {
        "saved": saved_items,
        "links": short_link_items,
        "shared": shared_items,
        "bookmarks": bookmark_items,
        "notes": note_items,
        "settings": [],
    }
    current_tab_items = tab_items[normalized_tab]
    searched_items = _filter_profile_items_by_search(current_tab_items, normalized_search) if normalized_tab != "settings" else []
    tag_scoped_items = _filter_profile_items_by_tag(searched_items, normalized_tag) if normalized_tab != "settings" else []
    filtered_items = _filter_profile_items_by_created_date(tag_scoped_items, normalized_created) if normalized_tab != "settings" else []

    bookmark_form_id = _coerce_identifier((bookmark_form or {}).get("id"))
    note_form_id = _coerce_identifier((note_form or {}).get("id"))
    bookmark_edit_target = None
    note_edit_target = None

    if normalized_tab == "bookmarks" and bookmark_edit_id is not None:
        bookmark_edit_target = _get_bookmark_or_404(session, bookmark_edit_id, current_user)
    elif bookmark_form_id is not None:
        bookmark_edit_target = _get_bookmark_or_404(session, bookmark_form_id, current_user)

    if normalized_tab == "notes" and note_edit_id is not None:
        note_edit_target = _get_note_or_404(session, note_edit_id, current_user)
    elif note_form_id is not None:
        note_edit_target = _get_note_or_404(session, note_form_id, current_user)

    bookmark_form_values = (
        bookmark_form
        if bookmark_form is not None
        else _bookmark_form_state_from_model(bookmark_edit_target)
    )
    note_form_values = (
        note_form
        if note_form is not None
        else _note_form_state_from_model(note_edit_target)
    )
    settings_form_values = settings_form or {
        "preferred_language": normalize_language(current_user.preferred_language),
        "theme_preference": normalize_theme_preference(current_user.theme_preference),
        "pygments_theme": _resolve_pygments_theme(current_user),
    }
    import_form_values = import_form or {"section": "all"}

    active_section_label = {
        "saved": tr("account.section_saved"),
        "links": tr("account.section_links"),
        "shared": tr("account.section_shared"),
        "bookmarks": tr("account.section_bookmarks"),
        "notes": tr("account.section_notes"),
        "settings": tr("account.section_settings"),
    }[normalized_tab]
    create_action = {
        "saved": {"label": tr("account.new_paste"), "url": "/"},
        "links": {"label": tr("account.new_short_link"), "url": "/?mode=link"},
        "shared": {"label": tr("account.new_paste"), "url": "/"},
        "bookmarks": {"label": tr("account.new_bookmark"), "url": "#bookmark-form"},
        "notes": {"label": tr("account.new_note"), "url": "#note-form"},
        "settings": None,
    }[normalized_tab]
    profile_count = len(filtered_items)
    search_placeholder = {
        "saved": tr("account.search_saved"),
        "links": tr("account.search_links"),
        "shared": tr("account.search_shared"),
        "bookmarks": tr("account.search_bookmarks"),
        "notes": tr("account.search_notes"),
        "settings": "",
    }[normalized_tab]
    sidebar_tag_items = (
        _account_tag_summary(
            request,
            searched_items,
            normalized_tab,
            search_query=normalized_search,
            active_tag=normalized_tag,
            active_date=normalized_created,
            active_month=normalized_month,
        )
        if normalized_tab != "settings"
        else []
    )
    calendar_context = (
        _account_calendar_context(
            request,
            tag_scoped_items,
            normalized_tab,
            search_query=normalized_search,
            active_tag=normalized_tag,
            active_date=normalized_created,
            active_month=normalized_month,
            language=language,
        )
        if normalized_tab != "settings"
        else {"label": "", "weekdays": [], "weeks": [], "previous_url": "#", "next_url": "#"}
    )

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context=_base_context(
            request,
            settings,
            title="Profile",
            current_user=current_user,
            account_tab=normalized_tab,
            active_tag=normalized_tag,
            active_search=normalized_search,
            active_created=_format_account_date(normalized_created),
            active_month=_format_account_month(normalized_month),
            active_section_label=active_section_label,
            profile_count=profile_count,
            profile_items=filtered_items if normalized_tab in {"saved", "links", "shared"} else [],
            bookmark_items=filtered_items if normalized_tab == "bookmarks" else [],
            note_items=filtered_items if normalized_tab == "notes" else [],
            owned_count=len(saved_items),
            short_link_count=len(short_link_items),
            shared_count=len(shared_items),
            bookmark_count=len(bookmark_items),
            note_count=len(note_items),
            saved_tab_url=_account_url(request, "saved", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            links_tab_url=_account_url(request, "links", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            shared_tab_url=_account_url(request, "shared", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            bookmarks_tab_url=_account_url(request, "bookmarks", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            notes_tab_url=_account_url(request, "notes", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            settings_tab_url=_account_url(request, "settings"),
            clear_tag_url=_account_url(request, normalized_tab, search_query=normalized_search, created=normalized_created, month=normalized_month),
            clear_search_url=_account_url(request, normalized_tab, normalized_tag, created=normalized_created, month=normalized_month),
            clear_date_url=_account_url(request, normalized_tab, normalized_tag, search_query=normalized_search, month=normalized_month),
            clear_all_filters_url=_account_url(request, normalized_tab, month=normalized_month),
            account_message=_account_message(language, deleted, updated_tags, notice, imported_count),
            create_item_label=create_action["label"] if create_action else "",
            create_item_url=create_action["url"] if create_action else "",
            show_create_item_button=create_action is not None,
            show_account_view_toggle=normalized_tab != "settings",
            search_placeholder=search_placeholder,
            calendar_context=calendar_context,
            sidebar_tag_items=sidebar_tag_items,
            has_active_filters=bool(normalized_search or normalized_tag or normalized_created),
            bookmark_dialog_open=bookmark_error is not None or bool(bookmark_form_values["id"]),
            bookmark_form_values=bookmark_form_values,
            bookmark_form_action=(
                f"/account/bookmarks/{bookmark_form_values['id']}"
                if bookmark_form_values["id"]
                else "/account/bookmarks"
            ),
            bookmark_form_mode="edit" if bookmark_form_values["id"] else "create",
            bookmark_cancel_url=_account_url(request, "bookmarks", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            bookmark_error=bookmark_error,
            note_form_values=note_form_values,
            note_form_action=(
                f"/account/notes/{note_form_values['id']}"
                if note_form_values["id"]
                else "/account/notes"
            ),
            note_form_mode="edit" if note_form_values["id"] else "create",
            note_cancel_url=_account_url(request, "notes", normalized_tag, search_query=normalized_search, created=normalized_created, month=normalized_month),
            note_dialog_open=note_error is not None or bool(note_form_values["id"]),
            note_error=note_error,
            settings_form_values=settings_form_values,
            settings_error=settings_error,
            pygments_theme_previews=_pygments_preview_blocks(),
            import_form_values=import_form_values,
            import_error=import_error,
        ),
    )

def _bookmark_form_state(
    *,
    bookmark_id: int | None = None,
    title: str = "",
    url: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> dict[str, str]:
    return {
        "id": str(bookmark_id or ""),
        "title": title,
        "url": url,
        "description": description,
        "tags": _format_tags_input(tags or []),
    }

def _bookmark_form_state_from_model(bookmark: Bookmark | None) -> dict[str, str]:
    if bookmark is None:
        return _bookmark_form_state()
    return _bookmark_form_state(
        bookmark_id=bookmark.id,
        title=bookmark.title or "",
        url=bookmark.url,
        description=bookmark.description or "",
        tags=_bookmark_tags(bookmark),
    )

def _note_form_state(
    *,
    note_id: int | None = None,
    title: str = "",
    content: str = "",
    tags: list[str] | None = None,
) -> dict[str, str]:
    return {
        "id": str(note_id or ""),
        "title": title,
        "content": content,
        "tags": _format_tags_input(tags or []),
    }

def _note_form_state_from_model(note: Note | None) -> dict[str, str]:
    if note is None:
        return _note_form_state()
    return _note_form_state(
        note_id=note.id,
        title=note.title or "",
        content=note.content,
        tags=_note_tags(note),
    )

def _normalize_profile_transfer_section(value: str | None) -> str | None:
    candidate = (value or "").strip().lower()
    return candidate if candidate in PROFILE_EXPORT_SECTIONS else None

def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()

def _parse_archive_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

def _normalize_import_tags_from_payload(value: object) -> list[str]:
    if isinstance(value, list):
        return _normalize_tags(" ".join(str(item) for item in value))
    if isinstance(value, str):
        return _normalize_tags(value)
    return []

def _export_revision_data(revision: PasteRevision) -> dict[str, object]:
    return {
        "revision_number": revision.revision_number,
        "event": revision.event,
        "title": revision.title,
        "content": revision.content,
        "syntax": revision.syntax,
        "view_mode": revision.view_mode,
        "editor_username": revision.editor.username if revision.editor else None,
        "created_at": _serialize_datetime(revision.created_at),
    }

def _export_paste_data(paste: Paste) -> dict[str, object]:
    revisions = sorted(paste.revisions, key=lambda item: item.revision_number)
    return {
        "slug": paste.slug,
        "title": paste.title,
        "content": paste.content,
        "syntax": paste.syntax,
        "view_mode": paste.view_mode,
        "is_url": paste.is_url,
        "tags": _paste_tags(paste),
        "creator_username": paste.creator.username if paste.creator else None,
        "last_editor_username": paste.last_editor.username if paste.last_editor else None,
        "created_at": _serialize_datetime(paste.created_at),
        "updated_at": _serialize_datetime(paste.updated_at),
        "revisions": [_export_revision_data(revision) for revision in revisions],
    }

def _export_bookmark_data(bookmark: Bookmark) -> dict[str, object]:
    return {
        "title": bookmark.title,
        "url": bookmark.url,
        "description": bookmark.description,
        "tags": _bookmark_tags(bookmark),
        "created_at": _serialize_datetime(bookmark.created_at),
        "updated_at": _serialize_datetime(bookmark.updated_at),
    }

def _export_note_data(note: Note) -> dict[str, object]:
    return {
        "title": note.title,
        "content": note.content,
        "tags": _note_tags(note),
        "created_at": _serialize_datetime(note.created_at),
        "updated_at": _serialize_datetime(note.updated_at),
    }

def _build_profile_export_payload(session: Session, current_user: User, section: str) -> dict[str, object]:
    owned_pastes = session.scalars(
        select(Paste)
        .options(joinedload(Paste.creator), joinedload(Paste.last_editor), selectinload(Paste.revisions).joinedload(PasteRevision.editor))
        .where(Paste.owner_id == current_user.id)
        .order_by(Paste.updated_at.desc(), Paste.created_at.desc())
    ).all()
    shared_memberships = session.scalars(
        select(PasteCollaborator)
        .options(
            joinedload(PasteCollaborator.paste).joinedload(Paste.creator),
            joinedload(PasteCollaborator.paste).joinedload(Paste.last_editor),
            joinedload(PasteCollaborator.paste).selectinload(Paste.revisions).joinedload(PasteRevision.editor),
        )
        .where(PasteCollaborator.user_id == current_user.id)
        .order_by(PasteCollaborator.joined_at.desc())
    ).all()
    shared_pastes = [
        membership.paste
        for membership in shared_memberships
        if membership.paste is not None and membership.paste.owner_id != current_user.id
    ]
    bookmarks = session.scalars(
        select(Bookmark)
        .where(Bookmark.user_id == current_user.id)
        .order_by(Bookmark.updated_at.desc(), Bookmark.created_at.desc())
    ).all()
    notes = session.scalars(
        select(Note)
        .where(Note.user_id == current_user.id)
        .order_by(Note.updated_at.desc(), Note.created_at.desc())
    ).all()

    saved_pastes = [paste for paste in owned_pastes if not paste.is_url and paste.view_mode != "link"]
    link_pastes = [paste for paste in owned_pastes if paste.is_url or paste.view_mode == "link"]
    sections = {
        "saved": [_export_paste_data(paste) for paste in saved_pastes],
        "shared": [_export_paste_data(paste) for paste in shared_pastes],
        "bookmarks": [_export_bookmark_data(bookmark) for bookmark in bookmarks],
        "notes": [_export_note_data(note) for note in notes],
        "links": [_export_paste_data(paste) for paste in link_pastes],
    }
    exported_sections = sections if section == "all" else {section: sections.get(section, [])}
    return {
        "app": "rivulet-bin",
        "version": 1,
        "section": section,
        "exported_at": _serialize_datetime(datetime.now(timezone.utc)),
        "user": {
            "username": current_user.username,
            "preferred_language": normalize_language(current_user.preferred_language),
            "theme_preference": normalize_theme_preference(current_user.theme_preference),
        },
        "sections": exported_sections,
    }

def _restore_paste_revisions(session: Session, paste: Paste, revisions_payload: object, current_user: User) -> None:
    if not isinstance(revisions_payload, list):
        _record_revision(session, paste, current_user, event="created")
        return

    seen_numbers: set[int] = set()
    restored_any = False
    for raw_revision in sorted(
        (item for item in revisions_payload if isinstance(item, dict)),
        key=lambda item: int(item.get("revision_number", 0) or 0),
    ):
        try:
            revision_number = int(raw_revision.get("revision_number", 0) or 0)
        except (TypeError, ValueError):
            continue
        if revision_number <= 0 or revision_number in seen_numbers:
            continue

        seen_numbers.add(revision_number)
        revision_created_at = _parse_archive_datetime(raw_revision.get("created_at")) or paste.created_at
        editor_username = str(raw_revision.get("editor_username") or "").strip().lower()
        session.add(
            PasteRevision(
                paste_id=paste.id,
                revision_number=revision_number,
                event=str(raw_revision.get("event") or "saved")[:24],
                title=(str(raw_revision.get("title") or "").strip()[:120] or None),
                content=str(raw_revision.get("content") or paste.content),
                syntax=(
                    str(raw_revision.get("syntax") or paste.syntax).strip().lower()
                    if str(raw_revision.get("syntax") or paste.syntax).strip().lower() in VALID_SYNTAX_VALUES
                    else paste.syntax
                ),
                view_mode=(
                    str(raw_revision.get("view_mode") or paste.view_mode).strip().lower()
                    if str(raw_revision.get("view_mode") or paste.view_mode).strip().lower() in VALID_MODE_VALUES
                    else paste.view_mode
                ),
                editor_id=current_user.id if editor_username == current_user.username else None,
                created_at=revision_created_at,
            )
        )
        restored_any = True

    if not restored_any:
        _record_revision(session, paste, current_user, event="created")

def _import_paste_items(
    session: Session,
    settings: Settings,
    current_user: User,
    items_payload: object,
    *,
    shared: bool,
) -> int:
    if not isinstance(items_payload, list):
        return 0

    imported = 0
    for raw_item in items_payload:
        if not isinstance(raw_item, dict):
            continue

        content = str(raw_item.get("content") or "")
        if not content.strip():
            continue

        desired_slug = normalize_slug(str(raw_item.get("slug") or ""))
        claimed_slug = _claim_slug(session, settings, desired_slug) if desired_slug else None
        slug = claimed_slug or _claim_slug(session, settings, "")
        if slug is None:
            continue

        syntax = str(raw_item.get("syntax") or "auto").strip().lower()
        if syntax not in VALID_SYNTAX_VALUES:
            syntax = "auto"
        view_mode = str(raw_item.get("view_mode") or "code").strip().lower()
        if view_mode not in VALID_MODE_VALUES:
            view_mode = "link" if raw_item.get("is_url") else "code"

        created_at = _parse_archive_datetime(raw_item.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_archive_datetime(raw_item.get("updated_at")) or created_at
        paste = Paste(
            slug=slug,
            title=(str(raw_item.get("title") or "").strip()[:120] or None),
            tags_json=_serialize_tags(_normalize_import_tags_from_payload(raw_item.get("tags"))),
            content=content,
            syntax=syntax,
            view_mode=view_mode,
            is_url=bool(raw_item.get("is_url")) or view_mode == "link",
            edit_key=generate_edit_key(),
            owner_id=None if shared else current_user.id,
            creator_id=current_user.id,
            last_editor_id=current_user.id,
            created_at=created_at,
            updated_at=updated_at,
        )
        session.add(paste)
        session.flush()

        if shared:
            session.add(PasteCollaborator(paste_id=paste.id, user_id=current_user.id))

        _restore_paste_revisions(session, paste, raw_item.get("revisions"), current_user)
        imported += 1

    return imported

def _import_bookmark_items(session: Session, current_user: User, items_payload: object) -> int:
    if not isinstance(items_payload, list):
        return 0

    imported = 0
    for raw_item in items_payload:
        if not isinstance(raw_item, dict):
            continue

        normalized_url = _normalize_url_scheme(str(raw_item.get("url") or ""))
        if normalized_url is None:
            continue

        created_at = _parse_archive_datetime(raw_item.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_archive_datetime(raw_item.get("updated_at")) or created_at
        session.add(
            Bookmark(
                user_id=current_user.id,
                title=(str(raw_item.get("title") or "").strip()[:120] or None),
                url=normalized_url,
                description=(str(raw_item.get("description") or "").strip()[:4000] or None),
                tags_json=_serialize_tags(_normalize_import_tags_from_payload(raw_item.get("tags"))),
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        imported += 1

    return imported

def _import_note_items(session: Session, current_user: User, items_payload: object) -> int:
    if not isinstance(items_payload, list):
        return 0

    imported = 0
    for raw_item in items_payload:
        if not isinstance(raw_item, dict):
            continue

        content = str(raw_item.get("content") or "")
        if not content.strip():
            continue

        created_at = _parse_archive_datetime(raw_item.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_archive_datetime(raw_item.get("updated_at")) or created_at
        session.add(
            Note(
                user_id=current_user.id,
                title=(str(raw_item.get("title") or "").strip()[:120] or None),
                content=content,
                tags_json=_serialize_tags(_normalize_import_tags_from_payload(raw_item.get("tags"))),
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        imported += 1

    return imported

def _import_profile_payload(
    session: Session,
    settings: Settings,
    current_user: User,
    payload: object,
    selected_section: str,
) -> int:
    if not isinstance(payload, dict):
        raise ValueError(translate(normalize_language(current_user.preferred_language), "settings.error_file_shape"))

    sections_payload = payload.get("sections")
    if not isinstance(sections_payload, dict):
        raise ValueError(translate(normalize_language(current_user.preferred_language), "settings.error_file_shape"))

    available_sections = (
        [section for section in ("saved", "shared", "bookmarks", "notes", "links") if section in sections_payload]
        if selected_section == "all"
        else [selected_section]
    )

    imported = 0
    for section_name in available_sections:
        raw_section = sections_payload.get(section_name)
        if section_name == "saved":
            imported += _import_paste_items(session, settings, current_user, raw_section, shared=False)
        elif section_name == "shared":
            imported += _import_paste_items(session, settings, current_user, raw_section, shared=True)
        elif section_name == "bookmarks":
            imported += _import_bookmark_items(session, current_user, raw_section)
        elif section_name == "notes":
            imported += _import_note_items(session, current_user, raw_section)
        elif section_name == "links":
            imported += _import_paste_items(session, settings, current_user, raw_section, shared=False)

    if selected_section == "all":
        user_payload = payload.get("user")
        if isinstance(user_payload, dict):
            preferred_language = str(user_payload.get("preferred_language") or "").strip().lower()
            theme_preference = str(user_payload.get("theme_preference") or "").strip().lower()
            if preferred_language in SUPPORTED_LANGUAGES:
                current_user.preferred_language = preferred_language
            if theme_preference in SUPPORTED_THEME_PREFERENCES:
                current_user.theme_preference = theme_preference
            session.add(current_user)

    session.commit()
    return imported

def _build_account_redirect_url(
    request: Request,
    tab: str,
    *,
    notice: str | None = None,
    tag: str | None = None,
    search_query: str | None = None,
    created: str | date | None = None,
    month: str | date | None = None,
    bookmark_edit: int | None = None,
    note_edit: int | None = None,
    deleted: str | None = None,
    updated_tags: str | None = None,
) -> str:
    url = request.url_for("account").include_query_params(tab=_normalize_account_tab(tab))
    if tag:
        url = url.include_query_params(tag=tag)
    normalized_search = _normalize_account_search(search_query)
    if normalized_search:
        url = url.include_query_params(q=normalized_search)
    normalized_created = _normalize_account_date(created)
    if normalized_created is not None:
        url = url.include_query_params(created=normalized_created.isoformat())
    normalized_month = _normalize_account_month(month, fallback=normalized_created)
    if month is not None:
        url = url.include_query_params(month=normalized_month.strftime("%Y-%m"))
    if notice in ACCOUNT_NOTICE_KEYS:
        url = url.include_query_params(notice=notice)
    if bookmark_edit is not None:
        url = url.include_query_params(bookmark_edit=bookmark_edit)
    if note_edit is not None:
        url = url.include_query_params(note_edit=note_edit)
    deleted_slug = normalize_slug(deleted or "")
    if deleted_slug:
        url = url.include_query_params(deleted=deleted_slug)
    updated_slug = normalize_slug(updated_tags or "")
    if updated_slug:
        url = url.include_query_params(updated_tags=updated_slug)
    return str(url)

def _profile_items(
    request: Request,
    pastes: list[Paste],
    current_user: User | None,
    *,
    shared: bool,
    language: str,
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
                "created_at": paste.created_at,
                "created_date": _item_created_date(paste.created_at),
                "public_url": origin + request.url_for("view_paste", slug=paste.slug).path,
                "raw_url": origin + request.url_for("raw_paste", slug=paste.slug).path,
                "edit_url": request.url_for("edit_paste_form", slug=paste.slug).path,
                "share_edit_url": share_edit_url,
                "changes_url": request.url_for("revisions_page", slug=paste.slug).path,
                "preview_kind": _profile_preview_kind(paste, resolved_syntax),
                "preview_text": _profile_preview_text(paste),
                "preview_html": _profile_preview_html(paste, resolved_syntax),
                "preview_label": _profile_preview_label(paste, resolved_syntax, language),
                "display_syntax": _profile_syntax_label(paste, resolved_syntax, language),
                "creator_label": _profile_user_label(paste.creator, language),
                "last_editor_label": _profile_user_label(paste.last_editor, language),
                "is_shared": shared,
                "can_manage_tags": _can_user_edit_paste(paste, current_user),
                "can_delete": _can_user_delete_paste(paste, current_user),
                "search_text": _search_blob(
                    paste.title,
                    paste.slug,
                    paste.content[:2000],
                    " ".join(_paste_tags(paste)),
                    paste.creator.username if paste.creator else "",
                    paste.last_editor.username if paste.last_editor else "",
                ),
            }
        )
    return items

def _bookmark_items(
    request: Request,
    bookmarks: list[Bookmark],
    active_tag: str | None = None,
    active_search: str | None = None,
    active_created: date | None = None,
    active_month: date | None = None,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for bookmark in bookmarks:
        host = _bookmark_host(bookmark.url)
        items.append(
            {
                "bookmark": bookmark,
                "tags": _bookmark_tags(bookmark),
                "created_at": bookmark.created_at,
                "created_date": _item_created_date(bookmark.created_at),
                "display_title": _bookmark_display_title(bookmark),
                "display_url": _truncate_text(bookmark.url, 88),
                "host": host,
                "preview_text": _bookmark_preview_text(bookmark),
                "search_text": _search_blob(
                    bookmark.title,
                    bookmark.url,
                    bookmark.description,
                    " ".join(_bookmark_tags(bookmark)),
                ),
                "edit_url": _build_account_redirect_url(
                    request,
                    "bookmarks",
                    tag=active_tag,
                    search_query=active_search,
                    created=active_created,
                    month=active_month,
                    bookmark_edit=bookmark.id,
                ),
            }
        )
    return items

def _note_items(
    request: Request,
    notes: list[Note],
    active_tag: str | None = None,
    active_search: str | None = None,
    active_created: date | None = None,
    active_month: date | None = None,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for note in notes:
        items.append(
            {
                "note": note,
                "tags": _note_tags(note),
                "created_at": note.created_at,
                "created_date": _item_created_date(note.created_at),
                "display_title": _note_display_title(note),
                "preview_text": _excerpt_text(note.content),
                "preview_html": _note_preview_html(note.content),
                "content_html": _note_content_html(note.content),
                "line_count": max(1, note.content.count("\n") + 1),
                "char_count": len(note.content),
                "search_text": _search_blob(
                    note.title,
                    note.content[:2000],
                    " ".join(_note_tags(note)),
                ),
                "edit_url": _build_account_redirect_url(
                    request,
                    "notes",
                    tag=active_tag,
                    search_query=active_search,
                    created=active_created,
                    month=active_month,
                    note_edit=note.id,
                ),
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

    return _excerpt_text(content)

def _profile_preview_html(paste: Paste, resolved_syntax: str) -> Markup | None:
    if paste.is_url or paste.view_mode in {"link", "markdown"}:
        return None

    content = paste.content.replace("\r\n", "\n").strip()
    if not content:
        return None

    return render_preview_html(_excerpt_text(content, max_lines=6, max_chars=280), resolved_syntax)

def _profile_preview_label(paste: Paste, resolved_syntax: str, language: str = "en") -> str:
    if paste.is_url or paste.view_mode == "link":
        return translate(language, "account.shortened_link")
    return _profile_syntax_label(paste, resolved_syntax, language)

def _profile_syntax_label(paste: Paste, resolved_syntax: str, language: str = "en") -> str:
    if paste.is_url or paste.view_mode == "link":
        return translate(language, "mode.link_short").upper()
    if paste.view_mode == "markdown":
        return "MARKDOWN"
    return resolved_syntax.upper()

def _profile_resolved_syntax(paste: Paste) -> str:
    if paste.view_mode != "code":
        return paste.syntax

    resolved_syntax = _resolve_view_syntax(paste.content, paste.syntax, paste.view_mode)
    return resolved_syntax if resolved_syntax in VALID_SYNTAX_VALUES else "text"

def _bookmark_tags(bookmark: Bookmark) -> list[str]:
    return _deserialize_tags(getattr(bookmark, "tags_json", "[]"))

def _note_tags(note: Note) -> list[str]:
    return _deserialize_tags(getattr(note, "tags_json", "[]"))

def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."

def _excerpt_text(content: str, *, max_lines: int = 8, max_chars: int = 340) -> str:
    normalized = content.replace("\r\n", "\n").strip()
    if not normalized:
        return "No content"

    lines = normalized.split("\n")
    excerpt = "\n".join(lines[:max_lines]).strip()
    if len(lines) > max_lines or len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip() + "..."
    return excerpt

def _note_content_html(content: str) -> Markup:
    return _note_rich_html(content)

def _note_preview_html(content: str) -> Markup:
    return _note_rich_html(content, max_lines=6, max_chars=320)

NOTE_TASK_RE = re.compile(r"^ {0,3}[-*+] \[([ xX])\](?:\s+(.*))?$")

def _note_rich_html(
    content: str,
    *,
    max_lines: int | None = None,
    max_chars: int | None = None,
) -> Markup:
    if max_lines is not None or max_chars is not None:
        normalized = _excerpt_text(
            content,
            max_lines=max_lines or 8,
            max_chars=max_chars or 340,
        )
    else:
        normalized = content.replace("\r\n", "\n").strip() or "No content"

    rendered_lines: list[str] = []
    task_index = 0
    for line in normalized.split("\n"):
        match = NOTE_TASK_RE.match(line)
        if match:
            checked = match.group(1).lower() == "x"
            label = _note_inline_html(match.group(2) or "")
            checked_attr = " checked" if checked else ""
            rendered_lines.append(
                f'<span class="note-task-item">'
                f'<input type="checkbox" class="task-checkbox" data-task-index="{task_index}"{checked_attr}> '
                f'{label}</span>'
            )
            task_index += 1
        else:
            rendered_lines.append(_note_inline_html(line))

    return Markup("\n".join(rendered_lines))

def _note_inline_html(normalized: str) -> str:
    parts: list[str] = []
    last_index = 0

    for match in NOTE_INLINE_TOKEN_RE.finditer(normalized):
        start, end = match.span()
        token = match.group(0)
        if start > last_index:
            parts.append(str(escape(normalized[last_index:start])))

        if token.startswith(("http://", "https://")):
            link_value, trailing = _split_note_link_trailing_punctuation(token)
            escaped_link = escape(link_value)
            parts.append(
                f'<a href="{escaped_link}" target="_blank" rel="noreferrer">{escaped_link}</a>'
            )
            if trailing:
                parts.append(str(escape(trailing)))
        else:
            parts.append(f'<span class="note-inline-command">{escape(token)}</span>')

        last_index = end

    if last_index < len(normalized):
        parts.append(str(escape(normalized[last_index:])))

    return "".join(parts)

def _split_note_link_trailing_punctuation(value: str) -> tuple[str, str]:
    trimmed = value
    trailing_chars: list[str] = []
    matching_brackets = {")": "(", "]": "[", "}": "{"}

    while trimmed and trimmed[-1] in ".,;:!?":
        trailing_chars.append(trimmed[-1])
        trimmed = trimmed[:-1]

    while trimmed and trimmed[-1] in matching_brackets:
        closing = trimmed[-1]
        opening = matching_brackets[closing]
        if trimmed.count(closing) <= trimmed.count(opening):
            break
        trailing_chars.append(closing)
        trimmed = trimmed[:-1]

    return trimmed or value, "".join(reversed(trailing_chars))

def _resolve_short_link_title(title: str | None, url: str, is_url: bool) -> str | None:
    if title or not is_url:
        return title
    return _short_link_site_title(url)

def _short_link_site_title(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    page_title = _fetch_short_link_page_title(url, parsed)
    if page_title:
        return page_title[:120]

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host[:120] or None

def _fetch_short_link_page_title(url: str, parsed_url) -> str | None:
    if not _is_safe_short_link_target(parsed_url):
        return None

    try:
        request = UrlRequest(
            url,
            headers={
                "User-Agent": "RivuletBin/1.0 (+https://localhost)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=3) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                return None
            charset = response.headers.get_content_charset() or "utf-8"
            snippet = response.read(65536).decode(charset, errors="ignore")
    except Exception:
        return None

    parser = _HTMLTitleExtractor()
    parser.feed(snippet)
    return parser.title

def _is_safe_short_link_target(parsed_url) -> bool:
    hostname = parsed_url.hostname
    if not hostname:
        return False

    try:
        resolved = socket.getaddrinfo(
            hostname,
            parsed_url.port or (443 if parsed_url.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False

    for entry in resolved:
        raw_ip = entry[4][0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False
    return True

class _HTMLTitleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_title = False
        self._chunks: list[str] = []

    @property
    def title(self) -> str | None:
        text = " ".join(chunk.strip() for chunk in self._chunks if chunk.strip())
        normalized = " ".join(text.split())
        return normalized or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title" and not self._chunks:
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self._chunks.append(data)

def _bookmark_host(url: str) -> str:
    parsed = urlparse(url.strip())
    return parsed.netloc or parsed.path or url

def _bookmark_display_title(bookmark: Bookmark) -> str:
    return bookmark.title or _bookmark_host(bookmark.url)

def _bookmark_preview_text(bookmark: Bookmark) -> str:
    description = (bookmark.description or "").strip()
    return _truncate_text(description or bookmark.url, 260)

def _note_display_title(note: Note) -> str:
    if note.title:
        return note.title

    first_line = next((line.strip() for line in note.content.splitlines() if line.strip()), "")
    if not first_line:
        return "Untitled note"
    return _truncate_text(first_line, 72)

def _profile_user_label(user: User | None, language: str = "en") -> str:
    if user is None:
        return translate(language, "common.guest")
    return f"@{user.username}"

def _revision_event_label(event: str) -> str:
    return "Created" if event == "created" else "Saved"

def _normalize_account_tab(tab: str | None) -> str:
    if tab in {"links", "shared", "bookmarks", "notes", "settings"}:
        return tab
    return "saved"

def _account_url(
    request: Request,
    tab: str,
    tag: str | None = None,
    *,
    search_query: str | None = None,
    created: str | date | None = None,
    month: str | date | None = None,
) -> str:
    return _build_account_redirect_url(
        request,
        tab,
        tag=tag,
        search_query=search_query,
        created=created,
        month=month,
    )

def _account_message(
    language: str,
    deleted: str | None,
    updated_tags: str | None = None,
    notice: str | None = None,
    imported_count: int | None = None,
) -> str | None:
    updated_slug = normalize_slug(updated_tags or "")
    if updated_slug:
        return translate(language, "notice.tags_updated", slug=updated_slug)

    if notice == "import_completed":
        return translate(language, "notice.import_completed", count=imported_count or 0)

    notice_key = ACCOUNT_NOTICE_KEYS.get(notice or "")
    if notice_key:
        return translate(language, notice_key)

    deleted_slug = normalize_slug(deleted or "")
    if not deleted_slug:
        return None
    return translate(language, "notice.paste_deleted", slug=deleted_slug)

def _coerce_identifier(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        coerced = int(str(value))
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None

def _normalize_url_scheme(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned or any(symbol.isspace() for symbol in cleaned):
        return None

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed.geturl()

def _search_blob(*parts: object) -> str:
    values = []
    for part in parts:
        if part is None:
            continue
        text = str(part).replace("\r\n", "\n").strip()
        if text:
            values.append(text.lower())
    return "\n".join(values)

def _item_created_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    return value.date()

def _account_tag_summary(
    request: Request,
    items: list[dict[str, object]],
    tab: str,
    *,
    search_query: str,
    active_tag: str | None,
    active_date: date | None,
    active_month: date,
) -> list[dict[str, object]]:
    counts: Counter[str] = Counter()
    for item in items:
        for tag in item.get("tags", []):
            counts[str(tag)] += 1

    return [
        {
            "tag": tag,
            "count": count,
            "url": _build_account_redirect_url(
                request,
                tab,
                tag=tag,
                search_query=search_query,
                created=active_date,
                month=active_month,
            ),
            "is_active": active_tag == tag,
        }
        for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]

def _account_calendar_context(
    request: Request,
    items: list[dict[str, object]],
    tab: str,
    *,
    search_query: str,
    active_tag: str | None,
    active_date: date | None,
    active_month: date,
    language: str,
) -> dict[str, object]:
    month_calendar = calendar.Calendar(firstweekday=0)
    item_counts: Counter[date] = Counter()
    for item in items:
        created_date = item.get("created_date")
        if isinstance(created_date, date):
            item_counts[created_date] += 1

    weeks: list[list[dict[str, object]]] = []
    for week in month_calendar.monthdatescalendar(active_month.year, active_month.month):
        week_entries: list[dict[str, object]] = []
        for day in week:
            in_current_month = day.month == active_month.month
            count = item_counts.get(day, 0)
            week_entries.append(
                {
                    "label": day.day,
                    "date_value": day.isoformat(),
                    "is_current_month": in_current_month,
                    "is_selected": active_date == day,
                    "has_items": count > 0,
                    "count": count,
                    "url": _build_account_redirect_url(
                        request,
                        tab,
                        tag=active_tag,
                        search_query=search_query,
                        created=day if in_current_month else None,
                        month=day if in_current_month else day.replace(day=1),
                    ),
                }
            )
        weeks.append(week_entries)

    previous_month = _shift_account_month(active_month, -1)
    next_month = _shift_account_month(active_month, 1)
    return {
        "label": calendar_label(active_month, language),
        "weekdays": calendar_weekdays(language),
        "weeks": weeks,
        "previous_url": _build_account_redirect_url(
            request,
            tab,
            tag=active_tag,
            search_query=search_query,
            month=previous_month,
        ),
        "next_url": _build_account_redirect_url(
            request,
            tab,
            tag=active_tag,
            search_query=search_query,
            month=next_month,
        ),
    }

def _get_bookmark_or_404(session: Session, bookmark_id: int, current_user: User) -> Bookmark:
    bookmark = session.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return bookmark

def _get_note_or_404(session: Session, note_id: int, current_user: User) -> Note:
    note = session.get(Note, note_id)
    if note is None or note.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Note not found")
    return note

def _is_owned_by_user(paste: Paste, user: User | None) -> bool:
    return user is not None and paste.owner_id == user.id

def _is_collaborator_for_user(paste: Paste, user: User | None) -> bool:
    if user is None:
        return False
    return any(collaborator.user_id == user.id for collaborator in paste.collaborators)

def _can_user_edit_paste(paste: Paste, user: User | None) -> bool:
    return _is_owned_by_user(paste, user) or _is_collaborator_for_user(paste, user)

def _can_user_delete_paste(paste: Paste, user: User | None) -> bool:
    if _is_owned_by_user(paste, user):
        return True
    return paste.owner_id is None and _is_collaborator_for_user(paste, user)

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

EXPIRE_VIEW_PRESETS = {"burn": 1, "views10": 10, "views100": 100}
EXPIRE_TIME_PRESETS = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1m": timedelta(days=30),
}

def _resolve_expiry(value: str) -> tuple[datetime | None, int | None]:
    if value in EXPIRE_VIEW_PRESETS:
        return None, EXPIRE_VIEW_PRESETS[value]
    if value in EXPIRE_TIME_PRESETS:
        return datetime.now(timezone.utc) + EXPIRE_TIME_PRESETS[value], None
    return None, None

def _is_paste_expired(paste: Paste) -> bool:
    if paste.expire_at is not None:
        expire_at = paste.expire_at
        if expire_at.tzinfo is None:
            expire_at = expire_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expire_at:
            return True
    if paste.expire_after_views is not None and paste.view_count >= paste.expire_after_views:
        return True
    return False

def _expiry_status_text(paste: Paste, language: str) -> str | None:
    if paste.expire_after_views is not None:
        remaining = max(paste.expire_after_views - paste.view_count, 0)
        return translate(language, "expires.views_remaining", count=remaining)
    if paste.expire_at is not None:
        return translate(language, "expires.at", date=paste.expire_at.strftime("%Y-%m-%d %H:%M UTC"))
    return None

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
    expires: str = "never",
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
                "expires": expires,
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
