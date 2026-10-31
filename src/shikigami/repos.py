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
