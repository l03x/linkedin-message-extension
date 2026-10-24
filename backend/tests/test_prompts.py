import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import PromptVersion
from app.seed import DEFAULT_SYSTEM_PROMPT
from tests.conftest import AUTH, gen_body


def _active_ids(db) -> list[int]:
    db.expire_all()
    return list(db.scalars(select(PromptVersion.id).where(PromptVersion.is_active.is_(True))))


def test_seed_creates_one_active_default(client, db):
    versions = client.get("/api/prompts", headers=AUTH).json()
    assert len(versions) == 1
    assert versions[0]["is_active"] and versions[0]["system_prompt"] == DEFAULT_SYSTEM_PROMPT


def test_seed_is_idempotent(app, client, db):
    from app.seed import seed

    seed(db)
    assert db.scalar(select(func.count(PromptVersion.id))) == 1


def test_creating_a_version_never_overwrites(client, db):
    original = client.get("/api/prompts", headers=AUTH).json()[0]
    r = client.post("/api/prompts", json={"name": "v2", "system_prompt": "Be brief."}, headers=AUTH)
    assert r.status_code == 201
    new = r.json()
    assert new["id"] != original["id"] and new["is_active"]

    old = client.get(f"/api/prompts/{original['id']}", headers=AUTH).json()
    assert old["system_prompt"] == original["system_prompt"] and old["name"] == original["name"]
    assert old["is_active"] is False
    assert _active_ids(db) == [new["id"]]


def test_create_without_activating(client, db):
    original = client.get("/api/prompts", headers=AUTH).json()[0]
    new = client.post("/api/prompts", json={"name": "draft", "system_prompt": "x", "activate": False}, headers=AUTH).json()
    assert new["is_active"] is False
    assert _active_ids(db) == [original["id"]]


def test_activate_switches_exactly_one(client, db):
    v1 = client.get("/api/prompts", headers=AUTH).json()[0]["id"]
    v2 = client.post("/api/prompts", json={"name": "v2", "system_prompt": "two"}, headers=AUTH).json()["id"]
    v3 = client.post("/api/prompts", json={"name": "v3", "system_prompt": "three"}, headers=AUTH).json()["id"]
    assert _active_ids(db) == [v3]
    assert client.post(f"/api/prompts/{v1}/activate", headers=AUTH).json()["is_active"]
    assert _active_ids(db) == [v1]
    client.post(f"/api/prompts/{v2}/activate", headers=AUTH)
    assert _active_ids(db) == [v2]
    assert client.post("/api/prompts/999/activate", headers=AUTH).status_code == 404


def test_db_refuses_two_active_versions(db):
    db.add(PromptVersion(name="rogue", system_prompt="x", is_active=True))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_generation_uses_and_records_active_version(client, llm, db):
    v2 = client.post("/api/prompts", json={"name": "v2", "system_prompt": "SYSTEM V2"}, headers=AUTH).json()
    llm.queue("hello")
    data = client.post("/api/generate", json=gen_body(), headers=AUTH).json()
    assert llm.calls[0][0]["content"] == "SYSTEM V2"
    assert data["prompt_version"] == {"id": v2["id"], "name": "v2"}


def test_unnamed_version_gets_a_default_name(client):
    new = client.post("/api/prompts", json={"system_prompt": "x"}, headers=AUTH).json()
    assert new["name"] == "v2"


def test_dashboard_prompt_pages(dash):
    dash.post("/prompts", data={"name": "v2", "system_prompt": "Line A\nLine B\n", "activate": "true"})
    page = dash.get("/prompts")
    assert page.status_code == 200 and "#2 v2" in page.text

    diff = dash.get("/prompts/diff", params={"a": 1, "b": 2})
    assert diff.status_code == 200
    assert '<div class="add">+Line A</div>' in diff.text
    assert 'class="del">-You write LinkedIn messages' in diff.text

    r = dash.post("/prompts/1/activate", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Activate" in r.text


def test_context_is_versioned(client):
    first = client.get("/api/context", headers=AUTH).json()
    assert first["is_placeholder"] is True
    saved = client.put("/api/context", json={"background": "Student at ENSIAS", "location": "Rabat"}, headers=AUTH).json()
    assert saved["background"] == "Student at ENSIAS" and saved["is_placeholder"] is False
    versions = client.get("/api/context/versions", headers=AUTH).json()
    assert [v["id"] for v in versions] == [saved["id"], first["id"]]
    assert client.get("/api/context", headers=AUTH).json()["id"] == saved["id"]
