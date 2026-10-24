from app.generation import char_count
from tests.conftest import AUTH, gen_body


def test_within_limit_makes_one_call(client, llm):
    llm.queue("a" * 300)
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 1 and data["over_limit"] is False
    assert len(llm.calls) == 1


def test_over_limit_retries_once_with_shorten_instruction(client, llm):
    llm.queue("a" * 340, "b" * 250)
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is False
    assert data["body"] == "b" * 250

    retry = llm.calls[1]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert retry[2]["content"] == "a" * 340
    assert "340 characters" in retry[3]["content"] and "hard limit is 300" in retry[3]["content"]
    assert "at most 270" in retry[3]["content"]


def test_still_over_limit_is_flagged(client, llm, db):
    from app.models import Generation

    llm.queue("a" * 400, "a" * 320)
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is True
    assert data["over"] == {"body": [320, 300]}
    assert any("over the limit" in w for w in data["warnings"])
    assert len(llm.calls) == 2
    assert db.get(Generation, data["generation_id"]).over_limit is True


def test_inmail_subject_over_limit_triggers_retry(client, llm):
    llm.queue("Subject: " + "s" * 250 + "\n\nbody", "Subject: short\n\nbody")
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["attempts"] == 2 and data["subject"] == "short"
    assert "the subject is 250 characters" in llm.calls[1][3]["content"]
    assert 'Keep the "Subject:" line format.' in llm.calls[1][3]["content"]


def test_inmail_body_limit(client, llm):
    llm.queue("Subject: ok\n\n" + "b" * 1901, "Subject: ok\n\n" + "b" * 1900)
    data = client.post("/api/generate", json=gen_body(mode="inmail"), headers=AUTH).json()
    assert data["attempts"] == 2 and data["over_limit"] is False


def test_counting_is_by_code_points(client, llm):
    # 300 Arabic letters and 150 emoji are within a 300 limit when counted as code points.
    assert char_count("م" * 300) == 300
    assert char_count("👋" * 150) == 150
    llm.queue("م" * 300)
    data = client.post("/api/generate", json=gen_body(language="ar"), headers=AUTH).json()
    assert data["attempts"] == 1 and data["over_limit"] is False
