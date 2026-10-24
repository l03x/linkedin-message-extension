from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

STATUSES = ("drafted", "sent", "replied", "no_reply")
# Statuses that mean "this message was actually sent".
SENT_STATUSES = ("sent", "replied", "no_reply")
# The strategies the model picks from (the generation contract's `strategy` field).
STRATEGIES = ("hook_only", "send_cv", "ask_referral")
RESEARCH_STATUSES = ("pending", "researching", "ready", "failed")
FACT_CATEGORIES = ("product", "domain", "engineering", "news")


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


class Company(Base):
    """A recipient's current company and its cached web research. One row per company, shared by every profile."""

    __tablename__ = "companies"
    __table_args__ = (CheckConstraint(f"status IN {RESEARCH_STATUSES}", name="ck_companies_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    # Normalised name (see company.normalize_name): the cache key when no LinkedIn company slug is known.
    name_key: Mapped[str] = mapped_column(String(300), unique=True)
    # From a /company/<slug>/ link on the profile page. Only ever used as a key; never fetched.
    linkedin_slug: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    # [{"category", "fact", "source_url", "date"}]: every entry has an http(s) source (research.validate_facts).
    facts: Mapped[list] = mapped_column(JSON, default=list)
    researched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    web_searches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected_facts: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # While status is "researching": when the claim lapses (a crashed run doesn't block the company forever).
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    manually_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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

    # The generation contract's JSON fields. NULL on drafts made before the contract existed.
    strategy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    strategy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cv_match: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"fact", "source", "kind": "url" | "profile" | "my_context", "verified"}]
    facts_used: Mapped[list | None] = mapped_column(JSON, nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    # True when <company_research> held sourced facts; the exact text sent is kept in research_snapshot.
    research_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    research_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="drafted")
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped[Profile] = relationship(back_populates="generations")
    prompt_version: Mapped[PromptVersion] = relationship()
    company: Mapped[Company | None] = relationship()
