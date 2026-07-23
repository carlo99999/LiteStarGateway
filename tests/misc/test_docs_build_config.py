"""Keep local and container MkDocs source projections in sync."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECTIONS = (
    ("README.md", "index.md"),
    ("EXAMPLES.md", "EXAMPLES.md"),
    ("CONTRIBUTING.md", "CONTRIBUTING.md"),
    ("SECURITY.md", "SECURITY.md"),
    ("docs", "docs"),
    ("issues", "issues"),
    ("plans", "plans"),
)


@pytest.mark.parametrize("build_file", ["justfile", "Dockerfile"])
def test_mkdocs_projection_includes_every_nav_source(build_file: str) -> None:
    contents = (ROOT / build_file).read_text()

    for source, target in PROJECTIONS:
        projection = f"ln -sfn ../{source} .mkdocs-docs/{target}"
        assert projection in contents, f"{build_file} is missing: {projection}"


def test_pr_coverage_runs_every_github_actions_job_locally() -> None:
    contents = (ROOT / "justfile").read_text()
    recipe = contents.split("pr-coverage:", 1)[1].split("\n\n", 1)[0]
    ui_recipe = contents.split("ui-ci:", 1)[1].split("\n\n", 1)[0]

    assert "just ui-ci" in recipe
    assert "just test-postgres" in recipe
    assert "just docker-ci" in recipe
    assert "export CI=true" in ui_recipe
