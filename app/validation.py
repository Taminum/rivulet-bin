from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import sqlglot
import yaml
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for
from tree_sitter_languages import get_parser


@dataclass(frozen=True)
class ValidationIssue:
    message: str
    line: int | None = None
    column: int | None = None

    def to_message(self, syntax: str) -> str:
        prefix = f"{syntax.upper()} validation error"
        if self.line is None or self.column is None:
            return f"{prefix}: {self.message}"
        return f"{prefix} at line {self.line}, column {self.column}: {self.message}"


TREE_SITTER_LANGUAGE_MAP = {
    "bash": "bash",
    "css": "css",
    "html": "html",
    "javascript": "javascript",
    "typescript": "typescript",
}

SCHEMA_LIKE_KEYS = {"$schema", "$defs", "definitions", "properties", "items"}
SQL_START_RE = re.compile(r"^\s*(select|with|insert|update|delete|create|alter|drop)\b", re.IGNORECASE)
HTML_START_RE = re.compile(r"^\s*<(?:!doctype|html|head|body|div|span|script|style|main|section|article|\w+-\w+)", re.IGNORECASE)
CSS_RE = re.compile(r"(^|})\s*[^{}\n]+?\{\s*[^{}:;\n]+:\s*[^{};\n]+;\s*[^{}]*\}", re.DOTALL)
YAML_RE = re.compile(r"^\s*[\w\"'\-]+\s*:\s*.+$", re.MULTILINE)
PYTHON_RE = re.compile(r"^\s*(def |class |from |import |async def |if __name__ == ['\"]__main__['\"]:)", re.MULTILINE)
BASH_RE = re.compile(r"^\s*(#!/bin/(ba)?sh|echo\b|export\b|if \[|for\b|while\b|case\b)", re.MULTILINE)
TYPESCRIPT_RE = re.compile(r"\b(interface|type|enum|implements|readonly|public|private|protected)\b|:\s*(string|number|boolean|unknown|never|void|Record<|Promise<|Array<)")
JAVASCRIPT_RE = re.compile(r"\b(const|let|var|function|console\.|document\.|window\.|import\s|export\s)\b|=>")
TEXT_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def validate_content(content: str, syntax: str) -> ValidationIssue | None:
    if syntax in {"auto", "text", "markdown"}:
        return None

    validator = VALIDATORS.get(syntax)
    if validator is None:
        return None
    return validator(content)


