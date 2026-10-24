from app.research import render_research, validate_facts
from tests.conftest import AUTH, PROFILE, draft_json, gen_body, research_json

GOOD = {"category": "product", "fact": "Acme sells a payments API.", "source_url": "https://acme.example/p", "date": None}


def test_unsourced_facts_are_rejected():
    raw = [
        GOOD,
        {"category": "news", "fact": "No source at all."},
        {"category": "news", "fact": "Empty source.", "source_url": ""},
        {"category": "news", "fact": "Not a URL.", "source_url": "their website"},
        {"category": "news", "fact": "Wrong scheme.", "source_url": "ftp://acme.example/x"},
        {"category": "news", "fact": "Relative.", "source_url": "/about"},
        {"category": "news", "fact": "LinkedIn page.", "source_url": "https://www.linkedin.com/company/acme/"},
        {"category": "news", "fact": "LinkedIn short link.", "source_url": "https://lnkd.in/abc"},
        {"category": "news", "fact": "", "source_url": "https://acme.example/empty"},
        "a string, not a fact",
    ]
    kept, rejected = validate_facts(raw)
    assert kept == [GOOD]
    assert rejected == 9


def test_facts_are_cleaned_deduped_and_capped():
    raw = [
        {**GOOD, "category": "weird", "fact": "  Acme   sells a payments API. "},
        {**GOOD, "fact": "acme sells a payments api."},  # duplicate
        *({**GOOD, "fact": f"Fact {i}", "source_url": f"https://acme.example/{i}"} for i in range(30)),
    ]
    kept, rejected = validate_facts(raw)
    assert kept[0] == {**GOOD, "category": "domain"}  # unknown category → domain, whitespace collapsed
    assert len(kept) == 20 and rejected == 0
    assert validate_facts(None) == ([], 0)


def test_only_sourced_facts_reach_the_draft(client, llm):
    facts = [GOOD, {"category": "news", "fact": "Rumour: Acme is being acquired.", "source_url": None, "date": None}]
    llm.queue_research(research_json(facts=facts))
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    prompt = llm.draft_calls[0][1]["content"]
    assert "Acme sells a payments API." in prompt and "(source: https://acme.example/p)" in prompt
    assert "Rumour" not in prompt
    assert data["company"]["rejected_facts"] == 1


def test_research_with_no_sourced_facts_fails(client, llm, db):
    unsourced = [{"category": "product", "fact": "Acme sells things.", "source_url": "", "date": None}]
    llm.queue_research(research_json(facts=unsourced))
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["research_used"] is False and data["company"]["status"] == "failed"
    assert "no facts with a source URL" in data["company"]["last_error"]
    assert llm.draft_calls[0][1]["content"].endswith("<company_research>none</company_research>")


def test_company_not_found_fails(client, llm):
    llm.queue_research(research_json(facts=[], found=False))
    r = client.post("/api/research", json={"profile": dict(PROFILE)}, headers=AUTH).json()
    assert "couldn't identify this company" in r["error"]


def test_manual_edits_need_sources_too(client, llm):
    llm.queue_research(research_json())
    cid = client.post("/api/research", json={"profile": dict(PROFILE)}, headers=AUTH).json()["company"]["id"]
    bad = {"facts": [GOOD, {"category": "news", "fact": "From memory.", "source_url": ""}]}
    r = client.put(f"/api/companies/{cid}", json=bad, headers=AUTH)
    assert r.status_code == 422 and "Fact 2 needs a source URL" in r.json()["detail"]["message"]
    li = {"facts": [{**GOOD, "source_url": "https://www.linkedin.com/company/acme/"}]}
    assert client.put(f"/api/companies/{cid}", json=li, headers=AUTH).status_code == 422
    # Nothing was saved: the researched facts are still there.
    assert len(client.get(f"/api/companies/{cid}", headers=AUTH).json()["facts"]) == 2
    # Empty rows are dropped, not errors.
    ok = client.put(f"/api/companies/{cid}", json={"facts": [GOOD, {"fact": "", "source_url": ""}]}, headers=AUTH)
    assert ok.status_code == 200 and len(ok.json()["facts"]) == 1


def test_render_none_without_usable_research():
    from datetime import timedelta

    assert render_research(None, timedelta(days=30)) == "none"
