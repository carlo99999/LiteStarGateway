"""Serve the built MkDocs site as static files at /docs (when present).

The narrative docs are built by `just docs-build` (mkdocs, `site_dir:
.mkdocs-site` in mkdocs.yml) and, in the container, by the Dockerfile's docs
stage. When that build output exists it is mounted read-only at /docs, next to
the Swagger UI served at /. Running from source (dev/CI/tests) has no built
site, so the mount is simply absent and the app boots unchanged.
"""

from __future__ import annotations

from pathlib import Path

from litestar import Request
from litestar.response import Redirect
from litestar.router import Router
from litestar.static_files import create_static_files_router
from litestar.status_codes import HTTP_308_PERMANENT_REDIRECT

# app.py lives at src/litestar_gateway/app.py and installs editable (a .pth into
# src/), so __file__ resolves inside the repo/image tree in dev and in the
# container alike. This module is src/litestar_gateway/infrastructure/web/
# docs_site.py, so the repo root (which holds .mkdocs-site/) is four parents up.
DOCS_SITE_DIR = Path(__file__).resolve().parents[4] / ".mkdocs-site"


def _redirect_docs_root(request: Request) -> Redirect | None:
    """Redirect the bare `/docs` (no trailing slash) to `/docs/`.

    MkDocs pages reference their assets relatively (e.g. `assets/…`), so a
    browser sitting at `/docs` would resolve them against the parent path
    (`/assets/…` → 404 → unstyled page). Sending it to `/docs/` first makes the
    relative asset URLs resolve under `/docs/`.

    The trailing slash must come from the raw request path: the static-files
    router normalizes `scope["path"]` (stripping the trailing slash) before this
    hook runs, so `/docs` and `/docs/` are indistinguishable there and comparing
    it would redirect `/docs/` onto itself in a loop. `raw_path` preserves the
    original bytes and excludes the query string (ASGI), so it tells them apart.
    """
    if request.scope.get("raw_path") == b"/docs":
        return Redirect("/docs/", status_code=HTTP_308_PERMANENT_REDIRECT)
    return None


def create_docs_router(site_dir: Path | None = None) -> Router | None:
    """Return a static-files router serving the built docs at /docs, or None
    when no site has been built.

    html_mode makes /docs and /docs/ resolve to index.html and sub-pages serve
    their own index.html, matching mkdocs' directory-style URLs. The
    before_request hook redirects the bare `/docs` to `/docs/` so relatively
    referenced assets resolve; it lives on this router, so it is active only
    when a built site is actually mounted. Degrades gracefully: with no built
    site directory the caller mounts nothing.
    """
    resolved = site_dir if site_dir is not None else DOCS_SITE_DIR
    if not resolved.is_dir():
        return None
    return create_static_files_router(
        path="/docs",
        directories=[resolved],
        html_mode=True,
        name="docs",
        before_request=_redirect_docs_root,
    )
