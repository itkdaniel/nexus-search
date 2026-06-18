"""
nexus-search configuration — reads from environment variables.
Pydantic-settings v2 for type-safe, validated, injectable config.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "nexus-search"
    version: str = "1.0.0"
    debug: bool = False
    port: int = 8002

    database_url: str = Field(
        default="sqlite+aiosqlite:///:memory:",
        description="Async SQLAlchemy DB URL (PostgreSQL in prod, SQLite in tests)",
    )
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL (used for cache-aside pattern)",
    )
    redis_ttl: int = Field(default=300, description="Cache TTL in seconds")

    jwt_secret: str = Field(
        default="change-me-in-production",
        description="HMAC-SHA256 signing secret for admin JWT",
    )

    model_config = {"env_file": ".env", "case_sensitive": False}
