"""SCIM 2.0 wire formats: resource/error builders + pure request parsing.

Only the attributes the gateway stores are mapped (userName ↔ email,
externalId, active ↔ is_active); anything else an IdP sends (displayName,
name.givenName, ...) is accepted and ignored, because rejecting unknown
attributes breaks Entra/Okta provisioning runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from litestar_gateway.domain.entities import User

SCIM_MEDIA_TYPE = "application/scim+json"

USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_URN = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# The only filter shape IdPs use for matching: `<attr> eq "<value>"`.
_FILTER_RE = re.compile(r'^\s*(userName|externalId)\s+eq\s+"([^"]*)"\s*$', re.IGNORECASE)
_CANONICAL_ATTR = {"username": "userName", "externalid": "externalId"}


@dataclass(frozen=True)
class ScimUserAttrs:
    """The gateway-managed subset of a SCIM User resource."""

    user_name: str | None
    external_id: str | None
    active: bool


def parse_filter(expression: str) -> tuple[str, str]:
    """Parse a SCIM filter into (attribute, value). Only equality on
    userName/externalId is supported — anything else raises ValueError."""
    match = _FILTER_RE.match(expression or "")
    if match is None:
        raise ValueError(
            "Unsupported SCIM filter: only 'userName eq \"...\"' and"
            " 'externalId eq \"...\"' are supported"
        )
    return _CANONICAL_ATTR[match.group(1).lower()], match.group(2)


def parse_user_payload(payload: dict[str, Any], *, require_user_name: bool = True) -> ScimUserAttrs:
    """Extract the stored attributes from a POST/PUT User resource."""
    user_name = payload.get("userName")
    if user_name is not None and not isinstance(user_name, str):
        raise ValueError("userName must be a string")
    if require_user_name and not (user_name and user_name.strip()):
        raise ValueError("userName is required")
    external_id = payload.get("externalId")
    if external_id is not None:
        external_id = str(external_id)
    return ScimUserAttrs(
        user_name=user_name,
        external_id=external_id,
        active=_as_bool(payload.get("active", True)),
    )


def apply_patch_ops(attrs: ScimUserAttrs, operations: list[dict[str, Any]]) -> ScimUserAttrs:
    """Apply RFC 7644 PATCH operations, returning a new ScimUserAttrs.

    Supports `replace`/`add` (case-insensitive — Entra sends "Replace"), both
    with a path and Entra's no-path form ({"op": "Replace", "value": {...}}).
    Unknown attribute paths are ignored; unsupported op types raise ValueError.
    """
    for operation in operations:
        op = str(operation.get("op", "")).lower()
        if op not in ("replace", "add"):
            raise ValueError(f"Unsupported PATCH op: {operation.get('op')!r}")
        path, value = operation.get("path"), operation.get("value")
        if path is None:
            if not isinstance(value, dict):
                raise ValueError("A PATCH op without a path requires an object value")
            for attr, attr_value in value.items():
                attrs = _set_attr(attrs, str(attr), attr_value)
        else:
            attrs = _set_attr(attrs, str(path), value)
    return attrs


def _set_attr(attrs: ScimUserAttrs, path: str, value: Any) -> ScimUserAttrs:
    # Tolerate URN-qualified paths ("urn:...:User:active"); attribute names are
    # case-insensitive per RFC 7643.
    attr = path.rsplit(":", 1)[-1].strip().lower()
    if attr == "active":
        return replace(attrs, active=_as_bool(value))
    if attr == "username":
        return replace(attrs, user_name=str(value))
    if attr == "externalid":
        return replace(attrs, external_id=str(value))
    return attrs


def _as_bool(value: Any) -> bool:
    """Booleans as sent in the wild: real JSON booleans, or Entra's "True"/"False"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"Expected a boolean, got: {value!r}")


def scim_user_resource(user: User) -> dict[str, Any]:
    return {
        "schemas": [USER_URN],
        "id": str(user.id),
        "userName": user.email,
        "externalId": user.external_id,
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat(),
            "location": f"/scim/v2/Users/{user.id}",
        },
    }


def scim_list_response(
    resources: list[dict[str, Any]], *, total: int, start_index: int
) -> dict[str, Any]:
    return {
        "schemas": [LIST_URN],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def scim_error(status: int, detail: str, scim_type: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schemas": [ERROR_URN], "status": str(status), "detail": detail}
    if scim_type is not None:
        body["scimType"] = scim_type
    return body
