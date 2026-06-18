# nexus-search Developer Guide

## BM25 Index

The `BM25Index` class maintains a persistent inverted index in memory.

### How it works

1. **Build phase** (O(n·L)): called at startup and after bulk operations
   - Tokenize every document's `name`, `description`, `tags` fields
   - Build inverted index: `term → {doc_id → term_freq}`
   - Track document lengths for BM25 normalization

2. **Query phase** (O(k·avg_postings)): called on every search request
   - Tokenize query
   - For each query token, look up postings in the index
   - Score each document using BM25 formula: `IDF * TF_normalized`
   - Sort and return

3. **Update phase** (O(L)): called on every write
   - Remove old document tokens from the index
   - Insert new document tokens
   - Update document length tracking

### Fuzzy Fallback

When all BM25 scores are zero (query term not in any document), the service
falls back to Levenshtein edit-distance matching on the `name` field. This
handles misspellings like "authetication" → "Auth Service".

## Tag Graph

The `TagGraph` class maintains an adjacency-list graph where edges connect
projects sharing at least one tag.

### BFS Recommendations

```
Auth Service {auth, jwt, security}
     │
     ├── Auth Gateway {auth, oauth, security}  (1 hop, shares auth+security)
     │
     └── (no further hops to Analytics/Commerce unless shared tags)
```

BFS respects `max_hops=2` to avoid irrelevant distant recommendations.

## Cache Strategy

Redis cache-aside pattern:

```
Request → Check Redis → HIT: return cached
                     → MISS: query DB → store in Redis with TTL → return
```

On writes: `pipeline.delete([cache_all, cache_id])` — atomic multi-key invalidation
in a single Redis round-trip.

## Auth

Admin-only endpoints use HMAC-SHA256 JWT (same secret as the main NexusConsult app):

```python
# Token structure: base64url(header).base64url(payload).base64url(HMAC-SHA256)
payload = {"sub": user_id, "role": "admin", "exp": unix_timestamp}
```

## Local Development

```bash
# 1. Install deps
pip install -r requirements-dev.txt

# 2. Start service (SQLite in-memory by default)
uvicorn app.main:app --port 8002 --reload

# 3. Browse docs
open http://localhost:8002/docs

# 4. Run all tests
pytest -v
```
