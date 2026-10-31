"""Reading an app's Argo CD Application (D85): Shikigami's retire pass parks the marker only once Argo has applied the
stop commit - with preserveResourcesOnDeletion, an Application removed before that leaves its pods running with nothing
to stop them. Read only (Meru/meru rbac/: `get applications` in argocd), with the pod's API token (nuwa-app
kubernetesApi)."""

from pathlib import Path

import httpx2

WORKLOAD = {"Deployment", "Job", "ScaledObject"}


async def application(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, name: str
) -> dict | None:
    """The Application object, or None if it doesn't exist. The token is re-read each call (the kubelet renews it)."""
    response = await http.get(
        f"{api_url}/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications/{name}",
        headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def not_stopped(app: dict, commit: str) -> str | None:
    """Why the app isn't stopped at `commit` yet, or None once Argo has synced it and nothing runs."""
    status = app.get("status") or {}
    sync = status.get("sync") or {}
    if commit not in (sync.get("revisions") or [sync.get("revision")]):
        return f"Argo hasn't seen {commit[:12]} yet"
    if sync.get("status") != "Synced":
        return f"sync is {sync.get('status')}"
    if (status.get("operationState") or {}).get("phase") == "Running":
        return "a sync is running"
    running = sorted({r["kind"] for r in status.get("resources") or [] if r.get("kind") in WORKLOAD})
    return f"still has {', '.join(running)}" if running else None
