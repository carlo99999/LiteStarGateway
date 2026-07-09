"""Serve the built MkDocs site as static files at /docs (when present).

The narrative docs are built by `just docs-build` (mkdocs, `site_dir:
.mkdocs-site` in mkdocs.yml) and, in the container, by the Dockerfile's docs
stage. When that build output exists it is mounted read-only at /docs, next to
the Swagger UI served at /. Running from source (dev/CI/tests) has no built
site, so the mount is simply absent and the app boots unchanged.
"""

from __future__ import annotations

from pathlib import Path

from litestar.router import Router
from litestar.static_files import create_static_files_router

# app.py lives at src/litestar_gateway/app.py and installs editable (a .pth into
# src/), so __file__ resolves inside the repo/image tree in dev and in the
# container alike. This module is src/litestar_gateway/infrastructure/web/
# docs_site.py, so the repo root (which holds .mkdocs-site/) is four parents up.
DOCS_SITE_DIR = Path(__file__).resolve().parents[4] / ".mkdocs-site"


def create_docs_router(site_dir: Path | None = None) -> Router | None:
    """Return a static-files router serving the built docs at /docs, or None
    when no site has been built.

    html_mode makes /docs and /docs/ resolve to index.html and sub-pages serve
    their own index.html, matching mkdocs' directory-style URLs. Degrades
    gracefully: with no built site directory the caller mounts nothing.
    """
    resolved = site_dir if site_dir is not None else DOCS_SITE_DIR
    if not resolved.is_dir():
        return None
    return create_static_files_router(
        path="/docs",
        directories=[resolved],
        html_mode=True,
        name="docs",
    )
