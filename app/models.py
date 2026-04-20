from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    owned_pastes: Mapped[list["Paste"]] = relationship(
        back_populates="owner",
        foreign_keys="Paste.owner_id",
    )
    created_pastes: Mapped[list["Paste"]] = relationship(
        back_populates="creator",
        foreign_keys="Paste.creator_id",
    )
    edited_pastes: Mapped[list["Paste"]] = relationship(
        back_populates="last_editor",
        foreign_keys="Paste.last_editor_id",
    )
    collaborator_memberships: Mapped[list["PasteCollaborator"]] = relationship(
        back_populates="user",
        foreign_keys="PasteCollaborator.user_id",
    )
    revision_authorships: Mapped[list["PasteRevision"]] = relationship(
        back_populates="editor",
        foreign_keys="PasteRevision.editor_id",
    )


class Paste(Base):
    __tablename__ = "pastes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tags_json: Mapped[str] = mapped_column(Text(), default="[]")
    content: Mapped[str] = mapped_column(Text())
    syntax: Mapped[str] = mapped_column(String(40), default="auto")
    view_mode: Mapped[str] = mapped_column(String(20), default="code")
    is_url: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_key: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    last_editor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped[User | None] = relationship(
        back_populates="owned_pastes",
        foreign_keys=[owner_id],
    )
    creator: Mapped[User | None] = relationship(
        back_populates="created_pastes",
        foreign_keys=[creator_id],
    )
    last_editor: Mapped[User | None] = relationship(
        back_populates="edited_pastes",
        foreign_keys=[last_editor_id],
    )
    collaborators: Mapped[list["PasteCollaborator"]] = relationship(
        back_populates="paste",
        cascade="all, delete-orphan",
    )
    revisions: Mapped[list["PasteRevision"]] = relationship(
        back_populates="paste",
        cascade="all, delete-orphan",
        order_by=lambda: PasteRevision.revision_number.desc(),
    )


class PasteCollaborator(Base):
    __tablename__ = "paste_collaborators"
    __table_args__ = (
        UniqueConstraint("paste_id", "user_id", name="uq_paste_collaborators_paste_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    paste_id: Mapped[int] = mapped_column(ForeignKey("pastes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    paste: Mapped[Paste] = relationship(back_populates="collaborators")
    user: Mapped[User] = relationship(back_populates="collaborator_memberships")


class PasteRevision(Base):
    __tablename__ = "paste_revisions"
    __table_args__ = (
        UniqueConstraint("paste_id", "revision_number", name="uq_paste_revisions_paste_revision_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    paste_id: Mapped[int] = mapped_column(ForeignKey("pastes.id"), index=True)
    revision_number: Mapped[int] = mapped_column(index=True)
    event: Mapped[str] = mapped_column(String(24), default="saved")
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content: Mapped[str] = mapped_column(Text())
    syntax: Mapped[str] = mapped_column(String(40), default="auto")
    view_mode: Mapped[str] = mapped_column(String(20), default="code")
    editor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    paste: Mapped[Paste] = relationship(back_populates="revisions")
    editor: Mapped[User | None] = relationship(back_populates="revision_authorships")
