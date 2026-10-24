from datetime import timedelta

from sqlalchemy import func, select

from app import store
from app.llm import Completion, LLMError
from app.models import Company, utcnow
from tests.conftest import AUTH, PROFILE, draft_json, gen_body, research_json


def research(client, **body):
    r = client.post("/api/research", json={"profile": dict(PROFILE), **body}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def company(db) -> Company:
    db.expire_all()
    return db.scalar(select(Company))


def test_research_is_cached_per_company(client, llm, db):
    llm.queue_research(research_json())
    first = research(client)
    assert first["researched"] is True and first["used"] is True
    assert [f["source_url"] for f in first["company"]["facts"]] == ["https://acme.example/product", "https://news.example/acme-b"]
    assert llm.kwargs[0]["web_search"] is True and llm.kwargs[0]["json_schema"]["required"] == ["company", "found", "facts"]

    # Another person at the same company: same row, no new search, and the draft gets the cached facts.
    colleague = dict(PROFILE, url="https://www.linkedin.com/in/john-roe/", name="John Roe", headline="SRE at ACME Inc.")
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(profile=colleague), headers=AUTH).json()
    assert data["research_used"] is True
    assert len(llm.research_calls) == 1
    assert db.scalar(select(func.count(Company.id))) == 1


def test_same_company_by_slug_or_by_name(client, llm, db):
    llm.queue_research(research_json())
    research(client)
    # A different spelling, but the same /company/acme/ link: one row.
    research(client, profile=dict(PROFILE, company={"name": "Acme Payments", "linkedin_slug": "acme"}))
    # No slug on this page, but the name normalises to the same key.
    research(client, profile=dict(PROFILE, company={"name": "ACME S.A.", "linkedin_slug": None}))
    assert len(llm.research_calls) == 1 and db.scalar(select(func.count(Company.id))) == 1


def test_expired_research_is_redone(client, llm, db):
    llm.queue_research(research_json(), research_json(facts=[{"category": "news", "fact": "New thing.", "source_url": "https://n.example/1", "date": None}]))
    research(client)
    c = company(db)
    c.researched_at = utcnow() - timedelta(days=31)
    db.commit()
    data = research(client)
    assert data["researched"] is True and len(llm.research_calls) == 2
    assert [f["fact"] for f in data["company"]["facts"]] == ["New thing."]


def test_expiry_is_configurable(client, llm, db, dash):
    llm.queue_research(research_json())
    research(client)
    c = company(db)
    c.researched_at = utcnow() - timedelta(days=10)
    db.commit()
    assert research(client)["researched"] is False  # 10 days < the default 30

    assert dash.post("/settings/research-ttl", data={"days": "7"}, follow_redirects=False).status_code == 303
    assert store.get_research_ttl_days(db, 30) == 7
    llm.queue_research(research_json())
    assert research(client)["researched"] is True
    assert dash.post("/settings/research-ttl", data={"days": "0"}).status_code == 400


def test_env_default_expiry(client, llm, db, settings):
    settings.research_ttl_days = 2
    llm.queue_research(research_json(), research_json())
    research(client)
    c = company(db)
    c.researched_at = utcnow() - timedelta(days=3)
    db.commit()
    assert research(client)["researched"] is True


def test_failed_research_backs_off_then_retries(client, llm, db):
    llm.queue_research(LLMError(429, "llm_rate_limited", "Slow down"))
    first = research(client)
    assert first["error"] == "Slow down" and first["used"] is False
    c = company(db)
    assert c.status == "failed" and c.failures == 1
    assert timedelta(minutes=59) < c.next_retry_at - utcnow() <= timedelta(hours=1)

    # Before next_retry_at: no new call, and drafting still works without research.
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["research_used"] is False and len(llm.research_calls) == 1
    assert research(client)["error"] == "Slow down" and len(llm.research_calls) == 1

    # A second failure backs off longer.
    c = company(db)
    c.next_retry_at = utcnow() - timedelta(seconds=1)
    db.commit()
    llm.queue_research(LLMError(504, "llm_timeout", "Too slow"))
    research(client)
    c = company(db)
    assert c.failures == 2 and c.next_retry_at - utcnow() > timedelta(hours=5)

    # Once it's due, it is retried and the failure count resets.
    c.next_retry_at = utcnow() - timedelta(seconds=1)
    db.commit()
    llm.queue_research(research_json())
    assert research(client)["used"] is True
    c = company(db)
    assert (c.status, c.failures, c.next_retry_at, c.last_error) == ("ready", 0, None, None)


