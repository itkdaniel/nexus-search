"""
Search & ranking algorithms for nexus-search.

Optimized design:
  - BM25Index: persistent inverted index built O(n) at startup; queries O(k)
    where k = result set size. Incremental O(1) updates on writes.
  - TagGraph: adjacency list built O(V+E) at startup; BFS queries O(V+E).
    add/remove nodes in O(V) worst case (small portfolio — acceptable).
  - fuzzy_match: Levenshtein DP O(|a|*|b|) — used only as BM25 fallback.
  - binary_search_id: O(log n) sorted ID lookup.

All complexity annotations are inline per task spec.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple


# ── Tokenizer ─────────────────────────────────────────────────────────────────

def tokenize(text: str) -> List[str]:
    """
    Lowercase + strip punctuation tokenizer.
    O(|text|) time and space.
    """
    return re.findall(r"[a-z0-9]+", text.lower())


# ── BM25 Inverted Index ───────────────────────────────────────────────────────
# BM25 parameters (Okapi BM25 defaults):
#   k1=1.5  term frequency saturation (higher = less saturation)
#   b=0.75  document length normalization (1.0 = full, 0.0 = none)

_K1, _B = 1.5, 0.75


class BM25Index:
    """
    Persistent inverted index for BM25 scoring.

    Build once at startup in O(n * avg_doc_length); each subsequent
    search runs in O(|query_tokens| * avg_postings) ≈ O(k) for k results.

    Writes update the index incrementally:
      - delete(doc_id)  O(unique_terms_in_doc)
      - upsert(doc)     O(doc_length)

    Thread-safety: all methods are synchronous and stateful; call from
    anyio.to_thread.run_sync() if needed for very large indexes.
    """

    def __init__(self, fields: List[str]) -> None:
        self._fields = fields
        # term -> {doc_id -> term_freq}
        self._inv: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # doc_id -> raw token list (needed for incremental removal)
        self._doc_tokens: Dict[str, List[str]] = {}
        # doc_id -> original doc dict
        self._docs: Dict[str, dict] = {}
        self._n_docs: int = 0
        self._total_length: int = 0

    @property
    def avg_dl(self) -> float:
        return self._total_length / max(self._n_docs, 1)

    def _extract_tokens(self, doc: dict) -> List[str]:
        text = " ".join(str(doc.get(f, "")) for f in self._fields)
        return tokenize(text)

    def build(self, docs: List[dict]) -> None:
        """
        Full rebuild from a list of docs.
        O(n * avg_doc_length) time; O(unique_terms * avg_postings) space.
        """
        self._inv = defaultdict(lambda: defaultdict(int))
        self._doc_tokens = {}
        self._docs = {}
        self._n_docs = 0
        self._total_length = 0

        for doc in docs:
            self._insert(doc)

    def _insert(self, doc: dict) -> None:
        """Internal: add one doc to the index. O(doc_length)."""
        doc_id = doc["id"]
        tokens = self._extract_tokens(doc)
        self._doc_tokens[doc_id] = tokens
        self._docs[doc_id] = doc
        self._n_docs += 1
        self._total_length += len(tokens)
        for tok in tokens:
            self._inv[tok][doc_id] += 1

    def _remove(self, doc_id: str) -> None:
        """Internal: remove one doc from the index. O(unique_terms_in_doc)."""
        if doc_id not in self._doc_tokens:
            return
        tokens = self._doc_tokens.pop(doc_id)
        self._docs.pop(doc_id, None)
        self._n_docs = max(0, self._n_docs - 1)
        self._total_length = max(0, self._total_length - len(tokens))
        seen: Set[str] = set()
        for tok in tokens:
            if tok not in seen:
                seen.add(tok)
                postings = self._inv.get(tok)
                if postings:
                    postings.pop(doc_id, None)
                    if not postings:
                        del self._inv[tok]

    def upsert(self, doc: dict) -> None:
        """
        Incremental update (insert or replace) for a single doc.
        O(doc_length) — removes old entry then inserts new.
        """
        self._remove(doc["id"])
        self._insert(doc)

    def delete(self, doc_id: str) -> None:
        """Remove a doc from the index. O(unique_terms_in_doc)."""
        self._remove(doc_id)

    def search(self, query: str, docs: Optional[List[dict]] = None) -> List[Tuple[float, dict]]:
        """
        Score docs against the BM25 query.

        If `docs` is provided, only those docs are scored (filtered search).
        Otherwise all indexed docs are scored. O(|query_tokens| * avg_postings).

        Returns [(score, doc)] sorted descending by score.
        """
        query_tokens = tokenize(query)
        if not query_tokens:
            target_docs = docs if docs is not None else list(self._docs.values())
            return [(0.0, d) for d in target_docs]

        # Which doc_ids to score
        if docs is not None:
            candidate_ids: Set[str] = {d["id"] for d in docs}
        else:
            candidate_ids = set(self._doc_tokens.keys())

        scores: Dict[str, float] = {doc_id: 0.0 for doc_id in candidate_ids}
        n = self._n_docs

        for term in query_tokens:
            postings = self._inv.get(term, {})
            # Document frequency (global — over full corpus for correct IDF)
            df = len(postings)
            if df == 0:
                continue
            # Log-smoothed IDF (BM25+)
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)

            for doc_id, tf in postings.items():
                if doc_id not in candidate_ids:
                    continue
                dl = len(self._doc_tokens.get(doc_id, []))
                norm = _K1 * (1 - _B + _B * dl / self.avg_dl)
                tf_score = tf * (_K1 + 1) / (tf + norm)
                scores[doc_id] += idf * tf_score

        # Map back to doc dicts
        result: List[Tuple[float, dict]] = []
        for doc_id, score in scores.items():
            doc = self._docs.get(doc_id)
            if doc is None and docs is not None:
                # Doc was provided but not in index yet (freshly created)
                doc = next((d for d in docs if d["id"] == doc_id), None)
            if doc is not None:
                result.append((score, doc))

        result.sort(key=lambda x: x[0], reverse=True)
        return result


# ── Tag Graph (BFS recommendations) ──────────────────────────────────────────

class TagGraph:
    """
    Adjacency-list graph of projects connected by shared tags.

    Build: O(V + E) where E = edges between docs sharing ≥1 tag.
    BFS query: O(V + E).
    Incremental add/remove: O(V) — acceptable for small portfolio.
    """

    def __init__(self) -> None:
        self._adj: Dict[str, Set[str]] = {}  # doc_id -> {neighbor_ids}
        self._tags: Dict[str, Set[str]] = {}  # doc_id -> {tags}

    def build(self, docs: List[dict]) -> None:
        """Full rebuild from a list of docs. O(V + E)."""
        self._adj = {}
        self._tags = {}
        for doc in docs:
            self._adj[doc["id"]] = set()
            self._tags[doc["id"]] = set(t.lower() for t in doc.get("tags", []))

        # Build edges: two docs share an edge if they share ≥1 tag
        ids = list(self._adj.keys())
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if self._tags[a] & self._tags[b]:
                    self._adj[a].add(b)
                    self._adj[b].add(a)

    def add_node(self, doc: dict) -> None:
        """Add or update a node and rewire edges. O(V)."""
        doc_id = doc["id"]
        # Remove old edges if updating
        self.remove_node(doc_id)
        self._tags[doc_id] = set(t.lower() for t in doc.get("tags", []))
        self._adj[doc_id] = set()
        for other_id, other_tags in self._tags.items():
            if other_id == doc_id:
                continue
            if self._tags[doc_id] & other_tags:
                self._adj[doc_id].add(other_id)
                self._adj[other_id].add(doc_id)

    def remove_node(self, doc_id: str) -> None:
        """Remove a node and all its edges. O(degree)."""
        if doc_id not in self._adj:
            return
        for neighbor in self._adj.pop(doc_id, set()):
            self._adj.get(neighbor, set()).discard(doc_id)
        self._tags.pop(doc_id, None)

    def bfs(self, start_id: str, max_hops: int = 2) -> List[str]:
        """
        BFS from start_id up to max_hops depth.
        Returns ordered list of related project IDs. O(V + E).
        """
        if start_id not in self._adj:
            return []
        visited: Set[str] = {start_id}
        queue: deque = deque([(start_id, 0)])
        result: List[str] = []

        while queue:
            node, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for neighbor in self._adj.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    result.append(neighbor)
                    queue.append((neighbor, depth + 1))
        return result


# ── Singleton instances (reset in tests) ─────────────────────────────────────

_bm25_index: Optional[BM25Index] = None
_tag_graph: Optional[TagGraph] = None


def get_bm25_index() -> BM25Index:
    global _bm25_index
    if _bm25_index is None:
        _bm25_index = BM25Index(fields=["name", "description", "tags"])
    return _bm25_index


def get_tag_graph() -> TagGraph:
    global _tag_graph
    if _tag_graph is None:
        _tag_graph = TagGraph()
    return _tag_graph


def reset_indexes() -> None:
    """Reset singletons — called between tests."""
    global _bm25_index, _tag_graph
    _bm25_index = None
    _tag_graph = None


# ── Standalone helpers (backward-compatible with python-service API) ───────────

def bm25_score(query: str, docs: List[dict], fields: List[str]) -> List[Tuple[float, dict]]:
    """
    One-shot BM25 scoring — builds a temporary index and queries it.
    Complexity: O(n * avg_doc_length) build + O(k) query.
    Use the BM25Index singleton for repeated queries on the same corpus.
    """
    idx = BM25Index(fields=fields)
    idx.build(docs)
    return idx.search(query, docs)


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard index: |A ∩ B| / |A ∪ B|. O(min(|A|,|B|))."""
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def tag_ranked(query_tags: List[str], docs: List[dict]) -> List[Tuple[float, dict]]:
    """
    Rank docs by Jaccard tag overlap. O(n * max_tags).
    Returns [(score, doc)] sorted descending.
    """
    q_set = {t.lower() for t in query_tags}
    return sorted(
        [(jaccard_similarity(q_set, set(d.get("tags", []))), d) for d in docs],
        key=lambda x: x[0],
        reverse=True,
    )


