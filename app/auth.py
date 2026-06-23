"""Dashboard auth helpers.

Gates ONLY the HTML pages (/dashboard, /tables). The JSON API (/api/*), /admin,
/gps, /tg, /health stay open by design.

Credential storage is intentionally simple: a single username/password from env
(DASHBOARD_USERNAME / DASHBOARD_PASSWORD), compared with secrets.compare_digest.
When DASHBOARD_PASSWORD is empty, auth is DISABLED and the app behaves exactly as
before — preserving the keyless / zero-config golden rule.
"""
import secrets
import sys
from pathlib import Path

from fastapi import HTTPException, Request

from .config import settings

_LOGIN_HTML = Path(__file__).with_name("login.html")

# Resolve the session secret ONCE at import: a configured value, or an ephemeral
# random one (sessions then won't survive a restart, but auth still works keyless).
if settings.session_secret:
    _SESSION_SECRET = settings.session_secret
else:
    _SESSION_SECRET = secrets.token_hex(32)
    print(
        "WARNING: SESSION_SECRET unset — generated an ephemeral secret. "
        "Login sessions will not survive a restart; set SESSION_SECRET in .env to persist.",
        file=sys.stderr,
    )


def get_session_secret() -> str:
    return _SESSION_SECRET


def auth_enabled() -> bool:
    """Auth is on only when a dashboard password is configured."""
    return bool(settings.dashboard_password)


def check_credentials(username: str, password: str) -> bool:
    """Constant-time credential check. Returns False when auth is disabled."""
    return (
        auth_enabled()
        and secrets.compare_digest(username, settings.dashboard_username)
        and secrets.compare_digest(password, settings.dashboard_password)
    )


def require_login(request: Request) -> None:
    """FastAPI dependency for the HTML pages.

    Short-circuits to "allow" when auth is disabled (keyless mode). Otherwise
    redirects unauthenticated browser requests to /login. Dependencies can't
    return a Response, so we raise an HTTPException whose 303 + Location header
    the browser follows with a GET.
    """
    if not auth_enabled():
        return
    if request.session.get("user") == settings.dashboard_username:
        return
    raise HTTPException(status_code=303, headers={"Location": "/login"})


def render_login_html(error: str = "") -> str:
    """Render the login page, injecting an optional error message."""
    return _LOGIN_HTML.read_text().replace("{{error}}", error)
