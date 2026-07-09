"""The built MkDocs site is served at /docs when present, and absent otherwise.

Running from source (dev/CI/tests) has no built `.mkdocs-site/`, so the /docs
mount must degrade gracefully: the app boots and /docs simply 404s. When a built
site exists, /docs/ serves its index.html. Both cases pin DOCS_SITE_DIR to a
controlled path so the assertions never depend on whether the repo happens to
have a locally built site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_404_NOT_FOUND
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.web import docs_site


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'docs_site.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


async def test_boots_without_built_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No built site → the app boots fine and /docs is simply absent (404).
    monkeypatch.setattr(docs_site, "DOCS_SITE_DIR", tmp_path / "does-not-exist")
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        assert (await client.get("/health")).status_code == HTTP_200_OK
        assert (await client.get("/docs/")).status_code == HTTP_404_NOT_FOUND


async def test_serves_docs_when_site_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<html><body>gateway docs</body></html>")
    monkeypatch.setattr(docs_site, "DOCS_SITE_DIR", site_dir)

    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/docs/")
        assert resp.status_code == HTTP_200_OK
        assert "gateway docs" in resp.text


def test_create_docs_router_returns_none_without_site(tmp_path: Path) -> None:
    assert docs_site.create_docs_router(tmp_path / "missing") is None