def binary_search_id(sorted_ids: List[str], target: str) -> int:
    """Standard binary search on a sorted list. O(log n). Returns index or -1."""
    lo, hi = 0, len(sorted_ids) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        if sorted_ids[mid] == target:
            return mid
        elif sorted_ids[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def levenshtein(a: str, b: str) -> int:
    """
    Classic bottom-up DP edit distance. O(|a| * |b|) time, O(|b|) space.
    Used as BM25 fallback for very short / misspelled queries.
    """
    a, b = a.lower(), b.lower()
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def fuzzy_match(query: str, docs: List[dict], field: str = "name") -> List[Tuple[int, dict]]:
    """
    Rank docs by Levenshtein distance to query on a given field.
    O(n * |query| * |field|). Lower distance = better match.
    """
    return sorted(
        [(levenshtein(query, str(d.get(field, ""))), d) for d in docs],
        key=lambda x: x[0],
    )


# ── Legacy wrappers (used by the standalone helper functions) ─────────────────

def build_tag_graph(docs: List[dict]) -> Dict[str, List[str]]:
    """Build adjacency list dict (legacy interface). O(V + E)."""
    g: Dict[str, List[str]] = defaultdict(list)
    for i, a in enumerate(docs):
        tags_a = set(a.get("tags", []))
        for j, b in enumerate(docs):
            if i != j and tags_a & set(b.get("tags", [])):
                g[a["id"]].append(b["id"])
    return dict(g)


def bfs_related(start_id: str, graph: Dict[str, List[str]], max_hops: int = 2) -> List[str]:
    """BFS using dict-based adjacency list (legacy interface). O(V + E)."""
    visited: Set[str] = {start_id}
    queue: deque = deque([(start_id, 0)])
    result: List[str] = []
    while queue:
        node, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                result.append(neighbor)
                queue.append((neighbor, depth + 1))
    return result
