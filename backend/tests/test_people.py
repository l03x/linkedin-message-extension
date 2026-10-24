from app import store
from tests.conftest import AUTH, PROFILE, draft_json, gen_body, research_json

BOB = dict(PROFILE, url="https://www.linkedin.com/in/bob/", name="Bob Roe", headline="CTO at Globex", company=None)
SAM = dict(PROFILE, url="https://www.linkedin.com/in/sam/", name="Sam Lee", headline="Student", company=None, raw_text="Sam\nStudent")


def _send(client, llm, profile, status=None, **kw):
    llm.queue(draft_json(**kw))
    g = client.post("/api/generate", json=gen_body(profile=profile), headers=AUTH).json()
    client.post(f"/api/generations/{g['generation_id']}/sent", json={"final_text": g["body"]}, headers=AUTH)
    if status:
        client.post(f"/api/generations/{g['generation_id']}/status", json={"status": status}, headers=AUTH)
    return g


def test_people_are_listed_once_with_their_counts(client, llm, db):
    llm.queue_research(research_json())
    _send(client, llm, PROFILE, status="no_reply")
    _send(client, llm, PROFILE, status="replied")
    _send(client, llm, BOB)
    llm.queue(draft_json())
    client.post("/api/generate", json=gen_body(profile=SAM), headers=AUTH)  # drafted, never sent

    rows = store.list_people(db)
    assert [r.profile.name for r in rows] == ["Bob Roe", "Jane Doe"]  # most recent first, Sam left out
    jane = rows[1]
    assert (jane.drafts, jane.sent, jane.replied, jane.awaiting, jane.last_status) == (2, 2, 1, 0, "replied")
    assert jane.company.name == "Acme" and rows[0].company.name == "Globex"

    everyone = store.list_people(db, include_unsent=True)
    assert [r.profile.name for r in everyone] == ["Sam Lee", "Bob Roe", "Jane Doe"]
    assert everyone[0].sent == 0 and everyone[0].last_status == "drafted" and everyone[0].company is None

    assert [r.profile.name for r in store.list_people(db, search="acme")] == ["Jane Doe"]  # by company
    assert [r.profile.name for r in store.list_people(db, search="cto")] == ["Bob Roe"]  # by headline


def test_people_page_links_to_their_messages(dash, client, llm, db):
    llm.queue_research(research_json())
    jane = _send(client, llm, PROFILE, message="Hi Jane, about Acme.")
    _send(client, llm, BOB, message="Hi Bob, about Globex.")

    page = dash.get("/people")
    assert page.status_code == 200
    assert "Jane Doe" in page.text and "Bob Roe" in page.text and "2 people" in page.text
    assert 'href="/companies/' in page.text and "Awaiting reply" in page.text
    pid = db.get(store.Generation, jane["generation_id"]).profile_id
    assert f'href="/history?profile_id={pid}&drafts=true"' in page.text

    history = dash.get(f"/history?profile_id={pid}&drafts=true").text
    assert "Messages to Jane Doe" in history
    assert "Hi Jane, about Acme." in history and "Hi Bob, about Globex." not in history
    assert dash.get("/people?q=nobody").text.count("No matches") == 1


def test_companies_list_shows_the_research_inline(dash, client, llm):
    llm.queue_research(research_json())
    client.post("/api/research", json={"profile": dict(PROFILE)}, headers=AUTH)
    page = dash.get("/companies").text
    assert "Show all research" in page and 'data-toggle="research-' in page
    assert "Acme sells a payments API." in page and "https://news.example/acme-b" in page
