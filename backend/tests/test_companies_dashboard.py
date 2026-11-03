from datetime import timedelta

import pytest
from sqlalchemy import select

from app.llm import LLMError
from app.models import Company, Generation, utcnow
from tests.conftest import AUTH, PROFILE, draft_json, gen_body, research_json


def _researched(client, llm) -> int:
    llm.queue_research(research_json())
    return client.post("/api/research", json={"profile": dict(PROFILE)}, headers=AUTH).json()["company"]["id"]


def test_companies_list_and_detail(dash, client, llm, db):
    cid = _researched(client, llm)
    r = dash.get("/companies")
    assert r.status_code == 200
    assert "Acme" in r.text and "Fresh" in r.text and 'value="30"' in r.text
    r = dash.get(f"/companies/{cid}")
    assert r.status_code == 200
    assert "https://acme.example/product" in r.text and "Acme sells a payments API." in r.text
    assert "2 web searches" in r.text
    assert dash.get("/companies/999").status_code == 404


def test_age_and_expired_status(dash, client, llm, db):
    cid = _researched(client, llm)
    c = db.get(Company, cid)
    c.researched_at = utcnow() - timedelta(days=40)
    db.commit()
    data = client.get(f"/api/companies/{cid}", headers=AUTH).json()
    assert data["age_days"] == 40 and data["fresh"] is False
    assert "Expired" in dash.get("/companies").text


