from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.responses import Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send, Message

from app.api.v1.router import router as v1_router
from app.core.errors import exception_handlers
from app.core.logging import setup_logging
from app.core.tenant import TenantMiddleware

_CORS_ALLOW_HEADERS = "Authorization, Content-Type, X-Tenant-ID, X-CSRF-Token"
_CORS_ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"


class PermissiveCORSMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin", "")

        if not origin:
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            request_headers = Headers(scope=scope)
            requested = request_headers.get("access-control-request-headers", _CORS_ALLOW_HEADERS)
            response = Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
                    "Access-Control-Allow-Headers": requested,
                    "Access-Control-Max-Age": "600",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("Access-Control-Allow-Origin", origin)
                headers.append("Access-Control-Allow-Credentials", "true")
            await send(message)

        await self.app(scope, receive, send_with_cors)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="DFP API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(PermissiveCORSMiddleware)
    app.add_middleware(TenantMiddleware)

    for exc_type, handler in exception_handlers.items():
        app.add_exception_handler(exc_type, handler)

    app.include_router(v1_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
