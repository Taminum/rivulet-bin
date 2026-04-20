from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import urlparse

AUTH_COOKIE_NAME = "rivulet_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
PASSWORD_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    salt_value = base64.urlsafe_b64encode(salt).decode("utf-8").rstrip("=")
    hash_value = base64.urlsafe_b64encode(derived).decode("utf-8").rstrip("=")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt_value}${hash_value}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_value, hash_value = password_hash.split("$", maxsplit=3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        iteration_count = int(iterations)
        salt = _urlsafe_b64decode(salt_value)
        expected_hash = _urlsafe_b64decode(hash_value)
    except (TypeError, ValueError):
        return False

    actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iteration_count)
    return hmac.compare_digest(actual_hash, expected_hash)


def create_session_token(user_id: int, secret_salt: str, issued_at: int | None = None) -> str:
    issued_at = issued_at or int(time.time())
    payload = f"{user_id}:{issued_at}"
    signature = _sign_value(payload, secret_salt)
    encoded = base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("utf-8")
    return encoded.rstrip("=")


def read_session_token(token: str | None, secret_salt: str) -> int | None:
    if not token:
        return None

    try:
        decoded = _urlsafe_b64decode(token).decode("utf-8")
        user_id_value, issued_at_value, signature = decoded.split(":", maxsplit=2)
        user_id = int(user_id_value)
        issued_at = int(issued_at_value)
    except (TypeError, ValueError):
        return None

    payload = f"{user_id}:{issued_at}"
    expected_signature = _sign_value(payload, secret_salt)
    if not hmac.compare_digest(signature, expected_signature):
        return None

    if issued_at + SESSION_TTL_SECONDS < int(time.time()):
        return None

    return user_id


def normalize_next_path(next_path: str | None, fallback: str = "/account") -> str:
    if not next_path:
        return fallback

    parsed = urlparse(next_path)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not next_path.startswith("/") or next_path.startswith("//"):
        return fallback
    return next_path


def _sign_value(value: str, secret_salt: str) -> str:
    return hmac.new(secret_salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _urlsafe_b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))