def detect_syntax(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return "auto"

    if _looks_like_url_text(stripped):
        return "text"

    if _looks_like_json(stripped):
        return "json"

    if _looks_like_yaml(stripped):
        return "yaml"

    if HTML_START_RE.search(stripped):
        return "html"

    if SQL_START_RE.search(stripped):
        return "sql"

    if BASH_RE.search(stripped):
        return "bash"

    if PYTHON_RE.search(stripped):
        return "python"

    if TYPESCRIPT_RE.search(stripped):
        return "typescript"

    if JAVASCRIPT_RE.search(stripped):
        return "javascript"

    if _looks_like_css(stripped):
        return "css"

    if _looks_like_plain_text(stripped):
        return "text"

    return "auto"


def _validate_python(content: str) -> ValidationIssue | None:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return ValidationIssue(
            message=exc.msg or "Invalid Python syntax.",
            line=exc.lineno,
            column=exc.offset,
        )
    return None


def _validate_json(content: str) -> ValidationIssue | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return ValidationIssue(message=exc.msg, line=exc.lineno, column=exc.colno)
    return _validate_schema_document(data)


def _validate_yaml(content: str) -> ValidationIssue | None:
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        mark = _select_yaml_error_mark(exc)
        return ValidationIssue(
            message=getattr(exc, "problem", None) or str(exc),
            line=(mark.line + 1) if mark else None,
            column=(mark.column + 1) if mark else None,
        )

    for document in documents:
        schema_issue = _validate_schema_document(document)
        if schema_issue:
            return schema_issue
    return None


def _validate_sql(content: str) -> ValidationIssue | None:
    try:
        sqlglot.parse(content)
    except sqlglot.errors.ParseError as exc:
        if exc.errors:
            first_error = exc.errors[0]
            return ValidationIssue(
                message=first_error.get("description", str(exc)),
                line=first_error.get("line"),
                column=first_error.get("col"),
            )
        return ValidationIssue(message=str(exc))
    return None


def _validate_tree_sitter(content: str, syntax: str) -> ValidationIssue | None:
    parser = _get_tree_sitter_parser(syntax)
    if parser is None:
        return None

    tree = parser.parse(content.encode("utf-8"))
    error_node = _find_error_node(tree.root_node)
    if error_node is None:
        return None

    line, column = error_node.start_point
    return ValidationIssue(
        message="Invalid syntax.",
        line=line + 1,
        column=column + 1,
    )


def _validate_schema_document(data: Any) -> ValidationIssue | None:
    if not _looks_like_json_schema(data):
        return None

    try:
        validator = validator_for(data)
        validator.check_schema(data)
    except jsonschema_exceptions.SchemaError as exc:
        path = "$" + "".join(f"[{repr(part)}]" for part in exc.absolute_path)
        return ValidationIssue(message=f"Schema is invalid at {path}: {exc.message}")
    return None


def _looks_like_json_schema(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if "$schema" in data:
        return True
    if "type" in data and ("properties" in data or "items" in data):
        return True
    return any(key in data for key in {"$defs", "definitions"})


def _looks_like_json(content: str) -> bool:
    return bool(content) and content[0] in "{["


def _looks_like_url_text(content: str) -> bool:
    if any(symbol.isspace() for symbol in content):
        return False
    parsed = urlparse(content)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_yaml(content: str) -> bool:
    if content.startswith("---"):
        return True
    if content.startswith("{") or content.startswith("["):
        return False
    if _looks_like_url_text(content):
        return False
    return bool(YAML_RE.search(content))


def _looks_like_css(content: str) -> bool:
    if content.startswith("{") or content.startswith("["):
        return False
    if "<" in content or "function" in content:
        return False
    return bool(CSS_RE.search(content))


def _looks_like_plain_text(content: str) -> bool:
    if len(content) < 120:
        return False

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    word_count = len(TEXT_WORD_RE.findall(content))
    if word_count < 18:
        return False

    code_symbol_count = sum(content.count(symbol) for symbol in "{}[]();<>`$\\")
    if code_symbol_count > max(6, len(content) // 100):
        return False

    sentence_punctuation_count = sum(content.count(symbol) for symbol in ".!?")
    long_text_lines = sum(1 for line in lines if len(TEXT_WORD_RE.findall(line)) >= 5)

    has_text_shape = len(lines) >= 3 or sentence_punctuation_count >= 2
    return has_text_shape and long_text_lines >= 1


def _select_yaml_error_mark(exc: yaml.YAMLError):
    problem = (getattr(exc, "problem", None) or "").lower()
    context = (getattr(exc, "context", None) or "").lower()
    context_mark = getattr(exc, "context_mark", None)
    problem_mark = getattr(exc, "problem_mark", None)

    # PyYAML often reports a missing ":" on the next valid line.
    # In that case the actionable location is the simple key it was scanning.
    if problem == "could not find expected ':'" and context == "while scanning a simple key" and context_mark:
        return context_mark

    return problem_mark or context_mark


def _find_error_node(node):
    if node.type == "ERROR" or getattr(node, "is_missing", False):
        return node
    if not node.has_error:
        return None
    for child in node.children:
        found = _find_error_node(child)
        if found is not None:
            return found
    return None


@lru_cache(maxsize=None)
def _get_tree_sitter_parser(syntax: str):
    language_name = TREE_SITTER_LANGUAGE_MAP.get(syntax)
    if language_name is None:
        return None
    return get_parser(language_name)


VALIDATORS = {
    "python": _validate_python,
    "javascript": lambda content: _validate_tree_sitter(content, "javascript"),
    "typescript": lambda content: _validate_tree_sitter(content, "typescript"),
    "json": _validate_json,
    "bash": lambda content: _validate_tree_sitter(content, "bash"),
    "html": lambda content: _validate_tree_sitter(content, "html"),
    "css": lambda content: _validate_tree_sitter(content, "css"),
    "sql": _validate_sql,
    "yaml": _validate_yaml,
}
