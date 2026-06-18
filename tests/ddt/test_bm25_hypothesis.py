"""
Data-Driven Tests (DDT) using Hypothesis for BM25 scoring properties.

Validates invariants that must hold for any input:
  - Scores are always non-negative
  - Empty corpus returns no positive scores
  - Single-doc corpus behaves correctly
  - Unicode inputs don't crash
  - Scores are sorted descending
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.algorithms.search import BM25Index, bm25_score, tokenize


# ── Text strategies ───────────────────────────────────────────────────────────

_safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs")),
    min_size=0,
    max_size=200,
)

_doc_strategy = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "name": _safe_text,
    "description": _safe_text,
    "tags": st.lists(st.text(min_size=1, max_size=20), max_size=10),
})


# ── Properties ────────────────────────────────────────────────────────────────

@given(
    query=_safe_text,
    docs=st.lists(_doc_strategy, min_size=0, max_size=20),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_bm25_scores_non_negative(query, docs):
    """BM25 scores must always be ≥ 0."""
    ranked = bm25_score(query, docs, ["name", "description"])
    for score, _ in ranked:
        assert score >= 0.0, f"Negative score {score} for query={query!r}"


@given(query=_safe_text)
@settings(max_examples=100)
def test_empty_corpus_no_positive_scores(query):
    """Empty corpus must always produce an empty result."""
    ranked = bm25_score(query, [], ["name", "description"])
    assert ranked == []


@given(query=_safe_text, doc=_doc_strategy)
@settings(max_examples=100)
def test_single_doc_corpus(query, doc):
    """Single-doc corpus must return exactly one result."""
    ranked = bm25_score(query, [doc], ["name", "description"])
    assert len(ranked) == 1


@given(
    query=_safe_text,
    docs=st.lists(_doc_strategy, min_size=2, max_size=20),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_scores_sorted_descending(query, docs):
    """Result list must be sorted by score descending."""
    ranked = bm25_score(query, docs, ["name", "description"])
    scores = [s for s, _ in ranked]
    assert scores == sorted(scores, reverse=True)


@given(
    query=st.text(min_size=0, max_size=50),
    docs=st.lists(_doc_strategy, min_size=1, max_size=10),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_bm25_does_not_crash_on_any_input(query, docs):
    """BM25 must not raise for any string input."""
    try:
        bm25_score(query, docs, ["name", "description"])
    except Exception as exc:
        pytest.fail(f"BM25 raised {type(exc).__name__}: {exc}")


@given(
    query=_safe_text,
    docs=st.lists(_doc_strategy, min_size=1, max_size=20),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_bm25_index_matches_one_shot(query, docs):
    """BM25Index.search() must return same scores as bm25_score()."""
    one_shot = bm25_score(query, docs, ["name", "description"])
    idx = BM25Index(fields=["name", "description"])
    idx.build(docs)
    idx_ranked = idx.search(query, docs)

    one_shot_map = {d["id"]: s for s, d in one_shot}
    idx_map = {d["id"]: s for s, d in idx_ranked}

    for doc_id in one_shot_map:
        assert abs(one_shot_map[doc_id] - idx_map.get(doc_id, 0.0)) < 1e-9, \
            f"Score mismatch for doc {doc_id}: one_shot={one_shot_map[doc_id]}, idx={idx_map.get(doc_id)}"


@given(
    docs=st.lists(_doc_strategy, min_size=0, max_size=15),
    doc=_doc_strategy,
)
@settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
def test_upsert_idempotent(docs, doc):
    """Upserting the same doc twice must be idempotent."""
    idx = BM25Index(fields=["name", "description"])
    idx.build(docs)
    idx.upsert(doc)
    count_after_first = idx._n_docs
    idx.upsert(doc)
    assert idx._n_docs == count_after_first, "n_docs changed on duplicate upsert"


@given(
    docs=st.lists(_doc_strategy, min_size=1, max_size=15),
)
@settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
def test_delete_reduces_count(docs):
    """Deleting a doc must reduce n_docs by exactly 1."""
    idx = BM25Index(fields=["name", "description"])
    idx.build(docs)
    before = idx._n_docs
    idx.delete(docs[0]["id"])
    assert idx._n_docs == before - 1
