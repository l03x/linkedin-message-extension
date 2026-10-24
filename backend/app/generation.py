"""Turn a profile + options into a draft: research the company, build the contract message, call the LLM, parse
and check its JSON, enforce limits, persist.

The user message and the JSON reply are a fixed contract the editable system prompt is written against:

    <mode>…</mode> <language>…</language> <extra_instruction>…</extra_instruction>
    <my_context>…</my_context> <target_profile>…</target_profile> <company_research>…</company_research>
    → {"strategy", "strategy_reason", "cv_match", "subject", "message", "facts_used": [{"fact", "source"}]}
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import research, store
from app.company import find_or_create, identify
from app.llm import ChatClient, LLMError, json_object
from app.modes import LANGUAGES, MODES, Mode
from app.models import SENT_STATUSES, STRATEGIES, Company, Generation

# Retries ask for this fraction of the hard limit, to leave headroom.
SHORTEN_TARGET = 0.9
# The contract's field for each limited Mode field.
CONTRACT_FIELD = {"subject": "subject", "body": "message"}

GENERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strategy", "strategy_reason", "cv_match", "subject", "message", "facts_used"],
    "properties": {
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "strategy_reason": {"type": "string"},
        "cv_match": {"type": "string"},
        "subject": {"type": ["string", "null"]},
        "message": {"type": "string"},
        "facts_used": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fact", "source"],
                "properties": {"fact": {"type": "string"}, "source": {"type": "string"}},
            },
        },
    },
}


def char_count(text: str) -> int:
    """Characters as the extension counts them: Unicode code points (JS `[...s].length`)."""
    return len(text)


@dataclass
class Draft:
    subject: str | None
    body: str
    strategy: str | None = None
    strategy_reason: str = ""
    cv_match: str = ""
    # [{"fact", "source", "kind": "url" | "profile" | "my_context" | "invalid", "verified"}]
    facts_used: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def over(self, mode: Mode) -> dict[str, tuple[int, int]]:
        """{field: (count, limit)} for every field above its hard limit."""
        values = {"subject": self.subject or "", "body": self.body}
        return {
            f: (char_count(values[f]), limit) for f, limit in mode.limits.items() if char_count(values[f]) > limit
        }


# --- reply parsing ----------------------------------------------------------------

_QUOTES = [('"', '"'), ("“", "”"), ("«", "»"), ("'", "'")]


def clean(text: str) -> str:
    """Strip whitespace and quotes wrapped around the whole message."""
    text = text.strip().replace("\r\n", "\n")
    for open_q, close_q in _QUOTES:
        if len(text) > 1 and text.startswith(open_q) and text.endswith(close_q) and text.count(open_q) <= 2:
            text = text[len(open_q) : -len(close_q)].strip()
            break
    return text


class BadReply(ValueError):
    pass


def _norm_url(url: str) -> str:
    return url.rstrip("/")


def check_facts_used(raw, research_sources: set[str]) -> tuple[list[dict], list[str]]:
    """Label each fact's source. A URL is verified only if it is one of the research sources sent."""
    known = {_norm_url(u) for u in research_sources}
    facts, warnings = [], []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("fact") or "").strip():
            continue
        fact, source = str(item["fact"]).strip(), str(item.get("source") or "").strip()
        key = source.lower().replace(" ", "_")
        if key in ("profile", "my_context"):
            facts.append({"fact": fact, "source": key, "kind": key, "verified": True})
        elif research.http_url(source):
            verified = _norm_url(source) in known
            facts.append({"fact": fact, "source": source, "kind": "url", "verified": verified})
        else:
            facts.append({"fact": fact, "source": source, "kind": "invalid", "verified": False})
    if any(f["kind"] == "url" and not f["verified"] for f in facts):
        warnings.append("The draft cites a web source that wasn't in the company research. Check it before sending.")
    if any(f["kind"] == "invalid" for f in facts):
        warnings.append("Some facts used have no valid source (a URL, profile or my_context). Check them.")
    return facts, warnings


