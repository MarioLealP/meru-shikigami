"""An app's repo (D76, D77), generated from its Nüwa template by the Shikigami bot.

The template is a Gitea template repo, and Gitea makes the bot admin of each repo it creates. Only the template's
files: no webhooks, labels or protection copied.
"""

import base64

import httpx2

from shikigami.clients.incoming import Incoming


# Gitea's answers - only the fields Shikigami reads.
class _RegistrationToken(Incoming):
    token: str


class _Contents(Incoming):
    content: str  # base64
    sha: str


class _Commit(Incoming):
    id: str


class _Branch(Incoming):
    commit: _Commit


class _Runner(Incoming):
    id: int


class _Runners(Incoming):
    runners: list[_Runner] | None = None


class _Permissions(Incoming):
    admin: bool = False


class _Repo(Incoming):
    archived: bool = False
    permissions: _Permissions | None = None


class NoTemplateError(Exception):
    """The request names a template that isn't a Nüwa template repo (yet)."""


def template_repo(template: str) -> str:
    """`nuwa/python` -> `nuwa-python`. Git URLs seed a repo differently (they need approval) - not handled here."""
    kind, _, name = template.partition("/")
    if kind != "nuwa" or not name:
        raise NoTemplateError(f"{template}: only nuwa/<name> templates make repos so far")
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
        raise NoTemplateError(f"{template}: no template repo {org}/{source}")
    response.raise_for_status()
    return True


async def runner_token(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, app: str) -> str:
    """The registration token the app's Sindri runner joins its repo with.

    Gitea asks for repo admin (our patched build, D80; upstream wants owner) - Shikigami is admin of the repos it
    creates. Answers the repo's current token, so asking again doesn't break a runner that already joined.
    """
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{app}/actions/runners/registration-token",
        headers={"Authorization": f"token {token}"},
    )
    response.raise_for_status()
    return _RegistrationToken.model_validate_json(response.content).token


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
    body = _Contents.model_validate_json(response.content)
    return base64.b64decode(body.content).decode(), body.sha


async def public_file(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, path: str) -> str | None:
    """A file from a public repo, read without credentials.

    The bot is restricted: it sees only repos it was added to, the public ones included.
    """
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
    """All the changes as one commit on the default branch.

    A stale sha fails the whole commit (Gitea answers 409/422), so nothing half-written lands.
    """
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/contents",
        headers={"Authorization": f"token {token}"},
        json={"message": message, "files": files},
    )
    response.raise_for_status()


async def run_workflow(
    http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, workflow: str, ref: str = "main"
) -> None:
    """Start a workflow by hand ("Run workflow", workflow_dispatch): the app's first build.

    CI skips pushes that only touch deploy/** (its own digest commits), and the marker commit is one.
    """
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/workflows/{workflow}/dispatches",
        headers={"Authorization": f"token {token}"},
        json={"ref": ref},
    )
    response.raise_for_status()


async def head(
    http: httpx2.AsyncClient, gitea_url: str, token: str | None, org: str, repo: str, branch: str = "main"
) -> str:
    """The commit a branch points at. token None: asked anonymously (public repos)."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/branches/{branch}",
        headers={"Authorization": f"token {token}"} if token else {},
    )
    response.raise_for_status()
    return _Branch.model_validate_json(response.content).commit.id


async def delete_runners(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> int:
    """Remove every runner registered to the repo (retiring, D85; repo admin with our patched build, D80).

    Returns how many went.
    """
    base = f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/runners"
    headers = {"Authorization": f"token {token}"}
    response = await http.get(base, headers=headers)
    response.raise_for_status()
    runners = _Runners.model_validate_json(response.content).runners or []
    for runner in runners:
        (await http.delete(f"{base}/{runner.id}", headers=headers)).raise_for_status()
    return len(runners)


async def archive(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> bool:
    """Archive the repo (read-only from then on); True if it was archived now, False if it already was."""
    url, headers = f"{gitea_url}/api/v1/repos/{org}/{repo}", {"Authorization": f"token {token}"}
    current = await http.get(url, headers=headers)
    current.raise_for_status()
    if _Repo.model_validate_json(current.content).archived:
        return False
    (await http.patch(url, headers=headers, json={"archived": True})).raise_for_status()
    return True


class BuildRun(Incoming):
    """A CI run as Gitea lists it. `run_attempt` counts re-runs of the same run (1 = never re-run)."""

    id: int
    status: str  # queued | waiting | in_progress | completed
    conclusion: str | None = None  # success | failure | cancelled | skipped, once completed
    run_attempt: int = 1


class _BuildRuns(Incoming):
    workflow_runs: list[BuildRun] | None = None


async def dispatched_run(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> BuildRun | None:
    """The newest hand-started ("Run workflow") run on main - the app's first build, which Shikigami dispatches."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/runs",
        params={"event": "workflow_dispatch", "branch": "main", "limit": 1},
        headers={"Authorization": f"token {token}"},
    )
    response.raise_for_status()
    runs = _BuildRuns.model_validate_json(response.content).workflow_runs or []
    return runs[0] if runs else None


