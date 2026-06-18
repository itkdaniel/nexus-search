"""
Unit tests for search and graph algorithms.

Tests are pure-Python (no DB/Redis) — cover all algorithm functions
and the new BM25Index / TagGraph classes.
"""
from __future__ import annotations

import pytest

from app.algorithms.search import (
    BM25Index,
    TagGraph,
    binary_search_id,
    bm25_score,
    bfs_related,
    build_tag_graph,
    fuzzy_match,
    jaccard_similarity,
    levenshtein,
    tag_ranked,
    tokenize,
)

SAMPLE_DOCS = [
    {"id": "1", "name": "Auth Service",     "description": "JWT authentication microservice", "tags": ["auth", "jwt", "security"]},
    {"id": "2", "name": "Analytics Engine", "description": "Real-time data streaming pipeline", "tags": ["kafka", "streaming", "data"]},
    {"id": "3", "name": "Commerce API",     "description": "Headless e-commerce GraphQL API",  "tags": ["graphql", "stripe", "ecommerce"]},
    {"id": "4", "name": "Auth Gateway",     "description": "OAuth2 gateway service",           "tags": ["auth", "oauth", "security"]},
]


# ── Tokenizer ──────────────────────────────────────────────────────────────────

class TestTokenize:
    def test_lowercases_and_splits(self):
        assert tokenize("Hello, World!") == ["hello", "world"]

    def test_strips_punctuation(self):
        assert tokenize("foo-bar.baz") == ["foo", "bar", "baz"]

    def test_empty_string(self):
        assert tokenize("") == []

    def test_unicode_ignored(self):
        tokens = tokenize("café crème")
        assert "caf" in tokens or "cafe" in tokens or tokens == ["caf", "cr", "me"]


# ── BM25Index ─────────────────────────────────────────────────────────────────

class TestBM25Index:
    def _make_index(self):
        idx = BM25Index(fields=["name", "description"])
        idx.build(SAMPLE_DOCS)
        return idx

    def test_build_indexes_all_docs(self):
        idx = self._make_index()
        assert len(idx._docs) == 4

    def test_search_returns_relevant_doc_first(self):
        idx = self._make_index()
        ranked = idx.search("GraphQL")
        scores = [s for s, _ in ranked]
        assert scores[0] >= scores[-1]  # sorted desc
        top = ranked[0][1]
        assert top["id"] == "3"

    def test_search_auth_returns_auth_docs(self):
        idx = self._make_index()
        ranked = idx.search("auth")
        top_ids = [d["id"] for s, d in ranked if s > 0]
        assert "1" in top_ids or "4" in top_ids

    def test_empty_query_returns_zero_scores(self):
        idx = self._make_index()
        ranked = idx.search("")
        assert all(s == 0.0 for s, _ in ranked)

    def test_unknown_query_returns_zero_scores(self):
        idx = self._make_index()
        ranked = idx.search("xyzzyunknown")
        assert all(s == 0.0 for s, _ in ranked)

    def test_upsert_new_doc(self):
        idx = self._make_index()
        new_doc = {"id": "5", "name": "Search Engine", "description": "Elasticsearch clone", "tags": []}
        idx.upsert(new_doc)
        assert "5" in idx._docs
        ranked = idx.search("elasticsearch")
        assert any(d["id"] == "5" for _, d in ranked if _ > 0)

    def test_upsert_updates_existing_doc(self):
        idx = self._make_index()
        updated = dict(SAMPLE_DOCS[0])
        updated["description"] = "now has elasticsearch instead"
        idx.upsert(updated)
        # Old tokens cleared, new ones present
        ranked = idx.search("elasticsearch")
        assert any(d["id"] == "1" for _, d in ranked if _ > 0)

    def test_delete_removes_doc(self):
        idx = self._make_index()
        idx.delete("1")
        assert "1" not in idx._docs
        ranked = idx.search("jwt")
        assert all(d["id"] != "1" for _, d in ranked)

    def test_delete_nonexistent_is_safe(self):
        idx = self._make_index()
        idx.delete("does-not-exist")  # should not raise

    def test_n_docs_tracks_correctly(self):
        idx = self._make_index()
        assert idx._n_docs == 4
        idx.delete("1")
        assert idx._n_docs == 3
        idx.upsert({"id": "new", "name": "New", "description": "new doc", "tags": []})
        assert idx._n_docs == 4

    def test_filtered_search(self):
        idx = self._make_index()
        subset = [SAMPLE_DOCS[0], SAMPLE_DOCS[3]]  # auth docs only
        ranked = idx.search("auth", docs=subset)
        result_ids = {d["id"] for _, d in ranked}
        assert result_ids <= {"1", "4"}


# ── TagGraph ──────────────────────────────────────────────────────────────────

