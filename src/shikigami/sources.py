"""Reading the two sides Shikigami compares: the requests (Meru/requests, D35) and what exists (Thoth)."""

import io
import tarfile
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx2
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

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


@asynccontextmanager
async def requests_checkout(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str
) -> AsyncGenerator[tuple[str, Path]]:
    """The branch's latest commit, downloaded as one archive into a temporary folder: (commit, the repo's root)."""
    headers = {"Authorization": f"token {token}"}
    branch_info = await http.get(f"{gitea_url}/api/v1/repos/{repo}/branches/{branch}", headers=headers)
    branch_info.raise_for_status()
    commit = _Branch.model_validate_json(branch_info.content).commit.id
    archive = await http.get(f"{gitea_url}/api/v1/repos/{repo}/archive/{commit}.tar.gz", headers=headers)
    archive.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(archive.content)) as tar:
        tar.extractall(tmp, filter="data")  # refuses links out, absolute paths, devices
        roots = list(Path(tmp).iterdir())  # Gitea wraps everything in one <repo>/ folder
        yield commit, roots[0] if len(roots) == 1 and roots[0].is_dir() else Path(tmp)


async def read_requests(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str
) -> RequestsSnapshot:
    """The branch's latest commit, checked with the same library as the gate (D69)."""
    async with requests_checkout(http, gitea_url, repo, branch, token) as (commit, root):
        loaded = load(root)
    return RequestsSnapshot(commit=commit, apps=loaded.apps, databases=loaded.databases, problems=loaded.problems)


class Proposal(BaseModel):
    """One request file someone wants on the requests repo (Janus asks before opening its PR, D96)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Annotated[str, StringConstraints(pattern=r"^(apps|databases)/[a-z0-9]([-a-z0-9]*[a-z0-9])?\.yaml$")]
    content: Annotated[str, StringConstraints(max_length=65536)]


class Verdict(BaseModel):
    """What the proposal would add to the problems of the requests repo's main - none = it may go in."""

    model_config = ConfigDict(frozen=True)

    commit: str  # the main it was checked against
    problems: list[str]


async def check_proposal(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str, proposal: Proposal
) -> Verdict:
    """The proposal checked together with every request on main, by the same load() a sync uses - so the checks
    across files (one host per app, a database's app) count too. Problems main already has aren't blamed on it."""
    async with requests_checkout(http, gitea_url, repo, branch, token) as (commit, root):
        before = set(load(root).problems)
        target = root / proposal.path
        target.parent.mkdir(exist_ok=True)
        target.write_text(proposal.content)
        after = load(root).problems
        prefix = f"{root}/"
    return Verdict(commit=commit, problems=[p.replace(prefix, "") for p in after if p not in before])


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


async def mark_retire_step(http: httpx2.AsyncClient, thoth_url: str, token: str, app: str, status: str) -> str:
    """Tell Thoth where an app's retirement stands (D85): decommissioning when the pass starts, retained when it's
    done, planned again when it's restored (D106). Bookkeeping, never a gate: the stop and the revokes matter more
    than Thoth's status, so a refusal is said and the pass goes on - the next pass (every sync) tries again. Network
    and server errors raise (retried)."""
    reason = "Shikigami's restore (D106)" if status == "planned" else "Shikigami's retire pass (D85)"
    response = await http.post(
        f"{thoth_url}/v1/retire-steps",
        json={"app": app, "status": status, "reason": reason},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 200:
        return status
    # no thoth.retire yet / not in Thoth / already further along / a Thoth that doesn't know restores yet
    if response.status_code in (403, 404, 409, 422):
        return f"not marked {status}: Thoth answered {response.status_code} {response.text[:200]}"
    response.raise_for_status()
    return f"not marked {status}: Thoth answered {response.status_code}"
