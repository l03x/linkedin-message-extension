from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import store
from app.cv_import import MAX_UPLOAD_BYTES, import_cv
from app.auth import require_bearer
from app.config import Settings
from app.deps import get_db, get_llm, get_settings
from app.generation import GenerationError, ProfileInput, generate
from app.llm import EFFORTS, ChatClient
from app.modes import MODES, options_payload
from app.schemas import (
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


@router.post("/generate", response_model=GenerateResponse)
def generate_draft(body: GenerateRequest, db: Session = Depends(get_db), llm: ChatClient = Depends(get_llm)):
    p = body.profile
    try:
        result = generate(
            db,
            llm,
            ProfileInput(url=p.url, name=p.name, headline=p.headline, raw_text=p.raw_text),
            body.mode,
            body.language,
            body.extra_instruction,
        )
    except GenerationError as e:
        raise _error(400, "bad_request", str(e)) from e
    except store.NotFound as e:
        raise _error(500, "no_active_prompt", str(e)) from e
    gen = result.generation
    return GenerateResponse(
        generation_id=gen.id,
        mode=gen.mode,
        language=gen.language,
        subject=result.draft.subject,
        body=result.draft.body,
        limits=MODES[gen.mode].limits,
        over_limit=gen.over_limit,
        over={f: [n, limit] for f, (n, limit) in result.over.items()},
        attempts=gen.attempts,
        prompt_version={"id": gen.prompt_version_id, "name": gen.prompt_version.name},
        warnings=result.draft.warnings,
        previous=[
            PreviousContact(generation_id=g.id, mode=g.mode, status=g.status, sent_at=g.sent_at)
            for g in result.previous
        ],
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
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return store.query_generations(
        db, history_filters(settings, mode, language, status, prompt_version_id, date_from, date_to, include_drafts)
    )


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
