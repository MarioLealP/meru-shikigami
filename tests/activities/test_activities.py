import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from shikigami.activities import Activities, BuildActivities, defined
from shikigami.clients.tokens import TokenSource
from shikigami.config import Settings
from shikigami.core import marker, trust
from shikigami.models.sync import RequestsSnapshot

TOKEN_FILE = Path(__file__).parent.parent / "fixtures" / "k8s-token"  # what the kubelet mounts for Authentik


def activities(status: int) -> Activities:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "auth.test":
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 600})
        return httpx2.Response(status, json={"title": "Snapshot refused"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    return Activities(
        settings=Settings.model_construct(thoth_url="http://thoth.test", gitea_token=SecretStr("tok")),
        http=http,
        thoth_tokens=tokens,
    )


SNAPSHOT = RequestsSnapshot(commit="c1", apps={}, databases={}, problems=[])


@pytest.mark.parametrize("status", [403, 422])
async def test_a_refused_snapshot_is_not_retried(status: int):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities(status).declare_apps, SNAPSHOT)
    assert raised.value.non_retryable
    assert str(status) in str(raised.value)


async def test_a_server_error_is_retried():
    with pytest.raises(httpx2.HTTPStatusError):
        await ActivityEnvironment().run(activities(503).declare_apps, SNAPSHOT)


@pytest.mark.parametrize("status", [403, 404, 422])
async def test_repo_refusals_are_not_retried(status: int):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities(status).ensure_repo, "echo", "nuwa/python", "x")
    assert raised.value.non_retryable


async def test_a_git_url_template_is_not_retried():
    with pytest.raises(ApplicationError, match="only nuwa/<name>"):
        await ActivityEnvironment().run(activities(201).ensure_repo, "echo", "https://git.example/x.git", "x")


TEMPLATE_IN = 'owner: ""\nchart: nuwa-app-0.7.0\nbuild: true\n'
VALUES = 'name: echo\nowner: "" # from the request\n'
PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


def file_answer(name: str, files: dict[str, str]) -> httpx2.Response:
    if name not in files:
        return httpx2.Response(404, json={})
    content = base64.b64encode(files[name].encode()).decode()
    return httpx2.Response(200, json={"content": content, "sha": f"sha-{name}"})


def contents_answer(request: httpx2.Request, files: dict[str, str], hidden: bool) -> httpx2.Response:
    assert request.headers["authorization"] == "token tok"
    if hidden:  # a repo the restricted bot wasn't added to
        return httpx2.Response(403, json={})
    return file_answer(request.url.path.removeprefix("/api/v1/repos/Meru/echo/contents/"), files)


def raw_answer(request: httpx2.Request, public: dict[str, str]) -> httpx2.Response:
    assert "authorization" not in request.headers  # public repos: read without the bot's token
    name = request.url.path.removeprefix("/api/v1/repos/Meru/")
    return httpx2.Response(200, text=public[name]) if name in public else httpx2.Response(404)


def post_answer(request: httpx2.Request, commits: list[dict]) -> httpx2.Response:
    path = request.url.path.removeprefix("/api/v1/repos/Meru/echo/")
    if path == "contents":
        commits.append(json.loads(request.content))
        return httpx2.Response(201, json={})
    if path == "actions/workflows/ci.yaml/dispatches":
        commits.append({"dispatch": json.loads(request.content)})
        return httpx2.Response(204)
    return httpx2.Response(500)


