from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth import LoginRedirect
from app.config import Settings, check_settings, get_settings
from app.db import Database
from app.llm import ChatClient, LLMClient, LLMError
from app.routers import api, dashboard
from app.seed import seed

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, llm: ChatClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    check_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db.create_all()
        with app.state.db.sessionmaker() as db:
            seed(db)
        yield

    app = FastAPI(title="LinkedIn outreach assistant", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    app.state.llm = llm or LLMClient(settings)

    # The extension calls the API from a chrome-extension:// origin with a bearer token (no cookies).
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"chrome-extension://[a-z]+",
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(LoginRedirect)
    async def _login(request: Request, _: LoginRedirect) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    app.include_router(api.public)
    app.include_router(api.router)
    app.include_router(dashboard.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn (`--factory`), so importing this module never needs a configured .env."""
    return create_app()
