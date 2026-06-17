from pydantic import BaseModel, EmailStr, field_validator


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


class InviteMemberRequest(BaseModel):
    full_name: str
    email: EmailStr

    @field_validator("full_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("full_name cannot be empty")
        return v


class InviteMemberResponse(BaseModel):
    success: bool = True
    data: dict  # {user_id, full_name, email, password, role}
