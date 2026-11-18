"""Shikigami's two annotations on an app's namespaces.

- removed-at (Charon step 1, D107): the retention clock. The retire pass stamps it ONCE on `<app>` (the request's
  removedAt if a legacy request has one, else now), copies it from there onto `<app>-build` and each `<app>-<env>`,
  and a restore (D106) takes it off. Charon deletes only namespaces that carry it, after its own 30-day check.
- theme: every pass writes the request's theme; Rancher's sorter (Meru/meru platform/rancher.yaml) files the
  namespace into that theme's project.

Meru/meru rbac/shikigami-marks-retired.yaml: get + patch namespaces, narrowed by an admission policy to these two
annotations on Meru app namespaces. The pod's API token (nuwa-app kubernetesApi), re-read each call (the kubelet
renews it).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2

REMOVED_AT = "meru.clockmost.com/removed-at"
THEME = "meru.clockmost.com/theme"


def stamp(removed_at: datetime) -> str:
    """The annotation's value: UTC, to the second, as Xolotl writes removedAt."""
    return removed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def set_annotation(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, key: str, value: str | None
) -> bool:
    """Set one annotation (None: remove it). False when the namespace doesn't exist - nothing to mark."""
    response = await http.patch(
        f"{api_url}/api/v1/namespaces/{namespace}",
        headers={
            "Authorization": f"Bearer {token_file.read_text().strip()}",
            "Content-Type": "application/merge-patch+json",
        },
        content=json.dumps({"metadata": {"annotations": {key: value}}}),
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


async def get_annotation(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, key: str
) -> tuple[bool, str | None]:
    """(namespace exists, the annotation's value or None)."""
    response = await http.get(
        f"{api_url}/api/v1/namespaces/{namespace}",
        headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
    )
    if response.status_code == 404:
        return False, None
    response.raise_for_status()
    return True, (response.json().get("metadata", {}).get("annotations") or {}).get(key)


async def set_removed_at(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, value: str | None
) -> bool:
    """The retire mark (None: remove it). False when the namespace doesn't exist."""
    return await set_annotation(http, api_url, token_file, namespace, REMOVED_AT, value)