def parse_reply(raw: str, mode: Mode, research_sources: set[str]) -> Draft:
    try:
        obj = json_object(raw)
    except ValueError as e:
        raise BadReply(str(e)) from e
    message = obj.get("message")
    if not isinstance(message, str) or not message.strip():
        raise BadReply('"message" is missing or empty')

    warnings = []
    strategy = obj.get("strategy")
    if strategy not in STRATEGIES:
        warnings.append(f"The model returned an unknown strategy ({strategy!r}); it is stored as none.")
        strategy = None
    subject = None
    if mode.has_subject:
        subject = clean(obj["subject"]) if isinstance(obj.get("subject"), str) else ""
        if not subject:
            warnings.append("The model did not return a subject line; write one yourself.")
    facts_used, fact_warnings = check_facts_used(obj.get("facts_used"), research_sources)
    return Draft(
        subject=subject,
        body=clean(message),
        strategy=strategy,
        strategy_reason=str(obj.get("strategy_reason") or "").strip(),
        cv_match=str(obj.get("cv_match") or "").strip(),
        facts_used=facts_used,
        warnings=warnings + fact_warnings,
    )


# --- prompt assembly -----------------------------------------------------------


@dataclass
class ProfileInput:
    url: str
    name: str
    headline: str
    raw_text: str
    # What the extension read as their current company: {"name", "linkedin_slug"}.
    company_hint: dict | None = None


def build_user_message(
    mode_key: str, language_key: str, extra: str, context_text: str, raw_profile: str, research_text: str
) -> str:
    """The contract's user message, exactly. Nothing else is added: wording and rules live in the system prompt."""
    return (
        f"<mode>{mode_key}</mode>\n"
        f"<language>{language_key}</language>\n"
        f"<extra_instruction>{extra.strip()}</extra_instruction>\n"
        f"<my_context>{context_text}</my_context>\n"
        f"<target_profile>{raw_profile.strip()}</target_profile>\n"
        f"<company_research>{research_text or 'none'}</company_research>"
    )


def shorten_request(over: dict[str, tuple[int, int]], mode: Mode) -> str:
    problems = "; ".join(
        f'"{CONTRACT_FIELD[f]}" is {n} characters but the hard limit is {limit} '
        f"(aim for at most {int(limit * SHORTEN_TARGET)})"
        for f, (n, limit) in over.items()
    )
    return (
        f"Too long: {problems}. Return the same JSON object with a shorter text. Keep the same language, strategy, "
        "hook and ask; cut words, not substance. Output only the JSON object."
    )


REPAIR_REQUEST = (
    "That wasn't the JSON object the instructions ask for. Reply with only one JSON object with the keys "
    '"strategy", "strategy_reason", "cv_match", "subject", "message" and "facts_used". No other text.'
)


# --- the whole flow ------------------------------------------------------------


class GenerationError(Exception):
    pass


@dataclass
class ResearchInfo:
    company: Company | None
    used: bool
    text: str
    note: str | None = None  # why research wasn't used, or a notice about it
    error: str | None = None
    researched: bool = False  # a web search ran for this request


@dataclass
class GenerationResult:
    generation: Generation
    draft: Draft
    over: dict[str, tuple[int, int]]
    previous: list[Generation]
    research: ResearchInfo


def company_hints(profile: ProfileInput) -> str:
    """Disambiguation hints for the research search. Data from the page; never a LinkedIn URL to fetch."""
    hints = [f"An employee's headline: {profile.headline}"] if profile.headline else []
    slug = (profile.company_hint or {}).get("linkedin_slug")
    if slug:
        hints.append(f"Its LinkedIn company handle: {slug}")
    return "\n".join(hints)


def stored_hints(db: Session, company: Company) -> str:
    """Hints for re-researching a company outside a generation: the headline of the last profile drafted for it."""
    from app.models import Profile

    headline = db.scalar(
        select(Profile.headline)
        .join(Generation, Generation.profile_id == Profile.id)
        .where(Generation.company_id == company.id)
        .order_by(Generation.generated_at.desc())
        .limit(1)
    )
    hint = ProfileInput(url="", name="", headline=headline or "", raw_text="", company_hint={"linkedin_slug": company.linkedin_slug})
    return company_hints(hint)


def resolve_company(db: Session, profile: ProfileInput, override: str = "") -> Company | None:
    ref = identify(profile.headline, profile.raw_text, profile.company_hint, override)
    return find_or_create(db, ref) if ref else None


