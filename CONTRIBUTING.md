# Contributing

Thanks for your interest in LiteStar Gateway! Contributions are welcome — bug
reports, docs fixes, and code alike.

## Developer Certificate of Origin (DCO)

All contributions must be signed off. By adding a `Signed-off-by` line you
certify the [Developer Certificate of Origin](https://developercertificate.org/)
— that you wrote the change (or have the right to submit it) under the
project's [Apache 2.0 license](LICENSE).

Sign off each commit with:

```bash
git commit -s
```

which appends `Signed-off-by: Your Name <you@example.com>` to the message.
PRs with unsigned commits can't be merged.

## Development setup

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # install dependencies (incl. dev group)
uv run pre-commit install     # lint/format/secret-scan on every commit
uv run litestar --app litestar_gateway.app:app run   # run the app
```

## Quality gate

Every PR must pass the same checks CI runs:

```bash
uv run pre-commit run --all-files   # ruff (lint + format), markdown (rumdl), detect-secrets, hygiene
uv run pyrefly check                 # type check
uv run --with pip-audit pip-audit    # dependency CVE scan
uv run pytest --cov=src/litestar_gateway --cov-fail-under=80   # full test suite + coverage gate
```

Guidelines:

- Develop on a branch, open a PR against `main` — no direct pushes.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
  `refactor:`, `test:`, `chore:`, `perf:`, `ci:`).
- New behavior comes with tests; keep the hexagonal boundaries (no
  `infrastructure` imports from `domain`/`application`).
- Schema changes ship with an Alembic migration (see
  [docs/db-migrations.md](docs/db-migrations.md)).

## Security issues

Please do not open a public issue for security vulnerabilities — report them
privately to the maintainer instead.
