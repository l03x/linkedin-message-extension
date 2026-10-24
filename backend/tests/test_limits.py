import json

from app.generation import char_count
from tests.conftest import AUTH, draft_json, gen_body


def test_within_limit_makes_one_call(client, llm):
    llm.queue(draft_json("a" * 300))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 1 and data["over_limit"] is False
    assert len(llm.draft_calls) == 1


def test_over_limit_retries_once_with_shorten_instruction(client, llm):
    llm.queue(draft_json("a" * 340), draft_json("b" * 250))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is False
    assert data["body"] == "b" * 250

    retry = llm.draft_calls[1]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert json.loads(retry[2]["content"])["message"] == "a" * 340
    assert '"message" is 340 characters' in retry[3]["content"] and "hard limit is 300" in retry[3]["content"]
    assert "at most 270" in retry[3]["content"] and "JSON object" in retry[3]["content"]


def test_still_over_limit_is_flagged(client, llm, db):
    from app.models import Generation

    llm.queue(draft_json("a" * 400), draft_json("a" * 320))
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is True
    assert data["over"] == {"body": [320, 300]}
    assert any("over the limit" in w for w in data["warnings"])
    assert len(llm.draft_calls) == 2
    assert db.get(Generation, data["generation_id"]).over_limit is True


def test_bad_json_on_the_shorten_retry_keeps_the_first_draft(client, llm):
    llm.queue(draft_json("a" * 340), "sorry, can't")
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["body"] == "a" * 340 and data["over_limit"] is True
    assert any("wasn't valid JSON" in w for w in data["warnings"])


def test_inmail_subject_over_limit_triggers_retry(client, llm):
    llm.queue(draft_json("body", subject="s" * 250), draft_json("body", subject="short"))
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["attempts"] == 2 and data["subject"] == "short"
    assert '"subject" is 250 characters' in llm.draft_calls[1][3]["content"]


def test_inmail_body_limit(client, llm):
    llm.queue(draft_json("b" * 1901, subject="ok"), draft_json("b" * 1900, subject="ok"))
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is False


def test_counting_is_by_code_points(client, llm):
    # 300 Arabic letters and 150 emoji are within a 300 limit when counted as code points.
    assert char_count("م" * 300) == 300
    assert char_count("👋" * 150) == 150
    llm.queue(draft_json("م" * 300))
    data = client.post("/api/generate", json=gen_body(language="ar"), headers=AUTH).json()
    assert data["attempts"] == 1 and data["over_limit"] is False
