import os
from pydantic import field_validator
from pydantic_settings import BaseSettings
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
    log_file: str = os.getenv("log_file", "")
    log_max_bytes: int = int(os.getenv("log_max_bytes", 104857600))
    log_backup_count: int = int(os.getenv("log_backup_count", 5))
    cloudinary_cloud_name: str = os.getenv("cloudinary_cloud_name")
    cloudinary_api_key: str = os.getenv("cloudinary_api_key")
    cloudinary_api_secret: str = os.getenv("cloudinary_api_secret")
    gemini_api_key: str = os.getenv("gemini_api_key", "")
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    # "nvidia" uses Llama-3.3-70B via NVIDIA NIM (free tier)
    # "gemini" uses Gemini 2.5 Flash
    digest_provider: str = os.getenv("DIGEST_PROVIDER", "nvidia")
    huggingface_api_key: str = os.getenv("huggingface_api_key", "")
    github_client_id: str = os.getenv("github_client_id")
    github_client_secret: str = os.getenv("github_client_secret")
    github_redirect_uri: str = os.getenv("github_redirect_uri")
    github_integration_redirect_uri: str = os.getenv("github_integration_redirect_uri", "")
    linear_client_id: str = os.getenv("linear_client_id", "")
    linear_client_secret: str = os.getenv("linear_client_secret", "")
    linear_redirect_uri: str = os.getenv("linear_redirect_uri", "")
    slack_client_id: str = os.getenv("slack_client_id", "")
    slack_client_secret: str = os.getenv("slack_client_secret", "")
    slack_redirect_uri: str = os.getenv("slack_redirect_uri", "")
    slack_signing_secret: str = os.getenv("slack_signing_secret", "")
    jira_client_id: str = os.getenv("jira_client_id", "")
    jira_client_secret: str = os.getenv("jira_client_secret", "")
    jira_redirect_uri: str = os.getenv("jira_redirect_uri", "")
    discord_client_id: str = os.getenv("discord_client_id", "")
    discord_client_secret: str = os.getenv("discord_client_secret", "")
    discord_redirect_uri: str = os.getenv("discord_redirect_uri", "")
    discord_public_key: str = os.getenv("discord_public_key", "")
    discord_bot_token: str = os.getenv("discord_bot_token", "")
    allowed_origins: str = ""
    frontend_url: str = os.getenv("frontend_url", "http://localhost:5173")
    backend_url: str = os.getenv("backend_url", "")
    cookie_secure: bool = os.getenv("cookie_secure", "False").lower() in ("true", "1")
    cookie_samesite: str = os.getenv("cookie_samesite", "lax")
    csrf_token_expire_minutes: int = int(os.getenv("csrf_token_expire_minutes", 60))
    super_admin_secret: str = os.getenv("SUPER_ADMIN_SECRET", "")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("database_url")
    @classmethod
    def ensure_async_driver(cls, v: str) -> str:
        # Railway (and some other providers) supply postgresql:// — rewrite to asyncpg scheme
        if v.startswith("postgresql://") or v.startswith("postgres://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        return v

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
