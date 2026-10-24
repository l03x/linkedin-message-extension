import json

from app import store
from tests.conftest import AUTH, draft_json, gen_body
from tests.test_cv_import import CV_LINES, LLM_FIELDS


def test_effort_comes_from_env_until_set(client, settings):
    settings.llm_effort = "low"
    r = client.get("/api/settings", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "model": "fake-model", "effort": "low", "efforts": ["low", "medium", "high", "xhigh", "max"], "from_env": True,
    }


def test_generate_uses_the_dashboard_effort(client, llm, dash):
    llm.queue(draft_json("Hi Jane, first."), draft_json("Hi Jane, second."), draft_json("Hi Jane, third."))
    client.post("/api/generate", json=gen_body(), headers=AUTH)
    # Never set: the client falls back to LLM_EFFORT, for the company research and for the draft.
    assert llm.efforts == [None, None]

    r = dash.post("/settings/effort", data={"effort": "high"})
    assert r.status_code == 200 and "Saved" in r.text and 'value="high" selected' in r.text
    client.post("/api/generate", json=gen_body(), headers=AUTH)
    assert llm.efforts[-1] == "high"

    # "Not sent" is stored as "" so it overrides LLM_EFFORT too.
    dash.post("/settings/effort", data={"effort": ""})
    client.post("/api/generate", json=gen_body(), headers=AUTH)
    assert llm.efforts[-1] == ""


def test_retry_and_cv_import_use_it_too(client, llm, db):
    store.set_effort(db, "xhigh")
    llm.queue(draft_json("x" * 400), draft_json("Short now."))
    assert client.post("/api/generate", json=gen_body(), headers=AUTH).json()["attempts"] == 2
    llm.queue(json.dumps(LLM_FIELDS))
    r = client.post("/api/context/import", files={"file": ("cv.txt", "\n".join(CV_LINES).encode(), "text/plain")}, headers=AUTH)
    assert r.status_code == 200
    assert llm.efforts == ["xhigh"] * 4  # research, draft, shorten retry, CV import


def test_bad_effort_is_rejected(client, dash):
    assert dash.post("/settings/effort", data={"effort": "turbo"}).status_code == 400
    r = client.put("/api/settings", json={"effort": "turbo"}, headers=AUTH)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_effort"
    r = client.put("/api/settings", json={"effort": "medium"}, headers=AUTH)
    assert r.json()["effort"] == "medium" and r.json()["from_env"] is False


def test_prompts_page_shows_the_picker(dash):
    page = dash.get("/prompts").text
    assert 'hx-post="/settings/effort"' in page and "fake-model" in page and "LLM_EFFORT" in page


def test_effort_setting_requires_login(client):
    r = client.post("/settings/effort", data={"effort": "max"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/settings").status_code == 401
