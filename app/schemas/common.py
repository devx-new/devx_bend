from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail


class Meta(BaseModel):
    page: int = 1
    per_page: int = 20
    total: int = 0


class SuccessResponse(BaseModel):
    success: bool = True
    data: dict | list | None = None
    meta: Meta | None = None


class PaginationMeta(BaseModel):
    page: int
    per_page: int
    total: int
