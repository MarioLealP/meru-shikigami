"""Reading the two sides Shikigami compares: the requests (Meru/requests, D35) and what exists.

What exists is no longer Thoth (rework 6): it is the app's Gitea repo and its Argo CD Application - see
`read_existing_apps`. Thoth still hears the request picture (`declare_apps`) and the retire steps
(`mark_retire_step`) until its own card retires those.
"""

import asyncio
import io
import tarfile
import tempfile
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx2
from pydantic import BaseModel, ConfigDict, StringConstraints

from shikigami.clients import argo, repos
from shikigami.clients.incoming import Incoming
from shikigami.core import retire
from shikigami.core.validate import load
from shikigami.models.sync import ExistingApp, RequestsSnapshot


class _Commit(Incoming):
    id: str


class _Branch(Incoming):
    commit: _Commit


def _unpack(archive: bytes, into: Path) -> Path:
    """Extract the archive into the folder; returns the repo's root."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")  # refuses links out, absolute paths, devices
    roots = list(into.iterdir())  # Gitea wraps everything in one <repo>/ folder
    return roots[0] if len(roots) == 1 and roots[0].is_dir() else into


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
    with tempfile.TemporaryDirectory() as tmp:
        yield commit, await asyncio.to_thread(_unpack, archive.content, Path(tmp))


async def read_requests(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str
) -> RequestsSnapshot:
    """The branch's latest commit, checked with the same library as the gate (D69)."""
    async with requests_checkout(http, gitea_url, repo, branch, token) as (commit, root):
        loaded = await asyncio.to_thread(load, root)
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


def _added_problems(root: Path, proposal: Proposal) -> list[str]:
    """The problems the checkout at `root` gains once the proposal is written into it."""
    before = set(load(root).problems)
    target = root / proposal.path
    target.parent.mkdir(exist_ok=True)
    target.write_text(proposal.content)
    prefix = f"{root}/"
    return [p.replace(prefix, "") for p in load(root).problems if p not in before]


async def check_proposal(
    http: httpx2.AsyncClient, gitea_url: str, repo: str, branch: str, token: str, proposal: Proposal
) -> Verdict:
    """The proposal checked together with every request on main, by the same load() a sync uses.

    So the checks across files (one host per app, a database's app) count too. Problems main already has aren't blamed
    on it.
    """
    async with requests_checkout(http, gitea_url, repo, branch, token) as (commit, root):
        problems = await asyncio.to_thread(_added_problems, root, proposal)
    return Verdict(commit=commit, problems=problems)


async def read_existing_apps(
    http: httpx2.AsyncClient,
    kube: httpx2.AsyncClient,
    gitea_url: str,
    org: str,
    kubernetes_url: str,
    kube_token_file: Path,
    argocd_namespace: str,
    names: Iterable[str],
) -> dict[str, ExistingApp]:
    """What exists for each requested name - its Gitea repo and its Argo CD Application, never Thoth (rework 6).

    The repo says the app was made; the Application (`app-<name>`, one per repo with a marker, Meru/meru's
    ApplicationSet) says it is deployed. The status keeps plan()'s vocabulary, read from the repo's own deploy
    files: D85's stop writes one `enabled: false # retired` line into deploy/values.yaml, and parking the marker
    takes deploy/meru.yaml away. So a stopped app with its marker still up is `decommissioning`, one without is
    `retained` (retired, kept until Charon purges), and anything else is `active` (in service or still onboarding).
    A name with neither repo nor Application is absent from the answer: plan() registers those from scratch.

    Only reads, in parallel per name: `get applications` in argocd (shikigami-reads-apps allows get, no list) and
    anonymous Gitea - the way Argo reads the repos (Meru/meru deploys public repos only), so a repo the restricted
    bot can't see (D89) still counts. `template` stays unknown: neither source records which template a repo came
    from (Thoth remembered what the last sync declared).
    """

    async def one(name: str) -> ExistingApp | None:
        deployed = await argo.application(kube, kubernetes_url, kube_token_file, argocd_namespace, f"app-{name}")
        repo = await repos.exists(http, gitea_url, org, name)
        if deployed is None and not repo:
            return None
        values = await repos.public_file(http, gitea_url, org, name, "deploy/values.yaml") if repo else None
        if values is not None and retire.STOPPED.search(values):
            marker = await repos.public_file(http, gitea_url, org, name, "deploy/meru.yaml")
            status = "decommissioning" if marker is not None else "retained"
        else:
            status = "active"
        return ExistingApp(key=name, status=status)

    found = await asyncio.gather(*(one(name) for name in sorted(names)))
    return {app.key: app for app in found if app is not None}


async def declare_apps(http: httpx2.AsyncClient, thoth_url: str, token: str, snapshot: RequestsSnapshot) -> dict:
    """Every requested app, as one Thoth snapshot (D52).

    Shikigami declares, Thoth keeps them `planned` until a scanner sees them, and only Anubis retires (D53). The scan id
    is the requests commit, so a resend changes nothing.
    """
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
    """Tell Thoth where an app's retirement stands (D85).

    Decommissioning when the pass starts, retained when it's done, planned again when it's restored (D106). Bookkeeping,
    never a gate: the stop and the revokes matter more than Thoth's status, so a refusal is said and the pass goes on -
    the next pass (every sync) tries again. Network and server errors raise (retried).
    """
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
