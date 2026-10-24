from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import store
from app.cv_import import MAX_UPLOAD_BYTES, import_cv
from app.auth import require_bearer
from app.config import Settings
from app.deps import get_db, get_llm, get_settings
from app import research
from app.generation import GenerationError, ProfileInput, company_research, generate, resolve_company, stored_hints
from app.llm import EFFORTS, ChatClient
from app.modes import MODES, options_payload
from app.models import Company, utcnow
from app.schemas import (
    CompanyEdit,
    CompanyOut,
    CompanyRefresh,
    ContextIn,
    ContextOut,
    CVImportOut,
    GenerateRequest,
    GenerateResponse,
    GenerationOut,
    ModelSettings,
    ModelSettingsIn,
    PreviousContact,
    PromptCreate,
    PromptOut,
    ResearchRequest,
    ResearchResponse,
    SentRequest,
    StatusRequest,
)

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@public.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/me")
def me(settings: Settings = Depends(get_settings)) -> dict:
    return {"ok": True, "model": settings.llm_model}


@router.get("/settings", response_model=ModelSettings)
def get_model_settings(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    effort = store.get_effort(db)
    return ModelSettings(
        model=settings.llm_model,
        effort=settings.llm_effort.strip() if effort is None else effort,
        efforts=list(EFFORTS),
        from_env=effort is None,
    )


@router.put("/settings", response_model=ModelSettings)
def put_model_settings(body: ModelSettingsIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    try:
        store.set_effort(db, body.effort.strip())
    except ValueError as e:
        raise _error(422, "bad_effort", str(e)) from e
    return get_model_settings(db, settings)


@router.get("/options")
def options() -> dict:
    return options_payload()


# --- generation ------------------------------------------------------------------


def _profile_input(p) -> ProfileInput:
    hint = p.company.model_dump() if p.company else None
    return ProfileInput(url=p.url, name=p.name, headline=p.headline, raw_text=p.raw_text, company_hint=hint)


def research_wait_s(settings: Settings) -> float:
    """How long a request waits for another request's search of the same company: one LLM call, plus margin."""
    return settings.llm_timeout_seconds + 30


def company_out(db: Session, company: Company, settings: Settings) -> CompanyOut:
    max_age = research.ttl(db, settings.research_ttl_days)
    since = company.edited_at if company.manually_edited and company.edited_at else company.researched_at
    return CompanyOut(
        id=company.id,
        name=company.name,
        website=company.website,
        linkedin_slug=company.linkedin_slug,
        status=company.status,
        facts=company.facts or [],
        researched_at=company.researched_at,
        age_days=(utcnow() - since).days if since else None,
        fresh=research.usable(company, max_age),
        manually_edited=company.manually_edited,
        edited_at=company.edited_at,
        last_error=company.last_error,
        next_retry_at=company.next_retry_at,
        web_searches=company.web_searches,
        rejected_facts=company.rejected_facts or 0,
    )


@router.post("/research", response_model=ResearchResponse)
def research_company(
    body: ResearchRequest,
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    """Make sure the profile's company is researched (cached when fresh). The panel calls this before /generate
    so it can show the two phases; /generate does the same on its own and then hits the cache."""
    profile = _profile_input(body.profile)
    company = resolve_company(db, profile, body.company_override)
    db.commit()
    info = company_research(
        db,
        llm,
        company,
        profile,
        ttl_days=settings.research_ttl_days,
        wait_s=research_wait_s(settings),
        force=body.force,
    )
    if info.company is None:
        return ResearchResponse(company=None, used=False, researched=False, error=None, notice=info.note)
    return ResearchResponse(
        company=company_out(db, info.company, settings),
        used=info.used,
        researched=info.researched,
        error=info.error,
        notice=info.note,
    )


@router.post("/generate", response_model=GenerateResponse)
def generate_draft(
    body: GenerateRequest,
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    try:
        result = generate(
            db,
            llm,
            _profile_input(body.profile),
            body.mode,
            body.language,
            body.extra_instruction,
            company_override=body.company_override,
            research_ttl_days=settings.research_ttl_days,
            research_wait_s=research_wait_s(settings),
        )
    except GenerationError as e:
        raise _error(400, "bad_request", str(e)) from e
    except store.NotFound as e:
        raise _error(500, "no_active_prompt", str(e)) from e
    gen, draft, info = result.generation, result.draft, result.research
    return GenerateResponse(
        generation_id=gen.id,
        mode=gen.mode,
        language=gen.language,
        subject=draft.subject,
        body=draft.body,
        limits=MODES[gen.mode].limits,
        over_limit=gen.over_limit,
        over={f: [n, limit] for f, (n, limit) in result.over.items()},
        attempts=gen.attempts,
        prompt_version={"id": gen.prompt_version_id, "name": gen.prompt_version.name},
        warnings=draft.warnings,
        previous=[
            PreviousContact(generation_id=g.id, mode=g.mode, status=g.status, sent_at=g.sent_at)
            for g in result.previous
        ],
        strategy=draft.strategy,
        strategy_reason=draft.strategy_reason,
        cv_match=draft.cv_match,
        facts_used=draft.facts_used,
        company=company_out(db, info.company, settings) if info.company else None,
        research_used=info.used,
        research_note=info.note,
    )


def _generation(db: Session, generation_id: int):
    try:
        return store.get_generation(db, generation_id)
    except store.NotFound as e:
        raise _error(404, "not_found", str(e)) from e


@router.post("/generations/{generation_id}/sent", response_model=GenerationOut)
def mark_sent(generation_id: int, body: SentRequest, db: Session = Depends(get_db)):
    gen = _generation(db, generation_id)
    if MODES[gen.mode].has_subject and body.final_subject is None:
        raise _error(400, "bad_request", "An InMail needs final_subject.")
    return store.mark_sent(db, gen, body.final_text, body.final_subject)


@router.post("/generations/{generation_id}/status", response_model=GenerationOut)
def set_status(generation_id: int, body: StatusRequest, db: Session = Depends(get_db)):
    gen = _generation(db, generation_id)
    try:
        return store.set_status(db, gen, body.status, body.replied_at)
    except store.Conflict as e:
        raise _error(409, "conflict", str(e)) from e


@router.get("/generations", response_model=list[GenerationOut])
def list_generations(
    mode: str | None = None,
    language: str | None = None,
    status: str | None = None,
    prompt_version_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_drafts: bool = False,
    strategy: str | None = None,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    f = history_filters(settings, mode, language, status, prompt_version_id, date_from, date_to, include_drafts)
    f.strategy, f.company_id = strategy or None, company_id or None
    return store.query_generations(db, f)


def history_filters(
    settings: Settings,
    mode: str | None,
    language: str | None,
    status: str | None,
    prompt_version_id: int | None,
    date_from: date | None,
    date_to: date | None,
    include_drafts: bool,
) -> store.HistoryFilters:
    """Local calendar dates (inclusive) → UTC bounds."""
    tz = ZoneInfo(settings.timezone)

    def to_utc(d: date) -> datetime:
        return datetime.combine(d, time.min, tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    return store.HistoryFilters(
        mode=mode or None,
        language=language or None,
        status=status or None,
        prompt_version_id=prompt_version_id or None,
        start=to_utc(date_from) if date_from else None,
        end=to_utc(date_to + timedelta(days=1)) if date_to else None,
        include_drafts=include_drafts,
    )


# --- companies -----------------------------------------------------------------------


def _company(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise _error(404, "not_found", f"Company {company_id} not found.")
    return company


@router.get("/companies", response_model=list[CompanyOut])
def list_companies(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return [company_out(db, c, settings) for c in store.list_companies(db)]


@router.get("/companies/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return company_out(db, _company(db, company_id), settings)


@router.put("/companies/{company_id}", response_model=CompanyOut)
def edit_company(
    company_id: int, body: CompanyEdit, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    """Replace the research with your own version. It is then never refreshed automatically."""
    company = _company(db, company_id)
    problems = research.save_manual(db, company, [f.model_dump() for f in body.facts], body.website)
    if problems:
        raise _error(422, "unsourced_facts", " ".join(problems))
    return company_out(db, company, settings)


@router.post("/companies/{company_id}/refresh", response_model=ResearchResponse)
def refresh_company(
    company_id: int,
    body: CompanyRefresh,
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    return run_refresh(db, llm, settings, _company(db, company_id), body.discard_edits)


def run_refresh(db: Session, llm: ChatClient, settings: Settings, company: Company, discard_edits: bool) -> ResearchResponse:
    outcome = research.ensure_research(
        db,
        llm,
        company,
        default_ttl_days=settings.research_ttl_days,
        hints=stored_hints(db, company),
        force=True,
        overwrite_edits=discard_edits,
        wait_s=research_wait_s(settings),
    )
    max_age = research.ttl(db, settings.research_ttl_days)
    return ResearchResponse(
        company=company_out(db, outcome.company, settings),
        used=research.usable(outcome.company, max_age),
        researched=outcome.researched,
        error=outcome.error,
        notice=outcome.notice,
    )


# --- prompts -----------------------------------------------------------------------


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts(db: Session = Depends(get_db)):
    return store.list_prompts(db)


@router.post("/prompts", response_model=PromptOut, status_code=201)
def create_prompt(body: PromptCreate, db: Session = Depends(get_db)):
    return store.create_prompt(db, body.name, body.system_prompt, body.activate)


@router.get("/prompts/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: int, db: Session = Depends(get_db)):
    try:
        return store.get_prompt(db, prompt_id)
    except store.NotFound as e:
        raise _error(404, "not_found", str(e)) from e


@router.post("/prompts/{prompt_id}/activate", response_model=PromptOut)
def activate_prompt(prompt_id: int, db: Session = Depends(get_db)):
    try:
        return store.activate_prompt(db, prompt_id)
    except store.NotFound as e:
        raise _error(404, "not_found", str(e)) from e


# --- context -------------------------------------------------------------------------


@router.get("/context", response_model=ContextOut | None)
def get_context(db: Session = Depends(get_db)):
    return store.current_context(db)


@router.put("/context", response_model=ContextOut)
def put_context(body: ContextIn, db: Session = Depends(get_db)):
    return store.save_context(db, **body.model_dump())


@router.get("/context/versions", response_model=list[ContextOut])
def context_versions(db: Session = Depends(get_db)):
    return store.list_contexts(db)


@router.post("/context/import", response_model=CVImportOut)
def import_context_from_cv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    """Extract a CV's text (OCR for scans) and let the LLM propose context fields. Saves nothing."""
    return run_cv_import(file, db, llm, settings)


def run_cv_import(file: UploadFile, db: Session, llm: ChatClient, settings: Settings) -> CVImportOut:
    # Read one byte past the limit so an oversized upload is detected without reading all of it.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    result = import_cv(llm, data, file.filename or "", settings.ocr_langs, store.get_effort(db))
    ex = result.extracted
    return CVImportOut(
        fields=ContextIn(**result.fields), text=ex.text, method=ex.method, ocr_pages=ex.ocr_pages, warnings=ex.warnings
    )
