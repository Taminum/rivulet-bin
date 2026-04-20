from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    site_name: str
    tagline: str
    database_url: str
    secret_salt: str
    max_content_size: int
    reserved_slugs: frozenset[str]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    reserved_slugs = frozenset(
        {
            "about",
            "account",
            "api",
            "docs",
            "edit",
            "healthz",
            "login",
            "logout",
            "openapi.json",
            "publish",
            "raw",
            "register",
            "static",
            "success",
        }
    )
    return Settings(
        site_name=os.getenv("SITE_NAME", "Rivulet Bin"),
        tagline=os.getenv(
            "SITE_TAGLINE",
            "Create pastes, publish notes, and shorten links without the clutter.",
        ),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./pastebin.db"),
        secret_salt=os.getenv("SECRET_SALT", "change-me-in-production"),
        max_content_size=int(os.getenv("MAX_CONTENT_SIZE", "200000")),
        reserved_slugs=reserved_slugs,
    )