class TestTagGraph:
    def _make_graph(self):
        g = TagGraph()
        g.build(SAMPLE_DOCS)
        return g

    def test_build_creates_adjacency(self):
        g = self._make_graph()
        # Auth Service (auth, jwt, security) shares tags with Auth Gateway (auth, oauth, security)
        assert "4" in g._adj["1"]

    def test_bfs_finds_related_by_shared_tag(self):
        g = self._make_graph()
        related = g.bfs("1")
        assert "4" in related

    def test_bfs_max_hops_0_empty(self):
        g = self._make_graph()
        assert g.bfs("1", max_hops=0) == []

    def test_bfs_isolated_node_empty(self):
        g = TagGraph()
        g.build([{"id": "x", "name": "Isolated", "description": "", "tags": []}])
        assert g.bfs("x") == []

    def test_add_node_creates_edges(self):
        g = self._make_graph()
        new_doc = {"id": "5", "name": "New Auth", "description": "auth service", "tags": ["auth", "security"]}
        g.add_node(new_doc)
        assert "5" in g._adj
        assert "1" in g._adj["5"]  # shares auth+security with Auth Service

    def test_remove_node_cleans_edges(self):
        g = self._make_graph()
        g.remove_node("1")
        assert "1" not in g._adj
        # Auth Gateway (4) should no longer have 1 as neighbor
        assert "1" not in g._adj.get("4", set())

    def test_remove_nonexistent_is_safe(self):
        g = self._make_graph()
        g.remove_node("does-not-exist")  # should not raise


# ── Jaccard ───────────────────────────────────────────────────────────────────

class TestJaccard:
    def test_identical_sets(self):
        assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        assert jaccard_similarity({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        score = jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
        assert 0 < score < 1

    def test_empty_sets(self):
        assert jaccard_similarity(set(), set()) == 0.0

    def test_one_empty(self):
        assert jaccard_similarity({"a"}, set()) == 0.0


# ── Binary Search ─────────────────────────────────────────────────────────────

class TestBinarySearch:
    def test_finds_element(self):
        assert binary_search_id(["a", "b", "c", "d"], "c") == 2

    def test_not_found(self):
        assert binary_search_id(["a", "b", "c"], "z") == -1

    def test_single_element_found(self):
        assert binary_search_id(["only"], "only") == 0

    def test_single_element_not_found(self):
        assert binary_search_id(["only"], "other") == -1

    def test_empty_list(self):
        assert binary_search_id([], "a") == -1


# ── Levenshtein / Fuzzy ───────────────────────────────────────────────────────

class TestLevenshtein:
    def test_identical(self):
        assert levenshtein("auth", "auth") == 0

    def test_case_insensitive(self):
        assert levenshtein("Auth", "auth") == 0

    def test_single_substitution(self):
        assert levenshtein("cat", "bat") == 1

    def test_empty_vs_nonempty(self):
        assert levenshtein("abc", "") == 3

    def test_insertions(self):
        assert levenshtein("ab", "abc") == 1


class TestFuzzyMatch:
    def test_exact_match_first(self):
        ranked = fuzzy_match("Auth Service", SAMPLE_DOCS, field="name")
        assert ranked[0][1]["id"] == "1"

    def test_lower_score_is_better(self):
        ranked = fuzzy_match("Auth", SAMPLE_DOCS, field="name")
        scores = [s for s, _ in ranked]
        assert scores == sorted(scores)  # ascending (lower = closer match)


# ── BFS (legacy dict-based) ────────────────────────────────────────────────────

class TestBFSLegacy:
    def test_finds_related(self):
        graph = build_tag_graph(SAMPLE_DOCS)
        related = bfs_related("1", graph)
        assert "4" in related

    def test_max_hops_0(self):
        graph = build_tag_graph(SAMPLE_DOCS)
        assert bfs_related("1", graph, max_hops=0) == []

    def test_isolated_node(self):
        graph = build_tag_graph([{"id": "x", "name": "Isolated", "description": "", "tags": []}])
        assert bfs_related("x", graph) == []


# ── One-shot bm25_score ───────────────────────────────────────────────────────

class TestBM25Score:
    def test_graphql_returns_commerce_first(self):
        ranked = bm25_score("GraphQL", SAMPLE_DOCS, ["name", "description"])
        assert ranked[0][1]["id"] == "3"

    def test_empty_query_all_zero(self):
        ranked = bm25_score("", SAMPLE_DOCS, ["name"])
        assert all(s == 0.0 for s, _ in ranked)


# ── Tag ranked ────────────────────────────────────────────────────────────────

class TestTagRanked:
    def test_exact_tag_match_top(self):
        ranked = tag_ranked(["graphql"], SAMPLE_DOCS)
        assert ranked[0][1]["id"] == "3"

    def test_no_overlap_zero_score(self):
        ranked = tag_ranked(["xyz"], SAMPLE_DOCS)
        assert all(s == 0.0 for s, _ in ranked)
