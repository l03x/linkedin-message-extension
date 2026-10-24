import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import Completion, LLMError
from app.main import create_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeLLM:
    """Replays queued replies and records every call.

    Research calls (web_search=True) and drafting calls have separate queues, so a test can queue both without
    caring about their order. A queued reply is a string, a Completion, or an exception to raise. With nothing
    queued, research fails like a server with web search turned off.
    """

    model = "fake-model"
    effort = ""

    def __init__(self):
        self.replies: list = []
        self.research_replies: list = []
        self.calls: list[list[dict]] = []
        self.efforts: list[str | None] = []
        self.kwargs: list[dict] = []

    def queue(self, *replies) -> "FakeLLM":
        self.replies.extend(replies)
        return self

    def queue_research(self, *replies) -> "FakeLLM":
        self.research_replies.extend(replies)
        return self

    @property
    def draft_calls(self) -> list[list[dict]]:
        return [c for c, kw in zip(self.calls, self.kwargs) if not kw.get("web_search")]

    @property
    def research_calls(self) -> list[list[dict]]:
        return [c for c, kw in zip(self.calls, self.kwargs) if kw.get("web_search")]

    def complete(self, messages: list[dict], effort: str | None = None, **kwargs) -> Completion:
        self.calls.append([dict(m) for m in messages])
        self.efforts.append(effort)
        self.kwargs.append(kwargs)
        queue = self.research_replies if kwargs.get("web_search") else self.replies
        if not queue and kwargs.get("web_search"):
            raise LLMError(502, "web_search_disabled", "Web search is off (FakeLLM default).")
        if not queue:
            raise AssertionError(f"FakeLLM: no {'research ' if kwargs.get('web_search') else ''}reply queued")
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, Completion) else Completion(reply)


def draft_json(message="Hi Jane, glad to connect.", strategy="hook_only", subject=None, facts_used=None, **extra) -> str:
    """A reply in the generation contract's JSON shape."""
    body = {
        "strategy": strategy,
        "strategy_reason": "First contact, so just a reason to connect.",
        "cv_match": "My Go rate limiter matches their payment work in Go.",
        "subject": subject,
        "message": message,
        "facts_used": facts_used if facts_used is not None else [{"fact": "Builds payment systems in Go", "source": "profile"}],
        **extra,
    }
    return json.dumps(body, ensure_ascii=False)


def research_json(facts=None, found=True, website="https://acme.example", searches=2, name="Acme") -> Completion:
    """A research reply that reports `searches` web searches."""
    if facts is None:
        facts = [
            {"category": "product", "fact": "Acme sells a payments API.", "source_url": "https://acme.example/product", "date": None},
            {"category": "news", "fact": "Acme raised a Series B in 2026.", "source_url": "https://news.example/acme-b", "date": "2026-03"},
        ]
    body = {"company": {"name": name, "website": website}, "found": found, "facts": facts}
    return Completion(json.dumps(body), web_searches=searches)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None, app_token=TOKEN, database_url="sqlite://", timezone="UTC", llm_model="fake-model", cookie_secure=False
    )


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def app(settings, llm):
    return create_app(settings, llm)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app, client):
    """A session on the same in-memory database the app uses (client fixture runs startup first)."""
    with app.state.db.sessionmaker() as session:
        yield session


@pytest.fixture
def dash(client):
    """A client logged in to the dashboard."""
    r = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    return client


PROFILE = {
    "url": "https://www.linkedin.com/in/Jane-Doe-123/?originalSubdomain=ma",
    "name": "Jane Doe",
    "headline": "Backend engineer at Acme",
    "company": {"name": "Acme", "linkedin_slug": "acme"},
    "raw_text": "Jane Doe\nBackend engineer at Acme\nAbout\nI build payment systems in Go.\nExperience\nAcme, 2021–now",
}


def gen_body(**overrides) -> dict:
    body = {"profile": dict(PROFILE), "mode": "connection_note", "language": "en", "extra_instruction": ""}
    body.update(overrides)
    return body
