# Design doc — Continuous Integration (GitHub Actions)

> **Status:** Implemented — see `.github/workflows/ci.yml` and
> `.pre-commit-config.yaml`. Retained as the original design rationale.

## 1. Goal

Run the checks we run manually today on **every push and PR**, so `main` stays
green without relying on discipline: `pytest`, `ruff`, `pyrefly`. The coverage
gate is enforced (80%).

## 2. Plan

- **`.github/workflows/ci.yml`** triggered on `push` to `main` and on
  `pull_request`.
- Use **`astral-sh/setup-uv`** (we already use `uv`), pin the Python version from
  `.python-version`, cache the uv environment.
- Steps: `uv sync --frozen` → `uv run pre-commit run --all-files` (ruff lint +
  format, file hygiene, `detect-secrets` + private-key detection, and the `rumdl`
  markdown linter) → `uv run pyrefly check` → `uv run --with pip-audit pip-audit`
  (dependency CVE scan) → `uv run pytest -q --cov … --cov-fail-under=80`.
- Run on `ubuntu-latest`; matrix on Python is optional (single version to start).
- Fast: the suite runs against SQLite in-process (~9s today), no external
  services needed.

## 3. Open decisions

1. **Postgres in CI**: add a `services: postgres` job once the Postgres work
   (`adding-postgres`) lands, so integration tests can run on both backends.
2. **Coverage gate**: enforced at 80% (`--cov --cov-fail-under=80`).
3. **Branch protection**: require the CI check to pass before merge (repo setting,
   not code) — recommended once the workflow is stable.
4. **Caching**: uv cache keyed on `uv.lock` for fast runs.

## 4. Rollout

1. `feat/ci` — the workflow file + green run on a PR. Then enable branch
   protection requiring it.
