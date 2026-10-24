from datetime import timedelta

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
    assert r.status_code == 200 and "Researched again: 1 sourced facts" in r.text
    assert len(llm.research_calls) == 2

    # Edited research needs the confirmation (the page sends discard_edits once you confirm).
    client.put(f"/api/companies/{cid}", json={"facts": [{"fact": "Mine.", "source_url": "https://m.example"}]}, headers=AUTH)
    page = dash.get(f"/companies/{cid}").text
    assert "data-confirm=" in page and 'name="discard_edits"' in page
    r = dash.post(f"/companies/{cid}/refresh")
    assert "edited this research by hand" in r.text and len(llm.research_calls) == 2
    llm.queue_research(research_json())
    r = dash.post(f"/companies/{cid}/refresh", data={"discard_edits": "true"})
    assert "Researched again: 2 sourced facts" in r.text and not db.get(Company, cid).manually_edited

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
