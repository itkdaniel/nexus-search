"""
Shared fixtures for nexus-search tests.

All tests run against:
  - In-memory SQLite (aiosqlite) — no external PostgreSQL needed
  - fakeredis — no external Redis needed

The `app` fixture runs the full lifespan (table creation + index warm-up)
so every test starts with a fully initialized service.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.algorithms.search import reset_indexes
from app.config import Settings
from app.main import create_app


@pytest.fixture(scope="function")
def settings() -> Settings:
    """In-memory test settings — no external services needed."""
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379",  # overridden by fakeredis fixture
        debug=True,
        jwt_secret="test-secret-nexus-search",
        port=8002,
    )


@pytest_asyncio.fixture(scope="function")
async def app(settings, fake_redis):
    """
    Full FastAPI app with lifespan (tables created, indexes warmed).
    Uses in-memory SQLite + fakeredis — no external deps.
    """
    reset_indexes()
    application = create_app(settings)
    # Patch redis to use fakeredis
    import app as app_module
    import app.database as db_module
    db_module._redis_client = fake_redis
    async with application.router.lifespan_context(application):
        yield application
    reset_indexes()


@pytest_asyncio.fixture(scope="function")
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    """httpx.AsyncClient bound to the test app — no real HTTP."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest_asyncio.fixture(scope="function")
async def admin_client(app, settings) -> AsyncGenerator[AsyncClient, None]:
    """AsyncClient with a valid admin JWT header."""
    token = _make_token(settings.jwt_secret, role="admin")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture(scope="function")
def fake_redis():
    """In-memory fakeredis instance — no Redis server needed."""
    import fakeredis.aioredis as fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


def _make_token(secret: str, role: str = "admin", exp_offset: int = 86400) -> str:
    """Build a minimal HMAC-SHA256 JWT for testing."""
    payload = {"sub": "1", "role": role, "exp": int(time.time()) + exp_offset}
    header = {"alg": "HS256", "typ": "JWT"}

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    h64, p64 = b64(header), b64(payload)
    sig = hmac.new(secret.encode(), f"{h64}.{p64}".encode(), hashlib.sha256).digest()
    s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{h64}.{p64}.{s64}"


@pytest_asyncio.fixture(scope="function")
async def seeded_client(app, settings):
    """
    Client with 4 sample projects already inserted.
    Returns (client, admin_client, project_ids).
    """
    token = _make_token(settings.jwt_secret, role="admin")
    projects = [
        {"name": "Auth Service", "description": "JWT authentication microservice",
         "type": "backend", "tags": ["auth", "jwt", "security"]},
        {"name": "Analytics Engine", "description": "Real-time data streaming pipeline",
         "type": "backend", "tags": ["kafka", "streaming", "data"]},
        {"name": "Commerce API", "description": "Headless e-commerce GraphQL API",
         "type": "backend", "tags": ["graphql", "stripe", "ecommerce"]},
        {"name": "Auth Gateway", "description": "OAuth2 gateway service",
         "type": "backend", "tags": ["auth", "oauth", "security"]},
    ]
    headers = {"Authorization": f"Bearer {token}"}
    ids = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as ac:
        for proj in projects:
            resp = await ac.post("/v1/projects/", json=proj)
            assert resp.status_code == 201, resp.text
            ids.append(resp.json()["id"])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac, ids
