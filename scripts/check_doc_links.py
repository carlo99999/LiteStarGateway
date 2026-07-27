"""Check that internal relative Markdown links in plans/ and docs/ resolve.

Scans every `plans/*.md` and `docs/**/*.md` file for Markdown links
(`[text](target)`), skips external links (http(s)/mailto) and pure-anchor
links (`#section`), resolves the remaining relative targets against the
linking file's directory, and fails if the target file doesn't exist.

This only catches the "renamed/moved a linked doc" failure mode — it does not
validate that in-file anchors exist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\]\(([^)]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


def find_markdown_files() -> list[Path]:
    files = sorted((REPO_ROOT / "plans").glob("*.md"))
    files += sorted((REPO_ROOT / "docs").rglob("*.md"))
    return files


def broken_links_in(path: Path) -> list[str]:
    broken = []
    text = path.read_text(encoding="utf-8")
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        if not target or target.startswith(EXTERNAL_PREFIXES) or target.startswith("#"):
            continue
        # Drop a trailing in-file anchor, e.g. `file.md#section`.
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        resolved = (path.parent / target_path).resolve()
        if not resolved.is_file():
            broken.append(f"{path.relative_to(REPO_ROOT)}: broken link -> {target}")
    return broken


def main() -> int:
    all_broken = []
    for md_file in find_markdown_files():
        all_broken.extend(broken_links_in(md_file))

    if all_broken:
        print(f"Found {len(all_broken)} broken internal Markdown link(s):", file=sys.stderr)
        for line in all_broken:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("No broken internal Markdown links found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