def company_research(
    db: Session,
    llm: ChatClient,
    company: Company | None,
    profile: ProfileInput,
    *,
    ttl_days: int,
    wait_s: float,
    force: bool = False,
) -> ResearchInfo:
    """Research for the prompt, never raising: drafting works without it."""
    if company is None:
        return ResearchInfo(None, False, "none", note="No current company found on this profile.")
    outcome = research.ensure_research(
        db, llm, company, default_ttl_days=ttl_days, hints=company_hints(profile), force=force, wait_s=wait_s
    )
    max_age = research.ttl(db, ttl_days)
    text = research.render_research(outcome.company, max_age)
    used = text != "none"
    note = outcome.notice
    if not used and note is None:
        note = f"Research unavailable: {outcome.error}" if outcome.error else "No usable research for this company."
    return ResearchInfo(outcome.company, used, text, note=note, error=outcome.error, researched=outcome.researched)


def _call(llm: ChatClient, messages: list[dict], effort: str | None) -> str:
    return llm.complete(messages, effort, json_schema=GENERATION_SCHEMA)


def generate(
    db: Session,
    llm: ChatClient,
    profile_in: ProfileInput,
    mode_key: str,
    language_key: str,
    extra_instruction: str = "",
    *,
    company_override: str = "",
    research_ttl_days: int = 30,
    research_wait_s: float = 240.0,
) -> GenerationResult:
    mode = MODES.get(mode_key)
    language = LANGUAGES.get(language_key)
    if mode is None:
        raise GenerationError(f"Unknown mode '{mode_key}'.")
    if language is None:
        raise GenerationError(f"Unknown language '{language_key}'.")

    prompt = store.active_prompt(db)
    ctx = store.current_context(db)
    profile = store.upsert_profile(db, profile_in.url, profile_in.name, profile_in.headline, profile_in.raw_text)
    previous = list(
        db.scalars(
            select(Generation)
            .where(Generation.profile_id == profile.id, Generation.status.in_(SENT_STATUSES))
            .order_by(Generation.sent_at.desc())
        )
    )
    effort = store.get_effort(db)
    company = resolve_company(db, profile_in, company_override)
    # Commit before the (slow) LLM calls so SQLite's write lock isn't held while we wait.
    db.commit()

    info = company_research(db, llm, company, profile_in, ttl_days=research_ttl_days, wait_s=research_wait_s)
    sources = research.source_urls(info.company) if info.used else set()

    user = build_user_message(
        mode.key, language.key, extra_instruction, store.render_context(ctx), profile_in.raw_text, info.text
    )
    messages = [{"role": "system", "content": prompt.system_prompt}, {"role": "user", "content": user}]
    raw = _call(llm, messages, effort)
    attempts = 1
    try:
        draft = parse_reply(raw, mode, sources)
    except BadReply:
        messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": REPAIR_REQUEST}]
        raw = _call(llm, messages, effort)
        attempts += 1
        try:
            draft = parse_reply(raw, mode, sources)
        except BadReply as e:
            raise LLMError(
                502, "llm_bad_json", f"The model didn't return the JSON the generation contract needs ({e}). Check the system prompt."
            ) from e

    over = draft.over(mode)
    if over:
        messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": shorten_request(over, mode)}]
        raw = _call(llm, messages, effort)
        attempts += 1
        try:
            draft = parse_reply(raw, mode, sources)
        except BadReply:
            draft.warnings.append("The shortened version wasn't valid JSON, so the first draft is kept.")
        over = draft.over(mode)

    if ctx is None or ctx.is_placeholder:
        draft.warnings.append("Your context is still the placeholder. Fill in 'My context' in the dashboard.")
    if over:
        draft.warnings.append("Still over the limit after one retry: trim it before sending.")
    if info.error and info.company is not None:
        draft.warnings.append(f"Drafted without company research: {info.error}")

    gen = Generation(
        profile_id=profile.id,
        mode=mode.key,
        language=language.key,
        extra_instruction=extra_instruction.strip(),
        prompt_version_id=prompt.id,
        user_context_id=ctx.id if ctx else None,
        model=getattr(llm, "model", ""),
        draft_subject=draft.subject,
        draft_text=draft.body,
        over_limit=bool(over),
        attempts=attempts,
        strategy=draft.strategy,
        strategy_reason=draft.strategy_reason,
        cv_match=draft.cv_match,
        facts_used=draft.facts_used,
        company_id=info.company.id if info.company else None,
        research_used=info.used,
        research_snapshot=info.text,
        status="drafted",
    )
    db.add(gen)
    db.commit()
    return GenerationResult(generation=gen, draft=draft, over=over, previous=previous, research=info)