def test_manual_edit_from_the_dashboard(dash, client, llm, db):
    cid = _researched(client, llm)
    form = {
        "website": "https://acme.example",
        "category": ["engineering", "news"],
        "fact": ["They run Kubernetes.", "Hiring interns for 2027."],
        "source_url": ["https://acme.example/eng", ""],
        "date": ["", "2026-09"],
    }
    r = dash.post(f"/companies/{cid}", data=form)
    assert r.status_code == 422 and "Fact 2 needs a source URL" in r.text
    assert "They run Kubernetes." in r.text  # your input is kept on the page
    form["source_url"][1] = "https://jobs.acme.example/interns"
    r = dash.post(f"/companies/{cid}", data=form, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    c = db.get(Company, cid)
    assert c.manually_edited and [f["fact"] for f in c.facts] == ["They run Kubernetes.", "Hiring interns for 2027."]
    assert c.facts[1]["date"] == "2026-09"
    assert "Edited by you" in dash.get("/companies").text


def test_refresh_from_the_dashboard(dash, client, llm, db):
    cid = _researched(client, llm)
    llm.queue_research(research_json(facts=[{"category": "news", "fact": "Fresh news.", "source_url": "https://n.example/2", "date": None}]))
    r = dash.post(f"/companies/{cid}/refresh")
    assert r.status_code == 200 and "Researched: 1 sourced facts" in r.text
    assert len(llm.research_calls) == 2

    # Edited research needs the confirmation (the page sends discard_edits once you confirm).
    client.put(f"/api/companies/{cid}", json={"facts": [{"fact": "Mine.", "source_url": "https://m.example"}]}, headers=AUTH)
    page = dash.get(f"/companies/{cid}").text
    assert "data-confirm=" in page and 'name="discard_edits"' in page
    r = dash.post(f"/companies/{cid}/refresh")
    assert "edited this research by hand" in r.text and len(llm.research_calls) == 2
    llm.queue_research(research_json())
    r = dash.post(f"/companies/{cid}/refresh", data={"discard_edits": "true"})
    assert "Researched: 2 sourced facts" in r.text and not db.get(Company, cid).manually_edited

    # A failed refresh says why.
    r = dash.post(f"/companies/{cid}/refresh")
    assert "Research failed" in r.text and "Web search is off" in r.text


def test_history_filters_by_strategy_and_company(dash, client, llm, db):
    llm.queue_research(research_json())
    llm.queue(draft_json("note one", strategy="hook_only"), draft_json("note two", strategy="send_cv"))
    a = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    other = dict(PROFILE, url="https://www.linkedin.com/in/bob/", name="Bob", headline="CTO at Globex", company=None)
    b = client.post("/api/generate", json=gen_body(profile=other), headers=AUTH).json()
    for g in (a, b):
        client.post(f"/api/generations/{g['generation_id']}/sent", json={"final_text": g["body"]}, headers=AUTH)

    def ids(**params):
        rows = client.get("/api/generations", params=params, headers=AUTH).json()
        return {r["id"] for r in rows}

    acme = db.get(Generation, a["generation_id"]).company_id
    globex = db.get(Generation, b["generation_id"]).company_id
    assert ids(strategy="hook_only") == {a["generation_id"]}
    assert ids(strategy="send_cv") == {b["generation_id"]}
    assert ids(company_id=acme) == {a["generation_id"]} and ids(company_id=globex) == {b["generation_id"]}
    assert ids(strategy="none") == set()

    page = dash.get(f"/history?strategy=hook_only&company_id={acme}").text
    assert "note one" in page and "note two" not in page
    assert '<option value="send_cv"' in page and "Globex" in page  # both filters are on the page
    assert "Hook only" in page and f'href="/companies/{acme}"' in page


def test_research_a_company_by_name(dash, client, llm, db):
    llm.queue_research(research_json(website="https://hps-worldwide.com", name="HPS"))
    r = dash.post("/companies/research", data={"name": "HPS", "website": "hps-worldwide.com"})
    assert r.status_code == 200 and "Researched: 2 sourced facts." in r.text
    user = llm.research_calls[0][1]["content"]
    assert "<company>HPS</company>" in user and "Its website: https://hps-worldwide.com" in user
    c = db.scalar(select(Company).where(Company.name_key == "hps"))
    assert (c.status, len(c.facts), c.website) == ("ready", 2, "https://hps-worldwide.com")
    assert "HPS" in dash.get("/companies").text

    # Fresh in the cache: opened as is, no second search. A draft for someone there reuses it too.
    r = dash.post("/companies/research", data={"name": "HPS S.A."})
    assert "still fresh, so it wasn't searched again" in r.text and len(llm.research_calls) == 1
    llm.queue(draft_json())
    profile = dict(PROFILE, company={"name": "HPS", "linkedin_slug": None})
    assert client.post("/api/generate", json=gen_body(profile=profile), headers=AUTH).json()["research_used"] is True
    assert len(llm.research_calls) == 1


def test_research_by_name_skips_the_failure_backoff(dash, llm, db):
    llm.queue_research(LLMError(504, "llm_timeout", "Too slow"))
    r = dash.post("/companies/research", data={"name": "Inetum"})
    assert "Research failed: Too slow" in r.text
    c = db.scalar(select(Company).where(Company.name_key == "inetum"))
    assert c.status == "failed" and c.next_retry_at is not None
    # You asked for it, so it searches now instead of waiting for next_retry_at.
    llm.queue_research(research_json(name="Inetum"))
    assert "Researched: 2 sourced facts." in dash.post("/companies/research", data={"name": "inetum"}).text
    assert len(llm.research_calls) == 2


def test_research_by_name_keeps_your_edits(dash, client, llm, db):
    cid = _researched(client, llm)
    mine = {"facts": [{"category": "engineering", "fact": "They use Rust.", "source_url": "https://blog.acme.example/rust"}]}
    client.put(f"/api/companies/{cid}", json=mine, headers=AUTH)
    r = dash.post("/companies/research", data={"name": "Acme", "website": "other.example"})
    assert "research you edited, so it wasn't searched again" in r.text and "They use Rust." in r.text
    assert len(llm.research_calls) == 1
    db.expire_all()
    assert db.get(Company, cid).website is None  # edited research isn't changed either


@pytest.mark.parametrize(
    "form, problem",
    [
        ({"name": ""}, "Type a company name"),
        ({"name": "Freelance"}, "Type a company name"),
        ({"name": "Acme", "website": "not a site"}, "The website must be a domain"),
        ({"name": "Acme", "website": "ftp://acme.example"}, "The website must be a domain"),
        ({"name": "Acme", "website": "https://www.linkedin.com/company/acme/"}, "not its LinkedIn page"),
    ],
)
def test_research_by_name_validation(dash, llm, db, form, problem):
    r = dash.post("/companies/research", data=form)
    assert r.status_code == 422 and problem in r.text
    assert llm.calls == [] and db.scalar(select(Company)) is None
