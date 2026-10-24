"""Prompt/context versioning, profile upserts and generation status changes, shared by the API and the dashboard."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.llm import EFFORTS
from app.models import (
    SENT_STATUSES,
    AppSetting,
    Company,
    Generation,
    PromptVersion,
    Profile,
    UserContextVersion,
    utcnow,
)

CONTEXT_FIELDS = ("background", "target_role", "target_stack", "availability", "location", "notes")


class NotFound(Exception):
    pass


# --- prompts -----------------------------------------------------------------


def active_prompt(db: Session) -> PromptVersion:
    prompt = db.scalar(select(PromptVersion).where(PromptVersion.is_active.is_(True)))
    if prompt is None:
        raise NotFound("No active prompt version.")
    return prompt


def list_prompts(db: Session) -> list[PromptVersion]:
    return list(db.scalars(select(PromptVersion).order_by(PromptVersion.id.desc())))


def get_prompt(db: Session, prompt_id: int) -> PromptVersion:
    prompt = db.get(PromptVersion, prompt_id)
    if prompt is None:
        raise NotFound(f"Prompt version {prompt_id} not found.")
    return prompt


def create_prompt(db: Session, name: str, system_prompt: str, activate: bool = True) -> PromptVersion:
    """Always inserts a new version; existing versions are never modified (except the active flag)."""
    name = name.strip() or f"v{(db.scalar(select(func.count(PromptVersion.id))) or 0) + 1}"
    prompt = PromptVersion(name=name, system_prompt=system_prompt, is_active=False)
    db.add(prompt)
    db.flush()
    if activate:
        _set_active(db, prompt.id)
    db.commit()
    return prompt


def activate_prompt(db: Session, prompt_id: int) -> PromptVersion:
    prompt = get_prompt(db, prompt_id)
    _set_active(db, prompt.id)
    db.commit()
    db.refresh(prompt)
    return prompt


def _set_active(db: Session, prompt_id: int) -> None:
    # Clear first, then set, so the partial unique index never sees two active rows.
    db.execute(update(PromptVersion).where(PromptVersion.is_active.is_(True)).values(is_active=False))
    db.flush()
    db.execute(update(PromptVersion).where(PromptVersion.id == prompt_id).values(is_active=True))
    db.flush()
    db.expire_all()


# --- user context --------------------------------------------------------------


def current_context(db: Session) -> UserContextVersion | None:
    return db.scalar(select(UserContextVersion).order_by(UserContextVersion.id.desc()).limit(1))


def list_contexts(db: Session) -> list[UserContextVersion]:
    return list(db.scalars(select(UserContextVersion).order_by(UserContextVersion.id.desc())))


def save_context(db: Session, **fields: str) -> UserContextVersion:
    ctx = UserContextVersion(**{k: (fields.get(k) or "").strip() for k in CONTEXT_FIELDS}, is_placeholder=False)
    db.add(ctx)
    db.commit()
    return ctx


def render_context(ctx: UserContextVersion | None) -> str:
    if ctx is None:
        return "(no context provided)"
    labels = {
        "background": "Background",
        "target_role": "Target role",
        "target_stack": "Stack",
        "availability": "Availability / dates",
        "location": "Location",
        "notes": "Notes",
    }
    parts = [f"{labels[k]}: {getattr(ctx, k)}" for k in CONTEXT_FIELDS if getattr(ctx, k)]
    return "\n".join(parts) or "(no context provided)"


# --- settings ------------------------------------------------------------------

EFFORT_KEY = "llm_effort"


def get_effort(db: Session) -> str | None:
    """The effort picked in the dashboard: "" means don't send one, None means never set (use LLM_EFFORT)."""
    row = db.get(AppSetting, EFFORT_KEY)
    return row.value if row else None


def set_effort(db: Session, effort: str) -> None:
    if effort and effort not in EFFORTS:
        raise ValueError(f"Unknown effort '{effort}'. Use one of: {', '.join(EFFORTS)}, or empty.")
    row = db.get(AppSetting, EFFORT_KEY)
    if row is None:
        db.add(AppSetting(key=EFFORT_KEY, value=effort))
    else:
        row.value = effort
    db.commit()


RESEARCH_TTL_KEY = "research_ttl_days"
MAX_RESEARCH_TTL_DAYS = 3650


def get_research_ttl_days(db: Session, default: int) -> int:
    """Days company research stays fresh: the dashboard's value, else RESEARCH_TTL_DAYS."""
    row = db.get(AppSetting, RESEARCH_TTL_KEY)
    return int(row.value) if row and row.value.isdigit() else default


def set_research_ttl_days(db: Session, days: int) -> None:
    if not 1 <= days <= MAX_RESEARCH_TTL_DAYS:
        raise ValueError(f"Research expiry must be between 1 and {MAX_RESEARCH_TTL_DAYS} days.")
    row = db.get(AppSetting, RESEARCH_TTL_KEY)
    if row is None:
        db.add(AppSetting(key=RESEARCH_TTL_KEY, value=str(days)))
    else:
        row.value = str(days)
    db.commit()


# --- profiles ------------------------------------------------------------------


def upsert_profile(db: Session, url: str, name: str, headline: str, raw_text: str) -> Profile:
    now = utcnow()
    profile = db.scalar(select(Profile).where(Profile.linkedin_url == url))
    if profile is None:
        profile = Profile(linkedin_url=url, first_seen=now)
        db.add(profile)
    profile.name = name or profile.name or ""
    profile.headline = headline or profile.headline or ""
    profile.raw_text = raw_text
    profile.last_seen = now
    db.flush()
    return profile


