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


async def exists(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str) -> bool:
    """Whether a public repo exists, asked without credentials: the restricted bot can't see a repo it left (D89)."""
    response = await http.get(f"{gitea_url}/api/v1/repos/{org}/{repo}")
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


async def ensure_repo(
    http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, app: str, template: str, description: str
) -> bool:
    """`org/app` from its template if it doesn't exist; True if it was made now. An existing repo is kept as it is."""
    source = template_repo(template)  # a request naming no Nüwa template is refused even if the repo exists
    if await exists(http, gitea_url, org, app):
        return False
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{source}/generate",
        json={"owner": org, "name": app, "description": description, "private": False, "git_content": True},
        headers={"Authorization": f"token {token}"},
    )
    if response.status_code == 409:  # already there: kept, whatever made it
        return False
    if response.status_code == 404:
        raise NoTemplate(f"{template}: no template repo {org}/{source}")
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


async def head(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, branch: str = "main") -> str:
    """The commit a branch points at."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/branches/{branch}", headers={"Authorization": f"token {token}"}
    )
    response.raise_for_status()
    return str(response.json()["commit"]["id"])


async def delete_runners(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> int:
    """Remove every runner registered to the repo (retiring, D85; repo admin with our patched build, D80). Returns
    how many went."""
    base = f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/runners"
    headers = {"Authorization": f"token {token}"}
    response = await http.get(base, headers=headers)
    response.raise_for_status()
    runners = response.json().get("runners") or []
    for runner in runners:
        (await http.delete(f"{base}/{runner['id']}", headers=headers)).raise_for_status()
    return len(runners)


async def archive(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> bool:
    """Archive the repo (read-only from then on); True if it was archived now, False if it already was."""
    url, headers = f"{gitea_url}/api/v1/repos/{org}/{repo}", {"Authorization": f"token {token}"}
    current = await http.get(url, headers=headers)
    current.raise_for_status()
    if current.json().get("archived"):
        return False
    (await http.patch(url, headers=headers, json={"archived": True})).raise_for_status()
    return True


# The end of onboarding (D89): Shikigami protects main, hands Write to the merger and leaves the repo - from then on
# a hijacked Shikigami can't push, force-push or change settings there.


async def is_admin(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> bool:
    """Whether the bot still administers the repo. A 404 means it left: the restricted bot no longer sees it."""
    response = await http.get(f"{gitea_url}/api/v1/repos/{org}/{repo}", headers={"Authorization": f"token {token}"})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return bool((response.json().get("permissions") or {}).get("admin"))


async def protect_main(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> str:
    """Branch protection on main: pushes as before (CI's digest commits), force push off - an append-only history.
    Says what it did: added, force push turned off on an existing rule, or kept."""
    base, headers = f"{gitea_url}/api/v1/repos/{org}/{repo}/branch_protections", {"Authorization": f"token {token}"}
    response = await http.get(base, headers=headers)
    response.raise_for_status()
    rule = next((r for r in response.json() if r.get("rule_name") == "main"), None)
    if rule is None:
        created = await http.post(
            base, headers=headers, json={"rule_name": "main", "enable_push": True, "enable_force_push": False}
        )
        created.raise_for_status()
        return "added"
    if rule.get("enable_force_push"):
        (await http.patch(f"{base}/main", headers=headers, json={"enable_force_push": False})).raise_for_status()
        return "force push turned off"
    return "kept"


async def add_writer(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, user: str) -> bool:
    """The user as a Write collaborator; False if no such user exists (yet) - nothing is added then."""
    if (await http.get(f"{gitea_url}/api/v1/users/{user}")).status_code == 404:
        return False
    response = await http.put(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/collaborators/{user}",
        headers={"Authorization": f"token {token}"},
        json={"permission": "write"},
    )
    response.raise_for_status()
    return True


async def leave(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, user: str) -> None:
    """The bot removes itself from the repo: no push, no settings - public, so it still reads it anonymously."""
    response = await http.delete(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/collaborators/{user}", headers={"Authorization": f"token {token}"}
    )
    response.raise_for_status()
