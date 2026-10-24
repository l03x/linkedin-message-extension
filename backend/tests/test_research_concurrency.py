import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import research
from app.config import Settings
from app.main import create_app
from app.models import Company, utcnow
from tests.conftest import AUTH, TOKEN, FakeLLM, draft_json, gen_body, research_json


class BlockingLLM(FakeLLM):
    """Research calls block until released, so concurrent callers overlap for sure."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def complete(self, messages, effort=None, **kwargs):
        if kwargs.get("web_search"):
            with self.lock:
                self.calls.append(messages)
                self.kwargs.append(kwargs)
            self.started.set()
            assert self.release.wait(10), "never released"
            return research_json()
        with self.lock:
            return super().complete(messages, effort, **kwargs)


@pytest.fixture
def file_app(tmp_path):
    # A real file, so every thread gets its own SQLite connection (as in production).
    settings = Settings(_env_file=None, app_token=TOKEN, database_url=f"sqlite:///{tmp_path}/app.db", timezone="UTC")
    llm = BlockingLLM()
    app = create_app(settings, llm)
    with TestClient(app) as client:
        yield app, llm, client


def _company_id(app) -> int:
    with app.state.db.sessionmaker() as db:
        c = Company(name="Acme", name_key="acme", status="pending", facts=[])
        db.add(c)
        db.commit()
        return c.id


def _ensure(app, llm, cid, results, **kw):
    with app.state.db.sessionmaker() as db:
        outcome = research.ensure_research(db, llm, db.get(Company, cid), default_ttl_days=30, **kw)
        results.append((outcome.researched, len(outcome.company.facts), outcome.error, outcome.notice))


def test_concurrent_requests_share_one_search(file_app):
    app, llm, _ = file_app
    cid = _company_id(app)
    results: list = []
    threads = [threading.Thread(target=_ensure, args=(app, llm, cid, results)) for _ in range(6)]
    for t in threads:
        t.start()
    assert llm.started.wait(5)
    time.sleep(0.3)  # let the other five reach the wait
    llm.release.set()
    for t in threads:
        t.join(10)

    assert len(llm.research_calls) == 1
    assert len(results) == 6
    assert all(researched and facts == 2 and error is None for researched, facts, error, _ in results)


def test_concurrent_generations_for_one_company(file_app):
    app, llm, client = file_app
    llm.queue(*(draft_json(f"Hi {i}") for i in range(4)))
    responses: list = []

    def post(i):
        profile = {
            "url": f"https://www.linkedin.com/in/person-{i}/",
            "name": f"Person {i}",
            "headline": "Engineer at Acme",
            "raw_text": f"Person {i}\nEngineer at Acme",
        }
        responses.append(client.post("/api/generate", json=gen_body(profile=profile), headers=AUTH))

    threads = [threading.Thread(target=post, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    assert llm.started.wait(5)
    time.sleep(0.3)
    llm.release.set()
    for t in threads:
        t.join(15)

    assert len(llm.research_calls) == 1
    assert [r.status_code for r in responses] == [200] * 4
    assert all(r.json()["research_used"] for r in responses)


def test_a_live_claim_from_another_process_is_respected(file_app):
    app, llm, _ = file_app
    cid = _company_id(app)
    with app.state.db.sessionmaker() as db:
        c = db.get(Company, cid)
        c.status, c.lease_until = "researching", utcnow() + timedelta(minutes=5)
        db.commit()
    results: list = []
    _ensure(app, llm, cid, results)
    assert llm.research_calls == []
    assert "already running" in results[0][3]

    # A lapsed claim (that process died) is taken over.
    with app.state.db.sessionmaker() as db:
        db.get(Company, cid).lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    llm.release.set()
    _ensure(app, llm, cid, results)
    assert len(llm.research_calls) == 1 and results[1][:2] == (True, 2)


def test_a_waiter_gives_up_after_wait_s(file_app):
    app, llm, _ = file_app
    cid = _company_id(app)
    results: list = []
    leader = threading.Thread(target=_ensure, args=(app, llm, cid, results))
    leader.start()
    assert llm.started.wait(5)
    _ensure(app, llm, cid, results, wait_s=0.2)
    assert "taking long" in results[0][3]
    llm.release.set()
    leader.join(10)
    assert len(llm.research_calls) == 1