def gitea(
    files: dict[str, str],
    public: dict[str, str],
    commits: list[dict],
    hidden: bool = False,
    admin: bool = True,
    pulls: list[dict] | None = None,
) -> Activities:
    """A Gitea with Meru/echo's files (token reads), public repo files (anonymous raw reads) and a commit endpoint."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/repos/Meru/echo":
            return httpx2.Response(200, json={"permissions": {"admin": admin, "push": admin, "pull": True}})
        if request.method == "POST" and path.startswith("/api/v1/repos/Meru/echo/"):
            return post_answer(request, commits)
        if path == "/api/v1/users/xolotl":
            return httpx2.Response(200, json={"login": "xolotl"})
        if request.method == "PUT" and path == "/api/v1/repos/Meru/echo/collaborators/xolotl":
            commits.append({"writer": json.loads(request.content)})
            return httpx2.Response(204)
        if path == "/api/v1/repos/Meru/echo/pulls":
            return httpx2.Response(200, json=pulls or [])
        if str(request.url) == "http://xolotl.test/ping":
            commits.append({"ping": True})
            return httpx2.Response(202, json={})
        if path.startswith("/api/v1/repos/Meru/echo/contents/"):
            return contents_answer(request, files, hidden)
        if "/raw/" in path:
            return raw_answer(request, public)
        return httpx2.Response(500)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    settings = Settings.model_construct(
        gitea_url="https://git.test",
        gitea_token=SecretStr("tok"),
        gitea_user="shikigami",
        apps_org="Meru",
        merger_user="xolotl",
        merger_ping_url="http://xolotl.test/ping",
    )
    return Activities(settings=settings, http=http, thoth_tokens=tokens)


READY = {"meru/raw/projects/echo.yaml": "kind: AppProject\n"}


async def deploy(
    files: dict[str, str],
    public: dict[str, str],
    hidden: bool = False,
    secrets: list[str] | None = None,
    admin: bool = True,
    pulls: list[dict] | None = None,
    entries: dict[str, str] | None = None,
) -> tuple[str, list[dict]]:
    """ensure_deploy_files over the fake Gitea; the AGit PR it opens is recorded among the calls."""
    calls: list[dict] = []

    async def fake_open_pr(remote, user, token, topic, title, description, files, read, force=False):
        calls.append({"pr": {"remote": remote, "topic": topic, "title": title, "files": files, "read": read}})

    with pytest.MonkeyPatch.context() as m:
        m.setattr("shikigami.activities.base.open_pr", fake_open_pr)
        result = await ActivityEnvironment().run(
            gitea(files, public, calls, hidden, admin, pulls).ensure_deploy_files,
            "echo",
            "mario",
            PUBLIC_KEY,
            secrets,
            entries,
        )
    return result, calls


TEMPLATE = {"deploy/meru.yaml.in": TEMPLATE_IN, "deploy/values.yaml": VALUES}


async def test_the_marker_is_a_pr_xolotl_can_merge_once_everything_is_ready():
    result, [writer, opened, ping] = await deploy(TEMPLATE, READY)
    assert result == "proposed in Meru/echo"
    assert ping == {"ping": True}  # Xolotl sweeps now, not at its backstop
    assert writer == {"writer": {"permission": "write"}}  # Xolotl merges it, so Write before the PR (D99)
    pr = opened["pr"]
    assert (pr["remote"], pr["topic"], pr["title"]) == (
        "https://git.test/Meru/echo.git",
        "deploy-echo",
        "chore(deploy): echo is a Meru app",
    )
    assert pr["files"] == {
        "deploy/meru.yaml": "owner: mario\nchart: nuwa-app-0.7.0\nbuild: true\n",
        "deploy/meru.yaml.in": None,
        "deploy/values.yaml": "name: echo\nowner: mario\n",
    }  # nothing straight to main, and no build asked for yet: that waits for the merge (first_build)


async def test_the_requests_secrets_are_wired_into_values():
    _, [_, opened, _] = await deploy(TEMPLATE, READY, secrets=["discord-token", "api-key"])
    assert opened["pr"]["files"]["deploy/values.yaml"] == (
        "name: echo\nowner: mario\n\n" + marker.SECRETS_COMMENT + "\n"
        "externalSecrets:\n  env: {}\n"
        "secretEnv:\n"
        "  DISCORD_TOKEN:\n    secret: env\n    key: discord-token\n"
        "  API_KEY:\n    secret: env\n    key: api-key\n"
    )


async def test_a_restore_after_a_change_wires_each_name_to_its_entry():
    entries = {"api-key": "env-20261105-a1b2c3"}
    _, [_, opened, _] = await deploy(TEMPLATE, READY, secrets=["discord-token", "api-key"], entries=entries)
    assert opened["pr"]["files"]["deploy/values.yaml"] == (
        "name: echo\nowner: mario\n\n" + marker.SECRETS_COMMENT + "\n"
        "externalSecrets:\n  env: {}\n  env-20261105-a1b2c3: {}\n"
        "secretEnv:\n"
        "  DISCORD_TOKEN:\n    secret: env\n    key: discord-token\n"
        "  API_KEY:\n    secret: env-20261105-a1b2c3\n    key: api-key\n"
    )


async def test_a_restore_keeps_the_secrets_the_first_onboarding_wired():
    _, [_, first, _] = await deploy(TEMPLATE, READY, secrets=["api-key"])
    wired = first["pr"]["files"]["deploy/values.yaml"]
    parked = {"deploy/meru.yaml.in": TEMPLATE_IN, "deploy/values.yaml": wired}
    _, [again, _] = await deploy(parked, READY, secrets=["api-key"], admin=False, pulls=[MERGED_PARK, MERGED_MARKER])
    assert "deploy/values.yaml" not in again["pr"]["files"]  # already as wanted: not part of the PR


async def test_a_template_that_already_wires_secrets_is_not_retried():
    files = {**TEMPLATE, "deploy/values.yaml": VALUES + "secretEnv: {}\n"}
    with pytest.raises(ApplicationError) as raised:
        await deploy(files, READY, secrets=["api-key"])
    assert raised.value.non_retryable
    assert "secretEnv" in str(raised.value)


# What a retire leaves in a repo Shikigami already left (D89): the marker parked, the stop's line still in values.
PARKED = {
    "deploy/meru.yaml.in": TEMPLATE_IN,
    "deploy/values.yaml": "name: echo\nowner: mario\nenabled: false # retired (D85): no workload until Charon purges "
    "the namespace\n",
}
MERGED_MARKER = {"number": 2, "title": "chore(deploy): echo is a Meru app", "state": "closed", "merged": True}
MERGED_PARK = {"number": 5, "title": "chore(retire): park echo", "state": "closed", "merged": True}


async def test_a_restore_proposes_the_marker_again_and_undoes_the_stop():
    result, [opened, _] = await deploy(PARKED, READY, admin=False, pulls=[MERGED_PARK, MERGED_MARKER])
    assert result == "proposed in Meru/echo"  # no Write given: Xolotl kept it, and Shikigami isn't admin to give it
    assert opened["pr"]["files"] == {
        "deploy/meru.yaml": "owner: mario\nchart: nuwa-app-0.7.0\nbuild: true\n",
        "deploy/meru.yaml.in": None,
        "deploy/values.yaml": "name: echo\nowner: mario\n",
    }


async def test_a_merged_marker_pr_newer_than_any_park_is_still_waited_on():
    result, calls = await deploy(TEMPLATE, READY, pulls=[{**MERGED_MARKER, "number": 7}, MERGED_PARK])
    assert result == "Meru/echo#7 merged, not on main yet"
    assert [c for c in calls if "pr" in c] == []


async def test_an_existing_marker_is_never_touched():
    assert await deploy({}, {**READY, "echo/raw/deploy/meru.yaml": "owner: someone\n"}) == ("kept", [])


async def test_a_marker_in_a_repo_the_bot_cant_see_still_counts():
    # thoth / shikigami: the restricted bot isn't a collaborator, its token gets 403 - the anonymous read decides
    files = {"deploy/meru.yaml.in": TEMPLATE_IN}
    assert await deploy(files, {**READY, "echo/raw/deploy/meru.yaml": "owner: mario\n"}, hidden=True) == ("kept", [])


async def test_waits_for_the_fence():
    result, commits = await deploy({"deploy/meru.yaml.in": TEMPLATE_IN, "deploy/values.yaml": VALUES}, {})
    assert result.startswith("waiting")
    assert "no fence" in result
    assert commits == []


async def test_waits_for_a_repo_without_the_template_file():
    result, commits = await deploy({"deploy/values.yaml": VALUES}, READY)
    assert result.startswith("waiting: no deploy/meru.yaml.in")
    assert commits == []


async def test_a_template_without_the_owner_line_is_not_retried():
    with pytest.raises(ApplicationError) as raised:
        await deploy({"deploy/meru.yaml.in": "chart: x\n", "deploy/values.yaml": VALUES}, READY)
    assert raised.value.non_retryable


GOLEM_KEY = "-----BEGIN PUBLIC KEY-----\nGOLEM\n-----END PUBLIC KEY-----\n"
TRUST_REPOS = {
    "meru/raw/kustomization.yaml": "resources:\n  - projects/echo.yaml\n  - appsets/meru-apps.yaml\n",
    "zot/raw/config.json": (Path(__file__).parent.parent / "fixtures" / "zot-config.json").read_text(),
}


PINGS: list[str] = []


async def onboard_trust(
    monkeypatch: pytest.MonkeyPatch, public: dict[str, str], pulls: dict[str, list[dict]], app: str = "golem", key=None
) -> tuple[str, list[dict]]:
    """ensure_trust against public trust repos + their PR lists; returns the result and the PRs it opened. Pings to
    Xolotl land in PINGS."""
    opened: list[dict] = []
    PINGS.clear()

    async def fake_open_pr(remote, user, token, topic, title, description, files, read, force=False):
        opened.append(
            {
                "remote": remote,
                "user": user,
                "topic": topic,
                "title": title,
                "files": files,
                "read": read,
                "force": force,
            }
        )

    monkeypatch.setattr("shikigami.activities.base.open_pr", fake_open_pr)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "xolotl.xolotl.svc":
            PINGS.append(request.method + " " + request.url.path)
            return httpx2.Response(202, json={"status": "queued"})
        path = request.url.path.removeprefix("/api/v1/repos/Meru/")
        if path.endswith("/pulls"):
            return httpx2.Response(200, json=pulls.get(path.split("/")[0], []))
        return httpx2.Response(200, text=public[path]) if path in public else httpx2.Response(404)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    settings = Settings.model_construct(
        gitea_url="https://git.test", gitea_token=SecretStr("tok"), gitea_user="shikigami", apps_org="Meru"
    )
    result = await ActivityEnvironment().run(
        Activities(settings=settings, http=http, thoth_tokens=tokens).ensure_trust, app, "a test app.", key or GOLEM_KEY
    )
    return result, opened


async def test_a_new_app_gets_one_pr_per_trust_repo(monkeypatch: pytest.MonkeyPatch):
    result, [fence, push] = await onboard_trust(monkeypatch, TRUST_REPOS, {})
    assert result == "fence proposed in Meru/meru; push rule proposed in Meru/zot"
    assert PINGS == ["POST /ping"]  # once per pass, not per PR
    assert (push["remote"], push["title"]) == (
        "https://git.test/Meru/zot.git",
        "chore(trust): onboard golem - push rule",
    )
    assert '"golem-builder"' in push["files"]["config.json"]
    assert push["read"] == {"config.json": TRUST_REPOS["zot/raw/config.json"]}
    assert fence["read"] == {
        "projects/golem.yaml": None,
        "kustomization.yaml": TRUST_REPOS["meru/raw/kustomization.yaml"],
    }
    assert (fence["remote"], fence["user"], fence["topic"]) == (
        "https://git.test/Meru/meru.git",
        "shikigami",
        "onboard-golem",
    )
    assert fence["title"] == "chore(trust): onboard golem - fence"
    assert set(fence["files"]) == {"projects/golem.yaml", "kustomization.yaml"}
    assert "  - projects/golem.yaml\n" in fence["files"]["kustomization.yaml"]


async def test_an_app_already_trusted_is_kept_without_a_pr(monkeypatch: pytest.MonkeyPatch):
    public = {
        **TRUST_REPOS,
        "meru/raw/projects/echo.yaml": "kind: AppProject\n",
    }
    result, opened = await onboard_trust(monkeypatch, public, {}, app="echo", key=PUBLIC_KEY)
    assert (result, opened) == ("fence kept; push rule kept", [])
    assert PINGS == []  # nothing opened, nothing to sweep


async def test_an_open_or_rejected_pr_is_never_proposed_again(monkeypatch: pytest.MonkeyPatch):
    pulls = {
        "meru": [{"number": 4, "title": "chore(trust): onboard golem - fence", "state": "open"}],
        "zot": [{"number": 2, "title": "chore(trust): onboard golem - push rule", "state": "closed", "merged": False}],
    }
    result, opened = await onboard_trust(monkeypatch, TRUST_REPOS, pulls)
    assert result.startswith(
        "fence waiting on Meru/meru#4; push rule Meru/zot#2 was closed unmerged - not proposed again"
    )
    assert opened == []


def fence_pr(**kw: object) -> dict[str, list[dict]]:
    return {"meru": [{"number": 47, "title": "chore(trust): onboard golem - fence", "state": "open", **kw}]}


MAIN_NOW = {"meru/branches/main": '{"name": "main", "commit": {"id": "main-now"}}'}


async def test_an_open_pr_that_conflicts_with_a_newer_main_is_rebuilt_in_place(monkeypatch: pytest.MonkeyPatch):
    # another app's fence merged first; base.sha is main's tip, as Gitea reports it - merge_base is where it was built
    pulls = fence_pr(mergeable=False, base={"sha": "main-now"}, merge_base="main-before")
    result, opened = await onboard_trust(monkeypatch, {**TRUST_REPOS, **MAIN_NOW}, pulls)
    assert result.startswith("fence re-proposed in Meru/meru#47 (it conflicted with main)")
    fence = next(pr for pr in opened if pr["title"] == "chore(trust): onboard golem - fence")
    assert fence["force"]
    assert fence["topic"] == "onboard-golem"  # the same AGit topic: the same PR, pushed again
    assert "  - projects/golem.yaml\n" in fence["files"]["kustomization.yaml"]  # worked out against today's main
    assert PINGS == ["POST /ping"]  # Xolotl checks the new commit now


@pytest.mark.parametrize(
    "pr",
    [
        {"mergeable": True, "merge_base": "main-before"},  # behind main but merges cleanly: Gitea handles it
        {"mergeable": False, "merge_base": "main-now"},  # just pushed: Gitea still checking, not a conflict
        {"mergeable": False},  # no merge base reported: never guess
    ],
)
async def test_an_open_pr_is_left_alone_unless_it_conflicts_with_a_newer_main(monkeypatch: pytest.MonkeyPatch, pr):
    result, opened = await onboard_trust(monkeypatch, {**TRUST_REPOS, **MAIN_NOW}, fence_pr(**pr))
    assert result.startswith("fence waiting on Meru/meru#47")
    assert not any(o["force"] for o in opened)


async def test_a_trust_repo_that_lost_its_layout_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    public = {**TRUST_REPOS, "meru/raw/kustomization.yaml": "resources: []\n"}
    with pytest.raises(ApplicationError, match="no `  - projects/") as raised:
        await onboard_trust(monkeypatch, public, {})
    assert raised.value.non_retryable


def runner_answer(request: httpx2.Request, calls: list[dict]) -> httpx2.Response:
    if request.url.path.endswith("/actions/runners"):
        return httpx2.Response(200, json={"runners": [{"id": 7}], "total_count": 1})
    if request.method == "DELETE" and request.url.path.endswith("/actions/runners/7"):
        calls.append({"deleted runner": 7})
        return httpx2.Response(204)
    return httpx2.Response(500)


def repo_answer(request: httpx2.Request, repo: dict, calls: list[dict]) -> httpx2.Response:
    if request.method == "GET":
        return httpx2.Response(200, json=repo)
    if request.method == "PATCH":
        repo.update(json.loads(request.content))
        calls.append({"patched": json.loads(request.content)})
        return httpx2.Response(200, json=repo)
    return httpx2.Response(500)


def retiring(
    kube_dir: Path,
    files: dict[str, str],
    argo: dict | None = None,
    pulls: dict | None = None,
    public: dict | None = None,
    admin: bool = True,
    argo_apps: dict[str, dict] | None = None,
) -> tuple[Activities, list[dict]]:
    """Activities over a fake Gitea (Meru/golem's files, its runners and archive flag, public trust repos + PR lists)
    and a fake Kubernetes API holding app-golem (or not) - or, with `argo_apps`, these Applications by name.
    `admin`: Shikigami still administers Meru/golem."""
    calls: list[dict] = []
    repo = {"archived": False, "permissions": {"admin": admin}}

    def gitea(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v1/repos/Meru/")
        if request.method == "POST" and path == "golem/contents":
            calls.append(json.loads(request.content))
            return httpx2.Response(201, json={})
        if path.startswith("golem/contents/"):
            return file_answer(path.removeprefix("golem/contents/"), files)
        if path == "golem/branches/main":
            return httpx2.Response(200, json={"commit": {"id": "c0ffee"}})
        if path.startswith("golem/actions/runners"):
            return runner_answer(request, calls)
        if path == "golem":
            return repo_answer(request, repo, calls)
        if path.endswith("/pulls"):
            return httpx2.Response(200, json=(pulls or {}).get(path.split("/")[0], []))
        if "/raw/" in path:
            return raw_answer(request, public or {})
        return httpx2.Response(500)

    def kubernetes(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer kube-token"
        prefix = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/"
        assert request.url.path.startswith(prefix)
        name = request.url.path.removeprefix(prefix)
        found = (argo_apps or {}).get(name) if argo_apps is not None or name != "app-golem" else argo
        return httpx2.Response(200, json=found) if found is not None else httpx2.Response(404, json={})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(gitea))
    kube = httpx2.AsyncClient(transport=httpx2.MockTransport(kubernetes))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    (kube_dir / "token").write_text("kube-token\n")  # the pod's API token (nuwa-app kubernetesApi)
    settings = Settings.model_construct(
        gitea_url="https://git.test",
        gitea_token=SecretStr("tok"),
        gitea_user="shikigami",
        apps_org="Meru",
        kubernetes_url="https://kube.test",
        kubernetes_dir=kube_dir,
        argocd_namespace="argocd",
    )
    return Activities(settings=settings, http=http, thoth_tokens=tokens, kube=kube), calls


GOLEM_MARKER = "owner: mario\nchart: nuwa-app-0.9.0\nbuild: true\n"
GOLEM_VALUES = "name: golem\nowner: mario\n"


async def test_the_stop_is_one_commit_and_kept_after(tmp_path: Path):
    activities, calls = retiring(tmp_path, {"deploy/meru.yaml": GOLEM_MARKER, "deploy/values.yaml": GOLEM_VALUES})
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "written"
    (commit,) = calls
    texts = {f["path"]: base64.b64decode(f["content"]).decode() for f in commit["files"]}
    assert "enabled: false" in texts["deploy/values.yaml"]
    assert "build: false" in texts["deploy/meru.yaml"]
    activities, calls = retiring(tmp_path, texts)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "kept"
    activities, calls = retiring(tmp_path, {"deploy/meru.yaml.in": GOLEM_MARKER, "deploy/values.yaml": GOLEM_VALUES})
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "kept (marker parked)"
    assert calls == []


ARGO_STOPPED = {
    "status": {"sync": {"status": "Synced", "revisions": ["n", "c0ffee"]}, "resources": [{"kind": "Service"}]}
}
ARGO_RUNNING = {
    "status": {"sync": {"status": "Synced", "revisions": ["n", "c0ffee"]}, "resources": [{"kind": "Deployment"}]}
}


def opened_prs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    opened: list[dict] = []

    async def fake_open_pr(remote, user, token, topic, title, description, files, read, force=False):
        opened.append({"remote": remote, "topic": topic, "title": title, "files": files, "read": read, "force": force})

    monkeypatch.setattr("shikigami.activities.base.open_pr", fake_open_pr)
    return opened


PUBLIC_GOLEM = {"golem/raw/deploy/meru.yaml": GOLEM_MARKER, "golem/raw/deploy/values.yaml": GOLEM_VALUES}


async def test_a_repo_shikigami_stepped_down_from_gets_the_stop_as_a_pr(tmp_path: Path, monkeypatch):
    opened = opened_prs(monkeypatch)
    activities, calls = retiring(tmp_path, {}, public=PUBLIC_GOLEM, admin=False)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "proposed in Meru/golem"
    [pr] = opened
    assert (pr["remote"], pr["topic"], pr["title"]) == (
        "https://git.test/Meru/golem.git",
        "retire-stop-golem",
        "chore(retire): stop golem",
    )
    assert "enabled: false" in pr["files"]["deploy/values.yaml"]
    assert "build: false" in pr["files"]["deploy/meru.yaml"]
    assert calls == []  # nothing committed straight to main


async def test_an_open_stop_pr_is_waited_on_not_opened_again(tmp_path: Path, monkeypatch):
    opened = opened_prs(monkeypatch)
    pulls = {"golem": [{"number": 3, "title": "chore(retire): stop golem", "state": "open"}]}
    activities, _ = retiring(tmp_path, {}, public=PUBLIC_GOLEM, pulls=pulls, admin=False)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "waiting on Meru/golem#3"
    assert opened == []


async def test_a_merged_stop_is_kept_and_the_park_is_the_next_pr(tmp_path: Path, monkeypatch):
    opened = opened_prs(monkeypatch)
    stopped = {
        "golem/raw/deploy/meru.yaml": "owner: mario\nchart: nuwa-app-0.9.0\nbuild: false # retired\n",
        "golem/raw/deploy/values.yaml": "name: golem\nowner: mario\nenabled: false # retired\n",
    }
    activities, _ = retiring(tmp_path, {}, public=stopped, admin=False)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "kept"
    assert await ActivityEnvironment().run(activities.retire_park_marker, "golem") == "proposed in Meru/golem"
    [pr] = opened
    assert pr["title"] == "chore(retire): park golem"
    assert pr["files"] == {
        "deploy/meru.yaml.in": 'owner: ""\nchart: nuwa-app-0.9.0\nbuild: true\n',
        "deploy/meru.yaml": None,
    }


async def test_retired_again_after_a_restore_the_stop_is_proposed_again(tmp_path: Path, monkeypatch):
    opened = opened_prs(monkeypatch)
    pulls = {
        "golem": [
            {"number": 9, "title": "chore(deploy): golem is a Meru app", "state": "closed", "merged": True},
            {"number": 4, "title": "chore(retire): stop golem", "state": "closed", "merged": True},
        ]
    }
    activities, _ = retiring(tmp_path, {}, public=PUBLIC_GOLEM, pulls=pulls, admin=False)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem") == "proposed in Meru/golem"
    assert [pr["title"] for pr in opened] == ["chore(retire): stop golem"]


async def test_runner_records_and_archiving_are_left_to_charon_without_admin(tmp_path: Path):
    activities, calls = retiring(tmp_path, {}, admin=False)
    assert await ActivityEnvironment().run(activities.retire_runner, "golem") == 0
    assert await ActivityEnvironment().run(activities.retire_archive, "golem") is False
    assert calls == []


GOLEM_ENV_VALUES = "owner: mario\nimage:\n  digest: sha256:abc\n"


async def test_the_stop_turns_each_environment_off_too(tmp_path: Path):
    files = {
        "deploy/meru.yaml": GOLEM_MARKER,
        "deploy/values.yaml": GOLEM_VALUES,
        "deploy/envs/prod/values.yaml": GOLEM_ENV_VALUES,
    }  # no deploy/envs/uat/values.yaml: an environment that never got its files deploys nothing
    activities, calls = retiring(tmp_path, files)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem", ["uat", "prod"]) == "written"
    (commit,) = calls
    texts = {f["path"]: base64.b64decode(f["content"]).decode() for f in commit["files"]}
    assert set(texts) == {"deploy/values.yaml", "deploy/meru.yaml", "deploy/envs/prod/values.yaml"}
    assert texts["deploy/envs/prod/values.yaml"].startswith("owner: mario\nenabled: false")
    activities, _ = retiring(tmp_path, texts)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem", ["uat", "prod"]) == "kept"
    half = {**texts, "deploy/envs/prod/values.yaml": GOLEM_ENV_VALUES}  # the app stopped, prod still running
    activities, calls = retiring(tmp_path, half)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem", ["uat", "prod"]) == "written"


async def test_the_stop_pr_carries_the_environments(tmp_path: Path, monkeypatch):
    opened = opened_prs(monkeypatch)
    public = {**PUBLIC_GOLEM, "golem/raw/deploy/envs/prod/values.yaml": GOLEM_ENV_VALUES}
    activities, _ = retiring(tmp_path, {}, public=public, admin=False)
    assert await ActivityEnvironment().run(activities.retire_stop, "golem", ["prod"]) == "proposed in Meru/golem"
    [pr] = opened
    assert set(pr["files"]) == {"deploy/values.yaml", "deploy/meru.yaml", "deploy/envs/prod/values.yaml"}
    assert "enabled: false" in pr["files"]["deploy/envs/prod/values.yaml"]


async def test_waiting_for_argo_covers_each_environment(tmp_path: Path):
    apps = {"app-golem": ARGO_STOPPED, "env-golem-prod": ARGO_RUNNING}
    activities, _ = retiring(tmp_path, {}, argo_apps=apps)
    with pytest.raises(ApplicationError, match="env-golem-prod not stopped yet"):
        await ActivityEnvironment().run(activities.retire_wait_stopped, "golem", ["uat", "prod"])
    activities, _ = retiring(tmp_path, {}, argo_apps={**apps, "env-golem-prod": ARGO_STOPPED})
    assert await ActivityEnvironment().run(activities.retire_wait_stopped, "golem", ["uat", "prod"]) == "stopped"
    activities, _ = retiring(tmp_path, {}, argo_apps={})
    assert await ActivityEnvironment().run(activities.retire_wait_stopped, "golem", ["prod"]) == "gone"


async def test_waiting_for_argo_retries_until_nothing_runs(tmp_path: Path):
    activities, _ = retiring(tmp_path, {}, argo=ARGO_RUNNING)
    with pytest.raises(ApplicationError, match="still has Deployment") as raised:
        await ActivityEnvironment().run(activities.retire_wait_stopped, "golem")
    assert not raised.value.non_retryable  # the workflow's policy retries it, 30 s apart
    activities, _ = retiring(tmp_path, {}, argo=ARGO_STOPPED)
    assert await ActivityEnvironment().run(activities.retire_wait_stopped, "golem") == "stopped"
    activities, _ = retiring(tmp_path, {}, argo=None)
    assert await ActivityEnvironment().run(activities.retire_wait_stopped, "golem") == "gone"


async def test_parking_moves_the_marker_back_to_the_template_file(tmp_path: Path):
    stopped = "owner: mario\nchart: nuwa-app-0.9.0\nbuild: false # retired\n"
    activities, calls = retiring(tmp_path, {"deploy/meru.yaml": stopped})
    assert await ActivityEnvironment().run(activities.retire_park_marker, "golem") == "parked"
    ops = {f["path"]: f for f in calls[0]["files"]}
    assert (
        base64.b64decode(ops["deploy/meru.yaml.in"]["content"]).decode()
        == 'owner: ""\nchart: nuwa-app-0.9.0\nbuild: true\n'
    )
    assert ops["deploy/meru.yaml"] == {"operation": "delete", "path": "deploy/meru.yaml", "sha": "sha-deploy/meru.yaml"}


async def test_the_runner_goes_and_the_repo_is_archived_once(tmp_path: Path):
    activities, calls = retiring(tmp_path, {})
    assert await ActivityEnvironment().run(activities.retire_runner, "golem") == 1
    assert await ActivityEnvironment().run(activities.retire_archive, "golem") is True
    assert await ActivityEnvironment().run(activities.retire_archive, "golem") is False
    assert calls == [{"deleted runner": 7}, {"patched": {"archived": True}}]


TRUSTED_GOLEM = {
    "meru/raw/projects/golem.yaml": "kind: AppProject\n",
    "meru/raw/kustomization.yaml": "resources:\n  - projects/echo.yaml\n  - projects/golem.yaml\n",
    "zot/raw/config.json": trust.with_push_rule(
        (Path(__file__).parent.parent / "fixtures" / "zot-config.json").read_text(), "golem"
    ),
}


async def test_retiring_trust_proposes_two_removal_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened: list[dict] = []

    async def fake_open_pr(remote, user, token, topic, title, description, files, read, force=False):
        opened.append({"topic": topic, "title": title, "files": files, "read": read, "description": description})

    monkeypatch.setattr("shikigami.activities.base.open_pr", fake_open_pr)
    pulls = {
        "meru": [{"number": 21, "title": "chore(trust): onboard golem - fence", "state": "closed", "merged": True}]
    }
    activities, _ = retiring(tmp_path, {}, pulls=pulls, public=TRUSTED_GOLEM)
    result = await ActivityEnvironment().run(activities.retire_trust, "golem")
    assert result == "fence proposed in Meru/meru; push rule proposed in Meru/zot"
    fence, push = opened
    assert (fence["title"], fence["topic"]) == (
        "chore(trust): retire golem - fence",
        "retire-golem",
    )  # the merged onboard PR is older
    assert fence["files"] == {"projects/golem.yaml": None, "kustomization.yaml": "resources:\n  - projects/echo.yaml\n"}
    assert '"golem-builder"' not in push["files"]["config.json"]
    assert "only removes `golem`'s entries" in fence["description"]
    # what each change was worked out from: open_pr refuses the push if main moved since (Meru/zot#21)
    assert push["read"] == {"config.json": TRUSTED_GOLEM["zot/raw/config.json"]}
    assert fence["read"] == {
        "projects/golem.yaml": TRUSTED_GOLEM["meru/raw/projects/golem.yaml"],
        "kustomization.yaml": TRUSTED_GOLEM["meru/raw/kustomization.yaml"],
    }


async def test_a_restore_proposes_onboarding_again_after_a_merged_retire(monkeypatch: pytest.MonkeyPatch):
    pulls = {
        "meru": [
            {"number": 21, "title": "chore(trust): onboard golem - fence", "state": "closed", "merged": True},
            {"number": 30, "title": "chore(trust): retire golem - fence", "state": "closed", "merged": True},
        ]
    }
    result, _ = await onboard_trust(monkeypatch, TRUST_REPOS, pulls)
    assert result.startswith("fence proposed in Meru/meru")  # not "Meru/meru#21 merged, not on main yet"


def locking(admin: bool, users: set[str], renovate: bool = False) -> tuple[Activities, list[tuple[str, str]]]:
    """Activities over a fake Gitea where Shikigami is (or isn't) admin of Meru/echo; records every write.

    renovate: the repo carries a renovate.json.
    """
    writes: list[tuple[str, str]] = []

    def gitea(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/repos/Meru/echo":
            return httpx2.Response(200, json={"permissions": {"admin": admin}})
        if request.method == "GET" and path == "/api/v1/repos/Meru/echo/contents/renovate.json":
            if not renovate:
                return httpx2.Response(404, json={})
            return httpx2.Response(200, json={"content": base64.b64encode(b"{}").decode(), "sha": "abc"})
        if request.method == "GET" and path.endswith("/branch_protections"):
            return httpx2.Response(200, json=[])
        if request.method == "GET" and path.startswith("/api/v1/users/"):
            return httpx2.Response(200 if path.rsplit("/", 1)[1] in users else 404, json={})
        writes.append((request.method, path.removeprefix("/api/v1/repos/Meru/echo")))
        return httpx2.Response(201 if request.method == "POST" else 204, json={})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(gitea))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    settings = Settings.model_construct(
        gitea_url="https://git.test",
        gitea_token=SecretStr("tok"),
        gitea_user="shikigami",
        merger_user="xolotl",
        release_user="meru-release",
        renovate_user="renovate-bot",
        apps_org="Meru",
    )
    return Activities(settings=settings, http=http, thoth_tokens=tokens), writes


async def test_lock_out_hands_over_protects_and_leaves_in_that_order():
    activities, writes = locking(admin=True, users={"xolotl", "meru-release"})
    result = await ActivityEnvironment().run(activities.lock_out, "echo")
    assert result == ("main protection added, xolotl given Write, meru-release given Write, Shikigami down to Read")
    # the release bot needs Write before it can be on main's push allowlist; each step needs the admin right the last
    # one gives up
    assert writes == [
        ("PUT", "/collaborators/xolotl"),
        ("PUT", "/collaborators/meru-release"),
        ("POST", "/branch_protections"),
        ("PUT", "/collaborators/shikigami"),
    ]


async def test_lock_out_gives_renovate_write_when_the_repo_opts_in():
    activities, writes = locking(admin=True, users={"xolotl", "meru-release", "renovate-bot"}, renovate=True)
    result = await ActivityEnvironment().run(activities.lock_out, "echo")
    assert result == (
        "main protection added, xolotl given Write, meru-release given Write, renovate-bot given Write,"
        " Shikigami down to Read"
    )
    assert writes == [
        ("PUT", "/collaborators/xolotl"),
        ("PUT", "/collaborators/meru-release"),
        ("PUT", "/collaborators/renovate-bot"),
        ("POST", "/branch_protections"),
        ("PUT", "/collaborators/shikigami"),
    ]


async def test_lock_out_still_leaves_before_the_merger_exists():
    activities, writes = locking(admin=True, users=set())
    result = await ActivityEnvironment().run(activities.lock_out, "echo")
    assert result == (
        "main protection added, xolotl skipped (no such user yet), meru-release skipped (no such user yet),"
        " Shikigami down to Read"
    )
    assert [w[0] for w in writes] == ["POST", "PUT"]


async def test_lock_out_is_kept_once_shikigami_is_not_admin():
    activities, writes = locking(admin=False, users={"xolotl"})
    assert await ActivityEnvironment().run(activities.lock_out, "echo") == "kept"
    assert writes == []


def first_build_gitea(admin: bool, run: dict | None) -> tuple[Activities, list[str]]:
    """Activities over a fake Gitea: Meru/echo (Shikigami admin or not) and its newest hand-started CI run."""
    calls: list[str] = []

    def gitea(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v1/repos/Meru/echo")
        calls.append(f"{request.method} {path}")
        if path == "":
            return httpx2.Response(200, json={"permissions": {"admin": admin}})
        if path == "/actions/runs":
            assert request.url.params["event"] == "workflow_dispatch"
            return httpx2.Response(
                200, json={"total_count": int(run is not None), "workflow_runs": [run] if run else []}
            )
        if path.endswith("/rerun-failed-jobs"):
            return httpx2.Response(201, json={})
        if path == "/actions/workflows/ci.yaml/dispatches":
            return httpx2.Response(204)
        return httpx2.Response(500)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(gitea))
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    settings = Settings.model_construct(
        gitea_url="https://git.test", gitea_token=SecretStr("tok"), apps_org="Meru", first_build_reruns=2
    )
    return Activities(settings=settings, http=http, thoth_tokens=tokens), calls


def ci_run(status: str, conclusion: str | None = None, attempt: int = 1) -> dict:
    return {"id": 1954, "status": status, "conclusion": conclusion, "run_attempt": attempt, "path": "ci.yaml@main"}


async def test_a_passed_first_build_is_done():
    activities, calls = first_build_gitea(True, ci_run("completed", "success", attempt=2))
    assert await ActivityEnvironment().run(activities.first_build, "echo") == "passed (attempt 2)"
    assert not any(c.startswith("POST") for c in calls)


async def test_a_running_first_build_is_asked_again_later():
    activities, calls = first_build_gitea(True, ci_run("in_progress"))
    with pytest.raises(ApplicationError, match="in_progress"):
        await ActivityEnvironment().run(activities.first_build, "echo")
    assert not any(c.startswith("POST") for c in calls)


async def test_a_failed_first_build_has_its_failed_jobs_rerun():
    activities, calls = first_build_gitea(True, ci_run("completed", "failure", attempt=2))
    with pytest.raises(ApplicationError, match="re-run"):  # then asked again a minute later
        await ActivityEnvironment().run(activities.first_build, "echo")
    assert calls[-1] == "POST /actions/runs/1954/rerun-failed-jobs"


async def test_after_the_last_rerun_a_failure_is_left_to_the_owner():
    activities, calls = first_build_gitea(True, ci_run("completed", "failure", attempt=3))
    result = await ActivityEnvironment().run(activities.first_build, "echo")
    assert result == "failure after 3 attempts - left to the owner"
    assert not any(c.startswith("POST") for c in calls)


async def test_a_repo_shikigami_left_is_not_watched():
    activities, calls = first_build_gitea(False, None)
    result = await ActivityEnvironment().run(activities.first_build, "echo")
    assert result == "not watched (Shikigami already left the repo)"
    assert calls == ["GET "]  # only the admin check: no runs read, nothing re-run


async def test_with_no_build_yet_the_first_one_is_started_and_watched():
    # D99: the marker is a PR now, so the first build is asked for here, once the marker is on main
    activities, calls = first_build_gitea(True, None)
    with pytest.raises(ApplicationError, match="first build started"):
        await ActivityEnvironment().run(activities.first_build, "echo")
    assert "POST /actions/workflows/ci.yaml/dispatches" in calls


def marking(
    tmp_path: Path, namespaces: dict[str, dict[str, str]], refuse: int | None = None, now: datetime | None = None
):
    """Activities whose cluster holds `namespaces` (name -> annotations); merge patches land in them."""

    def kubernetes(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer kube-token"
        name = request.url.path.removeprefix("/api/v1/namespaces/")
        if name not in namespaces:
            return httpx2.Response(404, json={})
        if request.method == "GET":
            return httpx2.Response(200, json={"metadata": {"name": name, "annotations": namespaces[name]}})
        assert request.method == "PATCH"
        assert request.headers["content-type"] == "application/merge-patch+json"
        if refuse:
            return httpx2.Response(refuse, json={"message": "denied"})
        for key, value in json.loads(request.content)["metadata"]["annotations"].items():
            if value is None:
                namespaces[name].pop(key, None)
            else:
                namespaces[name][key] = value
        return httpx2.Response(200, json={})

    (tmp_path / "token").write_text("kube-token\n")
    settings = Settings.model_construct(kubernetes_url="https://kube.test", kubernetes_dir=tmp_path)
    kube, http = httpx2.AsyncClient(transport=httpx2.MockTransport(kubernetes)), httpx2.AsyncClient()
    tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=TOKEN_FILE)
    clock = {"now": lambda: now} if now else {}
    return Activities(settings=settings, http=http, thoth_tokens=tokens, kube=kube, **clock)


async def test_retire_marks_both_namespaces_with_a_legacy_requests_removed_at(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {}, "golem-build": {"keep": "me"}}  # a pass that never stamped
    activities = marking(tmp_path, cluster)
    removed_at = datetime(2026, 11, 3, 13, 31, 18, tzinfo=timezone(timedelta(hours=1)))
    marked = await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", removed_at)
    assert marked == ["golem", "golem-build"]
    assert cluster == {
        "golem": {"meru.clockmost.com/removed-at": "2026-11-03T12:31:18Z"},  # UTC, as Xolotl stamps it
        "golem-build": {"keep": "me", "meru.clockmost.com/removed-at": "2026-11-03T12:31:18Z"},
    }


async def test_retire_marks_the_environments_namespaces_too(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {}, "golem-build": {}, "golem-prod": {}}  # no golem-uat yet
    activities = marking(tmp_path, cluster)
    removed_at = datetime(2026, 11, 3, 12, 31, 18, tzinfo=UTC)
    marked = await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", removed_at, ["uat", "prod"])
    assert marked == ["golem", "golem-build", "golem-prod"]
    assert await ActivityEnvironment().run(activities.restore_unmark_namespaces, "golem", ["uat", "prod"]) == marked
    assert cluster == {"golem": {}, "golem-build": {}, "golem-prod": {}}


async def test_a_missing_namespace_is_skipped_not_an_error(tmp_path: Path):
    activities = marking(tmp_path, {"golem": {}})  # no build namespace (build: false)
    removed_at = datetime(2026, 11, 3, 12, 31, 18, tzinfo=UTC)
    assert await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", removed_at) == ["golem"]


CLOCK = "meru.clockmost.com/removed-at"
NOW = datetime(2026, 11, 20, 9, 30, 5, 123456, tzinfo=UTC)


async def test_the_clock_is_stamped_on_the_app_namespace_from_the_legacy_request(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {}, "golem-build": {}}
    activities = marking(tmp_path, cluster, now=NOW)
    legacy = datetime(2026, 11, 3, 12, 31, 18, tzinfo=UTC)
    assert await ActivityEnvironment().run(activities.retire_stamp_clock, "golem", legacy) == "2026-11-03T12:31:18Z"
    assert cluster == {"golem": {CLOCK: "2026-11-03T12:31:18Z"}, "golem-build": {}}  # only the app's, until step 9


async def test_without_a_removed_at_the_clock_is_now(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {}}
    activities = marking(tmp_path, cluster, now=NOW)
    assert await ActivityEnvironment().run(activities.retire_stamp_clock, "golem", None) == "2026-11-20T09:30:05Z"
    assert cluster == {"golem": {CLOCK: "2026-11-20T09:30:05Z"}}


async def test_a_later_pass_never_moves_the_clock(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {CLOCK: "2026-11-03T12:31:18Z"}, "golem-build": {}}
    activities = marking(tmp_path, cluster, now=NOW)
    other = datetime(2026, 12, 1, tzinfo=UTC)  # the file now says something else
    assert await ActivityEnvironment().run(activities.retire_stamp_clock, "golem", other) == "2026-11-03T12:31:18Z"
    assert await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", other) == [
        "golem",
        "golem-build",
    ]
    assert cluster == {"golem": {CLOCK: "2026-11-03T12:31:18Z"}, "golem-build": {CLOCK: "2026-11-03T12:31:18Z"}}


async def test_a_missing_app_namespace_gets_no_stamp_and_no_error(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem-build": {}}  # nothing was ever deployed
    activities = marking(tmp_path, cluster, now=NOW)
    assert await ActivityEnvironment().run(activities.retire_stamp_clock, "golem", None) is None
    assert await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", None) == []
    assert cluster == {"golem-build": {}}


async def test_step_9_copies_the_app_namespaces_clock_not_the_requests(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {CLOCK: "2026-11-03T12:31:18Z"}, "golem-build": {}, "golem-prod": {}}
    activities = marking(tmp_path, cluster, now=NOW)
    forged = datetime(2020, 1, 1, tzinfo=UTC)
    await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", forged, ["prod"])
    assert {n: a[CLOCK] for n, a in cluster.items()} == dict.fromkeys(cluster, "2026-11-03T12:31:18Z")


async def test_a_restore_clears_the_clock_and_a_re_removal_starts_a_new_one(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"golem": {}, "golem-build": {}}
    activities = marking(tmp_path, cluster, now=NOW)
    await ActivityEnvironment().run(activities.retire_stamp_clock, "golem", None)
    await ActivityEnvironment().run(activities.restore_unmark_namespaces, "golem")
    assert cluster == {"golem": {}, "golem-build": {}}
    later = marking(tmp_path, cluster, now=NOW + timedelta(days=9))
    assert await ActivityEnvironment().run(later.retire_stamp_clock, "golem", None) == "2026-11-29T09:30:05Z"


async def test_a_refused_mark_is_retried(tmp_path: Path):
    activities = marking(tmp_path, {"golem": {}}, refuse=403)  # Meru/meru's rbac not merged yet: wait for it
    with pytest.raises(httpx2.HTTPStatusError):
        await ActivityEnvironment().run(activities.retire_mark_namespaces, "golem", datetime(2026, 11, 3, tzinfo=UTC))


async def test_the_theme_goes_onto_every_namespace_that_exists(tmp_path: Path):
    cluster: dict[str, dict[str, str]] = {"books": {"keep": "me"}, "books-build": {}, "books-uat": {}}  # no prod yet
    activities = marking(tmp_path, cluster)
    themed = await ActivityEnvironment().run(activities.apply_theme, "books", "home", ["uat", "prod"])
    assert themed == ["books", "books-build", "books-uat"]
    assert cluster == {
        "books": {"keep": "me", "meru.clockmost.com/theme": "home"},
        "books-build": {"meru.clockmost.com/theme": "home"},
        "books-uat": {"meru.clockmost.com/theme": "home"},
    }


@pytest.mark.parametrize("refuse", [403, 422])
async def test_a_refused_theme_is_skipped_not_an_error(tmp_path: Path, refuse: int):
    activities = marking(tmp_path, {"books": {}}, refuse=refuse)  # Meru/meru's policy not widened yet
    assert await ActivityEnvironment().run(activities.apply_theme, "books", "home", []) == []


async def test_restore_takes_the_mark_off(tmp_path: Path):
    cluster = {"golem": {"meru.clockmost.com/removed-at": "2026-11-03T12:31:18Z", "keep": "me"}, "golem-build": {}}
    activities = marking(tmp_path, cluster)
    assert await ActivityEnvironment().run(activities.restore_unmark_namespaces, "golem") == ["golem", "golem-build"]
    assert cluster == {"golem": {"keep": "me"}, "golem-build": {}}


def test_the_worker_registers_every_activity_including_the_theme(tmp_path: Path):
    activities = marking(tmp_path, {})
    names = {getattr(f, "__temporal_activity_definition").name for f in defined(activities)}
    assert "apply_theme" in names
    assert len(names) == len(defined(activities))


def test_activity_names_are_the_ones_running_workflows_replay_against():
    def names(group: type) -> set[str]:
        found = (getattr(getattr(group, n), "__temporal_activity_definition", None) for n in dir(group))
        return {d.name for d in found if d}

    assert names(Activities) == {
        "read_requests",
        "apply_theme",
        "read_existing_apps",
        "declare_apps",
        "ensure_repo",
        "ensure_trust",
        "ensure_glitchtip",
        "ensure_deploy_files",
        "first_build",
        "lock_out",
        "retire_stop",
        "retire_wait_stopped",
        "retire_park_marker",
        "retire_trust",
        "retire_runner",
        "retire_archive",
        "retire_stamp_clock",
        "retire_mark_namespaces",
        "restore_unmark_namespaces",
        "retire_mark",
        "dry_run",
    }
    assert names(BuildActivities) == {
        "ensure_build_keys",
        "ensure_zot_login",
        "ensure_runner_registration",
        "revoke_zot_login",
    }
