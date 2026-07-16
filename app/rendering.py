from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from urllib.parse import urlparse

import bleach
import markdown
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.styles import get_all_styles
from pygments.util import ClassNotFound

SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")
SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

WIKI_LINK_RE = re.compile(r"\[\[([a-zA-Z0-9_-]{3,64})(?:\|([^\]\n]+))?\]\]")

# "rivulet" is the hand-tuned palette shipped in styles.css; every other
# entry must exist in the installed Pygments (filtered at import time).
DEFAULT_PYGMENTS_THEME = "rivulet"
_PYGMENTS_THEME_CANDIDATES = (
    "monokai",
    "dracula",
    "nord",
    "github-dark",
    "one-dark",
    "gruvbox-dark",
    "solarized-dark",
    "solarized-light",
    "friendly",
    "vs",
)
_AVAILABLE_STYLES = set(get_all_styles())
PYGMENTS_THEMES = (DEFAULT_PYGMENTS_THEME,) + tuple(
    name for name in _PYGMENTS_THEME_CANDIDATES if name in _AVAILABLE_STYLES
)

MARKDOWN_TAGS = bleach.sanitizer.ALLOWED_TAGS.union(
    {
        "p",
        "pre",
        "code",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)

MARKDOWN_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel", "target"],
    "th": ["align"],
    "td": ["align"],
}


@dataclass(frozen=True)
class CodeLine:
    number: int
    html: Markup


def normalize_slug(value: str) -> str:
    return value.strip()


def validate_slug(value: str) -> bool:
    return bool(SLUG_PATTERN.fullmatch(value))


def generate_slug(length: int = 7) -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(length))


def generate_edit_key() -> str:
    return secrets.token_urlsafe(24)


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def decide_view_mode(content: str, requested_mode: str, syntax: str) -> tuple[bool, str]:
    if requested_mode == "link":
        return True, "link"

    if requested_mode == "markdown":
        return False, "markdown"

    if requested_mode == "code":
        return False, "code"

    if looks_like_url(content):
        return True, "link"

    if syntax == "markdown":
        return False, "markdown"

    return False, "code"


def render_markdown(content: str) -> Markup:
    rendered = markdown.markdown(
        content,
        extensions=[
            "extra",
            "nl2br",
            "sane_lists",
        ],
        output_format="html5",
    )
    cleaned = bleach.clean(
        rendered,
        tags=MARKDOWN_TAGS,
        attributes=MARKDOWN_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    return Markup(cleaned)


def render_code_lines(content: str, syntax: str, style: str = DEFAULT_PYGMENTS_THEME) -> list[CodeLine]:
    lexer = _resolve_lexer(content, syntax)
    formatter = _build_formatter(style)
    highlighted = highlight(content or " ", lexer, formatter)
    source_lines = content.split("\n") or [content]
    highlighted_lines = highlighted.split("\n")
    if len(highlighted_lines) < len(source_lines):
        highlighted_lines.extend([""] * (len(source_lines) - len(highlighted_lines)))
    if len(highlighted_lines) > len(source_lines):
        highlighted_lines = highlighted_lines[: len(source_lines)]
    if not highlighted_lines:
        highlighted_lines = [escape(content) or " "]

    return [
        CodeLine(
            number=index,
            html=Markup(
                bleach.clean(line, tags={"span", "code"}, attributes={"span": ["class"]}, strip=True) or " "
            ),
        )
        for index, line in enumerate(highlighted_lines, start=1)
    ]


def render_preview_html(content: str, syntax: str) -> Markup:
    lexer = _resolve_lexer(content, syntax)
    formatter = HtmlFormatter(nowrap=True)
    highlighted = highlight(content or " ", lexer, formatter)
    return Markup(
        bleach.clean(highlighted, tags={"span", "code"}, attributes={"span": ["class"]}, strip=True) or " "
    )


TASK_ITEM_RE = re.compile(r"^( {0,3}[-*+] \[)([ xX])(\].*)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def toggle_task_item(content: str, index: int) -> tuple[str, bool] | None:
    lines = (content or "").split("\n")
    in_fence = False
    fence_char = ""
    seen = 0
    for i, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            char = fence_match.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, char
            elif char == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue

        match = TASK_ITEM_RE.match(line)
        if not match:
            continue
        if seen == index:
            was_checked = match.group(2).lower() == "x"
            new_mark = " " if was_checked else "x"
            lines[i] = match.group(1) + new_mark + match.group(3)
            return "\n".join(lines), not was_checked
        seen += 1

    return None


def normalize_pygments_theme(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in PYGMENTS_THEMES else DEFAULT_PYGMENTS_THEME


@lru_cache(maxsize=64)
def pygments_theme_css(style: str, scope: str = ".code-view") -> str:
    # The default theme's colors live in styles.css; nothing to inject.
    if style not in PYGMENTS_THEMES or style == DEFAULT_PYGMENTS_THEME:
        return ""
    formatter = HtmlFormatter(style=style)
    # get_style_defs also emits unscoped rules (pre, td.linenos, ...) that
    # would leak outside the paste block - keep only the scoped ones.
    scoped_lines = [
        line for line in formatter.get_style_defs(scope).splitlines()
        if line.startswith(f"{scope} ")
    ]
    background = formatter.style.background_color
    if background:
        scoped_lines.insert(0, f"{scope} {{ background: {background}; }}")
    return "\n".join(scoped_lines)


def _build_formatter(style: str) -> HtmlFormatter:
    if style in _AVAILABLE_STYLES:
        return HtmlFormatter(nowrap=True, style=style)
    return HtmlFormatter(nowrap=True)


def extract_wiki_links(content: str) -> list[str]:
    seen: list[str] = []
    for match in WIKI_LINK_RE.finditer(content or ""):
        slug = match.group(1)
        if slug not in seen:
            seen.append(slug)
    return seen


def _resolve_lexer(content: str, syntax: str):
    if syntax == "text":
        return TextLexer(stripnl=False)

    if syntax not in {"", "auto"}:
        try:
            return get_lexer_by_name(syntax, stripnl=False)
        except ClassNotFound:
            return TextLexer(stripnl=False)

    try:
        return guess_lexer(content[:10000] or "text")
    except ClassNotFound:
        return TextLexer(stripnl=False)