def test_force_skips_cache_and_backoff(client, llm, db):
    llm.queue_research(LLMError(502, "llm_error", "boom"), research_json(), research_json())
    research(client)
    assert research(client, force=True)["used"] is True  # despite the backoff
    assert research(client, force=True)["researched"] is True  # despite fresh research
    assert len(llm.research_calls) == 3


def test_manual_edits_are_never_overwritten_automatically(client, llm, db, dash):
    llm.queue_research(research_json())
    cid = research(client)["company"]["id"]
    mine = {"facts": [{"category": "engineering", "fact": "They use Rust.", "source_url": "https://blog.acme.example/rust"}]}
    r = client.put(f"/api/companies/{cid}", json=mine, headers=AUTH)
    assert r.status_code == 200 and r.json()["manually_edited"] is True

    # Long expired: still used as is, and not searched again.
    c = company(db)
    c.researched_at = utcnow() - timedelta(days=400)
    c.edited_at = utcnow() - timedelta(days=400)
    db.commit()
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["research_used"] is True and "They use Rust." in llm.draft_calls[-1][1]["content"]

    # A forced refresh from the extension doesn't touch it either.
    forced = research(client, force=True)
    assert "edited this research by hand" in forced["notice"]
    assert [f["fact"] for f in forced["company"]["facts"]] == ["They use Rust."]
    assert len(llm.research_calls) == 1

    # The dashboard refresh without the confirmation is the same...
    assert client.post(f"/api/companies/{cid}/refresh", json={}, headers=AUTH).json()["researched"] is False
    # ...and with it, your version is replaced.
    llm.queue_research(research_json())
    replaced = client.post(f"/api/companies/{cid}/refresh", json={"discard_edits": True}, headers=AUTH).json()
    assert replaced["researched"] is True and replaced["company"]["manually_edited"] is False
    assert len(replaced["company"]["facts"]) == 2


def test_an_edit_made_during_a_search_wins(client, llm, db, app):
    llm.queue_research(research_json())
    research(client)
    cid = company(db).id

    class EditsMidway:
        model = "x"

        def complete(self, messages, effort=None, **kw):
            with app.state.db.sessionmaker() as other:
                c = other.get(Company, cid)
                c.manually_edited, c.facts = True, [{"category": "news", "fact": "Mine.", "source_url": "https://m.example", "date": None}]
                other.commit()
            return research_json()

    app.state.llm = EditsMidway()
    data = research(client, force=True)
    assert "edited this research meanwhile" in data["notice"]
    assert [f["fact"] for f in company(db).facts] == ["Mine."]


def test_web_search_disabled_never_falls_back_to_memory(client, llm, db):
    # FakeLLM's default for research: the server has web search off.
    data = research(client)
    assert "Web search is off" in data["error"] and data["used"] is False
    assert all(kw.get("web_search") for kw in llm.kwargs)  # no second, search-less call
    assert company(db).facts == []


def test_a_reply_without_searches_is_discarded(client, llm, db):
    # Facts with URLs, but the API reports that no search ran: that is the model's memory.
    llm.queue_research(research_json(searches=0))
    data = research(client)
    assert "did not search the web" in data["error"] and company(db).facts == []

    # An API that doesn't report searches at all can't prove it searched either.
    c = company(db)
    c.next_retry_at = None
    db.commit()
    llm.queue_research(Completion(research_json().__str__(), web_searches=None))
    data = research(client)
    assert "didn't report any web searches" in data["error"] and company(db).status == "failed"


def test_no_company_means_no_research(client, llm):
    profile = dict(PROFILE, company=None, headline="Student at nowhere special", raw_text="Jane\nStudent")
    profile["headline"] = "Freelance"
    data = research(client, profile=profile)
    assert data["company"] is None and data["notice"] == "No current company found on this profile."
    assert llm.calls == []
