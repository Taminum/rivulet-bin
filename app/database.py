from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

engine_kwargs: dict[str, object] = {
    "pool_pre_ping": True,
}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_database() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_runtime_schema()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _ensure_runtime_schema() -> None:
    inspector = inspect(engine)
    user_columns = {column["name"] for column in inspector.get_columns("users")} if inspector.has_table("users") else set()
    if not inspector.has_table("pastes"):
        with engine.begin() as connection:
            if "preferred_language" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN preferred_language TEXT DEFAULT 'en'"))
            if "theme_preference" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN theme_preference TEXT DEFAULT 'dark'"))
            connection.execute(text("UPDATE users SET preferred_language = 'en' WHERE preferred_language IS NULL"))
            connection.execute(text("UPDATE users SET theme_preference = 'dark' WHERE theme_preference IS NULL"))
        return

    paste_columns = {column["name"] for column in inspector.get_columns("pastes")}
    with engine.begin() as connection:
        if "preferred_language" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN preferred_language TEXT DEFAULT 'en'"))
        if "theme_preference" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN theme_preference TEXT DEFAULT 'dark'"))
        connection.execute(text("UPDATE users SET preferred_language = 'en' WHERE preferred_language IS NULL"))
        connection.execute(text("UPDATE users SET theme_preference = 'dark' WHERE theme_preference IS NULL"))
        if "tags_json" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN tags_json TEXT DEFAULT '[]'"))
        if "owner_id" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN owner_id INTEGER"))
        if "creator_id" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN creator_id INTEGER"))
        if "last_editor_id" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN last_editor_id INTEGER"))
        if "expire_at" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN expire_at TIMESTAMP"))
        if "expire_after_views" not in paste_columns:
            connection.execute(text("ALTER TABLE pastes ADD COLUMN expire_after_views INTEGER"))
        connection.execute(text("UPDATE pastes SET tags_json = '[]' WHERE tags_json IS NULL"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_pastes_owner_id ON pastes (owner_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_pastes_creator_id ON pastes (creator_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_pastes_last_editor_id ON pastes (last_editor_id)"))
        connection.execute(
            text(
                "UPDATE pastes "
                "SET creator_id = owner_id "
                "WHERE creator_id IS NULL AND owner_id IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE pastes "
                "SET last_editor_id = owner_id "
                "WHERE last_editor_id IS NULL AND owner_id IS NOT NULL"
            )
        )
