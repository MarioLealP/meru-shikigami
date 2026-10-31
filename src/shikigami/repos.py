"""An app's repo (D76, D77): generated from its Nüwa template - a Gitea template repo - by the Shikigami bot, which
Gitea makes admin of each repo it creates. Only the template's files: no webhooks, labels or protection copied."""

import base64

import httpx2


class NoTemplate(Exception):
    """The request names a template that isn't a Nüwa template repo (yet)."""


def template_repo(template: str) -> str:
    """`nuwa/python` -> `nuwa-python`. Git URLs seed a repo differently (they need approval) - not handled here."""
    kind, _, name = template.partition("/")
    if kind != "nuwa" or not name:
        raise NoTemplate(f"{template}: only nuwa/<name> templates make repos so far")
    return f"nuwa-{name}"


async def ensure_repo(
    http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, app: str, template: str, description: str
) -> bool:
    """`org/app` from its template if it doesn't exist; True if it was made now. An existing repo is kept as it is."""
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{template_repo(template)}/generate",
        json={"owner": org, "name": app, "description": description, "private": False, "git_content": True},
        headers={"Authorization": f"token {token}"},
    )
    if response.status_code == 409:  # already there: kept, whatever made it
        return False
    if response.status_code == 404:
        raise NoTemplate(f"{template}: no template repo {org}/{template_repo(template)}")
    response.raise_for_status()
    return True


async def runner_token(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, app: str) -> str:
    """The registration token the app's Sindri runner joins its repo with. Gitea asks for repo admin (our patched
    build, D80; upstream wants owner) - Shikigami is admin of the repos it creates. Answers the repo's current token,
    so asking again doesn't break a runner that already joined."""
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{app}/actions/runners/registration-token",
        headers={"Authorization": f"token {token}"},
    )
    response.raise_for_status()
    return str(response.json()["token"])


async def read_file(
    http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, path: str
) -> tuple[str, str] | None:
    """A file on the repo's default branch: (text, blob sha - what a change to it must name), or None."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/contents/{path}", headers={"Authorization": f"token {token}"}
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    return base64.b64decode(body["content"]).decode(), str(body["sha"])


async def public_file(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, path: str) -> str | None:
    """A file from a public repo, read without credentials (the bot is restricted: it sees only repos it was added
    to, the public ones included)."""
    response = await http.get(f"{gitea_url}/api/v1/repos/{org}/{repo}/raw/{path}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def change(operation: str, path: str, text: str | None = None, sha: str | None = None) -> dict[str, str]:
    """One file operation of a commit: create (text), update (text + sha), delete (sha)."""
    op = {"operation": operation, "path": path}
    if text is not None:
        op["content"] = base64.b64encode(text.encode()).decode()
    if sha is not None:
        op["sha"] = sha
    return op


async def commit_files(
    http: httpx2.AsyncClient,
    gitea_url: str,
    token: str,
    org: str,
    repo: str,
    message: str,
    files: list[dict[str, str]],
) -> None:
    """All the changes as one commit on the default branch. A stale sha fails the whole commit (Gitea answers 409/422),
    so nothing half-written lands."""
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/contents",
        headers={"Authorization": f"token {token}"},
        json={"message": message, "files": files},
    )
    response.raise_for_status()


async def run_workflow(
    http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, workflow: str, ref: str = "main"
) -> None:
    """Start a workflow by hand ("Run workflow", workflow_dispatch) - the app's first build: CI skips pushes that
    only touch deploy/** (its own digest commits), and the marker commit is one."""
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/workflows/{workflow}/dispatches",
        headers={"Authorization": f"token {token}"},
        json={"ref": ref},
    )
    response.raise_for_status()
