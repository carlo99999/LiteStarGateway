"""``litegateway`` — run the gateway with the API, Swagger, docs, and admin UI on
one port.

The admin console (``ui/``) is a static single-page app, so the gateway serves
its build (``ui/dist``) at ``/ui`` from the very same ASGI process: one command,
one process, one port exposes everything — no reverse proxy needed. When the UI
build is missing and the source tree plus pnpm are available, this command builds
it first; otherwise it simply starts without the console (the /ui mount is absent
and everything else works unchanged).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("litestar_gateway.cli")

# This module is src/litestar_gateway/cli.py, so the repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = _REPO_ROOT / "ui"
_UI_DIST = _UI_DIR / "dist"


def _build_ui() -> None:
    """Build the admin UI (``ui/dist``) when the source tree and pnpm are present.

    Best-effort: with no ``ui/`` source (e.g. an installed wheel that ships only
    the backend) or no pnpm on PATH, log why and return — the app just won't
    mount /ui. Raises ``CalledProcessError`` only if a build actually runs and
    fails, which the caller downgrades to a warning.
    """
    if not (_UI_DIR / "package.json").is_file():
        logger.info("no ui/ source tree found; starting without the admin console at /ui")
        return
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        logger.warning(
            "pnpm not found on PATH; skipping UI build. Install pnpm (https://pnpm.io) "
            "or run `just ui-build`, then restart to enable the admin console at /ui."
        )
        return
    logger.info("building admin UI (pnpm install + build) — first run only…")
    subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=_UI_DIR, check=True)  # noqa: S603
    subprocess.run([pnpm, "run", "build"], cwd=_UI_DIR, check=True)  # noqa: S603
    logger.info("admin UI built → %s", _UI_DIST)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="litegateway",
        description="Run Litestar Gateway (API + Swagger + docs + admin UI) on one port.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")  # noqa: S104
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument(
        "--reload", action="store_true", help="auto-reload on code changes (development)"
    )
    parser.add_argument(
        "--skip-ui-build",
        action="store_true",
        help="never build the UI, even if ui/dist is missing",
    )
    parser.add_argument(
        "--rebuild-ui",
        action="store_true",
        help="rebuild the UI even if ui/dist already exists",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.skip_ui_build and (args.rebuild_ui or not _UI_DIST.is_dir()):
        try:
            _build_ui()
        except subprocess.CalledProcessError as exc:
            logger.error("UI build failed (%s); starting without the admin console at /ui.", exc)

    # Imported here (not at module load) so `--help` and the build step don't pay
    # for the server import, and so a UI-only invocation never touches uvicorn.
    import uvicorn

    logger.info(
        "serving on http://%s:%d  (API+Swagger: /  · docs: /docs/  · admin UI: /ui/)",
        args.host,
        args.port,
    )
    uvicorn.run(
        "litestar_gateway.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
