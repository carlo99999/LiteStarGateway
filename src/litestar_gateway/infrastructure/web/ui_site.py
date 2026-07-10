"""Serve the built admin UI (Vite/React SPA) as static files at /ui (when present).

The admin console lives in `ui/` and is built by `just ui-build` (Vite, output
`ui/dist`) and, in the container, by the Dockerfile. When that build output
exists it is mounted at /ui, next to the Swagger UI served at / and the narrative
docs at /docs. Running from source with no build (dev/CI/tests) leaves the mount
absent and the app boots unchanged.

The UI is a single-page app with client-side, browser-history routing under the
`/ui` basepath, so any `/ui/*` path that is not a real build artifact must return
`index.html` (HTTP 200) and let the client router resolve it — the SPA fallback.
Real files (the hashed `/ui/assets/*` bundle) are served straight from disk with
their inferred content type; a genuinely missing asset is a real 404, not the
app shell.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from litestar import Request, get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import File, Redirect
from litestar.router import Router
from litestar.status_codes import HTTP_308_PERMANENT_REDIRECT

# This module is src/litestar_gateway/infrastructure/web/ui_site.py, so the repo
# root (which holds ui/dist) is four parents up — the same anchor docs_site uses.
UI_DIST_DIR = Path(__file__).resolve().parents[4] / "ui" / "dist"


def _resolve_within(root: Path, relative: str) -> Path | None:
    """Resolve `relative` under `root`, or None if it escapes the tree.

    Guards against path traversal: a `..` sequence resolving outside the build
    directory returns None, so the caller falls back to index.html instead of
    serving an arbitrary file on disk.
    """
    root = root.resolve()
    # A `:path` param can arrive with a leading slash; joining that as-is would be
    # treated as an absolute path and escape `root`. Strip it so the join stays
    # relative, then confirm the resolved result is still inside the tree.
    candidate = (root / relative.lstrip("/")).resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _redirect_ui_root(request: Request) -> Redirect | None:
    """Redirect the bare `/ui` (no trailing slash) to `/ui/`.

    The SPA references its assets absolutely (`/ui/assets/…`), so a bare `/ui`
    would still load, but redirecting keeps the canonical trailing-slash form and
    mirrors the /docs mount. `raw_path` preserves the original bytes (the router
    normalizes `scope["path"]`), so `/ui` and `/ui/` stay distinguishable and the
    redirect never loops on `/ui/`.
    """
    if request.scope.get("raw_path") == b"/ui":
        return Redirect("/ui/", status_code=HTTP_308_PERMANENT_REDIRECT)
    return None


def create_ui_router(dist_dir: Path | None = None) -> Router | None:
    """Return a router serving the built admin UI at /ui, or None when no build
    exists.

    A single catch-all handler serves the request path from the build directory
    when it maps to a real file (the hashed asset bundle) and otherwise returns
    index.html so the client-side router can resolve the route (SPA fallback).
    Degrades gracefully: with no built dist directory the caller mounts nothing.
    """
    resolved = (dist_dir if dist_dir is not None else UI_DIST_DIR).resolve()
    index_html = resolved / "index.html"
    if not index_html.is_file():
        return None

    def _shell() -> File:
        # content_disposition_type="inline": Litestar's File defaults to
        # "attachment", which makes the browser DOWNLOAD index.html instead of
        # rendering the app. Serve it (and every asset) inline.
        return File(index_html, media_type="text/html", content_disposition_type="inline")

    # Two handlers, not one with both paths: Litestar only binds `file_path` when
    # the path parameter is the handler's sole route, so `/ui/` gets its own
    # paramless handler and everything deeper flows through the catch-all.
    @get("/", name="ui-shell", include_in_schema=False, sync_to_thread=False)
    def serve_ui_root() -> File:
        return _shell()

    @get("/{file_path:path}", name="ui", include_in_schema=False, sync_to_thread=False)
    def serve_ui(file_path: FromPath[str]) -> File:
        target = _resolve_within(resolved, file_path)
        if target is not None and target.is_file():
            # Litestar's File does not infer a content type, so a `.js`/`.css`
            # asset would be served as octet-stream — which the browser refuses
            # to run as a module script. Set it from the suffix explicitly.
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return File(target, media_type=media_type, content_disposition_type="inline")
        # A missing hashed asset is a genuine 404 (a broken reference), not a
        # client-side route — don't mask it as the app shell.
        if file_path.strip("/").startswith("assets/"):
            raise NotFoundException()
        return _shell()

    return Router(
        path="/ui",
        route_handlers=[serve_ui_root, serve_ui],
        before_request=_redirect_ui_root,
    )
