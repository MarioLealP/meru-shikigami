"""Marking a retired app's namespaces (Charon step 1, D107): the retire pass copies the request's removedAt (Xolotl's
stamp) onto `<app>` and `<app>-build`, and a restore (D106) takes it off. Charon deletes only namespaces that carry
it, after its own 30-day check - two keys. Meru/meru rbac/shikigami-marks-retired.yaml: get + patch namespaces,
narrowed by an admission policy to this one annotation on Meru app namespaces. The pod's API token (nuwa-app
kubernetesApi), re-read each call (the kubelet renews it)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2

REMOVED_AT = "meru.clockmost.com/removed-at"


def stamp(removed_at: datetime) -> str:
    """The annotation's value: UTC, to the second, as Xolotl writes removedAt."""
    return removed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def set_removed_at(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, value: str | None
) -> bool:
    """Set the annotation (None: remove it). False when the namespace doesn't exist - nothing to mark."""
    response = await http.patch(
        f"{api_url}/api/v1/namespaces/{namespace}",
        headers={
            "Authorization": f"Bearer {token_file.read_text().strip()}",
            "Content-Type": "application/merge-patch+json",
        },
        content=json.dumps({"metadata": {"annotations": {REMOVED_AT: value}}}),
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True
