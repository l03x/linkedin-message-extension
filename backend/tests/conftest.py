import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeLLM:
    """Replays queued replies and records every message list it was sent."""

    model = "fake-model"

    def __init__(self):
        self.replies: list[str] = []
        self.calls: list[list[dict]] = []

    def queue(self, *replies: str) -> "FakeLLM":
        self.replies.extend(replies)
        return self

    def complete(self, messages: list[dict]) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("FakeLLM: no reply queued")
        return self.replies.pop(0)


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
    "raw_text": "Jane Doe\nBackend engineer at Acme\nAbout\nI build payment systems in Go.\nExperience\nAcme, 2021–now",
}


def gen_body(**overrides) -> dict:
    body = {"profile": dict(PROFILE), "mode": "connection_note", "language": "en", "extra_instruction": ""}
    body.update(overrides)
    return body
