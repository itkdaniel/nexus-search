"""
Regression / API contract tests for nexus-search.

These tests act as a safety net against breaking API changes.
They verify response shapes, required fields, and HTTP status codes.
"""
from __future__ import annotations

import pytest
import pytest_asyncio


# ── Health ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_shape(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "service" in body
    assert "version" in body
    assert "uptime_seconds" in body
    assert "redis" in body


@pytest.mark.asyncio
async def test_info_shape(client):
    resp = await client.get("/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "nexus-search"
    assert "version" in body
    assert "port" in body
    assert isinstance(body["endpoints"], list)
    assert len(body["endpoints"]) >= 9


# ── Projects list ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_projects_empty(client):
    resp = await client.get("/v1/projects/")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_projects_shape(client, admin_client):
    await admin_client.post("/v1/projects/", json={
        "name": "Test Project", "description": "A test", "type": "backend",
        "tags": ["test"]
    })
    resp = await client.get("/v1/projects/")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    p = body[0]
    for field in ["id", "name", "description", "type", "tags", "published", "featured",
                  "created_at", "updated_at", "status"]:
        assert field in p, f"Missing field: {field}"


# ── Single project ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_404(client):
    resp = await client.get("/v1/projects/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_project_200(client, admin_client):
    cr = await admin_client.post("/v1/projects/", json={
        "name": "Singleton", "description": "Lone project", "type": "backend", "tags": []
    })
    assert cr.status_code == 201
    project_id = cr.json()["id"]
    resp = await client.get(f"/v1/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == project_id


# ── CRUD auth ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_requires_auth(client):
    resp = await client.post("/v1/projects/", json={
        "name": "Unauthorized", "description": "no auth", "type": "backend", "tags": []
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_requires_auth(client, admin_client):
    cr = await admin_client.post("/v1/projects/", json={
        "name": "To Patch", "description": "original", "type": "backend", "tags": []
    })
    project_id = cr.json()["id"]
    resp = await client.patch(f"/v1/projects/{project_id}", json={"name": "Patched"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_requires_auth(client, admin_client):
    cr = await admin_client.post("/v1/projects/", json={
        "name": "To Delete", "description": "will be deleted", "type": "backend", "tags": []
    })
    project_id = cr.json()["id"]
    resp = await client.delete(f"/v1/projects/{project_id}")
    assert resp.status_code == 401


# ── CRUD success ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_project(admin_client):
    resp = await admin_client.post("/v1/projects/", json={
        "name": "New Project", "description": "Fresh new project", "type": "fullstack",
        "tags": ["react", "fastapi"]
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Project"
    assert "id" in body


@pytest.mark.asyncio
async def test_patch_project(admin_client):
    cr = await admin_client.post("/v1/projects/", json={
        "name": "Original", "description": "original desc", "type": "backend", "tags": []
    })
    pid = cr.json()["id"]
    resp = await admin_client.patch(f"/v1/projects/{pid}", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


@pytest.mark.asyncio
async def test_delete_project(admin_client, client):
    cr = await admin_client.post("/v1/projects/", json={
        "name": "Deletable", "description": "will be deleted", "type": "backend", "tags": []
    })
    pid = cr.json()["id"]
    resp = await admin_client.delete(f"/v1/projects/{pid}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/v1/projects/{pid}")
    assert get_resp.status_code == 404


# ── Search endpoint ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_requires_q(client):
    resp = await client.get("/v1/search/")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_returns_list(client, admin_client):
    await admin_client.post("/v1/projects/", json={
        "name": "Searchable", "description": "find me in search", "type": "backend", "tags": ["test"]
    })
    resp = await client.get("/v1/search/", params={"q": "searchable"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_search_finds_by_description(client, admin_client):
    await admin_client.post("/v1/projects/", json={
        "name": "Mystery", "description": "contains the word lighthouse", "type": "backend", "tags": []
    })
    resp = await client.get("/v1/search/", params={"q": "lighthouse"})
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "Mystery" in names


# ── Related projects ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_related_nonexistent_404(client):
    resp = await client.get("/v1/projects/does-not-exist/related")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_related_returns_list(client, admin_client):
    p1 = (await admin_client.post("/v1/projects/", json={
        "name": "P1", "description": "desc", "type": "backend", "tags": ["shared"]
    })).json()
    p2 = (await admin_client.post("/v1/projects/", json={
        "name": "P2", "description": "desc", "type": "backend", "tags": ["shared"]
    })).json()
    resp = await client.get(f"/v1/projects/{p1['id']}/related")
    assert resp.status_code == 200
    result_ids = [p["id"] for p in resp.json()]
    assert p2["id"] in result_ids


# ── Filters ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filter_by_featured(client, admin_client):
    await admin_client.post("/v1/projects/", json={
        "name": "Featured", "description": "this is featured", "type": "backend",
        "tags": [], "featured": True
    })
    await admin_client.post("/v1/projects/", json={
        "name": "NotFeatured", "description": "this is not featured", "type": "backend",
        "tags": [], "featured": False
    })
    resp = await client.get("/v1/projects/", params={"featured": "true"})
    assert resp.status_code == 200
    for p in resp.json():
        assert p["featured"] is True


@pytest.mark.asyncio
async def test_bm25_search_filter(client, admin_client):
    await admin_client.post("/v1/projects/", json={
        "name": "Kubernetes Operator", "description": "k8s operator for deployments",
        "type": "devops", "tags": ["k8s", "devops"]
    })
    resp = await client.get("/v1/projects/", params={"search": "kubernetes"})
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "Kubernetes Operator" in names
