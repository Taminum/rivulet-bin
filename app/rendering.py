from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse

import bleach
import markdown
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")
SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

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


def render_code_lines(content: str, syntax: str) -> list[CodeLine]:
    lexer = _resolve_lexer(content, syntax)
    formatter = HtmlFormatter(nowrap=True)
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
