# Contributing to nexus-search

## Branch Strategy
- `main` — production-ready code
- `feat/<name>` — new features
- `fix/<name>` — bug fixes
- `chore/<name>` — tooling/infra

## Commit Format
```
<type>(<scope>): <short summary>

Types: feat | fix | refactor | test | docs | chore | perf
Scope: algorithms | cache | routers | auth | database | tests
```

## Code Standards
- Python 3.12+, type annotations everywhere
- Algorithm complexities documented inline as `O(...)` comments
- All public functions have docstrings
- No `from __future__ import annotations` in test files unless needed

## Testing Requirements
- Unit tests for every algorithm function/method
- BDD scenarios for user-facing search flows
- Hypothesis DDT for BM25 invariants
- Regression tests for every public API endpoint
- E2E test for every user story

## Pull Request Checklist
- [ ] All tests pass (`pytest -v`)
- [ ] New algorithms have complexity annotations
- [ ] API changes reflected in README table
- [ ] `requirements.txt` updated if new deps added
