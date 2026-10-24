import pytest

from app.config import Settings
from app.main import create_app
from tests.conftest import AUTH, TOKEN


@pytest.mark.parametrize("path", ["/api/me", "/api/options", "/api/prompts", "/api/context", "/api/generations"])
def test_api_requires_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers=AUTH).status_code == 200


def test_write_routes_require_token(client):
    assert client.post("/api/generate", json={}).status_code == 401
    assert client.post("/api/generations/1/sent", json={"final_text": "x"}).status_code == 401
    assert client.post("/api/prompts", json={"system_prompt": "x"}).status_code == 401


def test_health_is_open(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_options_lists_modes_and_languages(client):
    data = client.get("/api/options", headers=AUTH).json()
    assert [m["key"] for m in data["modes"]] == ["connection_note", "message", "inmail"]
    assert {lang["key"] for lang in data["languages"]} == {"en", "fr", "ar"}
    assert next(lang for lang in data["languages"] if lang["key"] == "ar")["rtl"] is True


def test_dashboard_login_flow(client):
    r = client.get("/history", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.get("/history", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers.get("HX-Redirect") == "/login"

    r = client.post("/login", data={"token": "wrong"})
    assert r.status_code == 401 and "Wrong token" in r.text

    r = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.cookies.get("outreach_session")
    assert cookie and TOKEN not in cookie
    for page in ["/history", "/stats", "/prompts", "/context"]:
        assert client.get(page).status_code == 200, page

    client.post("/logout")
    assert client.get("/history", follow_redirects=False).status_code == 303


def test_dashboard_cookie_does_not_work_as_api_token(dash):
    assert dash.get("/api/me").status_code == 401


def test_cors_preflight_from_extension(client):
    r = client.options(
        "/api/generate",
        headers={
            "Origin": "chrome-extension://abcdefghijklmnop",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "chrome-extension://abcdefghijklmnop"
    r = client.options("/api/generate", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_missing_token_refuses_to_start():
    with pytest.raises(RuntimeError, match="APP_TOKEN"):
        create_app(Settings(app_token="change-me", database_url="sqlite://"))


def test_favicon_is_public(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert client.get("/static/favicon.svg").status_code == 200
