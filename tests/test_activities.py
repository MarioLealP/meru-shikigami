import base64
import json

import httpx2
import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from shikigami.activities import Activities
from shikigami.config import Settings
from shikigami.models import RequestsSnapshot
from shikigami.tokens import TokenSource


def activities(status: int) -> Activities:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "auth.test":
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 600})
        return httpx2.Response(status, json={"title": "Snapshot refused"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http, "https://auth.test/token/", "thoth", "shikigami", SecretStr("pw"))
    return Activities(
        Settings.model_construct(thoth_url="http://thoth.test", gitea_token=SecretStr("tok")), http, tokens
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
SIGNING_KEYS = "data:\n  echo: |\n    -----BEGIN PUBLIC KEY-----\n    AAAA\n    -----END PUBLIC KEY-----\n"
PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


def gitea(files: dict[str, str], public: dict[str, str], commits: list[dict], hidden: bool = False) -> Activities:
    """A Gitea with Meru/echo's files (token reads), public repo files (anonymous raw reads) and a commit endpoint."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/repos/Meru/echo/contents":
            commits.append(json.loads(request.content))
            return httpx2.Response(201, json={})
        if path.startswith("/api/v1/repos/Meru/echo/contents/"):
            assert request.headers["authorization"] == "token tok"
            if hidden:  # a repo the restricted bot wasn't added to
                return httpx2.Response(403, json={})
            name = path.removeprefix("/api/v1/repos/Meru/echo/contents/")
            if name not in files:
                return httpx2.Response(404, json={})
            content = base64.b64encode(files[name].encode()).decode()
            return httpx2.Response(200, json={"content": content, "sha": f"sha-{name}"})
        if "/raw/" in path:
            assert "authorization" not in request.headers  # public repos: read without the bot's token
            name = path.removeprefix("/api/v1/repos/Meru/")
            return httpx2.Response(200, text=public[name]) if name in public else httpx2.Response(404)
        return httpx2.Response(500)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http, "https://auth.test/token/", "thoth", "shikigami", SecretStr("pw"))
    settings = Settings.model_construct(gitea_url="https://git.test", gitea_token=SecretStr("tok"), apps_org="Meru")
    return Activities(settings, http, tokens)


READY = {"meru/raw/projects/echo.yaml": "kind: AppProject\n", "kyverno/raw/keys/meru-signing-keys.yaml": SIGNING_KEYS}


async def deploy(files: dict[str, str], public: dict[str, str], hidden: bool = False) -> tuple[str, list[dict]]:
    commits: list[dict] = []
    result = await ActivityEnvironment().run(
        gitea(files, public, commits, hidden).ensure_deploy_files, "echo", "mario", PUBLIC_KEY
    )
    return result, commits


async def test_writes_the_marker_in_one_commit_once_everything_is_ready():
    result, [commit] = await deploy({"deploy/meru.yaml.in": TEMPLATE_IN, "deploy/values.yaml": VALUES}, READY)
    assert result == "written"
    ops = {f["path"]: f for f in commit["files"]}
    assert ops["deploy/meru.yaml"]["operation"] == "create"
    assert base64.b64decode(ops["deploy/meru.yaml"]["content"]).decode().startswith("owner: mario\n")
    assert ops["deploy/meru.yaml.in"] == {
        "operation": "delete",
        "path": "deploy/meru.yaml.in",
        "sha": "sha-deploy/meru.yaml.in",
    }
    assert ops["deploy/values.yaml"]["sha"] == "sha-deploy/values.yaml"
    assert base64.b64decode(ops["deploy/values.yaml"]["content"]).decode() == "name: echo\nowner: mario\n"


async def test_an_existing_marker_is_never_touched():
    assert await deploy({}, {**READY, "echo/raw/deploy/meru.yaml": "owner: someone\n"}) == ("kept", [])


async def test_a_marker_in_a_repo_the_bot_cant_see_still_counts():
    # thoth / shikigami: the restricted bot isn't a collaborator, its token gets 403 - the anonymous read decides
    files = {"deploy/meru.yaml.in": TEMPLATE_IN}
    assert await deploy(files, {**READY, "echo/raw/deploy/meru.yaml": "owner: mario\n"}, hidden=True) == ("kept", [])


@pytest.mark.parametrize(
    ("public", "why"),
    [
        ({"kyverno/raw/keys/meru-signing-keys.yaml": SIGNING_KEYS}, "no fence"),
        ({"meru/raw/projects/echo.yaml": "x"}, "signing key"),
        ({**READY, "kyverno/raw/keys/meru-signing-keys.yaml": SIGNING_KEYS.replace("AAAA", "ZZZZ")}, "signing key"),
    ],
)
async def test_waits_for_the_fence_and_the_key(public: dict[str, str], why: str):
    result, commits = await deploy({"deploy/meru.yaml.in": TEMPLATE_IN, "deploy/values.yaml": VALUES}, public)
    assert result.startswith("waiting") and why in result
    assert commits == []


async def test_waits_for_a_repo_without_the_template_file():
    result, commits = await deploy({"deploy/values.yaml": VALUES}, READY)
    assert result.startswith("waiting: no deploy/meru.yaml.in")
    assert commits == []


async def test_a_template_without_the_owner_line_is_not_retried():
    with pytest.raises(ApplicationError) as raised:
        await deploy({"deploy/meru.yaml.in": "chart: x\n", "deploy/values.yaml": VALUES}, READY)
    assert raised.value.non_retryable