# --- companies -----------------------------------------------------------------


def list_companies(db: Session) -> list[Company]:
    return list(db.scalars(select(Company).order_by(func.lower(Company.name))))


def companies_with_generations(db: Session) -> list[Company]:
    """Companies that at least one draft was written for (for the History filter)."""
    ids = select(Generation.company_id).where(Generation.company_id.is_not(None)).distinct()
    return list(db.scalars(select(Company).where(Company.id.in_(ids)).order_by(func.lower(Company.name))))


# --- people ---------------------------------------------------------------------


@dataclass
class PersonRow:
    profile: Profile
    drafts: int  # every generation, sent or not
    sent: int
    replied: int
    awaiting: int
    last_sent_at: datetime | None
    last_status: str  # of their most recent sent message, or "drafted" if none was sent
    last_mode: str
    company: Company | None  # from their most recent draft that had one


def list_people(db: Session, include_unsent: bool = False, search: str = "") -> list[PersonRow]:
    """Everyone you drafted for, most recently contacted first. Without `include_unsent`, only people you
    actually sent a message to."""
    q = (
        select(Generation)
        .options(joinedload(Generation.profile), joinedload(Generation.company))
        .order_by(func.coalesce(Generation.sent_at, Generation.generated_at).desc(), Generation.id.desc())
    )
    people: dict[int, PersonRow] = {}
    for g in db.scalars(q):
        row = people.get(g.profile_id)
        if row is None:
            row = people[g.profile_id] = PersonRow(g.profile, 0, 0, 0, 0, None, "drafted", g.mode, None)
        row.drafts += 1
        if row.company is None and g.company is not None:
            row.company = g.company
        if g.status in SENT_STATUSES:
            if row.sent == 0:  # newest first, so the first sent one is the latest
                row.last_sent_at, row.last_status, row.last_mode = g.sent_at, g.status, g.mode
            row.sent += 1
            row.replied += g.status == "replied"
            row.awaiting += g.status == "sent"
    rows = [r for r in people.values() if include_unsent or r.sent]
    needle = search.strip().casefold()
    if needle:
        rows = [
            r
            for r in rows
            if needle in (r.profile.name or "").casefold()
            or needle in (r.profile.headline or "").casefold()
            or (r.company is not None and needle in r.company.name.casefold())
        ]
    return rows


# --- generations ---------------------------------------------------------------


class Conflict(Exception):
    pass


def get_generation(db: Session, generation_id: int) -> Generation:
    gen = db.get(Generation, generation_id)
    if gen is None:
        raise NotFound(f"Generation {generation_id} not found.")
    return gen


def mark_sent(db: Session, gen: Generation, final_text: str, final_subject: str | None = None) -> Generation:
    """Store the text actually sent. Re-marking updates the text but keeps the first sent_at."""
    gen.final_text = final_text.strip()
    gen.final_subject = final_subject.strip() if final_subject is not None else None
    if gen.status == "drafted":
        gen.status = "sent"
    if gen.sent_at is None:
        gen.sent_at = utcnow()
    db.commit()
    return gen


def set_status(db: Session, gen: Generation, status: str, replied_at: datetime | None = None) -> Generation:
    if gen.status == "drafted" or gen.sent_at is None:
        raise Conflict("Mark the message as sent before recording a reply.")
    if status not in SENT_STATUSES:
        raise Conflict(f"Unsupported status '{status}'.")
    gen.status = status
    if status == "replied":
        if replied_at is not None and replied_at.tzinfo is not None:
            replied_at = replied_at.astimezone(timezone.utc).replace(tzinfo=None)
        # A reply can't predate the message (e.g. a same-day date picked in the dashboard).
        gen.replied_at = max(replied_at or utcnow(), gen.sent_at)
    else:
        gen.replied_at = None
    db.commit()
    return gen


@dataclass
class HistoryFilters:
    mode: str | None = None
    language: str | None = None
    status: str | None = None
    prompt_version_id: int | None = None
    # UTC bounds on sent_at (or generated_at for drafts); end is exclusive.
    start: datetime | None = None
    end: datetime | None = None
    include_drafts: bool = False
    strategy: str | None = None  # "none" = drafts without a strategy (made before the generation contract)
    company_id: int | None = None
    profile_id: int | None = None


def query_generations(db: Session, f: HistoryFilters, limit: int = 500) -> list[Generation]:
    when = func.coalesce(Generation.sent_at, Generation.generated_at)
    q = select(Generation).options(
        joinedload(Generation.profile), joinedload(Generation.prompt_version), joinedload(Generation.company)
    )
    if f.mode:
        q = q.where(Generation.mode == f.mode)
    if f.language:
        q = q.where(Generation.language == f.language)
    if f.status:
        q = q.where(Generation.status == f.status)
    elif not f.include_drafts:
        q = q.where(Generation.status != "drafted")
    if f.prompt_version_id:
        q = q.where(Generation.prompt_version_id == f.prompt_version_id)
    if f.strategy == "none":
        q = q.where(Generation.strategy.is_(None))
    elif f.strategy:
        q = q.where(Generation.strategy == f.strategy)
    if f.company_id:
        q = q.where(Generation.company_id == f.company_id)
    if f.profile_id:
        q = q.where(Generation.profile_id == f.profile_id)
    if f.start:
        q = q.where(when >= f.start)
    if f.end:
        q = q.where(when < f.end)
    return list(db.scalars(q.order_by(when.desc(), Generation.id.desc()).limit(limit)))