async def rerun_failed(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, run: int) -> None:
    """Re-run a run's failed jobs (its run_attempt goes up by one); the jobs that passed keep their result."""
    response = await http.post(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/actions/runs/{run}/rerun-failed-jobs",
        headers={"Authorization": f"token {token}"},
    )
    response.raise_for_status()


# The end of onboarding (D89): Shikigami protects main, hands Write to the merger and drops itself to Read - from then
# on a hijacked Shikigami can't push, force-push or change settings there, only propose (cm-konk).


async def is_admin(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> bool:
    """Whether the bot still administers the repo. A 404 means it left: the restricted bot no longer sees it."""
    response = await http.get(f"{gitea_url}/api/v1/repos/{org}/{repo}", headers={"Authorization": f"token {token}"})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    permissions = _Repo.model_validate_json(response.content).permissions
    return permissions is not None and permissions.admin


# D126 step 4: a change under an environment called prod - its digest, its database - merges only by a repo admin's
# override (Mario). Gitea refuses it to everyone else on a PR merge, a scheduled auto-merge and a direct push alike;
# uat and the rest stay the owner's. Set on every app: on one without a prod environment it matches nothing.
PROD_FILES = "deploy/envs/prod/**"


def _with_prod_files(patterns: str) -> str:
    """The rule's protected file patterns (`;`-separated, as Gitea keeps them) with the prod one added, if missing."""
    kept = [p.strip() for p in patterns.split(";") if p.strip()]
    return ";".join(kept if PROD_FILES in kept else [*kept, PROD_FILES])


async def protect_main(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str) -> str:
    """Branch protection on main: pushes as before (CI's digest commits), force push off, prod's files protected.

    Force push off keeps the history append-only; PROD_FILES leaves prod to an admin's override (Mario).

    Says what it did: added, an existing rule tightened, or kept.
    """
    base, headers = f"{gitea_url}/api/v1/repos/{org}/{repo}/branch_protections", {"Authorization": f"token {token}"}
    response = await http.get(base, headers=headers)
    response.raise_for_status()
    rule = next((r for r in response.json() if r.get("rule_name") == "main"), None)
    if rule is None:
        created = await http.post(
            base,
            headers=headers,
            json={
                "rule_name": "main",
                "enable_push": True,
                "enable_force_push": False,
                "protected_file_patterns": PROD_FILES,
            },
        )
        created.raise_for_status()
        return "added"
    change: dict[str, object] = {}
    if rule.get("enable_force_push"):
        change["enable_force_push"] = False
    patterns = rule.get("protected_file_patterns") or ""
    if _with_prod_files(patterns) != patterns:
        change["protected_file_patterns"] = _with_prod_files(patterns)
    if not change:
        return "kept"
    (await http.patch(f"{base}/main", headers=headers, json=change)).raise_for_status()
    return (
        "tightened ("
        + ", ".join(
            {"enable_force_push": "force push off", "protected_file_patterns": "prod files protected"}[k]
            for k in change
        )
        + ")"
    )


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


async def become_reader(http: httpx2.AsyncClient, gitea_url: str, token: str, org: str, repo: str, user: str) -> None:
    """The bot drops itself to Read (cm-konk).

    No push, no settings - but it can still propose (AGit PRs, which Gitea takes from a reader) and see the repo at all
    (a restricted bot sees only repos it's a collaborator of).
    """
    response = await http.put(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/collaborators/{user}",
        headers={"Authorization": f"token {token}"},
        json={"permission": "read"},
    )
    response.raise_for_status()
