from fastapi import Request
from fastapi.responses import JSONResponse


class AppException(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


class NotFoundException(AppException):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(code="NOT_FOUND", message=message, status_code=404)


class ValidationException(AppException):
    def __init__(self, message: str = "Validation failed"):
        super().__init__(code="VALIDATION_ERROR", message=message, status_code=422)


class AuthException(AppException):
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(code="AUTH_ERROR", message=message, status_code=401)


class ForbiddenException(AppException):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(code="FORBIDDEN", message=message, status_code=403)


class ConflictException(AppException):
    def __init__(self, message: str = "Resource already exists"):
        super().__init__(code="CONFLICT", message=message, status_code=409)


class RateLimitException(AppException):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(code="RATE_LIMIT", message=message, status_code=429)


exception_handlers: dict[type[Exception], callable] = {}


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": {"code": exc.code, "message": exc.message},
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"},
        },
    )


exception_handlers[AppException] = app_exception_handler
exception_handlers[Exception] = unhandled_exception_handler
