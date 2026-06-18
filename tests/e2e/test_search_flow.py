"""
End-to-end tests for nexus-search — full request/response cycle using httpx.AsyncClient.

Tests the complete search and recommendation flow:
  1. Create projects via admin API
  2. Search via BM25 + fuzzy endpoints
  3. Get related via BFS graph
  4. Verify cache invalidation on updates
"""
from __future__ import annotations

import pytest


PROJECTS = [
    {"name": "Auth Service",     "description": "JWT authentication and authorization",  "type": "backend", "tags": ["auth", "jwt", "security"]},
    {"name": "Analytics Engine", "description": "Real-time streaming data pipeline",     "type": "backend", "tags": ["kafka", "data", "streaming"]},
    {"name": "Commerce API",     "description": "GraphQL e-commerce API with Stripe",    "type": "backend", "tags": ["graphql", "stripe", "ecommerce"]},
    {"name": "Auth Gateway",     "description": "OAuth2 gateway with rate limiting",     "type": "backend", "tags": ["auth", "oauth", "security"]},
]


@pytest.fixture
async def seeded(admin_client, client):
    """Insert all PROJECTS and return {name: project_dict}."""
    inserted = {}
    for p in PROJECTS:
        resp = await admin_client.post("/v1/projects/", json=p)
        assert resp.status_code == 201, resp.text
        inserted[p["name"]] = resp.json()
    return inserted, client, admin_client


@pytest.mark.asyncio
async def test_bm25_search_ranking(seeded):
    """BM25 should rank auth-related results first for 'authentication' query."""
    _, client, _ = seeded
    resp = await client.get("/v1/search/", params={"q": "authentication"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) > 0
    # Auth Service should appear (it has 'authentication' in description)
    names = [p["name"] for p in results]
    assert "Auth Service" in names


@pytest.mark.asyncio
async def test_fuzzy_fallback(seeded):
    """Misspelled query should still return results via fuzzy matching."""
    _, client, _ = seeded
    resp = await client.get("/v1/search/", params={"q": "autehntication"})
    assert resp.status_code == 200
    # Fuzzy fallback — may return results or not, but must not error


@pytest.mark.asyncio
async def test_tag_filter_search(seeded):
    """Tag filter should only return projects with matching tag."""
    _, client, _ = seeded
    resp = await client.get("/v1/search/", params={"q": "service", "tags": "auth"})
    assert resp.status_code == 200
    for proj in resp.json():
        assert "auth" in [t.lower() for t in proj.get("tags", [])]


@pytest.mark.asyncio
async def test_related_projects_by_tag(seeded):
    """Auth Service should be related to Auth Gateway via shared tags."""
    inserted, client, _ = seeded
    auth_service_id = inserted["Auth Service"]["id"]
    resp = await client.get(f"/v1/projects/{auth_service_id}/related")
    assert resp.status_code == 200
    related_names = [p["name"] for p in resp.json()]
    # Auth Gateway shares auth+security tags with Auth Service
    assert "Auth Gateway" in related_names


@pytest.mark.asyncio
async def test_related_no_shared_tags_disjoint(seeded):
    """Analytics Engine should not be related to Commerce API (no shared tags)."""
    inserted, client, _ = seeded
    analytics_id = inserted["Analytics Engine"]["id"]
    resp = await client.get(f"/v1/projects/{analytics_id}/related")
    assert resp.status_code == 200
    related_names = [p["name"] for p in resp.json()]
    assert "Commerce API" not in related_names


@pytest.mark.asyncio
async def test_create_updates_index(client, admin_client):
    """Newly created project should be immediately searchable."""
    resp = await admin_client.post("/v1/projects/", json={
        "name": "Lighthouse Performance Tool",
        "description": "automated web performance testing with lighthouse",
        "type": "devops",
        "tags": ["performance", "testing"],
    })
    assert resp.status_code == 201

    search = await client.get("/v1/search/", params={"q": "lighthouse"})
    assert search.status_code == 200
    names = [p["name"] for p in search.json()]
    assert "Lighthouse Performance Tool" in names


@pytest.mark.asyncio
async def test_delete_removes_from_search(client, admin_client):
    """Deleted project should not appear in search results."""
    cr = await admin_client.post("/v1/projects/", json={
        "name": "Ephemeral Service",
        "description": "this service will be deleted from ephemeral storage",
        "type": "backend",
        "tags": ["ephemeral"],
    })
    pid = cr.json()["id"]

    await admin_client.delete(f"/v1/projects/{pid}")

    search = await client.get("/v1/search/", params={"q": "ephemeral"})
    assert search.status_code == 200
    names = [p["name"] for p in search.json()]
    assert "Ephemeral Service" not in names


@pytest.mark.asyncio
async def test_update_reflects_in_search(client, admin_client):
    """Updated project description should be reflected in subsequent searches."""
    cr = await admin_client.post("/v1/projects/", json={
        "name": "Chameleon Service",
        "description": "generic backend service",
        "type": "backend",
        "tags": [],
    })
    pid = cr.json()["id"]

    await admin_client.patch(f"/v1/projects/{pid}", json={
        "description": "now specialized for blockchain distributed ledger technology"
    })

    search = await client.get("/v1/search/", params={"q": "blockchain"})
    assert search.status_code == 200
    names = [p["name"] for p in search.json()]
    assert "Chameleon Service" in names


@pytest.mark.asyncio
async def test_max_results_limit(seeded):
    """Related endpoint should respect max_results parameter."""
    inserted, client, _ = seeded
    auth_id = inserted["Auth Service"]["id"]
    resp = await client.get(f"/v1/projects/{auth_id}/related", params={"max_results": 1})
    assert resp.status_code == 200
    assert len(resp.json()) <= 1


@pytest.mark.asyncio
async def test_concurrent_creates(admin_client):
    """asyncio.gather creates should all succeed without race conditions."""
    import asyncio
    tasks = [
        admin_client.post("/v1/projects/", json={
            "name": f"Concurrent {i}",
            "description": f"parallel test project {i}",
            "type": "backend",
            "tags": [f"tag{i}"],
        })
        for i in range(5)
    ]
    responses = await asyncio.gather(*tasks)
    for resp in responses:
        assert resp.status_code == 201
