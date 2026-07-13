from __future__ import annotations

import secrets

from app.database import init_database
from fastapi import (
    FastAPI,
    Request,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers import account_routes, auth_routes, pages, paste_routes


app = FastAPI(title="Rivulet Bin")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        if "csrf_token" not in request.cookies:
            token = secrets.token_hex(32)
            is_https = request.url.scheme == "https"
            response.set_cookie("csrf_token", token, httponly=False, secure=is_https, samesite="strict", max_age=86400)
        return response

app.add_middleware(SecurityHeadersMiddleware)

@app.on_event("startup")
def on_startup() -> None:
    init_database()


app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(account_routes.router)
app.include_router(paste_routes.router)
