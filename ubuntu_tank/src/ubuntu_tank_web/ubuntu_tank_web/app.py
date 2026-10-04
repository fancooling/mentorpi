"""
FastAPI Application Factory for MentorPi Pi 5 Web Control.

Configures:
1. Strict Content Security Policy (CSP) and defense-in-depth security headers (X-Frame-Options: DENY, nosniff).
2. Request payload limit (64 KiB maximum) returning 413 Payload Too Large.
3. Cross-origin validation middleware rejecting cross-origin state mutations (HTTP 403).
4. REST API routes (/api/v1/*) and WebSocket control streaming (/api/v1/control).
5. Static PWA asset serving with fallback to index.html for Single Page Application navigation.
6. Offline OpenAPI documentation without external CDN dependencies.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from ubuntu_tank_protocol.camera_client import CameraIpcClient
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.constants import (
    DEFAULT_CAMERA_SOCKET_PATH,
    MAX_IPC_MESSAGE_BYTES,
)

from .lifecycle_client import LifecycleClient
from .operator_relay import OperatorRelay
from .routes_api import router as api_router
from .routes_ws import router as ws_router

logger = logging.getLogger(__name__)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce strict 64 KiB maximum payload limit for all HTTP requests."""

    def __init__(self, app: FastAPI, max_bytes: int = MAX_IPC_MESSAGE_BYTES) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "detail": f"Request body exceeds {self.max_bytes} bytes limit"
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defensive security and Content Security Policy (CSP) headers."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' wss: https:; "
            "frame-ancestors 'none'; "
            "base-uri 'self';"
        )
        return response


class OriginValidationMiddleware(BaseHTTPMiddleware):
    """Validate Origin header on mutating HTTP requests to prevent cross-origin abuse."""

    def __init__(self, app: FastAPI, allowed_origins: list[str]) -> None:
        super().__init__(app)
        self.allowed_origins = [o.rstrip("/") for o in allowed_origins]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin:
                clean_origin = origin.rstrip("/")
                if clean_origin not in self.allowed_origins:
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={
                            "detail": f"Cross-origin request from '{origin}' rejected",
                            "error": "CROSS_ORIGIN_DENIED",
                        },
                    )
        return await call_next(request)


def create_app(
    config: WebControlConfig | None = None,
    static_dir: str | None = None,
    relay: OperatorRelay | None = None,
    lifecycle: LifecycleClient | None = None,
    camera_client: CameraIpcClient | None = None,
    operator_socket_path: str | None = None,
    lifecycle_socket_path: str | None = None,
    camera_socket_path: str | None = None,
) -> FastAPI:
    """Create and configure the production FastAPI web application."""
    if config is None:
        config = WebControlConfig()

    op_sock = (
        operator_socket_path
        if operator_socket_path is not None
        else config.operator_socket_path
    )
    lc_sock = (
        lifecycle_socket_path
        if lifecycle_socket_path is not None
        else config.lifecycle_socket_path
    )
    cam_sock = (
        camera_socket_path
        if camera_socket_path is not None
        else getattr(config, "camera_socket_path", DEFAULT_CAMERA_SOCKET_PATH)
    )

    app = FastAPI(
        title="MentorPi Tank Web Control API",
        version="1.0.0",
        docs_url=None,  # Disabled external CDN Swagger in production
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )

    # Attach shared dependencies to app state
    app.state.config = config
    app.state.operator_socket_path = op_sock
    app.state.lifecycle_socket_path = lc_sock
    app.state.camera_socket_path = cam_sock
    app.state.operator_relay = (
        relay if relay is not None else OperatorRelay(socket_path=op_sock)
    )
    app.state.lifecycle_client = (
        lifecycle if lifecycle is not None else LifecycleClient(socket_path=lc_sock)
    )
    app.state.camera_client = (
        camera_client
        if camera_client is not None
        else CameraIpcClient(socket_path=cam_sock)
    )

    # Middleware stack
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        OriginValidationMiddleware, allowed_origins=config.allowed_origins
    )
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_IPC_MESSAGE_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Include REST and WebSocket routers
    app.include_router(api_router)
    app.include_router(ws_router)

    # Resolve static assets directory
    if static_dir is None:
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../..")
        )
        dev_static = os.path.join(repo_root, "ubuntu_tank/web/dist")
        prod_static = "/opt/ubuntu_tank/current/web/dist"
        if os.path.isdir(prod_static):
            static_dir = prod_static
        elif os.path.isdir(dev_static):
            static_dir = dev_static
        else:
            static_dir = dev_static

    if os.path.isdir(static_dir):
        assets_dir = os.path.join(static_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        index_file = os.path.join(static_dir, "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa_or_static(full_path: str) -> Response:
            # If path points to an existing file in static_dir, serve it
            target_path = os.path.join(static_dir, full_path)
            if os.path.isfile(target_path):
                return FileResponse(target_path)
            # Otherwise, fall back to index.html for SPA client routing
            if os.path.isfile(index_file):
                return FileResponse(index_file)
            return Response(
                content="MentorPi Tank Web Service Active", media_type="text/plain"
            )
    else:

        @app.get("/", include_in_schema=False)
        async def default_root() -> Response:
            return Response(
                content="MentorPi Tank Web Service Active (static UI assets not built yet)",
                media_type="text/plain",
            )

    return app
