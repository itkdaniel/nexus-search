# CI/CD — nexus-search

## Pipeline Overview

GitHub Actions runs 5 parallel jobs on every push to `main` and every PR:

| Job | Scope | Duration |
|-----|-------|----------|
| `unit-tests` | Algorithm correctness | ~30s |
| `ddt-tests` | Hypothesis property invariants | ~60s |
| `bdd-tests` | BDD feature scenarios | ~45s |
| `regression-tests` | API contract stability | ~45s |
| `e2e-tests` | Full search flow | ~60s |
| `docker-build` | Image build verification | ~120s |

## Running Locally

```bash
# All tests
pytest -v

# Single suite
pytest tests/unit/ -v
pytest tests/ddt/ -v
pytest tests/bdd/ -v
pytest tests/regression/ -v
pytest tests/e2e/ -v
```

## Environment Variables (CI)

| Variable | Source | Required |
|----------|--------|----------|
| `GITHUB_TOKEN` | GitHub Actions auto | CI push only |
| `JWT_SECRET` | GitHub Secrets | Production deploy |
| `DATABASE_URL` | GitHub Secrets | Production deploy |
| `REDIS_URL` | GitHub Secrets | Production deploy |

## Deployment

1. CI passes all 6 jobs
2. Docker image built and pushed to GHCR
3. Kubernetes deployment rolling update triggered
4. Health probe confirms `/health` returns `200 OK`
