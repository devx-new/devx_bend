from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.v1.router import router as v1_router
from app.core.errors import exception_handlers
from app.core.logging import setup_logging
from app.core.tenant import TenantMiddleware

_CORS_ALLOW_HEADERS = "Authorization, Content-Type, X-Tenant-ID, X-CSRF-Token"
_CORS_ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"


class PermissiveCORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        if request.method == "OPTIONS":
            response = Response(status_code=200)
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = _CORS_ALLOW_METHODS
            response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
            response.headers["Access-Control-Max-Age"] = "600"
            return response

        response = await call_next(request)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response


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
