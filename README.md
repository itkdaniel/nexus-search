# nexus-search

Standalone BM25 full-text search and BFS tag-graph recommendation microservice for [NexusConsult](https://github.com/itkdaniel/nexusconsult).

## Features

- **BM25 full-text search** — persistent inverted index (O(n) build, O(k) queries)
- **Fuzzy fallback** — Levenshtein edit-distance matching when BM25 returns no results
- **Tag-graph BFS recommendations** — find related projects by shared tags in O(V+E)
- **Jaccard tag ranking** — score projects by tag overlap
- **Redis cache-aside** — 5-min TTL, pipeline-batched invalidation
- **HMAC-SHA256 JWT auth** — same scheme as the main NexusConsult portfolio (admin required for writes)
- **In-memory index updates** — BM25 and tag-graph updated incrementally on every write

## API

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/health` | ❌ | Liveness probe |
| GET | `/info` | ❌ | Service metadata + endpoint list |
| GET | `/v1/projects` | ❌ | List projects (BM25 search + tag filter) |
| GET | `/v1/projects/{id}` | ❌ | Get single project (cache-aside) |
| POST | `/v1/projects` | ✅ Admin | Create project |
| PATCH | `/v1/projects/{id}` | ✅ Admin | Update project |
| DELETE | `/v1/projects/{id}` | ✅ Admin | Delete project |
| GET | `/v1/projects/{id}/related` | ❌ | BFS related projects |
| GET | `/v1/search` | ❌ | Unified BM25+fuzzy search |

### Query Parameters

**`GET /v1/projects`**
- `search` — BM25 full-text query
- `tags` — comma-separated tag filter (Jaccard)
- `featured` — boolean filter
- `published` — boolean filter (default: `true`)

**`GET /v1/search`**
- `q` — search query (required)
- `tags` — comma-separated tag pre-filter
- `published` — boolean (default: `true`)
- `limit` — max results, 1–100 (default: 20)

**`GET /v1/projects/{id}/related`**
- `max_results` — 1–20 (default: 5)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8002

# Or via Docker Compose
docker compose up
```

## Testing

```bash
pip install -r requirements-dev.txt

# Unit tests (pure-Python, no deps)
pytest tests/unit/ -v

# Property tests (Hypothesis DDT)
pytest tests/ddt/ -v

# BDD scenarios
pytest tests/bdd/ -v

# Regression / contract tests
pytest tests/regression/ -v

# E2E tests (full flow)
pytest tests/e2e/ -v

# All tests
pytest -v
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///:memory:` | Async SQLAlchemy URL |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `REDIS_TTL` | `300` | Cache TTL (seconds) |
| `JWT_SECRET` | `change-me-in-production` | HMAC-SHA256 signing key |
| `DEBUG` | `false` | Enable debug mode |
| `PORT` | `8002` | Service port |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    nexus-search                         │
│                                                         │
│  /v1/search ──┐                                        │
│               ▼                                         │
│  BM25Index (persistent inverted index, O(k) queries)   │
│  TagGraph  (persistent adjacency list, BFS O(V+E))     │
│               │                                         │
│  /v1/projects ┼── CacheService (Redis, pipeline batch)│
│               │                                         │
│               ▼                                         │
│  PostgreSQL (projects table)                            │
└─────────────────────────────────────────────────────────┘
```

### Algorithm Complexities

| Algorithm | Build | Query | Update |
|-----------|-------|-------|--------|
| BM25Index | O(n·L) | O(k) | O(L) |
| TagGraph | O(V+E) | O(V+E) BFS | O(V) |
| Jaccard | — | O(min(A,B)) | — |
| Levenshtein | — | O(a·b) | — |
| Binary search | — | O(log n) | — |

Where: n=docs, L=avg doc length, k=query terms, V=vertices, E=edges, a/b=string lengths.

## Integration with Main Portfolio

The main NexusConsult Express server proxies search requests to this service when `NEXUS_SEARCH_URL` is set:

```
NEXUS_SEARCH_URL=http://localhost:8002
```

Proxy mappings:
- `GET /api/search?q=...` → `GET /v1/search?q=...`
- `GET /api/projects` → `GET /v1/projects`
