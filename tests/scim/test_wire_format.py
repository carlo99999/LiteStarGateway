"""Pure unit tests for the SCIM wire helpers: filter parsing + PATCH op application."""

from __future__ import annotations

import pytest

from litestar_gateway.infrastructure.web.scim.schemas import (
    ScimUserAttrs,
    apply_patch_ops,
    parse_filter,
)


def test_parse_filter_supported_attributes() -> None:
    assert parse_filter('userName eq "a@b.com"') == ("userName", "a@b.com")
    assert parse_filter('externalId eq "x-1"') == ("externalId", "x-1")
    # Attribute names are case-insensitive in SCIM.
    assert parse_filter('USERNAME eq "a@b.com"') == ("userName", "a@b.com")


def test_parse_filter_rejects_unsupported_expressions() -> None:
    for expression in ('emails co "corp"', 'userName ne "x"', "userName eq unquoted", ""):
        with pytest.raises(ValueError, match="filter"):
            parse_filter(expression)


def test_apply_patch_ops_with_path_and_value_dict() -> None:
    attrs = ScimUserAttrs(user_name="a@b.com", external_id="e1", active=True)

    with_path = apply_patch_ops(attrs, [{"op": "replace", "path": "active", "value": False}])
    assert with_path == ScimUserAttrs(user_name="a@b.com", external_id="e1", active=False)
    assert attrs.active is True  # input untouched

    no_path = apply_patch_ops(
        attrs,
        [{"op": "Replace", "value": {"userName": "c@d.com", "active": "False"}}],
    )
    assert no_path == ScimUserAttrs(user_name="c@d.com", external_id="e1", active=False)


def test_apply_patch_ops_ignores_unknown_paths_and_rejects_bad_ops() -> None:
    attrs = ScimUserAttrs(user_name="a@b.com", external_id=None, active=True)
    unchanged = apply_patch_ops(attrs, [{"op": "replace", "path": "displayName", "value": "Alice"}])
    assert unchanged == attrs

    with pytest.raises(ValueError, match="op"):
        apply_patch_ops(attrs, [{"op": "remove", "path": "active"}])
