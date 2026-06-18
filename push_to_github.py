#!/usr/bin/env python3
"""
push_to_github.py — Push apps/nexus-search/ to github.com/itkdaniel/nexus-search.

Uses the GitHub Contents API (PUT with SHA) to upsert each file.
Requires GITHUB_TOKEN environment variable with 'repo' scope.

Usage:
    python push_to_github.py [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────
REPO_OWNER = "itkdaniel"
REPO_NAME  = "nexus-search"
BRANCH     = "main"
APP_DIR    = Path(__file__).parent  # apps/nexus-search/

IGNORE = {
    "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
    "*.pyc", "*.pyo", ".env", ".env.local", ".DS_Store",
    "*.egg-info", "dist", "build", ".git",
}

API_BASE = "https://api.github.com"
HEADERS  = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("❌  GITHUB_TOKEN not set. Export it before running.", file=sys.stderr)
        sys.exit(1)
    return token


def collect_files(base: Path) -> list[Path]:
    """Collect all files under base, skipping ignored patterns."""
    result = []
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(base)
        parts = set(rel.parts)
        skip = False
        for part in parts:
            for ignore in IGNORE:
                if ignore.startswith("*"):
                    if part.endswith(ignore[1:]):
                        skip = True
                        break
                elif part == ignore:
                    skip = True
                    break
            if skip:
                break
        if not skip:
            result.append(path)
    return result


def get_sha(client: httpx.Client, path: str) -> str | None:
    """Return the SHA of a file in the repo, or None if it doesn't exist."""
    url = f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    resp = client.get(url, params={"ref": BRANCH})
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def upsert_file(client: httpx.Client, rel_path: str, content: bytes, dry_run: bool) -> str:
    """Create or update a file via GitHub Contents API. Returns status."""
    url = f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{rel_path}"
    encoded = base64.b64encode(content).decode()

    if dry_run:
        return "DRY-RUN"

    sha = get_sha(client, rel_path)
    body: dict = {
        "message": f"feat: upsert {rel_path}",
        "content": encoded,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    resp = client.put(url, json=body)
    if resp.status_code in (200, 201):
        return "updated" if sha else "created"
    else:
        raise RuntimeError(f"PUT {rel_path} → {resp.status_code}: {resp.text[:200]}")


def ensure_repo(client: httpx.Client) -> None:
    """Create the repo if it doesn't exist."""
    url = f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}"
    resp = client.get(url)
    if resp.status_code == 200:
        print(f"✓  Repo {REPO_OWNER}/{REPO_NAME} already exists.")
        return

    print(f"  Creating repo {REPO_OWNER}/{REPO_NAME}...")
    resp = client.post(f"{API_BASE}/user/repos", json={
        "name": REPO_NAME,
        "description": "Standalone BM25 search and BFS recommendation service for NexusConsult",
        "private": False,
        "auto_init": False,
    })
    if resp.status_code == 201:
        print("✓  Repo created.")
        time.sleep(2)
    else:
        raise RuntimeError(f"Failed to create repo: {resp.status_code} {resp.text[:200]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Push nexus-search to GitHub")
    parser.add_argument("--dry-run", action="store_true", help="Preview without pushing")
    args = parser.parse_args()

    token = get_token()
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}

    files = collect_files(APP_DIR)
    print(f"Found {len(files)} files to push to {REPO_OWNER}/{REPO_NAME}.\n")

    if args.dry_run:
        for f in files:
            print(f"  [DRY-RUN] {f.relative_to(APP_DIR)}")
        return

    with httpx.Client(headers=auth_headers, timeout=30) as client:
        ensure_repo(client)

        created = updated = failed = 0
        for f in files:
            rel = str(f.relative_to(APP_DIR))
            try:
                content = f.read_bytes()
                status = upsert_file(client, rel, content, dry_run=args.dry_run)
                if status == "created":
                    created += 1
                    print(f"  ✚  {rel}")
                elif status == "updated":
                    updated += 1
                    print(f"  ↺  {rel}")
                time.sleep(0.15)
            except Exception as exc:
                failed += 1
                print(f"  ✗  {rel} — {exc}", file=sys.stderr)

    print(f"\n{'─'*50}")
    print(f"✓  Done. Created: {created}  Updated: {updated}  Failed: {failed}")
    if failed:
        sys.exit(1)
    else:
        print(f"\n🔗  https://github.com/{REPO_OWNER}/{REPO_NAME}")


if __name__ == "__main__":
    main()
