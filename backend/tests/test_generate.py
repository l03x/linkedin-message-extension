import json

import pytest
from sqlalchemy import func, select

from app.generation import build_user_message, clean
from app.models import Generation, Profile
from tests.conftest import AUTH, PROFILE, draft_json, gen_body, research_json


def test_user_message_matches_the_contract_exactly(client, llm, db):
    client.put("/api/context", json={"background": "I built a Go rate limiter.", "target_role": "Backend intern"}, headers=AUTH)
    llm.queue_research(research_json())
    llm.queue(draft_json())

    r = client.post("/api/generate", json=gen_body(extra_instruction="mention Go"), headers=AUTH)
    assert r.status_code == 200, r.text

    system, user = llm.draft_calls[0]
    assert system["role"] == "system"
    date = db.scalar(select(Generation)).company.researched_at.strftime("%Y-%m-%d")
    assert user["content"] == (
        "<mode>connection_note</mode>\n"
        "<language>en</language>\n"
        "<extra_instruction>mention Go</extra_instruction>\n"
        "<my_context>Background: I built a Go rate limiter.\nTarget role: Backend intern</my_context>\n"
        f"<target_profile>{PROFILE['raw_text']}</target_profile>\n"
        "<company_research>Company: Acme (https://acme.example)\n"
        f"Researched on {date} with web search\n"
        "- [product] Acme sells a payments API. (source: https://acme.example/product)\n"
        "- [news] Acme raised a Series B in 2026. (2026-03) (source: https://news.example/acme-b)"
        "</company_research>"
    )
    # The draft asks for the contract's JSON shape.
    schema = llm.kwargs[-1]["json_schema"]
    assert set(schema["required"]) == {"strategy", "strategy_reason", "cv_match", "subject", "message", "facts_used"}
    assert llm.kwargs[-1].get("web_search") is not True


def test_empty_extra_instruction_and_no_research():
    msg = build_user_message("inmail", "fr", "  ", "ctx", "profile text\n", "none")
    assert msg == (
        "<mode>inmail</mode>\n<language>fr</language>\n<extra_instruction></extra_instruction>\n"
        "<my_context>ctx</my_context>\n<target_profile>profile text</target_profile>\n"
        "<company_research>none</company_research>"
    )


def test_all_contract_fields_are_returned_and_stored(client, llm, db):
    llm.queue_research(research_json())
    facts = [
        {"fact": "Acme sells a payments API", "source": "https://acme.example/product"},
        {"fact": "Builds payment systems in Go", "source": "profile"},
        {"fact": "I built a Go rate limiter", "source": "my_context"},
    ]
    llm.queue(draft_json("Hi Jane, about Acme's payments API...", strategy="send_cv", facts_used=facts))
    data = client.post("/api/generate", json=gen_body(mode="message"), headers=AUTH).json()

    assert data["strategy"] == "send_cv"
    assert data["strategy_reason"] and data["cv_match"]
    assert data["subject"] is None and data["body"].startswith("Hi Jane")
    assert [(f["kind"], f["verified"]) for f in data["facts_used"]] == [("url", True), ("profile", True), ("my_context", True)]
    assert data["research_used"] is True and data["company"]["name"] == "Acme"
    assert data["warnings"] == [] or all("placeholder" in w for w in data["warnings"])

    gen = db.get(Generation, data["generation_id"])
    assert (gen.strategy, gen.draft_text, gen.draft_subject) == ("send_cv", data["body"], None)
    assert gen.strategy_reason == data["strategy_reason"] and gen.cv_match == data["cv_match"]
    assert gen.facts_used == data["facts_used"]
    assert gen.research_used is True and gen.company.name == "Acme"
    assert "Acme sells a payments API." in gen.research_snapshot


def test_a_cited_url_not_in_the_research_is_flagged(client, llm):
    llm.queue_research(research_json())
    facts = [{"fact": "Acme has 900 engineers", "source": "https://made-up.example/acme"}, {"fact": "x", "source": "a blog"}]
    llm.queue(draft_json(facts_used=facts))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert [(f["kind"], f["verified"]) for f in data["facts_used"]] == [("url", False), ("invalid", False)]
    assert any("wasn't in the company research" in w for w in data["warnings"])
    assert any("no valid source" in w for w in data["warnings"])


def test_inmail_subject(client, llm):
    llm.queue(draft_json("Hi Jane,\nI'm a student...", subject="Backend internship at Acme"))
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["subject"] == "Backend internship at Acme"
    assert data["body"] == "Hi Jane,\nI'm a student..."
    assert data["limits"] == {"subject": 200, "body": 1900}


def test_missing_inmail_subject_warns_and_other_modes_drop_it(client, llm):
    llm.queue(draft_json("body", subject=None), draft_json("note", subject="ignored"))
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["subject"] == "" and any("subject" in w for w in data["warnings"])
    assert client.post("/api/generate", json=gen_body(), headers=AUTH).json()["subject"] is None


def test_unknown_strategy_is_stored_as_none(client, llm, db):
    llm.queue(draft_json(strategy="beg"))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["strategy"] is None and any("unknown strategy" in w for w in data["warnings"])


def test_fenced_json_is_accepted(client, llm):
    llm.queue("```json\n" + draft_json("Hello there") + "\n```")
    assert client.post("/api/generate", json=gen_body(), headers=AUTH).json()["body"] == "Hello there"


