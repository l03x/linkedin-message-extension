"""Client for an OpenAI-compatible /v1/chat/completions endpoint (e.g. claude-headless-api)."""

from typing import Protocol

import httpx

from app.config import Settings


class LLMError(Exception):
    """A failure the API reports to the extension as {"detail": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ChatClient(Protocol):
    model: str

    def complete(self, messages: list[dict]) -> str: ...


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

    def complete(self, messages: list[dict]) -> str:
        payload: dict = {"model": self.model, "messages": messages}
        if self.effort:
            payload["effort"] = self.effort
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
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(502, "llm_bad_response", "The LLM API returned a response without choices[0].message.content.") from e
        if not isinstance(content, str) or not content.strip():
            raise LLMError(502, "llm_empty", "The LLM API returned an empty message.")
        return content
