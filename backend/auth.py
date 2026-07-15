"""Authentication helpers: password hashing, session tokens, current-user
dependency, and the auth gate middleware."""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as OrmSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as _StarletteJSONResponse

from db import SessionLocal, User, UserSession, get_db

_logger = logging.getLogger("alphafinder.auth")

ADMIN_EMAIL = "akisami24@gmail.com"
SESSION_COOKIE = "af_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

# Paths reachable without an active session. Everything else under /api/ is gated.
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/signup",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
}


def is_valid_email(email: str) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def create_session(db: OrmSession, user: User, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    sess = UserSession(
        token=token,
        user_id=user.id,
        user_agent=request.headers.get("user-agent", "")[:400],
        ip_address=_client_ip(request),
    )
    db.add(sess)
    db.commit()
    return token


def get_current_user(request: Request, db: OrmSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sess = (
        db.query(UserSession)
        .filter(UserSession.token == token, UserSession.revoked == False)  # noqa: E712
        .first()
    )
    if not sess:
        raise HTTPException(status_code=401, detail="Session invalid or revoked")
    user = db.query(User).filter(User.id == sess.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    sess.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Requires a valid, non-revoked session cookie for every /api/* route
    except the public auth endpoints and the health check."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            _logger.warning("[AUTH] 401 no-cookie path=%s ip=%s ua=%s", path, _client_ip(request), request.headers.get("user-agent", "")[:120])
            return _StarletteJSONResponse({"error": "authentication required"}, status_code=401)
        db = SessionLocal()
        try:
            sess = (
                db.query(UserSession)
                .filter(UserSession.token == token, UserSession.revoked == False)  # noqa: E712
                .first()
            )
            if not sess:
                _logger.warning("[AUTH] 401 session-not-found-or-revoked path=%s token_prefix=%s ip=%s ua=%s", path, token[:8], _client_ip(request), request.headers.get("user-agent", "")[:120])
                return _StarletteJSONResponse({"error": "authentication required"}, status_code=401)
            sess.last_seen_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
        return await call_next(request)