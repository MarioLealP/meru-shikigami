"""Reading the two sides Shikigami compares: the requests (Meru/requests, D35) and what exists (Thoth)."""

import io
import tarfile
import tempfile
from pathlib import Path

import httpx2

from shikigami.models import ExistingApp, RequestsSnapshot
from shikigami.validate import load


async def read_requests(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str
) -> RequestsSnapshot:
    """The branch's latest commit, downloaded as one archive and checked with the same library as the gate (D69)."""
    headers = {"Authorization": f"token {token}"}
    branch_info = await http.get(f"{gitea_url}/api/v1/repos/{repo}/branches/{branch}", headers=headers)
    branch_info.raise_for_status()
    commit = branch_info.json()["commit"]["id"]
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
    return {
        item["key"]: ExistingApp(key=item["key"], status=item["status"], template=item["attributes"].get("template"))
        for item in response.json()
    }
