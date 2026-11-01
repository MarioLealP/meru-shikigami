"""Reading the two sides Shikigami compares: the requests (Meru/requests, D35) and what exists (Thoth)."""

import io
import tarfile
import tempfile
from pathlib import Path

import httpx2
from pydantic import Field, TypeAdapter

from shikigami.incoming import Incoming
from shikigami.models import ExistingApp, RequestsSnapshot
from shikigami.validate import load


class _Commit(Incoming):
    id: str


class _Branch(Incoming):
    commit: _Commit


class _Item(Incoming):
    """A Thoth item, as /v1/items lists it."""

    key: str
    status: str
    attributes: dict[str, object] = Field(default_factory=dict)


_ITEMS = TypeAdapter(list[_Item])


async def read_requests(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str
) -> RequestsSnapshot:
    """The branch's latest commit, downloaded as one archive and checked with the same library as the gate (D69)."""
    headers = {"Authorization": f"token {token}"}
    branch_info = await http.get(f"{gitea_url}/api/v1/repos/{repo}/branches/{branch}", headers=headers)
    branch_info.raise_for_status()
    commit = _Branch.model_validate_json(branch_info.content).commit.id
    archive = await http.get(f"{gitea_url}/api/v1/repos/{repo}/archive/{commit}.tar.gz", headers=headers)
    archive.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(archive.content)) as tar:
        tar.extractall(tmp, filter="data")  # refuses links out, absolute paths, devices
        roots = list(Path(tmp).iterdir())  # Gitea wraps everything in one <repo>/ folder
        loaded = load(roots[0] if len(roots) == 1 and roots[0].is_dir() else Path(tmp))
    return RequestsSnapshot(commit=commit, apps=loaded.apps, databases=loaded.databases, problems=loaded.problems)


async def read_existing_apps(http: httpx2.AsyncClient, thoth_url: str, token: str) -> dict[str, ExistingApp]:
    response = await http.get(
        f"{thoth_url}/v1/items", params={"type": "app", "limit": 1000}, headers={"Authorization": f"Bearer {token}"}
    )
    response.raise_for_status()
    apps = {}
    for item in _ITEMS.validate_json(response.content):
        template = item.attributes.get("template")
        apps[item.key] = ExistingApp(
            key=item.key, status=item.status, template=template if isinstance(template, str) else None
        )
    return apps


async def declare_apps(http: httpx2.AsyncClient, thoth_url: str, token: str, snapshot: RequestsSnapshot) -> dict:
    """Every requested app, as one Thoth snapshot (D52): Shikigami declares, Thoth keeps them `planned` until a scanner
    sees them, and only Anubis retires (D53). The scan id is the requests commit, so a resend changes nothing."""
    items = [
        {"type": "app", "key": name, "name": name, "attributes": {"template": app.spec.template}}
        for name, app in sorted(snapshot.apps.items())
    ]
    response = await http.put(
        f"{thoth_url}/v1/scans/requests-{snapshot.commit}/shikigami/all/items",
        json={"items": items},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()
