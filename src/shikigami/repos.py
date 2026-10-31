"""An app's repo (D76, D77): generated from its Nüwa template - a Gitea template repo - by the Shikigami bot, which
Gitea makes admin of each repo it creates. Only the template's files: no webhooks, labels or protection copied."""

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