def test_bad_json_gets_one_repair_retry(client, llm):
    llm.queue("Hi Jane, here is a plain-text note.", draft_json("Fixed."))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["body"] == "Fixed." and data["attempts"] == 2
    repair = llm.draft_calls[1]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert "JSON object" in repair[3]["content"]


def test_bad_json_twice_is_a_clear_error(client, llm, db):
    llm.queue("not json", '{"message": ""}')
    r = client.post("/api/generate", json=gen_body(), headers=AUTH)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "llm_bad_json"
    assert db.scalar(select(func.count(Generation.id))) == 0


@pytest.mark.parametrize("research", ["failed", "no_company"])
def test_drafting_works_without_research(client, llm, db, research):
    profile = dict(PROFILE)
    if research == "no_company":
        profile.update(company=None, headline="Student", raw_text="Jane Doe\nStudent\nAbout\nI like Go.")
    # With nothing queued, the fake research call fails like a server with web search off.
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(profile=profile), headers=AUTH).json()
    assert data["research_used"] is False
    assert llm.draft_calls[0][1]["content"].endswith("<company_research>none</company_research>")
    gen = db.get(Generation, data["generation_id"])
    assert gen.research_used is False and gen.research_snapshot == "none"
    if research == "failed":
        assert data["company"]["status"] == "failed"
        assert any("without company research" in w for w in data["warnings"])
        assert "web search" in data["research_note"].lower()
    else:
        assert data["company"] is None and gen.company_id is None
        assert data["research_note"] == "No current company found on this profile."
        assert llm.research_calls == []


def test_company_override_is_used(client, llm, db):
    llm.queue_research(research_json(name="Globex"))
    llm.queue(draft_json())
    data = client.post("/api/generate", json=gen_body(company_override="Globex Corp"), headers=AUTH).json()
    assert data["company"]["name"] == "Globex Corp"
    assert "<company>Globex Corp</company>" in llm.research_calls[0][1]["content"]


def test_profile_is_upserted(client, llm, db):
    llm.queue(draft_json("one"), draft_json("two"))
    client.post("/api/generate", json=gen_body(), headers=AUTH)
    first_seen = db.scalar(select(Profile.first_seen))
    updated = dict(PROFILE, headline="Staff engineer at Acme", url="https://linkedin.com/in/jane-doe-123")
    client.post("/api/generate", json=gen_body(profile=updated), headers=AUTH)

    db.expire_all()
    assert db.scalar(select(func.count(Profile.id))) == 1
    p = db.scalar(select(Profile))
    assert p.headline == "Staff engineer at Acme"
    assert p.first_seen == first_seen and p.last_seen >= first_seen
    assert db.scalar(select(func.count(Generation.id))) == 2


def test_clean_strips_wrapping_quotes():
    assert clean('"Hello there"') == "Hello there"
    assert clean("«Bonjour»") == "Bonjour"
    # Quotes inside the text are left alone.
    assert clean('I liked "Designing Data-Intensive Apps", thanks') == 'I liked "Designing Data-Intensive Apps", thanks'


def test_placeholder_context_warns(client, llm):
    llm.queue(draft_json("Hello"))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert any("placeholder" in w for w in data["warnings"])


def test_previous_contacts_are_returned(client, llm):
    llm.queue(draft_json("first"), draft_json("second"))
    gid = client.post("/api/generate", json=gen_body(), headers=AUTH).json()["generation_id"]
    client.post(f"/api/generations/{gid}/sent", json={"final_text": "first (edited)"}, headers=AUTH)
    data = client.post("/api/generate", json=gen_body(mode="message"), headers=AUTH).json()
    assert [p["generation_id"] for p in data["previous"]] == [gid]
    assert data["previous"][0]["status"] == "sent"


def test_rejects_bad_input(client, llm):
    bad_url = gen_body(profile=dict(PROFILE, url="https://www.linkedin.com/company/acme/"))
    assert client.post("/api/generate", json=bad_url, headers=AUTH).status_code == 422
    r = client.post("/api/generate", json=gen_body(mode="tweet"), headers=AUTH)
    assert r.status_code == 400 and "Unknown mode" in r.json()["detail"]["message"]
    assert llm.calls == []


def test_raw_text_is_capped(client, llm, db):
    llm.queue(draft_json("ok"))
    huge = dict(PROFILE, raw_text="x" * 50_000)
    client.post("/api/generate", json=gen_body(profile=huge), headers=AUTH)
    assert len(db.scalar(select(Profile.raw_text))) == 20_000


def test_llm_error_is_reported_clearly(client, app):
    from app.llm import LLMError

    class Down:
        model = "x"

        def complete(self, messages, effort=None, **kw):
            raise LLMError(502, "llm_unreachable", "Can't reach the LLM API at http://nowhere/v1")

    app.state.llm = Down()
    r = client.post("/api/generate", json=gen_body(), headers=AUTH)
    assert r.status_code == 502
    assert r.json() == {"detail": {"code": "llm_unreachable", "message": "Can't reach the LLM API at http://nowhere/v1"}}


def test_generations_list_has_the_new_fields(client, llm):
    llm.queue(draft_json(strategy="ask_referral"))
    gid = client.post("/api/generate", json=gen_body(), headers=AUTH).json()["generation_id"]
    rows = client.get("/api/generations?include_drafts=true", headers=AUTH).json()
    assert rows[0]["id"] == gid and rows[0]["strategy"] == "ask_referral"
    assert rows[0]["facts_used"][0]["kind"] == "profile"
    assert json.dumps(rows)  # serialisable as is
