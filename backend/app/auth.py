"""One static token: as a bearer header for /api, and as an HMAC-derived cookie for the dashboard."""

import hashlib
import hmac

from fastapi import Depends, HTTPException, Request

from app.config import Settings
from app.deps import get_settings

COOKIE_NAME = "outreach_session"


class LoginRedirect(Exception):
    """Raised by dashboard routes; the app turns it into a redirect to /login."""


def token_matches(sent: str | None, settings: Settings) -> bool:
    return bool(sent) and hmac.compare_digest(sent.encode(), settings.app_token.encode())


def cookie_value(settings: Settings) -> str:
    # Derived from the token, so the cookie never holds the token itself and rotating the token logs out.
    return hmac.new(settings.app_token.encode(), b"dashboard-v1", hashlib.sha256).hexdigest()


def require_bearer(request: Request, settings: Settings = Depends(get_settings)) -> None:
    auth = request.headers.get("authorization", "")
    sent = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token_matches(sent, settings):
        raise HTTPException(
            401,
            detail={"code": "unauthorized", "message": "Missing or wrong token. Check the token in the extension settings."},
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_dashboard(request: Request, settings: Settings = Depends(get_settings)) -> None:
    sent = request.cookies.get(COOKIE_NAME, "")
    if not sent or not hmac.compare_digest(sent, cookie_value(settings)):
        raise LoginRedirect()
