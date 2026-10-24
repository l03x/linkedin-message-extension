import json

import httpx
import pytest

from app.config import Settings
from app.llm import LLMClient, LLMError


def _client(handler, **overrides) -> LLMClient:
    # Explicit values, so LLM_* variables in the environment (e.g. inside the container) don't leak in.
    settings = {"llm_effort": "", "llm_timeout_seconds": 180.0, **overrides}
    s = Settings(app_token="t", llm_base_url="http://llm.test/v1/", llm_api_key="chk_abc", llm_model="sonnet", **settings)
    return LLMClient(s, transport=httpx.MockTransport(handler))


def _ok(text="Hello"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def test_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return _ok("Hi")

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert _client(handler).complete(msgs) == "Hi"
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer chk_abc"
    assert seen["body"] == {"model": "sonnet", "messages": msgs}


def test_effort_is_sent_only_when_configured():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok()

    client = _client(handler, llm_effort="low")
    client.complete([{"role": "user", "content": "u"}])
    assert bodies[0]["effort"] == "low"
    # A per-call effort (picked in the dashboard) wins; "" means send none.
    client.complete([{"role": "user", "content": "u"}], effort="max")
    client.complete([{"role": "user", "content": "u"}], effort="")
    assert bodies[1]["effort"] == "max" and "effort" not in bodies[2]


@pytest.mark.parametrize(
    "response, status, code, fragment",
    [
        (httpx.Response(401, json={"error": {"message": "Invalid API key.", "code": "invalid_api_key"}}), 502, "llm_auth", "Invalid API key."),
        (httpx.Response(403, json={"error": {"message": "Model not allowed"}}), 502, "llm_forbidden", "Model not allowed"),
        (httpx.Response(429, json={"error": {"message": "Retry in 12s"}}), 429, "llm_rate_limited", "Retry in 12s"),
        (httpx.Response(500, text="boom"), 502, "llm_error", "HTTP 500"),
        (httpx.Response(200, json={"nope": True}), 502, "llm_bad_response", "choices"),
        (httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}), 502, "llm_empty", "empty"),
    ],
)
def test_http_errors_are_mapped(response, status, code, fragment):
    with pytest.raises(LLMError) as e:
        _client(lambda r: response).complete([{"role": "user", "content": "u"}])
    assert (e.value.status, e.value.code) == (status, code)
    assert fragment in e.value.message


def test_connection_refused_explains_docker_networking():
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    with pytest.raises(LLMError) as e:
        _client(handler).complete([{"role": "user", "content": "u"}])
    assert e.value.code == "llm_unreachable" and "host.docker.internal" in e.value.message
    assert "http://llm.test/v1" in e.value.message


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMError) as e:
        _client(handler, llm_timeout_seconds=42).complete([{"role": "user", "content": "u"}])
    assert e.value.status == 504 and "42s" in e.value.message


def test_web_search_and_json_schema_are_sent_and_searches_read():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "server_tool_use": {"web_search_requests": 3}},
            },
        )

    schema = {"type": "object"}
    reply = _client(handler).complete([{"role": "user", "content": "u"}], web_search=True, json_schema=schema)
    assert reply == "{}" and reply.web_searches == 3
    assert seen["body"]["web_search_options"] == {}
    assert seen["body"]["response_format"] == {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}


def test_plain_calls_send_neither_and_report_no_searches():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _ok("Hi")

    reply = _client(handler).complete([{"role": "user", "content": "u"}])
    assert reply.web_searches is None
    assert "web_search_options" not in seen["body"] and "response_format" not in seen["body"]


def test_web_search_disabled_is_explained():
    response = httpx.Response(400, json={"error": {"message": "Web search is not enabled on this server.", "code": "web_search_disabled"}})
    with pytest.raises(LLMError) as e:
        _client(lambda r: response).complete([{"role": "user", "content": "u"}], web_search=True)
    assert e.value.code == "web_search_disabled" and "Allow web search" in e.value.message
