import difflib
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import research, stats, store
from app.auth import COOKIE_NAME, cookie_value, require_dashboard, token_matches
from app.config import Settings
from app.deps import get_db, get_llm, get_settings
from app.llm import EFFORTS, ChatClient
from app.modes import LANGUAGES, MODES
from app.models import FACT_CATEGORIES, SENT_STATUSES, STATUSES, STRATEGIES, Company, Generation, Profile, utcnow
from app.routers.api import company_out, history_filters, run_cv_import, run_refresh
from app.schemas import CVImportOut

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
STATUS_LABELS = {"drafted": "Draft", "sent": "Awaiting reply", "replied": "Replied", "no_reply": "No reply"}
templates.env.globals.update(
    MODES=MODES,
    LANGUAGES=LANGUAGES,
    STATUSES=STATUSES,
    STATUS_LABELS=STATUS_LABELS,
    STRATEGIES=STRATEGIES,
    STRATEGY_LABELS=stats.STRATEGY_LABELS,
    FACT_CATEGORIES=FACT_CATEGORIES,
)
# Cache-busting suffix for /static assets: changes whenever the server restarts with new files.
templates.env.globals["asset_v"] = max(
    int(f.stat().st_mtime) for f in (Path(__file__).parent.parent / "static").rglob("*") if f.is_file()
)


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _duration(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "< 1 h"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


templates.env.filters.update(initials=_initials, duration=_duration)

router = APIRouter(include_in_schema=False)
auth = [Depends(require_dashboard)]


def _tz(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    tz = _tz(request.app.state.settings)

    def local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if dt is None:
            return ""
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt)

    ctx.setdefault("active_page", name.split(".")[0])
    return templates.TemplateResponse(
        request, name, {"local": local, "timezone": str(tz), **ctx}, status_code=status_code
    )


# --- login ---------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    return _render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, token: str = Form(""), settings: Settings = Depends(get_settings)):
    if not token_matches(token.strip(), settings):
        return _render(request, "login.html", status_code=401, error="Wrong token.")
    resp = RedirectResponse("/history", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        cookie_value(settings),
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=60 * 60 * 24 * 90,
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/", dependencies=auth)
def index():
    return RedirectResponse("/history", status_code=303)


# --- prompts ---------------------------------------------------------------------------


def _prompt_usage(db: Session) -> dict[int, dict]:
    q = select(
        Generation.prompt_version_id,
        func.count(),
        func.sum(case((Generation.status.in_(SENT_STATUSES), 1), else_=0)),
        func.sum(case((Generation.status == "replied", 1), else_=0)),
    ).group_by(Generation.prompt_version_id)
    return {pid: {"drafts": n, "sent": s or 0, "replied": r or 0} for pid, n, s, r in db.execute(q)}


def _versions_ctx(db: Session) -> dict:
    return {"versions": store.list_prompts(db), "usage": _prompt_usage(db)}


def _model_ctx(db: Session, settings: Settings) -> dict:
    effort = store.get_effort(db)
    return {
        "model": settings.llm_model,
        "efforts": EFFORTS,
        "effort": settings.llm_effort.strip() if effort is None else effort,
        "from_env": effort is None,
    }


@router.get("/prompts", dependencies=auth)
def prompts_page(
    request: Request, edit: int | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    active = store.active_prompt(db)
    base = store.get_prompt(db, edit) if edit else active
    return _render(request, "prompts.html", active=active, base=base, **_versions_ctx(db), **_model_ctx(db, settings))


@router.post("/settings/effort", dependencies=auth)
def settings_effort(
    request: Request, effort: str = Form(""), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    try:
        store.set_effort(db, effort.strip())
    except ValueError as e:
        return Response(str(e), status_code=400)
    return _render(request, "_model_settings.html", saved=True, **_model_ctx(db, settings))


@router.post("/prompts", dependencies=auth)
def prompts_create(
    name: str = Form(""),
    system_prompt: str = Form(...),
    activate: bool = Form(False),
    db: Session = Depends(get_db),
):
    store.create_prompt(db, name, system_prompt.replace("\r\n", "\n"), activate)
    return RedirectResponse("/prompts", status_code=303)


@router.post("/prompts/{prompt_id}/activate", dependencies=auth)
def prompts_activate(request: Request, prompt_id: int, db: Session = Depends(get_db)):
    store.activate_prompt(db, prompt_id)
    return _render(request, "_prompt_versions.html", **_versions_ctx(db))


@router.get("/prompts/diff", dependencies=auth)
def prompts_diff(request: Request, a: int, b: int, db: Session = Depends(get_db)):
    pa, pb = store.get_prompt(db, a), store.get_prompt(db, b)
    lines = []
    for line in difflib.unified_diff(
        pa.system_prompt.splitlines(), pb.system_prompt.splitlines(), f"#{pa.id} {pa.name}", f"#{pb.id} {pb.name}", lineterm=""
    ):
        if line.startswith(("---", "+++")):
            kind = "file"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append((kind, line))
    return _render(
        request, "prompt_diff.html", active_page="prompts", a=pa, b=pb, lines=lines, versions=store.list_prompts(db)
    )


# --- context -----------------------------------------------------------------------------


@router.get("/context", dependencies=auth)
def context_page(request: Request, saved: bool = False, db: Session = Depends(get_db)):
    versions = store.list_contexts(db)
    ctx = versions[0] if versions else None
    return _render(
        request, "context.html", ctx=ctx, count=len(versions), saved=saved, preview=store.render_context(ctx)
    )


@router.post("/context", dependencies=auth)
def context_save(
    background: str = Form(""),
    target_role: str = Form(""),
    target_stack: str = Form(""),
    availability: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    store.save_context(
        db,
        background=background,
        target_role=target_role,
        target_stack=target_stack,
        availability=availability,
        location=location,
        notes=notes,
    )
    return RedirectResponse("/context?saved=1", status_code=303)


@router.post("/context/import", dependencies=auth, response_model=CVImportOut)
def context_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    # Same as POST /api/context/import, behind the dashboard cookie. Returns JSON; the page fills the form.
    return run_cv_import(file, db, llm, settings)


# --- history -------------------------------------------------------------------------------


def _opt_int(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


def _opt_date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@router.get("/history", dependencies=auth)
def history_page(
    request: Request,
    mode: str = "",
    language: str = "",
    status: str = "",
    prompt_version_id: str = "",
    date_from: str = "",
    date_to: str = "",
    drafts: bool = False,
    strategy: str = "",
    company_id: str = "",
    profile_id: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    f = history_filters(
        settings, mode, language, status, _opt_int(prompt_version_id), _opt_date(date_from), _opt_date(date_to), drafts
    )
    f.strategy, f.company_id, f.profile_id = strategy or None, _opt_int(company_id), _opt_int(profile_id)
    rows = store.query_generations(db, f)
    filters = {
        "mode": mode,
        "language": language,
        "status": status,
        "prompt_version_id": prompt_version_id,
        "date_from": date_from,
        "date_to": date_to,
        "drafts": drafts,
        "strategy": strategy,
        "company_id": company_id,
        "profile_id": profile_id,
    }
    return _render(
        request,
        "history.html",
        rows=rows,
        filters=filters,
        person=db.get(Profile, f.profile_id) if f.profile_id else None,
        versions=store.list_prompts(db),
        companies=store.companies_with_generations(db),
        today=datetime.now(_tz(settings)).date().isoformat(),
    )


@router.post("/history/{generation_id}/status", dependencies=auth)
def history_status(
    request: Request,
    generation_id: int,
    status: str = Form(...),
    replied_on: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    gen = store.get_generation(db, generation_id)
    replied_at = None
    day = _opt_date(replied_on)
    if status == "replied" and day is not None:
        tz = _tz(settings)
        if day == datetime.now(tz).date():
            replied_at = utcnow()
        else:
            # A backdated reply: noon local time on that day.
            replied_at = datetime.combine(day, time(12), tz)
    try:
        store.set_status(db, gen, status, replied_at)
    except store.Conflict as e:
        return Response(str(e), status_code=409)
    gen = store.get_generation(db, generation_id)
    return _render(request, "_history_row.html", g=gen, today=datetime.now(_tz(settings)).date().isoformat())


# --- stats -------------------------------------------------------------------------------------


@router.get("/stats", dependencies=auth)
def stats_page(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    data = stats.compute(db, _tz(settings))
    return _render(request, "stats.html", data=data)


# --- people ----------------------------------------------------------------------------------------


@router.get("/people", dependencies=auth)
def people_page(request: Request, q: str = "", unsent: bool = False, db: Session = Depends(get_db)):
    rows = store.list_people(db, include_unsent=unsent, search=q)
    return _render(request, "people.html", rows=rows, q=q, unsent=unsent)


# --- companies -------------------------------------------------------------------------------------


def _ttl_ctx(db: Session, settings: Settings) -> dict:
    return {"ttl_days": store.get_research_ttl_days(db, settings.research_ttl_days), "ttl_env": settings.research_ttl_days}


def _company_usage(db: Session) -> dict[int, dict]:
    q = select(
        Generation.company_id,
        func.count(),
        func.sum(case((Generation.status.in_(SENT_STATUSES), 1), else_=0)),
    ).where(Generation.company_id.is_not(None)).group_by(Generation.company_id)
    return {cid: {"drafts": n, "sent": s or 0} for cid, n, s in db.execute(q)}


@router.get("/companies", dependencies=auth)
def companies_page(request: Request, saved: bool = False, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    rows = [company_out(db, c, settings) for c in store.list_companies(db)]
    return _render(request, "companies.html", rows=rows, usage=_company_usage(db), saved=saved, **_ttl_ctx(db, settings))


@router.post("/settings/research-ttl", dependencies=auth)
def settings_research_ttl(days: str = Form(""), db: Session = Depends(get_db)):
    try:
        store.set_research_ttl_days(db, int(days))
    except ValueError as e:
        return Response(str(e) if days.isdigit() else "Enter a whole number of days.", status_code=400)
    return RedirectResponse("/companies?saved=1", status_code=303)


def _company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Company not found.")
    return company


def _company_page(request: Request, db: Session, settings: Settings, company: Company, status_code: int = 200, **ctx):
    recent = list(
        db.scalars(
            select(Generation)
            .where(Generation.company_id == company.id)
            .order_by(Generation.generated_at.desc())
            .limit(20)
        )
    )
    return _render(
        request,
        "company.html",
        status_code=status_code,
        active_page="companies",
        c=company_out(db, company, settings),
        recent=recent,
        **_ttl_ctx(db, settings),
        **ctx,
    )


@router.get("/companies/{company_id}", dependencies=auth)
def company_page(
    request: Request, company_id: int, saved: bool = False, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    return _company_page(request, db, settings, _company_or_404(db, company_id), saved=saved)


@router.post("/companies/{company_id}", dependencies=auth)
async def company_edit(
    request: Request, company_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    company = _company_or_404(db, company_id)
    form = await request.form()
    columns = [form.getlist(k) for k in ("category", "fact", "source_url", "date")]
    facts = [
        {"category": cat, "fact": fact, "source_url": url.strip(), "date": date.strip() or None}
        for cat, fact, url, date in zip(*columns)
    ]
    website = str(form.get("website") or "").strip() or None
    problems = research.save_manual(db, company, facts, website)
    if problems:
        db.rollback()
        return _company_page(request, db, settings, company, status_code=422, problems=problems, draft_facts=facts, draft_website=website)
    return RedirectResponse(f"/companies/{company.id}?saved=1", status_code=303)


@router.post("/companies/{company_id}/refresh", dependencies=auth)
def company_refresh(
    request: Request,
    company_id: int,
    discard_edits: bool = Form(False),
    db: Session = Depends(get_db),
    llm: ChatClient = Depends(get_llm),
    settings: Settings = Depends(get_settings),
):
    company = _company_or_404(db, company_id)
    result = run_refresh(db, llm, settings, company, discard_edits)
    return _company_page(request, db, settings, db.get(Company, company_id), refresh=result)
