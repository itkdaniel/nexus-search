"""
Unified search endpoint — GET /v1/search.

Combines BM25 full-text scoring with fuzzy fallback and optional tag filtering.
Uses the persistent BM25Index singleton for O(k) query performance.
"""
from __future__ import annotations

from typing import List, Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select

from app.algorithms.search import fuzzy_match, get_bm25_index, tag_ranked
from app.database import get_db, get_redis
from app.models.project import ProjectModel, ProjectResponse
from app.services.cache import CacheService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.get("/", response_model=List[ProjectResponse])
async def unified_search(
    q: str = Query(..., min_length=1, description="Search query (BM25 + fuzzy fallback)"),
    tags: Optional[str] = Query(None, description="Comma-separated tag pre-filter"),
    published: bool = Query(True),
    limit: int = Query(20, ge=1, le=100),
    redis=Depends(get_redis),
):
    """
    Unified search across projects.

    Algorithm:
    1. Fetch all published projects (cache-aside)
    2. Pre-filter by tags (Jaccard) if `tags` param provided
    3. Score with BM25 — returns ranked results
    4. Fuzzy fallback (Levenshtein on `name`) if all BM25 scores are zero
    5. Return top `limit` results

    Complexity: O(k) for BM25 query against the persistent index.
    """
    cache = CacheService(redis)
    cache_key = f"search:q:{q}:tags:{tags}:pub:{published}:lim:{limit}"

    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    async with get_db() as db:
        stmt = (
            select(ProjectModel)
            .where(ProjectModel.published == published)
            .order_by(desc(ProjectModel.created_at))
        )
        result = await db.execute(stmt)
        docs = [ProjectResponse.model_validate(p).model_dump(mode="json")
                for p in result.scalars().all()]

    # Tag pre-filter
    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        docs = [d for score, d in tag_ranked(tag_list, docs) if score > 0]

    # BM25 scoring using persistent index
    idx = get_bm25_index()
    ranked = idx.search(q, docs)

    if all(score == 0.0 for score, _ in ranked):
        # Fuzzy fallback on `name` field
        fuzzy = fuzzy_match(q, docs, field="name")
        results = [d for _, d in fuzzy][:limit]
    else:
        results = [d for score, d in ranked if score > 0][:limit]

    await cache.set(cache_key, results, ttl=60)
    return results
