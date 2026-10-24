from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

STATUSES = ("drafted", "sent", "replied", "no_reply")
# Statuses that mean "this message was actually sent".
SENT_STATUSES = ("sent", "replied", "no_reply")


def utcnow() -> datetime:
    """Naive UTC: SQLite has no timezone type, so every stored datetime is UTC without tzinfo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        # At most one active row; the service layer keeps it at exactly one.
        Index("uq_prompt_versions_one_active", "is_active", unique=True, sqlite_where=text("is_active = 1")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    system_prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class UserContextVersion(Base):
    __tablename__ = "user_context_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    background: Mapped[str] = mapped_column(Text, default="")
    target_role: Mapped[str] = mapped_column(Text, default="")
    target_stack: Mapped[str] = mapped_column(Text, default="")
    availability: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Settings edited in the dashboard, one row per key. A missing row means "use the .env value"."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    linkedin_url: Mapped[str] = mapped_column(String(500), unique=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    headline: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    generations: Mapped[list["Generation"]] = relationship(back_populates="profile")


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES}", name="ck_generations_status"),
        Index("ix_generations_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    mode: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(10))
    extra_instruction: Mapped[str] = mapped_column(Text, default="")
    prompt_version_id: Mapped[int] = mapped_column(ForeignKey("prompt_versions.id"))
    user_context_id: Mapped[int | None] = mapped_column(ForeignKey("user_context_versions.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(100), default="")

    draft_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_text: Mapped[str] = mapped_column(Text)
    final_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    over_limit: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(20), default="drafted")
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped[Profile] = relationship(back_populates="generations")
    prompt_version: Mapped[PromptVersion] = relationship()
