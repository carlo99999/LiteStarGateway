# Design doc — Continuous Integration (GitHub Actions)

> **Status:** Draft / parked (pre-v1). Branch `adding-ci`. No code yet.

## 1. Goal

Run the checks we run manually today on **every push and PR**, so `main` stays
green without relying on discipline: `pytest`, `ruff`, `pyrefly`. Add a coverage
report (gate lands in v2).

## 2. Plan

- **`.github/workflows/ci.yml`** triggered on `push` to `main` and on
  `pull_request`.
- Use **`astral-sh/setup-uv`** (we already use `uv`), pin the Python version from
  `.python-version`, cache the uv environment.
- Steps: `uv sync` → `uv run ruff check .` → `uv run pyrefly check` →
  `uv run pytest -q`.
- Run on `ubuntu-latest`; matrix on Python is optional (single version to start).
- Fast: the suite runs against SQLite in-process (~9s today), no external
  services needed.

## 3. Open decisions

1. **Postgres in CI**: add a `services: postgres` job once the Postgres work
   (`adding-postgres`) lands, so integration tests can run on both backends.
2. **Coverage gate**: report now, enforce 80% in v2 (`--cov --cov-fail-under`).
3. **Branch protection**: require the CI check to pass before merge (repo setting,
   not code) — recommended once the workflow is stable.
4. **Caching**: uv cache keyed on `uv.lock` for fast runs.

## 4. Rollout

1. `feat/ci` — the workflow file + green run on a PR. Then enable branch
   protection requiring it.
