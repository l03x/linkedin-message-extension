"""Client for an OpenAI-compatible /v1/chat/completions endpoint (e.g. claude-headless-api)."""

import json
import re
from typing import Protocol

import httpx

from app.config import Settings


# Values claude-headless-api accepts for its non-standard "effort" field, lowest first.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class LLMError(Exception):
    """A failure the API reports to the extension as {"detail": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Completion(str):
    """The reply text, plus what the API reported about the run.

    `web_searches` is claude-headless-api's `usage.server_tool_use.web_search_requests`; None when the API
    doesn't report it, which research treats as "not searched".
    """

    web_searches: int | None

    def __new__(cls, text: str, web_searches: int | None = None) -> "Completion":
        obj = super().__new__(cls, text)
        obj.web_searches = web_searches
        return obj


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def json_object(raw: str) -> dict:
    """The JSON object in a model reply, tolerating code fences and text around it. ValueError if there is none."""
    text = raw.strip()
    fence = _FENCE.match(text)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    obj = json.loads(text[start : end + 1])  # json.JSONDecodeError is a ValueError
    if not isinstance(obj, dict):
        raise ValueError("the reply's JSON is not an object")
    return obj


class ChatClient(Protocol):
    model: str
    effort: str

    def complete(
        self,
        messages: list[dict],
        effort: str | None = None,
        *,
        web_search: bool = False,
        json_schema: dict | None = None,
    ) -> Completion: ...


def _upstream_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(body.get("detail"), str):
            return body["detail"]
    return resp.text[:300]


def _upstream_code(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return str(err.get("code") or "") if isinstance(err, dict) else ""


def _web_searches(body: dict) -> int | None:
    usage = body.get("usage") if isinstance(body, dict) else None
    server = usage.get("server_tool_use") if isinstance(usage, dict) else None
    n = server.get("web_search_requests") if isinstance(server, dict) else None
    return n if isinstance(n, int) else None


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.effort = settings.llm_effort.strip()
        self.timeout_s = settings.llm_timeout_seconds
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(self.timeout_s, connect=10.0),
            transport=transport,
        )

    def complete(
        self,
        messages: list[dict],
        effort: str | None = None,
        *,
        web_search: bool = False,
        json_schema: dict | None = None,
    ) -> Completion:
        """`effort` overrides LLM_EFFORT for this call; "" sends none. `web_search` lets the model search the web
        (claude-headless-api's `web_search_options`); `json_schema` asks for structured output."""
        effort = self.effort if effort is None else effort
        payload: dict = {"model": self.model, "messages": messages}
        if effort:
            payload["effort"] = effort
        if web_search:
            payload["web_search_options"] = {}
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        try:
            resp = self._client.post("/chat/completions", json=payload)
        except httpx.ConnectError as e:
            raise LLMError(
                502,
                "llm_unreachable",
                f"Can't reach the LLM API at {self.base_url} ({e}). Is it running? From inside Docker, a server "
                "on your host is at http://host.docker.internal:<port>/v1 and must listen on 0.0.0.0.",
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                504,
                "llm_timeout",
                f"The LLM API did not answer within {self.timeout_s:.0f}s. Try again, or raise LLM_TIMEOUT_SECONDS.",
            ) from e
        except httpx.HTTPError as e:
            raise LLMError(502, "llm_error", f"Request to the LLM API failed: {e}") from e

        if resp.status_code == 400 and _upstream_code(resp) == "web_search_disabled":
            raise LLMError(
                502,
                "web_search_disabled",
                "The LLM API has web search turned off. In claude-headless-api, tick \"Allow web search\" in Settings.",
            )
        if resp.status_code == 401:
            raise LLMError(502, "llm_auth", f"The LLM API rejected LLM_API_KEY: {_upstream_message(resp)}")
        if resp.status_code == 403:
            raise LLMError(502, "llm_forbidden", f"The LLM API refused the request: {_upstream_message(resp)}")
        if resp.status_code == 429:
            raise LLMError(429, "llm_rate_limited", f"The LLM API is rate limiting: {_upstream_message(resp)}")
        if resp.status_code >= 400:
            raise LLMError(
                502, "llm_error", f"The LLM API returned HTTP {resp.status_code}: {_upstream_message(resp)}"
            )

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(502, "llm_bad_response", "The LLM API returned a response without choices[0].message.content.") from e
        if not isinstance(content, str) or not content.strip():
            raise LLMError(502, "llm_empty", "The LLM API returned an empty message.")
        return Completion(content, _web_searches(body))
