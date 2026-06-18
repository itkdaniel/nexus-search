"""
Projects router — CRUD + BM25 search + BFS tag-graph recommendations.

All read endpoints use the persistent BM25Index and TagGraph singletons
built at startup (O(n)), giving O(k) query performance.

Write endpoints update both the DB and the in-memory indexes incrementally
and invalidate ALL derived Redis caches (list, search-query, related) via
pipeline-batched pattern deletion in a single round-trip.

Cache key strategy:
  - List (no filters):  search:projects:pub:{published}
  - List (with filters): not cached (too many combinations)
  - Single project:      search:projects:id:{id}
  - BFS related:         search:related:{id}:{max_results}
  - Search queries:      search:q:{q}:tags:{t}:pub:{p}:lim:{l}   (in search.py)

On any write, ALL derived caches are invalidated by SCAN+pipeline pattern
delete covering: search:projects:pub:*, search:q:*, search:related:*
so callers never see stale data after mutations.

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

# Cache key builders — all include discriminating params to prevent poisoning
_CACHE_LIST  = "search:projects:pub:{published}"   # one key per published value
_CACHE_ID    = "search:projects:id:"               # prefix + project_id
# Patterns for bulk invalidation (SCAN-based):
_PATTERN_ALL = "search:*"                           # full flush on any write


def _list_key(published: Optional[bool]) -> str:
    return f"search:projects:pub:{published}"


def _doc_dict(project: ProjectModel) -> dict:
    return ProjectResponse.model_validate(project).model_dump(mode="json")


async def _invalidate_all_derived(cache: CacheService, project_id: str) -> None:
    """
    Bulk-invalidate ALL derived cache keys after any write operation.

    Scans and deletes all keys matching:
      - search:projects:pub:*   (list caches for each published variant)
      - search:projects:id:*    (individual project caches)
      - search:q:*              (unified search result caches)
      - search:related:*        (BFS recommendation caches)

    All DEL commands are pipelined (single round-trip per pattern scan batch).
    """
    await asyncio.gather(
        cache.delete_pattern("search:projects:pub:*"),
        cache.delete_pattern("search:projects:id:*"),
        cache.delete_pattern("search:q:*"),
        cache.delete_pattern(f"search:related:{project_id}:*"),
    )


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

    Cache strategy:
    - No search/tag/featured filters: cache-aside keyed by `published` value
      so published=true and published=false never share a cache entry.
    - Any filter present: skip cache (combinatorial explosion avoided).

    BM25 scoring:
    - Score > 0 → ranked results
    - All zero → Levenshtein fuzzy fallback on `name` field
    """
    cache = CacheService(redis)
    has_filter = bool(search or tags or featured is not None)
    # Cache key includes the `published` discriminator — avoids cross-poisoning
    list_cache_key = _list_key(published)

    if not has_filter:
        cached = await cache.get(list_cache_key)
        if cached is not None:
            return cached

    async with get_db() as db:
        stmt = select(ProjectModel).order_by(desc(ProjectModel.created_at))
        if published is not None:
            stmt = stmt.where(ProjectModel.published == published)
        if featured is not None:
            stmt = stmt.where(ProjectModel.featured == featured)
        result = await db.execute(stmt)
        docs = [_doc_dict(p) for p in result.scalars().all()]

    if search:
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
        await cache.set(list_cache_key, docs)

    return docs


# ── GET /v1/projects/{id} ─────────────────────────────────────────────────────

@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, redis=Depends(get_redis)):
    cache = CacheService(redis)
    cached = await cache.get(f"{_CACHE_ID}{project_id}")
    if cached is not None:
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
    Create a project. Invalidates ALL derived caches (list, search, related)
    via pipelined SCAN+DEL pattern deletion. Updates BM25 and TagGraph
    incrementally so subsequent reads are immediately consistent.
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

    # Incremental in-memory index update (O(doc_length))
    get_bm25_index().upsert(data)
    get_tag_graph().add_node(data)

    # Invalidate ALL derived caches + publish event
    await asyncio.gather(
        _invalidate_all_derived(cache, project.id),
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
    Update a project. Invalidates list, per-id, search-query, and BFS caches
    covering all published variants via SCAN+pipeline batching.
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

    # Incremental in-memory index update
    get_bm25_index().upsert(data)
    get_tag_graph().add_node(data)

    # Invalidate ALL derived caches + publish event
    await asyncio.gather(
        _invalidate_all_derived(cache, project_id),
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
    """
    Delete a project. Removes from DB, BM25 index, and TagGraph, then
    invalidates ALL derived caches (list, search-query, related, per-id)
    via pipelined SCAN+DEL — callers see consistent data immediately.
    """
    cache = CacheService(redis)

    async with get_db() as db:
        result = await db.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        await db.delete(project)

    # Remove from in-memory indexes
    get_bm25_index().delete(project_id)
    get_tag_graph().remove_node(project_id)

    # Invalidate ALL derived caches + publish event
    await asyncio.gather(
        _invalidate_all_derived(cache, project_id),
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
    Cache key includes project_id + max_results for correct isolation.
    O(V + E) BFS query against the persistent TagGraph singleton.
    """
    cache = CacheService(redis)
    cache_key = f"search:related:{project_id}:{max_results}"
    cached = await cache.get(cache_key)
    if cached is not None:
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

    # Prefer in-memory index docs (O(1)); fall back to DB for any missing
    bm25 = get_bm25_index()
    id_to_doc = bm25._docs

    related: List[dict] = []
    missing_ids: List[str] = []
    for rid in related_ids:
        doc = id_to_doc.get(rid)
        if doc:
            related.append(doc)
        else:
            missing_ids.append(rid)

    if missing_ids:
        async with get_db() as db:
            result = await db.execute(
                select(ProjectModel).where(ProjectModel.id.in_(missing_ids))
            )
            for p in result.scalars().all():
                related.append(_doc_dict(p))

    await cache.set(cache_key, related, ttl=60)
    return related
