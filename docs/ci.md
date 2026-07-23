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
- The primary coverage job runs against SQLite in-process with no external
  services.
- A second `postgres` job runs alongside it with a `services: postgres`
  container: it applies migrations via the same `litestar … database upgrade`
  command prod runs on every deploy, then runs the **full test suite** with
  `DATABASE_URL` pointed at that container. This is what
  catches a migration that's valid under SQLite's permissive typing but broken
  under Postgres (type/constraint/JSON semantics) before it reaches production.

## 3. Open decisions

1. **Postgres in CI**: done — see the `postgres` job in
   `.github/workflows/ci.yml`; it applies the full migration chain and runs the
   full suite against Postgres.
2. **Coverage gate**: enforced at 80% (`--cov --cov-fail-under=80`) on the
   SQLite job; the Postgres job is a dialect/integration gate and does not
   collect coverage a second time.
3. **Branch protection**: require the CI check to pass before merge (repo setting,
   not code) — recommended once the workflow is stable.
4. **Caching**: uv cache keyed on `uv.lock` for fast runs.

## 4. Rollout

1. `feat/ci` — the workflow file + green run on a PR. Then enable branch
   protection requiring it.
