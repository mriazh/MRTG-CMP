"""Authentication routes: login form, credential check, session teardown."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette import status

from mrtg_cmp.auth import (
    SESSION_COOKIE_NAME,
    create_user_session,
    get_current_user_optional,
    get_db,
    revoke_session,
    validate_redirect_url,
    verify_password,
)
from mrtg_cmp.config import settings
from mrtg_cmp.db import Database

from ..dependencies import templates

router = APIRouter()

LOGIN_CONTEXT_KEYS = (
    "site_name",
    "location_name",
    "uplink_name",
    "app_title",
)


def _login_context(
    next_url: str,
    error: str | None,
    current_user: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "next_url": next_url,
        "error": error,
        "current_user": current_user,
        "site_name": settings.site_name,
        "location_name": settings.location_name,
        "uplink_name": settings.uplink_name,
        "app_title": settings.app_title,
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = "/",
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> Any:
    """Render login form or redirect to dashboard if already authenticated."""
    # Validate redirect URL to prevent open redirects (CWE-601)
    next = validate_redirect_url(next)

    if current_user is not None:
        return RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_login_context(next, None, None),
    )


@router.post("/login")
def process_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember_me: bool = Form(False),
    next: str = Form("/"),
    db: Database = Depends(get_db),
) -> Any:
    """Validate credentials, issue session cookie, and redirect."""
    # Validate redirect URL to prevent open redirects (CWE-601)
    next = validate_redirect_url(next)

    user = db.get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_login_context(next, "Invalid username or password", None),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token, ttl = create_user_session(
        database=db,
        user_id=user["id"],
        username=user["username"],
        remember_me=remember_me,
    )

    response = RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return response


@router.get("/logout")
@router.post("/logout")
def process_logout(
    request: Request,
    db: Database = Depends(get_db),
) -> Response:
    """Revoke current session and redirect to login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(db, token)

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


