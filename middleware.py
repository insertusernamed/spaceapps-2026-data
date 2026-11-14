"""Request IDs, gzip compression and opt-in cross-origin access for every response."""
from __future__ import annotations

import logging
import os
import re
import time
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

REQUEST_ID_HEADER = "X-Request-Id"
# Matched when CORS_ORIGINS includes the word "localhost": any port on this machine.
LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# A child of uvicorn's logger, so lines appear wherever uvicorn writes its own output.
log = logging.getLogger("uvicorn.error.requests")


def cors_origins(value: str | None) -> list[str]:
    """Parse CORS_ORIGINS: comma-separated origins, "localhost" for any local port, or * for any origin."""
    return [origin.strip().rstrip("/") for origin in (value or "").split(",") if origin.strip()]


class RequestIdMiddleware:
    """Reuse a caller's X-Request-Id when it is safe to echo, otherwise create one.

    The ID is returned on every response and logged with method, path, status and
    duration, so a client can quote it when reporting a failure.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"").decode("latin-1")
        request_id = incoming if _SAFE_ID.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (REQUEST_ID_HEADER.lower().encode(), request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info("%s %s %s %d %.1fms", request_id, scope["method"], scope["path"], status, (time.perf_counter() - started) * 1000)


def install(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)
    # Level 5 compresses map pages about 4x at a fraction of level 9's cost.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    origins = cors_origins(os.environ.get("CORS_ORIGINS"))
    if not origins:
        return  # CORS is off: browsers on other origins are refused, same-origin and server calls work.
    local = "localhost" in origins
    origins = [o for o in origins if o != "localhost"]
    # Added last so it runs first and answers preflight requests itself.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=LOCAL_ORIGINS if local and "*" not in origins else None,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
        max_age=600,
    )
