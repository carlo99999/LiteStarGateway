"""Fixtures for the provider-native endpoint tests.

The native endpoints reuse the OpenAI-compatible inference harness wholesale
(same app, same team/model/key setup), so re-export the `client` fixture from
the completions suite rather than duplicating it. `tests/` is on `sys.path`
during collection, so `completions` imports as a top-level package.
"""

from __future__ import annotations

from completions.conftest import client as client  # noqa: F401  (fixture re-export)
