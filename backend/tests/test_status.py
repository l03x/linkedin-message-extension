from datetime import datetime

from app.models import Generation
from tests.conftest import AUTH, gen_body


def _draft(client, llm, text="original draft", **kw) -> int:
    llm.queue(text)
    return client.post("/api/generate", json=gen_body(**kw), headers=AUTH).json()["generation_id"]


def test_sent_stores_final_edited_text(client, llm, db):
    gid = _draft(client, llm)
    r = client.post(f"/api/generations/{gid}/sent", json={"final_text": "  my edited version  "}, headers=AUTH)
    assert r.status_code == 200
    gen = db.get(Generation, gid)
    assert gen.final_text == "my edited version"
    assert gen.draft_text == "original draft"
    assert gen.status == "sent" and gen.sent_at is not None


def test_resending_keeps_first_sent_at(client, llm, db):
    gid = _draft(client, llm)
    first = client.post(f"/api/generations/{gid}/sent", json={"final_text": "v1"}, headers=AUTH).json()
    second = client.post(f"/api/generations/{gid}/sent", json={"final_text": "v2"}, headers=AUTH).json()
    assert second["sent_at"] == first["sent_at"] and second["final_text"] == "v2"


def test_inmail_requires_subject(client, llm):
    gid = _draft(client, llm, "Subject: s\n\nbody", mode="inmail")
    assert client.post(f"/api/generations/{gid}/sent", json={"final_text": "b"}, headers=AUTH).status_code == 400
    r = client.post(f"/api/generations/{gid}/sent", json={"final_text": "b", "final_subject": "edited s"}, headers=AUTH)
    assert r.json()["final_subject"] == "edited s"


def test_status_requires_sent(client, llm):
    gid = _draft(client, llm)
    r = client.post(f"/api/generations/{gid}/status", json={"status": "replied"}, headers=AUTH)
    assert r.status_code == 409
    assert client.post("/api/generations/999/status", json={"status": "replied"}, headers=AUTH).status_code == 404


def _backdate_sent(db, gid, when=datetime(2026, 1, 1, 10, 0)):
    gen = db.get(Generation, gid)
    gen.sent_at = when
    db.commit()


def test_replied_and_no_reply(client, llm, db):
    gid = _draft(client, llm)
    client.post(f"/api/generations/{gid}/sent", json={"final_text": "x"}, headers=AUTH)
    _backdate_sent(db, gid)

    r = client.post(f"/api/generations/{gid}/status", json={"status": "replied", "replied_at": "2026-10-20T09:00:00+01:00"}, headers=AUTH).json()
    assert r["status"] == "replied" and r["replied_at"] == "2026-10-20T08:00:00"

    r = client.post(f"/api/generations/{gid}/status", json={"status": "no_reply"}, headers=AUTH).json()
    assert r["status"] == "no_reply" and r["replied_at"] is None

    r = client.post(f"/api/generations/{gid}/status", json={"status": "replied"}, headers=AUTH).json()
    assert r["replied_at"] is not None

    assert client.post(f"/api/generations/{gid}/status", json={"status": "drafted"}, headers=AUTH).status_code == 422

    # A reply date before the send date is clamped to the send date.
    r = client.post(f"/api/generations/{gid}/status", json={"status": "replied", "replied_at": "2025-12-01T00:00:00Z"}, headers=AUTH).json()
    assert r["replied_at"] == "2026-01-01T10:00:00"


def test_history_filters(client, llm, dash, db):
    a = _draft(client, llm, "note text")
    b = _draft(client, llm, "message text", mode="message", language="fr")
    _draft(client, llm, "never sent")
    client.post(f"/api/generations/{a}/sent", json={"final_text": "note final"}, headers=AUTH)
    client.post(f"/api/generations/{b}/sent", json={"final_text": "message final"}, headers=AUTH)
    client.post(f"/api/generations/{b}/status", json={"status": "replied"}, headers=AUTH)

    ids = lambda **q: [g["id"] for g in client.get("/api/generations", params=q, headers=AUTH).json()]  # noqa: E731
    assert sorted(ids()) == [a, b]
    assert len(ids(include_drafts=True)) == 3
    assert ids(mode="message") == [b]
    assert ids(language="en") == [a]
    assert ids(status="drafted") != []
    assert ids(status="replied") == [b]
    assert ids(date_from="2000-01-01", date_to="2000-01-02") == []

    page = dash.get("/history", params={"mode": "message"})
    assert "message final" in page.text and "note final" not in page.text

    row = dash.post(f"/history/{a}/status", data={"status": "no_reply"}, headers={"HX-Request": "true"})
    assert row.status_code == 200 and f'id="gen-{a}"' in row.text and 'class="badge no_reply"' in row.text
    _backdate_sent(db, a)
    row = dash.post(f"/history/{a}/status", data={"status": "replied", "replied_on": "2026-01-05"})
    assert 'class="badge replied"' in row.text and "05 Jan" in row.text
