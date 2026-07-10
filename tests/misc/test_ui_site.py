"""The built admin UI is served at /ui when present, and absent otherwise.

Running from source (dev/CI/tests) has no built `ui/dist/`, so the /ui mount
must degrade gracefully: the app boots and /ui simply 404s. When a build exists,
/ui/ serves index.html, real assets are served with their content type, unknown
routes fall back to index.html (SPA), and a missing asset is a real 404. Both
cases pin UI_DIST_DIR to a controlled path so assertions never depend on whether
the repo happens to have a locally built UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_308_PERMANENT_REDIRECT,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.web import ui_site


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ui_site.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


def _build_dist(root: Path) -> Path:
    """A minimal Vite-style build: index.html + a hashed asset under assets/."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head>"
        '<script type="module" src="/ui/assets/app.js"></script>'
        "</head><body>admin console</body></html>"
    )
    (dist / "assets" / "app.js").write_text("console.log('gateway ui')")
    return dist


async def test_boots_without_built_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No built UI → the app boots fine and /ui is simply absent (404).
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", tmp_path / "does-not-exist")
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        assert (await client.get("/health")).status_code == HTTP_200_OK
        assert (await client.get("/ui/")).status_code == HTTP_404_NOT_FOUND


async def test_serves_shell_when_build_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", _build_dist(tmp_path))
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/ui/")
        assert resp.status_code == HTTP_200_OK
        assert "admin console" in resp.text
        assert resp.headers["content-type"].startswith("text/html")
        # Must be inline, not "attachment" — otherwise the browser downloads
        # index.html instead of rendering the app.
        assert "attachment" not in resp.headers.get("content-disposition", "")


async def test_bare_ui_redirects_to_trailing_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", _build_dist(tmp_path))
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        redirect = await client.get("/ui", follow_redirects=False)
        assert redirect.status_code == HTTP_308_PERMANENT_REDIRECT
        assert redirect.headers["location"] == "/ui/"


async def test_real_asset_served_with_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", _build_dist(tmp_path))
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/ui/assets/app.js")
        assert resp.status_code == HTTP_200_OK
        # A module script must carry a JS content type or the browser won't run it.
        assert resp.headers["content-type"].startswith("text/javascript")
        assert "gateway ui" in resp.text


async def test_unknown_route_falls_back_to_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A client-side route (browser-history SPA) must serve index.html, not 404.
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", _build_dist(tmp_path))
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/ui/organizations")
        assert resp.status_code == HTTP_200_OK
        assert "admin console" in resp.text


async def test_missing_asset_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing hashed asset is a broken reference, not a client route.
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", _build_dist(tmp_path))
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        assert (await client.get("/ui/assets/missing.js")).status_code == HTTP_404_NOT_FOUND


async def test_path_traversal_does_not_escape_build_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = _build_dist(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    monkeypatch.setattr(ui_site, "UI_DIST_DIR", dist)
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        # Escaping the build dir must never leak a sibling file; it falls back to
        # the shell instead.
        resp = await client.get("/ui/..%2f..%2fsecret.txt")
        assert "TOP SECRET" not in resp.text


def test_create_ui_router_returns_none_without_build(tmp_path: Path) -> None:
    assert ui_site.create_ui_router(tmp_path / "missing") is None
