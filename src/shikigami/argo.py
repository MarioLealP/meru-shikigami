"""Reading an app's Argo CD Application (D85).

Shikigami's retire pass parks the marker only once Argo has applied the stop commit - with preserveResourcesOnDeletion,
an Application removed before that leaves its pods running with nothing to stop them. Read only (Meru/meru rbac/: `get
applications` in argocd), with the pod's API token (nuwa-app kubernetesApi).
"""

from pathlib import Path

import httpx2
from pydantic import Field

from shikigami.incoming import Incoming

WORKLOAD = {"Deployment", "Job", "ScaledObject"}


# The Application, only the status fields the retire pass reads. Argo leaves most of them out until they apply.
class _Sync(Incoming):
    status: str | None = None
    revision: str | None = None
    revisions: list[str] | None = None


class _Operation(Incoming):
    phase: str | None = None


class _Resource(Incoming):
    kind: str | None = None


class _Status(Incoming):
    sync: _Sync | None = None
    operation_state: _Operation | None = Field(None, alias="operationState")
    resources: list[_Resource] | None = None


class Application(Incoming):
    """An Argo CD Application: only its status is read."""

    status: _Status | None = None


async def application(
    http: httpx2.AsyncClient, api_url: str, token_file: Path, namespace: str, name: str
) -> Application | None:
    """The Application object, or None if it doesn't exist. The token is re-read each call (the kubelet renews it)."""
    response = await http.get(
        f"{api_url}/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications/{name}",
        headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return Application.model_validate_json(response.content)


def not_stopped(app: Application, commit: str) -> str | None:
    """Why the app isn't stopped at `commit` yet, or None once Argo has synced it and nothing runs."""
    status = app.status or _Status()
    sync = status.sync or _Sync()
    if commit not in (sync.revisions or [sync.revision]):
        return f"Argo hasn't seen {commit[:12]} yet"
    if sync.status != "Synced":
        return f"sync is {sync.status}"
    if status.operation_state is not None and status.operation_state.phase == "Running":
        return "a sync is running"
    running = sorted({r.kind for r in status.resources or [] if r.kind in WORKLOAD})
    return f"still has {', '.join(running)}" if running else None
