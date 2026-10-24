"""Prompt/context versioning, profile upserts and generation status changes, shared by the API and the dashboard."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.llm import EFFORTS
from app.models import SENT_STATUSES, AppSetting, Generation, PromptVersion, Profile, UserContextVersion, utcnow

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


def query_generations(db: Session, f: HistoryFilters, limit: int = 500) -> list[Generation]:
    when = func.coalesce(Generation.sent_at, Generation.generated_at)
    q = select(Generation).options(joinedload(Generation.profile), joinedload(Generation.prompt_version))
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
    if f.start:
        q = q.where(when >= f.start)
    if f.end:
        q = q.where(when < f.end)
    return list(db.scalars(q.order_by(when.desc(), Generation.id.desc()).limit(limit)))
