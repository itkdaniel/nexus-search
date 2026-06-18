"""
nexus-search — Standalone BM25 Search & Recommendation Service.

Architecture:
  - FastAPI factory pattern (create_app(settings)) for testability
  - Async PostgreSQL via SQLAlchemy 2.0
  - Redis cache-aside (5-min TTL, pipeline invalidation)
  - Persistent BM25 inverted index (built O(n) at startup, O(k) queries)
  - Persistent tag-graph (built O(V+E) at startup, BFS O(V+E) queries)
  - HMAC-SHA256 JWT auth for admin write endpoints

Endpoints:
  GET  /health                    — liveness / readiness probe
  GET  /info                      — service metadata
  GET  /v1/projects               — list with BM25 search + tag filter
  GET  /v1/projects/{id}          — single project (cache-aside)
  POST /v1/projects               — create (admin JWT)
  PATCH /v1/projects/{id}         — update + index update (admin JWT)
  DELETE /v1/projects/{id}        — delete + index removal (admin JWT)
  GET  /v1/projects/{id}/related  — BFS tag-graph recommendations
  GET  /v1/search                 — unified BM25+fuzzy search endpoint
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.algorithms.search import get_bm25_index, get_tag_graph, reset_indexes
from app.auth import configure_auth
from app.config import Settings
from app.database import (
    close_connections,
    configure_engine,
    configure_redis,
    create_tables,
    get_db,
    get_redis,
)
from app.models.project import ProjectModel, ProjectResponse
from app.routers.projects import router as projects_router
from app.routers.search import router as search_router

logger = structlog.get_logger(__name__)

_start_time: float = 0.0


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Application factory.
    Pass a Settings instance to override defaults (used in tests).
    """
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _start_time
        _start_time = time.monotonic()
        logger.info("Starting nexus-search", port=settings.port, debug=settings.debug)

        # Warn if default JWT secret is used in production
        if not settings.debug and settings.jwt_secret == "change-me-in-production":
            logger.warning(
                "INSECURE: jwt_secret is the default placeholder — set JWT_SECRET env var"
            )

        # 1. Wire connections
        configure_engine(settings)
        configure_redis(settings)
        configure_auth(settings)

        # 2. Bootstrap tables (dev/test; production uses Alembic)
        await create_tables()

        # 3. Warm up indexes: load all projects and build BM25 + TagGraph
        reset_indexes()
        await _warm_indexes()

        yield

        logger.info("Shutting down nexus-search")
        await close_connections()

    app = FastAPI(
        title="nexus-search",
        version=settings.version,
        description=(
            "Standalone BM25 search and BFS recommendation service for NexusConsult. "
            "Provides full-text search, tag-graph recommendations, and fuzzy matching."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(projects_router)
    app.include_router(search_router)

    @app.get("/health", tags=["health"])
    async def health():
        uptime = time.monotonic() - _start_time
        redis_ok = False
        try:
            r = get_redis()
            redis_ok = bool(await r.ping())
        except Exception:
            pass
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.version,
            "uptime_seconds": round(uptime, 2),
            "redis": "ok" if redis_ok else "unavailable",
        }

    @app.get("/info", tags=["health"])
    async def info():
        return {
            "name": settings.app_name,
            "version": settings.version,
            "port": settings.port,
            "endpoints": [
                {"method": "GET",    "path": "/health",                      "auth": False},
                {"method": "GET",    "path": "/info",                        "auth": False},
                {"method": "GET",    "path": "/v1/projects",                 "auth": False},
                {"method": "GET",    "path": "/v1/projects/{id}",            "auth": False},
                {"method": "POST",   "path": "/v1/projects",                 "auth": True},
                {"method": "PATCH",  "path": "/v1/projects/{id}",            "auth": True},
                {"method": "DELETE", "path": "/v1/projects/{id}",            "auth": True},
                {"method": "GET",    "path": "/v1/projects/{id}/related",    "auth": False},
                {"method": "GET",    "path": "/v1/search",                   "auth": False},
            ],
        }

    @app.exception_handler(Exception)
    async def global_handler(request, exc):
        import uuid as _uuid
        request_id = str(_uuid.uuid4())
        logger.error("unhandled exception", path=request.url.path, error=str(exc), request_id=request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "code": "INTERNAL_ERROR",
                "details": str(exc) if settings.debug else None,
                "request_id": request_id,
            },
        )

    return app


async def _warm_indexes() -> None:
    """
    Load all projects from DB and build BM25 + TagGraph at startup.
    O(n * avg_doc_length) for BM25, O(V + E) for the tag graph.
    """
    try:
        async with get_db() as db:
            result = await db.execute(select(ProjectModel))
            docs = [
                ProjectResponse.model_validate(p).model_dump(mode="json")
                for p in result.scalars().all()
            ]

        get_bm25_index().build(docs)
        get_tag_graph().build(docs)
        logger.info("search indexes warmed up", n_docs=len(docs))
    except Exception as exc:
        logger.warning("index warm-up skipped (empty DB?)", error=str(exc))


# Module-level app for uvicorn
app = create_app()
