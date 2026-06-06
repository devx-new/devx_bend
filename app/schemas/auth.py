from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    organization_name: str


class AuthSuccessResponse(BaseModel):
    """
    Returned by login, refresh, and OAuth endpoints.
    Tokens are never included in the response body — they are set as HttpOnly cookies.
    """
    success: bool = True
    data: dict  # e.g. {"user_id": "...", "role": "member"}
