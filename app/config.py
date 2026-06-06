import os
from pydantic import field_validator
from pydantic_settings import BaseSettings
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    database_url: str = os.getenv("database_url")
    redis_url: str = os.getenv("redis_url")
    secret_key: str = os.getenv("secret_key")
    jwt_algorithm: str = os.getenv("jwt_algorithm")
    access_token_expire_minutes: int = int(os.getenv("access_token_expire_minutes", 15))
    refresh_token_expire_days: int = int(os.getenv("refresh_token_expire_days", 7))
    bcrypt_cost: int = int(os.getenv("bcrypt_cost", 12))
    log_level: str = os.getenv("log_level")
    log_file: str = os.getenv("log_file")
    log_max_bytes: int = int(os.getenv("log_max_bytes", 104857600))
    log_backup_count: int = int(os.getenv("log_backup_count", 5))
    cloudinary_cloud_name: str = os.getenv("cloudinary_cloud_name")
    cloudinary_api_key: str = os.getenv("cloudinary_api_key")
    cloudinary_api_secret: str = os.getenv("cloudinary_api_secret")
    gemini_api_key: str = os.getenv("gemini_api_key")
    github_client_id: str = os.getenv("github_client_id")
    github_client_secret: str = os.getenv("github_client_secret")
    github_redirect_uri: str = os.getenv("github_redirect_uri")
    allowed_origins: str = os.getenv("allowed_origins")
    cookie_secure: bool = os.getenv("cookie_secure", "False").lower() in ("true", "1")
    cookie_samesite: str = os.getenv("cookie_samesite")
    csrf_token_expire_minutes: int = int(os.getenv("csrf_token_expire_minutes", 60))

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_strong(cls, v: str) -> str:
        if v == "change-me-in-production" or len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be set to a strong random value (≥32 chars). "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v


settings = Settings()
