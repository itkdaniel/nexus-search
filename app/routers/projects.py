"""
Projects router — CRUD + BM25 search + BFS tag-graph recommendations.

All read endpoints use the persistent BM25Index and TagGraph singletons
built at startup (O(n)), giving O(k) query performance.

Write endpoints update both the DB and the in-memory indexes incrementally
(O(1) amortized) and invalidate Redis cache via pipeline batching.

Auth: admin JWT required for POST/PATCH/DELETE.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import List, Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import desc, select

from app.algorithms.search import (
    fuzzy_match,
    get_bm25_index,
    get_tag_graph,
    tag_ranked,
)
from app.auth import require_admin
from app.database import get_db, get_redis
from app.models.project import (
    ProjectCreate,
    ProjectModel,
    ProjectResponse,
    ProjectUpdate,
)
from app.services.cache import CacheService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"])

_CACHE_ALL = "search:projects:all"
_CACHE_ID = "search:projects:id:"
_CACHE_SEARCH_PREFIX = "search:query:"


def _cache(redis=Depends(get_redis)) -> CacheService:
    from app.config import Settings
    # Inject TTL from configured settings (accessible via redis client attributes)
    return CacheService(redis, ttl=300)


def _doc_dict(project: ProjectModel) -> dict:
    return ProjectResponse.model_validate(project).model_dump(mode="json")


def _rebuild_indexes(docs: List[dict]) -> None:
    """Full rebuild of BM25 index and tag graph. Called as a BackgroundTask."""
    get_bm25_index().build(docs)
    get_tag_graph().build(docs)
    logger.info("search indexes rebuilt", n_docs=len(docs))


# ── GET /v1/projects ──────────────────────────────────────────────────────────

@router.get("/", response_model=List[ProjectResponse])
async def list_projects(
    search: Optional[str] = Query(None, description="BM25 full-text search"),
    tags: Optional[str] = Query(None, description="Comma-separated tag filter"),
    featured: Optional[bool] = Query(None),
    published: Optional[bool] = Query(True),
    redis=Depends(get_redis),
):
    """
    List projects with optional BM25 search and tag filtering.

    - No filters: cache-aside (5-min TTL)
    - With filters: skip cache (too many combinations), query index
    - BM25 score > 0 → ranked results; all zero → fuzzy fallback
    """
    cache = CacheService(redis)
    has_filter = search or tags or featured is not None

    async with get_db() as db:
        stmt = select(ProjectModel).order_by(desc(ProjectModel.created_at))
        if published is not None:
            stmt = stmt.where(ProjectModel.published == published)
        if featured is not None:
            stmt = stmt.where(ProjectModel.featured == featured)

        if not has_filter:
            cached = await cache.get(_CACHE_ALL)
            if cached:
                return cached

        result = await db.execute(stmt)
        docs = [_doc_dict(p) for p in result.scalars().all()]

    if search:
        # Use persistent index for O(k) scoring
        idx = get_bm25_index()
        ranked = idx.search(search, docs)
        if all(score == 0.0 for score, _ in ranked):
            docs = [d for _, d in fuzzy_match(search, docs)]
        else:
            docs = [d for score, d in ranked if score > 0]

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        docs = [d for score, d in tag_ranked(tag_list, docs) if score > 0]

    if not has_filter:
        await cache.set(_CACHE_ALL, docs)

    return docs


# ── GET /v1/projects/{id} ─────────────────────────────────────────────────────

@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, redis=Depends(get_redis)):
    cache = CacheService(redis)
    cached = await cache.get(f"{_CACHE_ID}{project_id}")
    if cached:
        return cached

    async with get_db() as db:
        result = await db.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    data = _doc_dict(project)
    await cache.set(f"{_CACHE_ID}{project_id}", data)
    return data


# ── POST /v1/projects ─────────────────────────────────────────────────────────

@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    background_tasks: BackgroundTasks,
    _admin=Depends(require_admin),
    redis=Depends(get_redis),
):
    """
    Create a project. Invalidates the list cache and updates indexes.
    BackgroundTask rebuilds indexes asynchronously so the response is fast.
    """
    cache = CacheService(redis)

    async with get_db() as db:
        project = ProjectModel(
            id=str(uuid.uuid4()),
            **payload.model_dump(),
        )
        db.add(project)
        await db.flush()
        await db.refresh(project)
        data = _doc_dict(project)

    # Incremental index update (O(doc_length))
    get_bm25_index().upsert(data)
    get_tag_graph().add_node(data)

    await asyncio.gather(
        cache.delete(_CACHE_ALL),
        cache.publish("search:events", {"type": "created", "id": project.id}),
    )
    logger.info("project created", id=project.id)
    return data


# ── PATCH /v1/projects/{id} ───────────────────────────────────────────────────

@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    _admin=Depends(require_admin),
    redis=Depends(get_redis),
):
    """
    Update a project. Invalidates list + individual cache keys.
    Uses Redis pipeline for atomic multi-key invalidation.
    """
    cache = CacheService(redis)

    async with get_db() as db:
        result = await db.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(project, field, value)

        await db.flush()
        await db.refresh(project)
        data = _doc_dict(project)

    # Incremental index update
    get_bm25_index().upsert(data)
    get_tag_graph().add_node(data)

    # Pipeline-batched invalidation (one round-trip)
    await asyncio.gather(
        cache.delete_many([_CACHE_ALL, f"{_CACHE_ID}{project_id}"]),
        cache.publish("search:events", {"type": "updated", "id": project_id}),
    )
    logger.info("project updated", id=project_id)
    return data


# ── DELETE /v1/projects/{id} ──────────────────────────────────────────────────

@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    _admin=Depends(require_admin),
    redis=Depends(get_redis),
):
    """Delete a project and invalidate caches + indexes."""
    cache = CacheService(redis)

    async with get_db() as db:
        result = await db.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        await db.delete(project)

    # Incremental index removal
    get_bm25_index().delete(project_id)
    get_tag_graph().remove_node(project_id)

    await asyncio.gather(
        cache.delete_many([_CACHE_ALL, f"{_CACHE_ID}{project_id}"]),
        cache.publish("search:events", {"type": "deleted", "id": project_id}),
    )
    logger.info("project deleted", id=project_id)


# ── GET /v1/projects/{id}/related ─────────────────────────────────────────────

@router.get("/{project_id}/related", response_model=List[ProjectResponse])
async def related_projects(
    project_id: str,
    max_results: int = Query(5, ge=1, le=20),
    redis=Depends(get_redis),
):
    """
    BFS tag-graph traversal to find related projects by shared tags.
    Uses the persistent TagGraph singleton (built at startup).
    O(V + E) query — graph already built, no DB read needed.
    """
    cache = CacheService(redis)
    cache_key = f"search:related:{project_id}:{max_results}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    # Verify project exists
    async with get_db() as db:
        result = await db.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Project not found")

    graph = get_tag_graph()
    related_ids = graph.bfs(project_id, max_hops=2)[:max_results]

    # Fetch related projects from DB (or from index)
    bm25 = get_bm25_index()
    id_to_doc = bm25._docs  # read-only access to indexed docs

    related = []
    missing_ids = []
    for rid in related_ids:
        doc = id_to_doc.get(rid)
        if doc:
            related.append(doc)
        else:
            missing_ids.append(rid)

    # If some docs not in index, fetch from DB
    if missing_ids:
        async with get_db() as db:
            result = await db.execute(
                select(ProjectModel).where(ProjectModel.id.in_(missing_ids))
            )
            for p in result.scalars().all():
                related.append(_doc_dict(p))

    await cache.set(cache_key, related, ttl=60)
    return related
